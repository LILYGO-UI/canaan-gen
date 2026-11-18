"""Exercise boot-time expansion with synthetic disks and command failures.

The command doubles model partition-table/kernel-size updates independently,
so these tests cover interrupted expansion without accessing real block devices.
"""
import importlib.util
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'stage2/canaan-grow-rootfs.py'
SPEC = importlib.util.spec_from_file_location('canaan_grow_rootfs', SCRIPT)
GROW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GROW)
ROOT_START = 32
INITIAL_SIZE = 64
DISK_SIZE = 512
FULL_SIZE = DISK_SIZE - ROOT_START


def mbr(root_start=ROOT_START, root_size=INITIAL_SIZE):
    data = bytearray((index * 17 + 3) % 256 for index in range(512))
    data[446:510] = bytes(64)
    for offset, start, size in ((446, 8, 8), (462, root_start, root_size)):
        data[offset + 4] = 0x83
        struct.pack_into('<II', data, offset + 8, start, size)
    data[510:512] = b'\x55\xaa'
    return data


class RootExpansionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='canaan-grow-test-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.disk = directory / 'card'
        self.disk.write_bytes(mbr())
        self.root = directory / 'root-device'
        self.disk_sys = directory / 'sys/card'
        self.root_sys = self.disk_sys / 'cardp2'
        self.root_sys.mkdir(parents=True)
        (self.disk_sys / 'size').write_text(str(DISK_SIZE))
        (self.root_sys / 'start').write_text(str(ROOT_START))
        (self.root_sys / 'size').write_text(str(INITIAL_SIZE))
        self.grow_status = 0
        self.new_size = FULL_SIZE
        self.grow_refreshes_kernel = True
        self.partx_refreshes_kernel = True
        self.partx_status = 0
        self.resize_status = 0
        self.filesystem_size = INITIAL_SIZE
        self.mutate_after_grow = lambda data: None
        self.calls = []
        self.environments = []
        self.messages = []
        patches = (
            mock.patch.object(GROW, 'root_devices', return_value=(
                self.root, self.disk, self.root_sys, self.disk_sys)),
            mock.patch.object(GROW.subprocess, 'check_output', return_value='ext4\n'),
            mock.patch.object(GROW.subprocess, 'run', side_effect=self.run_command),
            mock.patch.object(GROW, 'log', side_effect=self.messages.append),
        )
        self.devices, self.findmnt, self.commands, self.logger = [p.start() for p in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'growpart':
            self.environments.append(kwargs['env'])
            data = bytearray(self.disk.read_bytes())
            if self.new_size is not None:
                struct.pack_into('<I', data, 474, self.new_size)
            self.mutate_after_grow(data)
            self.disk.write_bytes(data)
            if self.grow_refreshes_kernel:
                (self.root_sys / 'size').write_text(str(struct.unpack_from('<I', data, 474)[0]))
            return subprocess.CompletedProcess(args, self.grow_status)
        if args[0] == 'partx':
            if self.partx_status:
                raise subprocess.CalledProcessError(self.partx_status, args)
            if self.partx_refreshes_kernel:
                size = struct.unpack_from('<I', self.disk.read_bytes(), 474)[0]
                (self.root_sys / 'size').write_text(str(size))
        elif args[0] == 'resize2fs':
            if self.resize_status:
                raise subprocess.CalledProcessError(self.resize_status, args)
            self.filesystem_size = int((self.root_sys / 'size').read_text())
        else:
            self.fail(f'unexpected command: {args}')
        self.assertTrue(kwargs.get('check'), 'command failures must propagate')
        return subprocess.CompletedProcess(args, 0)

    def assert_failure(self, message):
        self.assertEqual(GROW.main(), 1)
        self.assertTrue(any(message in line for line in self.messages), self.messages)
        self.assertFalse(any(call[0] == 'resize2fs' for call in self.calls))

    def test_larger_card_grows_partition_then_filesystem(self):
        before = self.disk.read_bytes()
        self.assertEqual(GROW.main(), 0)
        self.assertEqual([call[0] for call in self.calls], ['growpart', 'resize2fs'])
        self.assertEqual(self.calls[-1][-1], str(self.root))
        self.assertEqual(self.filesystem_size, FULL_SIZE)
        self.assertEqual(self.environments[0]['TMPDIR'], '/run')
        after = self.disk.read_bytes()
        self.assertEqual(before[:474], after[:474])
        self.assertEqual(before[478:], after[478:])

    def test_same_size_card_still_checks_filesystem(self):
        (self.disk_sys / 'size').write_text(str(ROOT_START + INITIAL_SIZE))
        self.grow_status, self.new_size = 1, None
        before = self.disk.read_bytes()
        self.assertEqual(GROW.main(), 0)
        self.assertEqual(self.disk.read_bytes(), before)
        self.assertEqual(self.filesystem_size, INITIAL_SIZE)
        self.assertEqual([call[0] for call in self.calls], ['growpart', 'resize2fs'])

    def test_repeat_boot_is_idempotent(self):
        self.assertEqual(GROW.main(), 0)
        before = self.disk.read_bytes()
        self.grow_status, self.new_size = 1, None
        self.calls.clear()
        self.assertEqual(GROW.main(), 0)
        self.assertEqual(self.disk.read_bytes(), before)
        self.assertEqual(self.filesystem_size, FULL_SIZE)
        self.assertEqual([call[0] for call in self.calls], ['growpart', 'resize2fs'])

    def test_nochange_recovers_partition_grown_before_filesystem(self):
        self.disk.write_bytes(mbr(root_size=FULL_SIZE))
        (self.root_sys / 'size').write_text(str(FULL_SIZE))
        self.grow_status, self.new_size = 1, None
        self.assertEqual(GROW.main(), 0)
        self.assertEqual(self.filesystem_size, FULL_SIZE)

    def test_kernel_size_is_refreshed_before_resize(self):
        self.grow_refreshes_kernel = False
        self.assertEqual(GROW.main(), 0)
        self.assertEqual([call[0] for call in self.calls], ['growpart', 'partx', 'resize2fs'])
        self.assertEqual(self.filesystem_size, FULL_SIZE)

    def test_nochange_repairs_kernel_size_after_interruption(self):
        self.disk.write_bytes(mbr(root_size=FULL_SIZE))
        self.grow_status, self.new_size = 1, None
        self.grow_refreshes_kernel = False
        self.assertEqual(GROW.main(), 0)
        self.assertEqual([call[0] for call in self.calls], ['growpart', 'partx', 'resize2fs'])
        self.assertEqual(self.filesystem_size, FULL_SIZE)

    def test_growpart_failure_does_not_resize(self):
        for status in (2, 127, -15):
            with self.subTest(status=status):
                self.grow_status = status
                self.assert_failure(f'exit status {status}')

    def test_partx_failure_does_not_resize(self):
        self.grow_refreshes_kernel = False
        self.partx_status = 1
        self.assert_failure('partx')

    def test_stale_kernel_size_does_not_resize(self):
        self.grow_refreshes_kernel = self.partx_refreshes_kernel = False
        self.assert_failure('kernel partition size is stale')

    def test_failed_filesystem_resize_can_retry_on_next_boot(self):
        self.resize_status = 1
        self.assertEqual(GROW.main(), 1)
        self.assertEqual(self.filesystem_size, INITIAL_SIZE)
        self.assertTrue(any('next boot will retry' in line for line in self.messages))
        self.resize_status = 0
        self.grow_status, self.new_size = 1, None
        self.assertEqual(GROW.main(), 0)
        self.assertEqual(self.filesystem_size, FULL_SIZE)

    def test_chs_updates_do_not_prevent_resize(self):
        def change_chs(data):
            data[463:466] = b'\xfe\xff\xff'
            data[467:470] = b'\xfe\xff\xff'
        self.mutate_after_grow = change_chs
        self.assertEqual(GROW.main(), 0)

    def test_unexpected_boot_code_change_stops_resize(self):
        self.mutate_after_grow = lambda data: data.__setitem__(32, data[32] ^ 1)
        self.assert_failure('unexpected partition-table change')

    def test_unexpected_disk_identity_change_stops_resize(self):
        self.mutate_after_grow = lambda data: data.__setitem__(440, data[440] ^ 1)
        self.assert_failure('unexpected partition-table change')

    def test_unexpected_boot_partition_change_stops_resize(self):
        self.mutate_after_grow = lambda data: struct.pack_into('<I', data, 458, 9)
        self.assert_failure('unexpected partition-table change')

    def test_moved_root_partition_stops_resize(self):
        self.new_size = INITIAL_SIZE
        self.mutate_after_grow = lambda data: struct.pack_into('<I', data, 470, ROOT_START + 1)
        self.assert_failure('unexpected partition-table change')

    def test_shrunk_root_partition_stops_resize(self):
        self.new_size = INITIAL_SIZE - 1
        self.assert_failure('root partition moved or shrank')

    def test_non_ext4_is_rejected_before_accessing_devices(self):
        self.findmnt.return_value = 'overlay\n'
        self.assert_failure('root must be ext4')
        self.devices.assert_not_called()
        self.commands.assert_not_called()

    def test_root_start_mismatch_is_rejected_before_writes(self):
        (self.root_sys / 'start').write_text(str(ROOT_START + 1))
        self.assert_failure('mounted root start does not match')
        self.commands.assert_not_called()

    def test_unsafe_layouts_are_rejected_before_writes(self):
        cases = {
            'missing MBR signature': lambda data: data.__setitem__(510, 0),
            'extra partitions': lambda data: data.__setitem__(478, 1),
            'primary Linux': lambda data: data.__setitem__(450, 0xee),
            'partition extends beyond': lambda data: struct.pack_into('<I', data, 474, DISK_SIZE),
            'overlapping': lambda data: struct.pack_into('<I', data, 458, ROOT_START),
        }
        for error, mutation in cases.items():
            with self.subTest(error=error):
                data = mbr()
                mutation(data)
                self.disk.write_bytes(data)
                self.assert_failure(error)
                self.commands.assert_not_called()

    def test_oversized_mbr_disk_is_rejected_before_writes(self):
        (self.disk_sys / 'size').write_text(str(0x100000000))
        self.assert_failure('MBR addressing limit')
        self.commands.assert_not_called()

    def test_findmnt_error_is_reported_without_writes(self):
        self.findmnt.side_effect = subprocess.CalledProcessError(1, ['findmnt'])
        self.assert_failure('findmnt')
        self.commands.assert_not_called()


class RootDeviceDetectionTests(unittest.TestCase):
    def test_mmc_enumeration_is_detected_by_mounted_device_number(self):
        real_stat = os.stat
        for card in ('mmcblk0', 'mmcblk1'):
            with self.subTest(card=card), tempfile.TemporaryDirectory() as temporary:
                disk_sys = Path(temporary).resolve() / card
                root_sys = disk_sys / f'{card}p2'
                root_sys.mkdir(parents=True)
                (root_sys / 'partition').write_text('2\n')
                (disk_sys / 'queue').mkdir()
                (disk_sys / 'queue/logical_block_size').write_text('512\n')
                device = os.makedev(179, 2 if card == 'mmcblk0' else 10)
                sys_link = f'/sys/dev/block/{os.major(device)}:{os.minor(device)}'

                def mapped_path(value):
                    return root_sys if str(value) == sys_link else Path(value)

                def device_stat(path, *args, **kwargs):
                    if str(path) == '/':
                        return SimpleNamespace(st_dev=device)
                    if str(path) == f'/dev/{card}p2':
                        return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=device)
                    if str(path) == f'/dev/{card}':
                        return SimpleNamespace(st_mode=stat.S_IFBLK)
                    return real_stat(path, *args, **kwargs)

                with mock.patch.object(GROW, 'Path', side_effect=mapped_path), \
                        mock.patch.object(GROW.os, 'stat', side_effect=device_stat):
                    root, disk, found_root_sys, found_disk_sys = GROW.root_devices()
                self.assertEqual(root, Path(f'/dev/{card}p2'))
                self.assertEqual(disk, Path(f'/dev/{card}'))
                self.assertEqual(found_root_sys, root_sys)
                self.assertEqual(found_disk_sys, disk_sys)


if __name__ == '__main__':
    unittest.main()
