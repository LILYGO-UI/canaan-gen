#!/usr/bin/env python3
"""Exercise real growpart/resize2fs on disposable regular files, never devices.

Run on Linux with cloud-guest-utils, fdisk, e2fsprogs and Python installed:
    python3 tests/integration_grow_rootfs.py

The production helper's root discovery and kernel partition-size update are
mocked. Its partition checks and the actual filesystem tools run unchanged.
No loop devices, mounts, physical storage or privileged container are needed.
If systemd-analyze is installed, the service ordering is verified against the
host units, with only its executable path redirected to a temporary stub.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SECTOR = 512
MIB = 1024 * 1024
ROOT_START = 16 * MIB // SECTOR
ROOT_BYTES = 32 * MIB
UUID = 'deef49c7-931a-4107-a928-fcdd4294b050'
PAYLOAD = b'canaan-gen rootfs growth preserves existing files\n'
REAL_RUN = subprocess.run
REAL_OUTPUT = subprocess.check_output


def run(*args, **kwargs):
    return REAL_RUN(list(args), check=True, capture_output=True, text=True, **kwargs)


def copy_into(source, destination, offset):
    with source.open('rb') as incoming, destination.open('r+b') as outgoing:
        outgoing.seek(offset)
        shutil.copyfileobj(incoming, outgoing)


def layout(image):
    with image.open('rb') as stream:
        mbr = stream.read(SECTOR)
    return mbr, struct.unpack_from('<II', mbr, 470)


def boot_hash(image):
    with image.open('rb') as stream:
        stream.seek(SECTOR)
        return hashlib.sha256(stream.read(ROOT_START * SECTOR - SECTOR)).hexdigest()


def ext4_uuid(filesystem):
    with filesystem.open('rb') as stream:
        stream.seek(1024 + 104)
        return stream.read(16)


def ext4_bytes(filesystem):
    with filesystem.open('rb') as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
    count = struct.unpack_from('<I', superblock, 4)[0]
    if struct.unpack_from('<I', superblock, 96)[0] & 0x80:
        count |= struct.unpack_from('<I', superblock, 336)[0] << 32
    return count * (1024 << struct.unpack_from('<I', superblock, 24)[0])


def make_image(directory):
    source = directory / 'source'
    source.mkdir()
    (source / 'preserved.txt').write_bytes(PAYLOAD)
    filesystem = directory / 'rootfs.ext4'
    with filesystem.open('wb') as stream:
        stream.truncate(ROOT_BYTES)
    run('mke2fs', '-q', '-t', 'ext4', '-F', '-U', UUID, '-d', str(source), str(filesystem))
    image = directory / 'card.img'
    with image.open('wb') as stream:
        stream.truncate(ROOT_START * SECTOR + ROOT_BYTES)
    run('sfdisk', str(image), input=(
        'label: dos\nlabel-id: 0x31415926\nunit: sectors\n\n'
        f'start=2048, size=8192, type=83\n'
        f'start={ROOT_START}, size={ROOT_BYTES // SECTOR}, type=83\n'))
    boot = directory / 'boot.ext4'
    with boot.open('wb') as stream:
        stream.truncate(4 * MIB)
    run('mke2fs', '-q', '-t', 'ext4', '-F', str(boot))
    copy_into(boot, image, 2048 * SECTOR)
    copy_into(filesystem, image, ROOT_START * SECTOR)
    # A distinctive unpartitioned bootloader payload and disk boot code.
    with image.open('r+b') as stream:
        stream.write((b'CANAAN-BOOT-CODE\0' * 27)[:440])
        stream.seek(8 * MIB)
        stream.write(b'CANAAN-BOOTLOADER-PAYLOAD\0' * 128)
    return image, filesystem


def extract_root(image, filesystem):
    _, (start, size) = layout(image)
    with image.open('rb') as incoming, filesystem.open('wb') as outgoing:
        incoming.seek(start * SECTOR)
        remaining = size * SECTOR
        while remaining:
            block = incoming.read(min(remaining, MIB))
            if not block:
                raise AssertionError('truncated test image')
            outgoing.write(block)
            remaining -= len(block)
    return start, size


def exercise(helper, directory, capacity_mib, recover=False):
    image, filesystem = make_image(directory)
    original_mbr, (_, original_size) = layout(image)
    original_boot = boot_hash(image)
    original_uuid = ext4_uuid(filesystem)
    with image.open('r+b') as stream:
        stream.truncate(capacity_mib * MIB)
    disk_sys = directory / 'sys' / 'testdisk'
    root_sys = disk_sys / 'testdisk2'
    root_sys.mkdir(parents=True)
    (disk_sys / 'size').write_text(str(capacity_mib * MIB // SECTOR))
    (root_sys / 'start').write_text(str(ROOT_START))
    (root_sys / 'size').write_text(str(original_size))
    if recover:
        run('growpart', '--fudge', '0', str(image), '2')
        _, (_, expanded_size) = layout(image)
        (root_sys / 'size').write_text(str(expanded_size))
    events = []

    def output(args, **kwargs):
        if args == ['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', '/']:
            return 'ext4\n'
        return REAL_OUTPUT(args, **kwargs)

    def command(args, **kwargs):
        if args[0] == 'growpart':
            assert args == ['growpart', '--fudge', '0', str(image), '2'], args
            result = REAL_RUN(args, capture_output=True, text=True, **kwargs)
            events.append({'command': 'growpart', 'returncode': result.returncode})
            if result.returncode not in (0, 1):
                raise AssertionError(result.stdout + result.stderr)
            return result
        if args[0] == 'partx':
            assert args == ['partx', '--update', '--nr', '2', str(image)], args
            _, (_, size) = layout(image)
            (root_sys / 'size').write_text(str(size))
            events.append({'command': 'partx', 'mocked': True})
            return subprocess.CompletedProcess(args, 0)
        if args[0] == 'resize2fs':
            assert args == ['resize2fs', str(filesystem)], args
            start, _ = extract_root(image, filesystem)
            result = REAL_RUN(args, capture_output=True, text=True, **kwargs)
            copy_into(filesystem, image, start * SECTOR)
            events.append({'command': 'resize2fs', 'returncode': result.returncode})
            return result
        raise AssertionError(f'unexpected helper command: {args!r}')

    with patch.object(helper, 'root_devices', return_value=(filesystem, image, root_sys, disk_sys)), \
            patch.object(helper.subprocess, 'check_output', side_effect=output), \
            patch.object(helper.subprocess, 'run', side_effect=command):
        assert helper.main() == 0
        assert helper.main() == 0

    updated_mbr, (start, sectors) = layout(image)
    changed = [index for index, (old, new) in enumerate(zip(original_mbr, updated_mbr)) if old != new]
    allowed = set(range(463, 466)) | set(range(467, 470)) | set(range(474, 478))
    assert set(changed) <= allowed, changed
    assert start == ROOT_START
    assert sectors > original_size
    # DOS growpart deliberately leaves some trailing sectors for alignment.
    assert 0 <= capacity_mib * MIB - (start + sectors) * SECTOR < MIB
    assert boot_hash(image) == original_boot
    assert ext4_uuid(filesystem) == original_uuid
    assert ext4_bytes(filesystem) <= sectors * SECTOR
    assert sectors * SECTOR - ext4_bytes(filesystem) < 4096
    assert run('debugfs', '-R', 'cat /preserved.txt', str(filesystem)).stdout.encode() == PAYLOAD
    run('e2fsck', '-f', '-n', str(filesystem))
    growth_events = [event for event in events if event['command'] == 'growpart']
    assert [event['returncode'] for event in growth_events] == ([1, 1] if recover else [0, 1])
    assert len([event for event in events if event['command'] == 'resize2fs']) == 2
    print(json.dumps({'capacity_mib': capacity_mib, 'recover_existing_partition': recover,
                      'mbr_changed_offsets': changed, 'root_partition_bytes': sectors * SECTOR,
                      'ext4_bytes': ext4_bytes(filesystem), 'events': events}, sort_keys=True), flush=True)


def verify_service(directory):
    if not shutil.which('systemd-analyze'):
        print('systemd-analyze unavailable; unit verification skipped', flush=True)
        return
    binary = directory / 'canaan-grow-rootfs'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    unit = directory / 'canaan-grow-rootfs.service'
    source = (PROJECT / 'stage2/canaan-grow-rootfs.service').read_text()
    assert source.count('ExecStart=/usr/sbin/canaan-grow-rootfs') == 1
    unit.write_text(source.replace('ExecStart=/usr/sbin/canaan-grow-rootfs', f'ExecStart={binary}'))
    result = REAL_RUN(['systemd-analyze', 'verify', '--man=no', str(unit)],
                      capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    print('systemd-analyze verify: passed', flush=True)


def main():
    for executable in ('growpart', 'sfdisk', 'mke2fs', 'resize2fs', 'e2fsck', 'debugfs'):
        if not shutil.which(executable):
            raise SystemExit(f'missing required tool: {executable}')
    spec = importlib.util.spec_from_file_location('canaan_grow_rootfs', PROJECT / 'stage2/canaan-grow-rootfs.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with tempfile.TemporaryDirectory(prefix='canaan-grow-integration-') as temporary:
        directory = Path(temporary)
        for index, (capacity, recover) in enumerate(((96, False), (160, False), (128, True))):
            case = directory / f'case-{index}'
            case.mkdir()
            exercise(helper, case, capacity, recover)
        verify_service(directory)
    print('All regular-file growth integration checks passed.', flush=True)


if __name__ == '__main__':
    main()
