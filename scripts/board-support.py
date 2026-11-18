#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate and snapshot the T-Display-K230 files from lilygo-linux-dtoverlays."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


BOARD_PATH = Path('boards/T-Display-K230')


def source_files(board):
    return sorted(path for path in board.rglob('*')
                  if path.is_file() and '__pycache__' not in path.parts
                  and path.suffix not in ('.pyc', '.pyo')
                  and path.name != 'SOURCE-PROVENANCE.json')


def hashes(board):
    return {str(path.relative_to(board)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_files(board)}


def validate(board):
    spec = importlib.util.spec_from_file_location('check_bsp', Path(__file__).with_name('check-bsp.py'))
    bsp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bsp)
    bsp.read_board_profile(board / 'profile.json')
    bsp.read_kernel_requirements(board / 'profile.json')
    bsp.read_keyboard_requirements(board / 'profile.json')
    bsp.read_peripheral_requirements(board / 'profile.json')
    profile = json.loads((board / 'profile.json').read_text())
    runtime = profile.get('runtime', {})
    if runtime.get('service') != 'lilygo-k230-audio.service':
        raise ValueError('board profile must provide lilygo-k230-audio.service')
    if not {'alsa-utils', 'python3-libgpiod'}.issubset(runtime.get('packages', [])):
        raise ValueError('board profile must require alsa-utils and python3-libgpiod')
    files = runtime.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('board profile must list its Debian runtime files')
    payload = board / 'debian/rootfs'
    for child in payload.iterdir():
        if child.name not in ('etc', 'usr'):
            raise ValueError(f'board runtime must not contain top-level path: {child.name}')
    for relative in files:
        if not isinstance(relative, str):
            raise ValueError('invalid board runtime path')
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or path.parts[:1] not in (('etc',), ('usr',)):
            raise ValueError(f'invalid board runtime path: {relative}')
        source = board / 'debian/rootfs' / relative
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(f'missing board runtime file: {source}')
        if relative.startswith(('usr/bin/', 'usr/libexec/')) and not os.access(source, os.X_OK):
            raise ValueError(f'board runtime helper is not executable: {source}')
    actual = {str(path.relative_to(payload)) for path in payload.rglob('*') if path.is_file()}
    if actual != set(files):
        raise ValueError('board runtime files must exactly match profile.runtime.files')
    services = runtime_services(profile)
    for service in services:
        if (not isinstance(service, str) or not re.fullmatch(r'[a-z0-9][a-z0-9@._-]*\.service', service)
                or f'usr/lib/systemd/system/{service}' not in files):
            raise ValueError(f'board runtime service has no unit file: {service}')
    if runtime['service'] not in services or len(set(services)) != len(services):
        raise ValueError('board profile runtime.services must list each unit once, including runtime.service')
    for step in runtime_builds(profile):
        output = PurePosixPath(step.get('output', '')) if isinstance(step, dict) else None
        if (output is None or step.get('source') not in files or not step['source'].endswith('.c')
                or output.is_absolute() or '..' in output.parts or output.parts[:1] != ('usr',)
                or str(output) in files):
            raise ValueError('board runtime build steps need a listed C source and an unlisted output under usr/')
    # These are source files, not a rootfs: links could escape the snapshot.
    for path in board.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'board source must not contain symlinks: {path}')
    result = subprocess.run([sys.executable, str(board / 'scripts/stage-sdk.py'), '--verify'],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError('board Linux source verification failed: ' + result.stdout + result.stderr)
    return profile


def runtime_services(profile):
    """Units to enable; profiles before the services list only had the audio unit."""
    runtime = profile['runtime']
    services = runtime.get('services', [runtime['service']])
    if not isinstance(services, list) or not services:
        raise ValueError('board profile runtime.services must be a nonempty list')
    return services


def runtime_builds(profile):
    """Helpers the image build compiles in the target rootfs: source and output paths."""
    builds = profile['runtime'].get('build', [])
    if not isinstance(builds, list):
        raise ValueError('board profile runtime.build must be a list')
    return builds


def snapshot(repo, destination):
    board = repo / BOARD_PATH
    validate(board)
    before = hashes(board)
    try:
        revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                           stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    shutil.copytree(board, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    validate(destination)
    if hashes(destination) != before or hashes(board) != before:
        raise ValueError('board source changed during snapshot; restart the build')
    evidence = {'repository': 'lilygo-linux-dtoverlays', 'revision': revision,
                'board': 't-display-k230', 'files_sha256': before,
                'linux_patches_applied_by_generator': False}
    (destination / 'SOURCE-PROVENANCE.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence


def check_rootfs(board, root):
    profile = validate(board)
    root = root.resolve()
    for relative in profile['runtime']['files']:
        path = root / relative
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'board runtime missing from rootfs: {relative}')
        if relative.startswith(('usr/bin/', 'usr/libexec/')) and not os.access(path, os.X_OK):
            raise ValueError(f'board runtime helper is not executable in rootfs: {relative}')
    # systemctl is-enabled and the Debian executables are checked inside the
    # chroot too; here resolve the usual absolute systemd link within rootfs.
    for service in runtime_services(profile):
        link = root / 'etc/systemd/system/multi-user.target.wants' / service
        if not link.is_symlink():
            raise ValueError(f'board service is not enabled in rootfs: {service}')
        target = Path(os.readlink(link))
        resolved = root / str(target).lstrip('/') if target.is_absolute() else link.parent / target
        if not resolved.is_file():
            raise ValueError(f'board service link is broken in rootfs: {service}')
        expected = root / 'usr/lib/systemd/system' / service
        if resolved.resolve() != expected or not resolved.resolve().is_relative_to(root):
            raise ValueError(f'board service link has an unexpected target: {service}')
    for step in runtime_builds(profile):
        built = root / step['output']
        if not built.is_file() or built.read_bytes()[:4] != b'\x7fELF':
            raise ValueError(f'board runtime helper was not built in rootfs: {step["output"]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--repo', type=Path)
    source.add_argument('--board-dir', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--snapshot', type=Path)
    action.add_argument('--rootfs', type=Path)
    action.add_argument('--list', choices=('services', 'packages', 'build'),
                                help='print one runtime list of the validated profile, one entry per line')
    args = parser.parse_args()
    board = args.repo / BOARD_PATH if args.repo is not None else args.board_dir
    try:
        if args.snapshot is not None:
            if args.repo is None:
                raise ValueError('--snapshot requires --repo')
            snapshot(args.repo, args.snapshot)
        elif args.rootfs is not None:
            check_rootfs(board, args.rootfs)
        elif args.list is not None:
            profile = validate(board)
            if args.list == 'services':
                lines = runtime_services(profile)
            elif args.list == 'packages':
                lines = profile['runtime']['packages']
            else:
                lines = [f"{step['source']} {step['output']}" for step in runtime_builds(profile)]
            print('\n'.join(lines))
            return
        else:
            validate(board)
    except (ValueError, OSError, TypeError) as error:
        parser.exit(1, f'canaan-gen: {error}\n')
    print('T-Display-K230 board source/runtime checks passed')


if __name__ == '__main__':
    main()
