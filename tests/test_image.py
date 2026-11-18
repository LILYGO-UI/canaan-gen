"""Black-box image export checks using synthetic MBR/ext4 metadata.

These fixtures intentionally contain only the metadata the image utility reads.
They are not mountable filesystems and do not replace real e2fsck/boot tests.
"""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image.py"
SECTOR = 512
ROOT_START = 32
ROOT_COUNT = 64
ROOT_SIZE_FIELD = 446 + 16 + 12
FS_UUID = uuid.UUID("971ac38e-b143-4b18-969f-215e6bd6cdd2")


def partition(start, count, kind=0x83, active=0):
    result = bytearray(16)
    result[0] = active
    result[1:4] = b"\x12\x34\x56"
    result[4] = kind
    result[5:8] = b"\x65\x43\x21"
    struct.pack_into("<II", result, 8, start, count)
    return result


def filesystem(size, fs_uuid=FS_UUID):
    result = bytearray((i * 37 + 11) % 256 for i in range(size))
    result[1024:2048] = bytes(1024)
    struct.pack_into("<I", result, 1024 + 4, size // 1024)
    struct.pack_into("<I", result, 1024 + 24, 0)
    result[1024 + 56:1024 + 58] = b"\x53\xef"
    result[1024 + 104:1024 + 120] = fs_uuid.bytes
    result[1024 + 120:1024 + 131] = b"debian-k230"
    return result


def disk_image():
    result = bytearray((i * 13 + 7) % 256
                       for i in range((ROOT_START + ROOT_COUNT) * SECTOR))
    result[446:510] = bytes(64)
    result[446:462] = partition(8, 8)
    result[462:478] = partition(ROOT_START, ROOT_COUNT)
    result[510:512] = b"\x55\xaa"
    result[8 * SECTOR:16 * SECTOR] = filesystem(8 * SECTOR)
    result[ROOT_START * SECTOR:] = filesystem(ROOT_COUNT * SECTOR)
    return result


class ImageExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="canaan-gen-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.base = self.directory / "base.img"
        self.rootfs = self.directory / "rootfs.ext4"
        self.output = self.directory / "debian.img"
        self.base.write_bytes(disk_image())
        self.rootfs.write_bytes(filesystem(48 * 1024))

    def command(self, *arguments, succeeds=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)],
                                capture_output=True, text=True, check=False)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("canaan-gen:", result.stderr)
        return result

    def inspect(self, succeeds=True, root_partition=2):
        return self.command("inspect", "--image", self.base,
                            "--root-partition", root_partition, succeeds=succeeds)

    def assemble(self, succeeds=True, output=None):
        return self.command("assemble", "--image", self.base,
                            "--rootfs", self.rootfs, "--output", output or self.output,
                            "--root-partition", 2, succeeds=succeeds)

    def assert_rejected_image(self, data):
        self.base.write_bytes(data)
        before = self.base.read_bytes()
        self.inspect(succeeds=False)
        self.assemble(succeeds=False)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.base.read_bytes(), before)

    def test_inspects_supported_layout(self):
        before = hashlib.sha256(self.base.read_bytes()).digest()
        info = self.inspect()
        self.assertEqual(info["table"], "dos")
        self.assertEqual(info["sector_size"], 512)
        self.assertEqual(info["root_partition"], 2)
        self.assertEqual([p["number"] for p in info["partitions"]], [1, 2])
        root = info["partitions"][1]
        self.assertEqual(root["start_sector"], ROOT_START)
        self.assertEqual(root["sectors"], ROOT_COUNT)
        self.assertEqual(root["filesystem"]["uuid"], str(FS_UUID))
        self.assertEqual(hashlib.sha256(self.base.read_bytes()).digest(), before)

    def test_export_preserves_boot_and_inputs_and_sets_exact_partition_size(self):
        original = self.base.read_bytes()
        replacement = self.rootfs.read_bytes()
        info = self.assemble()
        exported = self.output.read_bytes()
        expected_prefix = bytearray(original[:ROOT_START * SECTOR])
        struct.pack_into("<I", expected_prefix, ROOT_SIZE_FIELD, len(replacement) // SECTOR)
        self.assertEqual(exported[:ROOT_START * SECTOR], expected_prefix)
        self.assertEqual(exported[ROOT_START * SECTOR:], replacement)
        self.assertEqual(len(exported), ROOT_START * SECTOR + len(replacement))
        self.assertEqual(info["partitions"][1]["sectors"], len(replacement) // SECTOR)
        self.assertEqual(self.base.read_bytes(), original)
        self.assertEqual(self.rootfs.read_bytes(), replacement)

    def test_smaller_rootfs_removes_old_rootfs_tail(self):
        self.rootfs.write_bytes(filesystem(8 * 1024))
        self.assemble()
        self.assertEqual(self.output.stat().st_size, ROOT_START * SECTOR + 8 * 1024)

    def test_non_sector_aligned_payload_gets_zero_padding(self):
        payload = filesystem(8 * 1024) + b"tail"
        self.rootfs.write_bytes(payload)
        self.assemble()
        exported = self.output.read_bytes()[ROOT_START * SECTOR:]
        self.assertEqual(exported[:len(payload)], payload)
        self.assertEqual(exported[len(payload):], bytes(SECTOR - 4))

    def test_invalid_signature_and_truncated_input_are_rejected(self):
        for description, data in (("signature", disk_image()), ("truncated", bytearray(511))):
            with self.subTest(description=description):
                if description == "signature":
                    data[510:512] = b"\x00\x00"
                self.assert_rejected_image(data)

    def test_unknown_partition_layouts_are_rejected(self):
        for description, kind in (("protective GPT", 0xEE), ("extended", 0x05),
                                  ("extended LBA", 0x0F), ("NTFS", 0x07)):
            with self.subTest(description=description):
                data = disk_image()
                data[446 + 4] = kind
                self.assert_rejected_image(data)

    def test_extra_partition_is_rejected(self):
        data = disk_image()
        data[478:494] = partition(20, 4)
        self.assert_rejected_image(data)

    def test_missing_boot_partition_is_rejected(self):
        data = disk_image()
        data[446:462] = bytes(16)
        self.assert_rejected_image(data)

    def test_overlap_is_rejected(self):
        data = disk_image()
        data[446:462] = partition(8, ROOT_START - 7)
        self.assert_rejected_image(data)

    def test_out_of_bounds_partition_is_rejected(self):
        for entry in (0, 1):
            with self.subTest(partition=entry + 1):
                data = disk_image()
                struct.pack_into("<I", data, 446 + entry * 16 + 12, len(data) // SECTOR)
                self.assert_rejected_image(data)

    def test_zero_partition_range_is_rejected(self):
        for field in (8, 12):
            with self.subTest(field=field):
                data = disk_image()
                struct.pack_into("<I", data, 446 + 16 + field, 0)
                self.assert_rejected_image(data)

    def test_rootfs_must_be_last_and_number_two(self):
        data = disk_image()
        data[446:462] = partition(ROOT_START + 40, 8)
        data[462:478] = partition(ROOT_START, 32)
        data[ROOT_START * SECTOR:(ROOT_START + 32) * SECTOR] = filesystem(16 * 1024)
        self.assert_rejected_image(data)
        self.base.write_bytes(disk_image())
        self.inspect(succeeds=False, root_partition=1)

    def test_invalid_boot_flag_is_rejected_but_active_flag_is_accepted(self):
        data = disk_image()
        data[446] = 0x80
        self.base.write_bytes(data)
        self.inspect()
        data[446] = 0x01
        self.assert_rejected_image(data)

    def test_invalid_source_ext4_is_rejected(self):
        data = disk_image()
        data[ROOT_START * SECTOR + 1024 + 56] = 0
        self.assert_rejected_image(data)

    def test_source_ext4_cannot_extend_past_partition(self):
        data = disk_image()
        struct.pack_into("<I", data, ROOT_START * SECTOR + 1024 + 4, ROOT_COUNT)
        self.assert_rejected_image(data)

    def test_64_bit_ext4_block_count_cannot_extend_past_partition(self):
        data = disk_image()
        superblock = ROOT_START * SECTOR + 1024
        struct.pack_into("<I", data, superblock + 96, 0x80)
        struct.pack_into("<I", data, superblock + 336, 1)
        self.assert_rejected_image(data)

    def test_invalid_replacement_ext4_is_rejected(self):
        data = filesystem(8 * 1024)
        data[1024 + 56] = 0
        self.rootfs.write_bytes(data)
        self.assemble(succeeds=False)
        self.assertFalse(self.output.exists())

    def test_replacement_ext4_cannot_extend_past_file(self):
        data = filesystem(8 * 1024)
        struct.pack_into("<I", data, 1024 + 4, 9)
        self.rootfs.write_bytes(data)
        self.assemble(succeeds=False)
        self.assertFalse(self.output.exists())

    def test_replacement_uuid_mismatch_is_rejected(self):
        self.rootfs.write_bytes(filesystem(8 * 1024, uuid.UUID(int=1)))
        self.assemble(succeeds=False)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_not_overwritten(self):
        self.output.write_bytes(b"keep existing build")
        self.assemble(succeeds=False)
        self.assertEqual(self.output.read_bytes(), b"keep existing build")

    def test_inputs_and_aliases_cannot_be_outputs(self):
        for source in (self.base, self.rootfs):
            with self.subTest(source=source.name):
                before = source.read_bytes()
                self.assemble(succeeds=False, output=source)
                alias = self.directory / (source.name + ".alias")
                alias.symlink_to(source)
                self.assemble(succeeds=False, output=alias)
                hardlink = self.directory / (source.name + ".hardlink")
                hardlink.hardlink_to(source)
                self.assemble(succeeds=False, output=hardlink)
                self.assertEqual(source.read_bytes(), before)

    def test_directory_input_is_rejected(self):
        self.command("inspect", "--image", self.directory, succeeds=False)


if __name__ == "__main__":
    unittest.main()
