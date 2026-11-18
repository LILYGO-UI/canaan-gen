#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Small read-only FDT v17 decoder used to verify the booted board DTB."""
import struct


def parse(data):
    """Return absolute node paths mapped to raw property bytes.

    Reject malformed or ambiguous trees rather than relying on substring
    matches in a DTB's string table. No device-tree code is executed.
    """
    if len(data) < 40:
        raise ValueError('truncated FDT header')
    (magic, total, tree_offset, strings_offset, reserve_offset, version,
     compatible, boot_cpu, strings_size, tree_size) = struct.unpack_from('>10I', data)
    if magic != 0xd00dfeed or version < 17 or compatible > 17:
        raise ValueError('unsupported FDT header/version')
    if total > len(data) or total < 40:
        raise ValueError('invalid FDT total size')
    for offset, size in ((tree_offset, tree_size), (strings_offset, strings_size)):
        if offset < 40 or offset + size > total:
            raise ValueError('FDT block lies outside the blob')
    if tree_offset % 4 or tree_size % 4:
        raise ValueError('unaligned FDT structure block')
    if max(tree_offset, strings_offset) < min(tree_offset + tree_size, strings_offset + strings_size):
        raise ValueError('overlapping FDT blocks')
    tree = data[tree_offset:tree_offset + tree_size]
    names = data[strings_offset:strings_offset + strings_size]
    nodes = {}
    stack = []
    position = 0
    ended = False

    def string(block, start):
        if start >= len(block):
            raise ValueError('FDT string offset outside block')
        end = block.find(b'\0', start)
        if end < 0:
            raise ValueError('unterminated FDT string')
        try:
            value = block[start:end].decode('ascii')
        except UnicodeError as error:
            raise ValueError('non-ASCII FDT node/property name') from error
        return value, end + 1

    while position < len(tree):
        if position + 4 > len(tree):
            raise ValueError('truncated FDT token')
        token, = struct.unpack_from('>I', tree, position)
        position += 4
        if token == 1:  # FDT_BEGIN_NODE
            name, position = string(tree, position)
            position = (position + 3) & ~3
            if '/' in name or (not stack and name) or (stack and not name):
                raise ValueError('invalid FDT node name')
            path = (stack[-1].rstrip('/') + '/' + name) if stack else '/'
            if path in nodes:
                raise ValueError('duplicate FDT node')
            nodes[path] = {}
            stack.append(path)
        elif token == 2:  # FDT_END_NODE
            if not stack:
                raise ValueError('unbalanced FDT end node')
            stack.pop()
        elif token == 3:  # FDT_PROP
            if not stack or position + 8 > len(tree):
                raise ValueError('invalid FDT property')
            size, name_offset = struct.unpack_from('>II', tree, position)
            position += 8
            if position + size > len(tree):
                raise ValueError('truncated FDT property value')
            name, _ = string(names, name_offset)
            if not name or name in nodes[stack[-1]]:
                raise ValueError('empty or duplicate FDT property')
            nodes[stack[-1]][name] = tree[position:position + size]
            position = (position + size + 3) & ~3
        elif token == 4:  # FDT_NOP
            continue
        elif token == 9:  # FDT_END
            if stack or '/' not in nodes:
                raise ValueError('incomplete FDT tree')
            ended = True
            break
        else:
            raise ValueError(f'unknown FDT token {token}')
    if not ended:
        raise ValueError('missing FDT end token')
    return nodes


def cells(value):
    if len(value) % 4:
        raise ValueError('FDT cell property has a partial cell')
    return list(struct.unpack('>' + 'I' * (len(value) // 4), value))


def strings(value):
    if not value or not value.endswith(b'\0'):
        raise ValueError('FDT string property is not NUL terminated')
    try:
        return value[:-1].decode('ascii').split('\0')
    except UnicodeError as error:
        raise ValueError('non-ASCII FDT string property') from error
