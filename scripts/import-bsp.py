#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract canaan-gen BSP inputs from a vendor .img, .img.gz or .img.xz file.

Only regular image files are accepted. No filesystem is mounted, no target
binary is executed, and no block device is opened. A failed import is retained
in its newly created staging directory, whose path is reported to the caller.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zlib

CHUNK = 4 * 1024 * 1024
MAX_CONFIG = 8 * 1024 * 1024

# The Buildroot userspace is intentionally not copied wholesale into Debian.
# These are the small, board-specific pieces needed to bring up the GC2093
# pipeline.  The binaries use the same riscv64 glibc ABI as Debian; the
# service installed by stage2 controls their startup and module ordering.
CAMERA_RUNTIME_FILES = (
    ('/usr/bin/isp_media_server', 'target/usr/bin/isp_media_server'),
    ('/usr/bin/media-ctl', 'target/usr/bin/media-ctl'),
    ('/usr/bin/v4l2-sysfs-path', 'target/usr/bin/v4l2-sysfs-path'),
    ('/usr/lib/libvvcam.so', 'target/usr/lib/libvvcam.so'),
    ('/usr/lib/libmxml.so.1.6', 'target/usr/lib/libmxml.so.1.6'),
)


def sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMAGE = sibling('canaan_image', 'image.py')
BSP = sibling('canaan_check_bsp', 'check-bsp.py')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def input_kind(path):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f'input must be a regular image file, not a device or directory: {path}')
    for ending, kind in (('.img.gz', 'gzip'), ('.img.xz', 'xz'), ('.img', 'raw')):
        if path.name.endswith(ending):
            return kind
    raise ValueError('input filename must end in .img, .img.gz or .img.xz')


def unpack_image(source, destination, kind):
    opener = {'gzip': gzip.open, 'xz': lzma.open, 'raw': open}[kind]
    with opener(source, 'rb') as incoming, destination.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing, CHUNK)


def slice_partition(source, part, destination):
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        incoming.seek(part['start_sector'] * IMAGE.SECTOR)
        IMAGE.copy_bytes(incoming, outgoing, part['sectors'] * IMAGE.SECTOR)


def run_debugfs(executable, filesystem, command, work, log):
    # Commands and extraction destinations are fixed strings. User-controlled
    # paths are passed as subprocess arguments, never inserted in debugfs syntax.
    result = subprocess.run([executable, '-R', command, str(filesystem)], cwd=work,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, 'LC_ALL': 'C'})
    combined = result.stdout + result.stderr
    with log.open('a') as stream:
        stream.write(f'$ debugfs -R {command!r} {filesystem.name}\n{combined}\n')
    # debugfs can report extraction failures while returning status 0. A
    # non-root import may fail to reproduce root ownership; stage2 deliberately
    # normalizes owners. All other diagnostic output is an import failure.
    errors = []
    for line in combined.splitlines():
        if not line.strip() or re.fullmatch(r'debugfs [0-9][^\r\n]*', line):
            continue
        if re.fullmatch(r'(?:rdump|dump_file): Operation not permitted while changing ownership of .+', line):
            continue
        errors.append(line)
    if result.returncode or errors:
        detail = errors[0] if errors else f'exit status {result.returncode}'
        raise ValueError(f'debugfs extraction failed: {detail}; see {log}')


def kernel_configuration(kernel):
    data = kernel.read_bytes()
    start = 0
    while True:
        start = data.find(b'IKCFG_ST', start)
        if start < 0:
            raise ValueError('vendor boot/Image has no readable embedded IKCONFIG; use the exact completed SDK output instead')
        end = data.find(b'IKCFG_ED', start + 8)
        if end < 0:
            raise ValueError('vendor boot/Image has an incomplete embedded IKCONFIG')
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data[start + 8:end])) as compressed:
                config = compressed.read(MAX_CONFIG + 1)
            if len(config) > MAX_CONFIG:
                raise ValueError('embedded kernel configuration exceeds the 8 MiB limit')
            config = config.decode('utf-8')
            if re.search(r'^CONFIG_[A-Za-z0-9_]+=', config, re.MULTILINE):
                return config
        except (gzip.BadGzipFile, EOFError, UnicodeDecodeError, zlib.error):
            pass
        start += 8


def kernel_release(kernel, modules):
    versions = sorted(path.name for path in modules.iterdir() if path.is_dir())
    if len(versions) != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', versions[0]):
        raise ValueError('extracted modules must contain exactly one valid kernel release')
    release = versions[0]
    found = {match.decode('ascii', errors='replace') for match in
             re.findall(rb'Linux version ([^ \x00]+)', kernel.read_bytes())}
    if found != {release}:
        raise ValueError(f'boot/Image kernel release does not match module directory {release}')
    return release


def payload_manifest(root):
    entries = []
    for path in sorted((root / 'target').rglob('*')):
        if path.is_symlink():
            entries.append({'path': str(path.relative_to(root)), 'symlink': os.readlink(path)})
        elif path.is_file():
            entries.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                            'sha256': sha256(path)})
    return entries


