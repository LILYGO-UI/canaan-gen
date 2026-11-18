#!/usr/bin/python3
"""Grow the running K230 root partition and ext4 filesystem to fit its card."""
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys


def log(message):
    print(f'canaan-grow-rootfs: {message}', flush=True)


def root_devices():
    # /proc/mounts may call the root device /dev/root. Its device number works
    # independently of that alias, UUID mounts and the order of MMC probing.
    device = os.stat('/').st_dev
    root_sys = Path(f'/sys/dev/block/{os.major(device)}:{os.minor(device)}').resolve(strict=True)
    if (root_sys / 'partition').read_text().strip() != '2':
        raise ValueError('root must be the second ordinary disk partition')
    root = Path('/dev') / root_sys.name
    disk_sys = root_sys.parent
    disk = Path('/dev') / disk_sys.name
    if not stat.S_ISBLK(root.stat().st_mode) or root.stat().st_rdev != device:
        raise ValueError('root device does not match the mounted filesystem')
    if not stat.S_ISBLK(disk.stat().st_mode):
        raise ValueError('parent device is not a block device')
    if (disk_sys / 'queue/logical_block_size').read_text().strip() != '512':
        raise ValueError('only 512-byte logical sectors are supported')
    return root, disk, root_sys, disk_sys


def partition_layout(disk, disk_sectors):
    """Reject layouts other than the two primary Linux partitions we export."""
    with disk.open('rb') as stream:
        mbr = stream.read(512)
    if len(mbr) != 512 or mbr[510:512] != b'\x55\xaa':
        raise ValueError('missing MBR signature')
    parts = []
    for index in range(4):
        entry = mbr[446 + index * 16:462 + index * 16]
        if index >= 2:
            if entry != bytes(16):
                raise ValueError('extra partitions are not supported')
            continue
        start, size = struct.unpack_from('<II', entry, 8)
        if entry[0] not in (0, 0x80) or entry[4] != 0x83 or not start or not size:
            raise ValueError('expected two primary Linux partitions')
        if start + size > disk_sectors:
            raise ValueError('partition extends beyond the disk')
        parts.append((start, size))
    if parts[0][0] + parts[0][1] > parts[1][0]:
        raise ValueError('root must be last without overlapping the boot partition')
    if disk_sectors > 0xffffffff:
        raise ValueError('disk exceeds the supported MBR addressing limit')
    return mbr, parts[1]


def grow_rootfs():
    filesystem = subprocess.check_output(
        ['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', '/'], text=True).strip()
    if filesystem != 'ext4':
        raise ValueError(f'root must be ext4, found {filesystem!r}')
    root, disk, root_sys, disk_sys = root_devices()
    disk_sectors = int((disk_sys / 'size').read_text())
    before, (start, old_size) = partition_layout(disk, disk_sectors)
    if int((root_sys / 'start').read_text()) != start:
        raise ValueError('mounted root start does not match the partition table')

    log(f'checking {root} on {disk} ({disk_sectors * 512} bytes)')
    # growpart normally uses /tmp; use tmpfs so a full rootfs can still grow.
    environment = os.environ.copy()
    environment['TMPDIR'] = '/run'
    result = subprocess.run(['growpart', '--fudge', '0', str(disk), '2'], env=environment)
    # growpart documents 1 as NOCHANGE; 2 is a real error. Still resize ext4
    # after NOCHANGE, including recovery after an interrupted previous boot.
    if result.returncode not in (0, 1):
        raise RuntimeError(f'growpart failed with exit status {result.returncode}')
    after, (new_start, new_size) = partition_layout(disk, disk_sectors)
    # sfdisk may update the CHS addresses of p2. Everything else, including
    # boot code, disk identity, p1 and p2 start/type, must remain unchanged.
    allowed = set(range(463, 466)) | set(range(467, 470)) | set(range(474, 478))
    if any(left != right and offset not in allowed
           for offset, (left, right) in enumerate(zip(before, after))):
        raise ValueError('unexpected partition-table change; refusing filesystem resize')
    if new_start != start or new_size < old_size:
        raise ValueError('root partition moved or shrank; refusing filesystem resize')
    if int((root_sys / 'size').read_text()) != new_size:
        # Also handles a prior run that wrote the MBR but could not inform the
        # kernel. Never grow ext4 against a stale in-memory partition size.
        subprocess.run(['partx', '--update', '--nr', '2', str(disk)], check=True)
        if int((root_sys / 'size').read_text()) != new_size:
            raise RuntimeError('kernel partition size is stale; reboot to retry')
    subprocess.run(['resize2fs', str(root)], check=True)
    log(f'root filesystem uses the available partition ({new_size * 512} bytes)')


def main():
    try:
        grow_rootfs()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        log(f'failed: {error}; the next boot will retry')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
