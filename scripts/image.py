#!/usr/bin/env python3
"""Inspect/assemble K230 MBR image files without loop devices or block writes."""
import argparse
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import uuid

SECTOR = 512
MBR_TABLE = 446


def regular_file(path):
    p = Path(path)
    if not stat.S_ISREG(p.stat().st_mode):
        raise ValueError(f"not a regular file: {p}")
    return p


def ext4_info(stream, offset, available):
    stream.seek(offset + 1024)
    sb = stream.read(1024)
    if len(sb) != 1024 or sb[56:58] != b'\x53\xef':
        raise ValueError("rootfs is not an ext4 filesystem")
    log_block = struct.unpack_from('<I', sb, 24)[0]
    if log_block > 6:
        raise ValueError("invalid ext4 block size")
    blocks = struct.unpack_from('<I', sb, 4)[0]
    incompat = struct.unpack_from('<I', sb, 96)[0]
    if incompat & 0x80:
        blocks |= struct.unpack_from('<I', sb, 336)[0] << 32
    size = blocks * (1024 << log_block)
    if size < 2048 or size > available:
        raise ValueError("ext4 size exceeds its containing file/partition")
    return {'bytes': size, 'uuid': str(uuid.UUID(bytes=sb[104:120])),
            'label': sb[120:136].rstrip(b'\0').decode('ascii', errors='replace')}


def inspect(path, root_partition=2):
    path = regular_file(path)
    length = path.stat().st_size
    if length % SECTOR:
        raise ValueError("image length must be a multiple of 512 bytes")
    with path.open('rb') as stream:
        mbr = stream.read(SECTOR)
        if len(mbr) != SECTOR or mbr[510:512] != b'\x55\xaa':
            raise ValueError("missing MBR signature")
        parts = []
        for index in range(4):
            raw = mbr[MBR_TABLE + index * 16:MBR_TABLE + (index + 1) * 16]
            boot, kind = raw[0], raw[4]
            start, count = struct.unpack_from('<II', raw, 8)
            if raw == bytes(16):
                continue
            if boot not in (0, 0x80) or kind != 0x83 or not start or not count:
                raise ValueError("unsupported MBR partition (only primary Linux partitions are supported)")
            if start + count > length // SECTOR:
                raise ValueError("partition extends beyond image")
            parts.append({'number': index + 1, 'type': kind,
                          'start_sector': start, 'sectors': count})
        if [p['number'] for p in parts] != [1, 2] or root_partition != 2:
            raise ValueError("T-Display-K230 requires exactly boot p1 and rootfs p2")
        by_start = sorted(parts, key=lambda p: p['start_sector'])
        if by_start[-1]['number'] != root_partition:
            raise ValueError("rootfs must be the last partition")
        for left, right in zip(by_start, by_start[1:]):
            if left['start_sector'] + left['sectors'] > right['start_sector']:
                raise ValueError("overlapping partitions")
        for part in parts:
            part['filesystem'] = ext4_info(stream, part['start_sector'] * SECTOR,
                                           part['sectors'] * SECTOR)
    return {'table': 'dos', 'sector_size': SECTOR, 'image_bytes': length,
            'disk_signature': mbr[440:444].hex(), 'partitions': parts,
            'root_partition': root_partition}


def copy_bytes(src, dst, count):
    while count:
        block = src.read(min(count, 4 * 1024 * 1024))
        if not block:
            raise ValueError('unexpected end of input')
        dst.write(block)
        count -= len(block)


def assemble(base, rootfs, output, root_partition=2):
    base, rootfs = regular_file(base), regular_file(rootfs)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError(f'output already exists: {output}')
    if output.resolve() in (base.resolve(), rootfs.resolve()):
        raise ValueError('output must differ from inputs')
    info = inspect(base, root_partition)
    root = info['partitions'][root_partition - 1]
    length = rootfs.stat().st_size
    with rootfs.open('rb') as source:
        fs = ext4_info(source, 0, length)
    if fs['uuid'] != root['filesystem']['uuid']:
        raise ValueError('replacement rootfs UUID must match the template (preserve root=UUID compatibility)')
    sectors = (length + SECTOR - 1) // SECTOR
    if root['start_sector'] + sectors > 0xffffffff:
        raise ValueError('image exceeds MBR addressing limit')
    prefix = root['start_sector'] * SECTOR
    fd, temporary = tempfile.mkstemp(prefix=f'.{output.name}.', dir=output.parent)
    try:
        with os.fdopen(fd, 'w+b') as destination:
            with base.open('rb') as source:
                copy_bytes(source, destination, prefix)
            with rootfs.open('rb') as source:
                copy_bytes(source, destination, length)
            destination.truncate(prefix + sectors * SECTOR)
            destination.seek(MBR_TABLE + (root_partition - 1) * 16 + 12)
            destination.write(struct.pack('<I', sectors))
            destination.flush()
            os.fsync(destination.fileno())
        inspect(temporary, root_partition)
        # Link instead of rename so a concurrently created output is never overwritten.
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
    return inspect(output, root_partition)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'assemble'):
        command = subs.add_parser(name)
        command.add_argument('--image', required=True)
        command.add_argument('--root-partition', type=int, default=2)
        if name == 'assemble':
            command.add_argument('--rootfs', required=True)
            command.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        result = (inspect(args.image, args.root_partition) if args.command == 'inspect'
                  else assemble(args.image, args.rootfs, args.output, args.root_partition))
    except (ValueError, OSError) as error:
        parser.exit(1, f'canaan-gen: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