def import_bsp(source, output, debugfs=None, include_camera_runtime=False):
    source = Path(source).absolute()
    output = Path(output).absolute()
    kind = input_kind(source)
    if os.path.lexists(output):
        raise ValueError(f'output already exists; choose a new directory: {output}')
    if not output.parent.is_dir():
        raise ValueError(f'output parent directory does not exist: {output.parent}')
    debugfs = debugfs or shutil.which('debugfs')
    if not debugfs:
        raise ValueError('debugfs is required; install the e2fsprogs package on the Linux build host')
    stage = Path(tempfile.mkdtemp(prefix=f'.{output.name}.import-', dir=output.parent))
    try:
        (stage / 'images').mkdir()
        (stage / 'target/lib').mkdir(parents=True)
        temporary = stage / '.extract'
        temporary.mkdir()
        full_image = stage / 'images/sysimage-sdcard.img'
        print(f'Import staging: {stage}', flush=True)
        source_hash = sha256(source)
        unpack_image(source, full_image, kind)
        layout = IMAGE.inspect(full_image)
        parts = {entry['number']: entry for entry in layout['partitions']}
        boot = temporary / 'boot.ext4'
        rootfs = temporary / 'rootfs.ext4'
        slice_partition(full_image, parts[1], boot)
        slice_partition(full_image, parts[2], rootfs)
        log = stage / 'import.log'
        run_debugfs(debugfs, boot, 'dump /Image Image', temporary, log)
        kernel = temporary / 'Image'
        if not kernel.is_file() or not kernel.stat().st_size:
            raise ValueError('boot partition did not provide a nonempty Image')
        config_text = kernel_configuration(kernel)
        # The rootfs is inspected through debugfs only. Its init scripts and
        # programs are never copied or executed.
        run_debugfs(debugfs, rootfs, 'rdump /lib/modules target/lib', stage, log)
        run_debugfs(debugfs, rootfs, 'rdump /lib/firmware target/lib', stage, log)
        if include_camera_runtime:
            (stage / 'target/usr/bin').mkdir(parents=True)
            (stage / 'target/usr/lib').mkdir(parents=True)
            (stage / 'target/etc').mkdir(parents=True)
            for source_path, destination in CAMERA_RUNTIME_FILES:
                run_debugfs(debugfs, rootfs, f'dump {source_path} {destination}', stage, log)
            # Calibration/tuning data is a directory containing the GC2093
            # profiles and the generic sensor profiles used by libvvcam.so.
            run_debugfs(debugfs, rootfs, 'rdump /etc/vvcam target/etc', stage, log)
        modules = stage / 'target/lib/modules'
        if not modules.is_dir():
            raise ValueError('rootfs extraction did not provide /lib/modules')
        release = kernel_release(kernel, modules)
        kernel_tree = stage / f'build/linux-{release}'
        kernel_tree.mkdir(parents=True)
        config = kernel_tree / '.config'
        config.write_text(config_text)
        # Retain the exact kernel alongside the full image for provenance.
        kernel_output = stage / 'images/Image'
        kernel.rename(kernel_output)
        checks = BSP.check(stage, config, require_camera_userspace=include_camera_runtime)
        checks['kernel_config'] = str(config.relative_to(stage))
        checks['target'] = 'target'
        provenance = {
            'kind': 'vendor-image-extracted-bsp-input',
            'source_recompiled': False,
            'board_boot_tested': False,
            'imported_utc': datetime.now(timezone.utc).isoformat(),
            'source_image': {'path': str(source), 'compression': kind, 'sha256': source_hash},
            'sysimage': {'path': 'images/sysimage-sdcard.img', 'sha256': sha256(full_image)},
            'kernel': {'path': 'images/Image', 'sha256': sha256(kernel_output),
                       'release': release, 'config_path': str(config.relative_to(stage)),
                       'config_sha256': sha256(config), 'config_source': 'boot/Image embedded IKCONFIG'},
            'layout': layout,
            'checks': checks,
            'payload': payload_manifest(stage),
        }
        (stage / 'INPUT-PROVENANCE.json').write_text(json.dumps(provenance, indent=2) + '\n')
        camera_provenance_note = (
            '- Camera runtime files were imported only when --include-camera-runtime was used.\n'
            if include_camera_runtime else
            '- No vendor userspace, dynamic loader, or init scripts were imported.\n'
        )
        (stage / 'INPUT-PROVENANCE.md').write_text(
            '# BSP input extracted from a vendor release image\n\n'
            'This input was extracted from a prebuilt firmware image. It is not a\n'
            'source rebuild or a completed Buildroot SDK. `INPUT-PROVENANCE.json`\n'
            'records the input image hash, partition layout, kernel/config hashes,\n'
            'and extracted module/firmware hashes.\n\n'
            '- `images/sysimage-sdcard.img` is the decompressed, otherwise unchanged vendor image.\n'
            '- `images/Image` and the embedded IKCONFIG came from its boot partition.\n'
            '- `target/lib/modules` and `target/lib/firmware` came from its rootfs.\n'
            + camera_provenance_note
            + '- No filesystem was mounted, target binary executed, or board flashed.\n'
            + '- File ownership is normalized to root:root by canaan-gen stage2.\n\n'
            + 'Kernel/module checks passed. Hardware boot and peripheral behavior\n'
            + 'still require testing the generated Debian image on the board.\n')
        shutil.rmtree(temporary)
        # mkdir provides exclusive creation even if another process claimed
        # output while extraction ran. Never overwrite an existing destination.
        output.mkdir()
        for child in stage.iterdir():
            child.rename(output / child.name)
        stage.rmdir()
        print(f'Imported BSP: {output} (kernel {release}, {checks["module_count"]} modules)', flush=True)
        return provenance
    except BaseException as error:
        if stage.exists():
            (stage / 'IMPORT-FAILED.txt').write_text(f'Import did not complete: {error}\n')
            print(f'Import failed; staging retained at {stage}', file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--include-camera-runtime', action='store_true',
                        help='extract the allowlisted GC2093 ISP runtime and /etc/vvcam tuning data')
    args = parser.parse_args()
    try:
        import_bsp(args.image, args.output, include_camera_runtime=args.include_camera_runtime)
    except (ValueError, OSError, EOFError, lzma.LZMAError, zlib.error) as error:
        parser.exit(1, f'canaan-gen: {error}\n')


if __name__ == '__main__':
    main()
