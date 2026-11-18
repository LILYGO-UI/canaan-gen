#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a pinned T-Display-K230 BSP in an exclusively managed cache.

The upstream board apply script is deliberately run only before the first
build of a generation. A changed source input selects a new generation, so
Buildroot cannot silently reuse a Linux tree patched with an older series.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


BASE = Path(__file__).resolve().parents[1]
DEFCONFIG = 'k230_canmv_t_display_rm69a10_defconfig'
MIN_FREE_GIB = 60


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, **kwargs):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    subprocess.run([str(arg) for arg in command], check=True, **kwargs)


def output(command):
    return subprocess.check_output([str(arg) for arg in command], text=True).strip()


def plan(board, work):
    board = Path(board).resolve(strict=True)
    stage = load_module('canaan_stage_sdk', board / 'scripts/stage-sdk.py')
    metadata = stage.read_sources(board)
    # Validate the complete local layer too, using the same entry point used
    # when staging. No network access or SDK checkout is needed for --verify.
    subprocess.run([sys.executable, str(board / 'scripts/stage-sdk.py'), '--verify'],
                   check=True, stdout=subprocess.DEVNULL)
    # Reject a malformed hardware contract before any expensive SDK download.
    checker = load_module('canaan_peripheral_profile', BASE / 'scripts/check-bsp.py')
    checker.read_peripheral_requirements(board / 'profile.json')
    for key in ('commit', 'sdk_commit', 'kernel_commit'):
        if not re.fullmatch('[0-9a-f]{40}', metadata[key]):
            raise ValueError(f'{key} must pin a complete Git commit')
    inputs = {}
    for path in sorted(board.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
            if path.name != 'SOURCE-PROVENANCE.json':
                inputs[str(path.relative_to(board))] = digest(path)
    recipe = {'format': 1, 'board': 't-display-k230', 'defconfig': DEFCONFIG,
              'board_repository': metadata['repository'], 'board_commit': metadata['commit'],
              'sdk_repository': metadata['sdk_repository'], 'sdk_commit': metadata['sdk_commit'],
              'kernel_repository': metadata['kernel_repository'], 'kernel_commit': metadata['kernel_commit'],
              'board_files_sha256': inputs,
              'builder_sha256': digest(Path(__file__)),
              'dockerfile_sha256': digest(BASE / 'Dockerfile.bsp')}
    key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:24]
    generation = Path(work).absolute() / 'builds' / key
    return {'key': key, 'recipe': recipe, 'generation': str(generation),
            'bsp_dir': str(generation / 'sdk/output' / DEFCONFIG)}


def check_space(path, minimum):
    ancestor = Path(path).absolute()
    while not ancestor.exists():
        ancestor = ancestor.parent
    available = shutil.disk_usage(ancestor).free / (1024 ** 3)
    if available < minimum:
        raise ValueError(f'BSP source build needs at least {minimum:g} GiB free; '
                         f'{ancestor} has {available:.1f} GiB. Free space or move BSP_WORK_DIR / '
                         'the Docker disk before building; no sources have been downloaded.')
    return available


@contextmanager
def cache_lock(work):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if work.is_symlink() or (work / '.build.lock').is_symlink():
        raise ValueError('BSP cache and lock must not be symlinks')
    with (work / '.build.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('another BSP build is using this cache; retry when it finishes') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def checkout(repository, commit, path, sparse=None):
    if path.is_symlink():
        raise ValueError(f'refusing source checkout symlink: {path}')
    if not path.exists():
        run(['git', 'init', path])
        run(['git', '-C', path, 'remote', 'add', 'origin', repository])
    if output(['git', '-C', path, 'remote', 'get-url', 'origin']) != repository:
        raise ValueError(f'cached source has a different origin: {path}')
    ready = subprocess.run(['git', '-C', str(path), 'rev-parse', '--verify', 'HEAD'],
                           capture_output=True, text=True)
    if ready.returncode == 0:
        if ready.stdout.strip() != commit:
            raise ValueError(f'cached source HEAD differs from pinned commit: {path}')
        return
    fetch_args = ['git', '-C', path, 'fetch', '--depth=1']
    if sparse:
        run(['git', '-C', path, 'config', 'remote.origin.promisor', 'true'])
        run(['git', '-C', path, 'config', 'remote.origin.partialclonefilter', 'blob:none'])
        fetch_args.append('--filter=blob:none')
    run([*fetch_args, 'origin', commit])
    if sparse:
        run(['git', '-C', path, 'sparse-checkout', 'init', '--cone'])
        run(['git', '-C', path, 'sparse-checkout', 'set', sparse])
    run(['git', '-C', path, 'checkout', '--detach', commit])
    if output(['git', '-C', path, 'rev-parse', 'HEAD']) != commit:
        raise ValueError(f'checkout did not produce pinned commit: {path}')
    if not sparse and (path / '.gitmodules').is_file():
        run(['git', '-C', path, 'submodule', 'update', '--init', '--recursive'])


def snapshot_board(board, generation, expected):
    """Freeze source inputs before a potentially long download/build."""
    destination = generation / 'board-layer'
    if not destination.exists():
        temporary = generation / 'board-layer.partial'
        if temporary.exists():
            # A partial snapshot is only an input copy made by this builder.
            # Preserve it for diagnosis instead of deleting user-visible data.
            temporary.rename(generation / ('board-layer.interrupted-' +
                             datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
        shutil.copytree(board, temporary, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo',
                                                                       'SOURCE-PROVENANCE.json'))
        actual = {str(p.relative_to(temporary)): digest(p) for p in temporary.rglob('*') if p.is_file()}
        if actual != expected:
            raise ValueError('board sources changed while taking the build snapshot; retry')
        temporary.rename(destination)
    actual = {str(p.relative_to(destination)): digest(p) for p in destination.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')}
    if actual != expected:
        raise ValueError('cached board source snapshot changed')
    return destination


def sdk_inputs(sdk):
    """Detect manual/partial input edits when resuming an interrupted build."""
    # The SDK generates some caches inside buildroot-overlay itself. Its Git
    # ignore rules distinguish those from source files, while --others keeps
    # the new board patches and fragments that staging adds to the checkout.
    names = subprocess.check_output([
        'git', '-C', str(sdk), 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
    ]).split(b'\0')
    result = {}
    for raw in sorted(set(names)):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if '.lilygo-k230-backups' in relative.parts:
            continue
        path = Path(sdk) / relative
        if path.is_symlink():
            result[str(relative)] = hashlib.sha256(
                b'symlink\0' + os.fsencode(os.readlink(path))).hexdigest()
        elif path.is_file():
            result[str(relative)] = digest(path)
        elif not path.exists():
            # The vendor apply script deliberately removes tracked first-boot
            # resize files. Seal their absence as part of the prepared inputs;
            # later deletion or restoration still changes the saved mapping.
            result[str(relative)] = None
        else:
            raise ValueError(f'cached SDK source file has an unsupported type: {path}')
    return result


def toolchain_ready(compiler):
    if not compiler.is_file():
        return False
    try:
        subprocess.run([str(compiler), '--version'], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        # The driver can work even if an interrupted extraction omitted cc1 or
        # libc. Compile and link a tiny RISC-V executable to validate the tools.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='canaan-toolchain-') as directory:
            subprocess.run([str(compiler), '-x', 'c', '-', '-o', str(Path(directory) / 'probe')],
                           input=b'int main(void) { return 0; }\n', check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def kernel_config(bsp):
    configs = list((bsp / 'build').glob('linux-*/.config'))
    if len(configs) != 1:
        raise ValueError('source BSP must contain exactly one built Linux .config')
    return configs[0]


def require_clean_checkout(path):
    changes = output(['git', '-C', path, 'status', '--porcelain', '--untracked-files=normal'])
    if changes:
        raise ValueError(f'pinned source checkout contains local changes: {path}')


def payload_hashes(bsp):
    """Seal boot artifacts and every vendor payload imported by stage2."""
    paths = [bsp / 'images/sysimage-sdcard.img', bsp / 'images/Image', kernel_config(bsp)]
    paths.extend(sorted((bsp / 'images').glob('*.dtb')))
    paths.extend(p for p in sorted((bsp / 'target/lib/modules').rglob('*'))
                 if p.is_file() and p.name not in ('build', 'source'))
    for root in ('target/lib/firmware', 'target/etc/vvcam'):
        paths.extend(p for p in sorted((bsp / root).rglob('*')) if p.is_file() or p.is_symlink())
    for name in ('usr/bin/isp_media_server', 'usr/bin/media-ctl', 'usr/bin/v4l2-sysfs-path',
                 'usr/lib/libvvcam.so', 'usr/lib/libmxml.so.1.6'):
        path = bsp / 'target' / name
        if path.exists() or path.is_symlink():
            paths.append(path)
    result = {}
    for path in paths:
        value = digest(path) if path.is_file() else ''
        if path.is_symlink():
            value = hashlib.sha256(('symlink\0' + os.readlink(path) + '\0' + value).encode()).hexdigest()
        result[str(path.relative_to(bsp))] = value
    return result


def validate_bsp(board, bsp):
    checker = load_module('canaan_check_bsp_source', BASE / 'scripts/check-bsp.py')
    result = checker.check(bsp, kernel_config(bsp), require_camera_userspace=True,
                           board_profile=Path(board) / 'profile.json')
    image = load_module('canaan_image_source', BASE / 'scripts/image.py')
    image.inspect(bsp / 'images/sysimage-sdcard.img')
    return result


def check_cached(board, build):
    bsp = Path(build['bsp_dir'])
    record = bsp / 'SOURCE-BUILD.json'
    if not record.is_file():
        raise ValueError('no completed source BSP for these inputs; run build-docker.sh --bsp-only '
                         '(or build.sh --bsp-only on Linux) first')
    saved = json.loads(record.read_text())
    if saved.get('recipe') != build['recipe'] or saved.get('payload_sha256') != payload_hashes(bsp):
        raise ValueError('cached BSP inputs or payload changed; refusing to reuse this generation')
    validate_bsp(board, bsp)
    return bsp


def build_bsp(board, work, jobs, minimum):
    build = plan(board, work)
    if (Path(build['bsp_dir']) / 'SOURCE-BUILD.json').exists():
        return check_cached(board, build)
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('the pinned Xuantie compiler needs x86_64 Linux; use build-docker.sh '
                         '(it selects linux/amd64 on Apple Silicon)')
    check_space(work, minimum)
    with cache_lock(work):
        # A successful generation is immutable and is reused only after checking
        # its recorded content; failures keep intermediate output for resuming.
        if (Path(build['bsp_dir']) / 'SOURCE-BUILD.json').exists():
            return check_cached(board, build)
        generation = Path(build['generation'])
        generation.mkdir(parents=True, exist_ok=True)
        state = generation / 'recipe.json'
        if state.exists() and json.loads(state.read_text()) != build['recipe']:
            raise ValueError('cache generation belongs to different source inputs')
        save_json(state, build['recipe'])
        board_source = generation / 'T-Display-K230'
        sdk = generation / 'sdk'
        recipe = build['recipe']
        layer = snapshot_board(board, generation, recipe['board_files_sha256'])
        checkout(recipe['board_repository'], recipe['board_commit'], board_source, sparse='k230_bsp')
        checkout(recipe['sdk_repository'], recipe['sdk_commit'], sdk)
        require_clean_checkout(board_source)
        downloads = Path(work).absolute() / 'downloads'
        downloads.mkdir(exist_ok=True)
        download_link = sdk / 'dl'
        if not download_link.exists() and not download_link.is_symlink():
            download_link.symlink_to(downloads, target_is_directory=True)
        if not download_link.is_symlink() or download_link.resolve() != downloads.resolve():
            raise ValueError('managed SDK download cache has an unexpected destination')
        prepared = generation / 'prepared.json'
        if not prepared.exists():
            applying = generation / 'applying.json'
            if not applying.exists():
                require_clean_checkout(sdk)
                save_json(applying, {'recipe': recipe})
            run(['bash', board_source / 'k230_bsp/scripts/apply.sh', sdk])
            run([sys.executable, layer / 'scripts/stage-sdk.py', '--sdk', sdk])
            save_json(prepared, {'recipe': recipe, 'sdk_inputs': sdk_inputs(sdk)})
        else:
            previous = json.loads(prepared.read_text())
            if previous.get('recipe') != recipe or previous.get('sdk_inputs') != sdk_inputs(sdk):
                raise ValueError('prepared SDK inputs changed; refusing to reuse an already patched build tree')
        # The SDK installs a fixed x86_64 Xuantie toolchain into /opt/toolchain.
        # Parse its actual defconfig instead of silently using a host compiler.
        config = (sdk / 'buildroot-overlay/configs' / DEFCONFIG).read_text()
        match = re.search(r'^BR2_TOOLCHAIN_EXTERNAL_PATH="([^"]+)"$', config, re.M)
        if not match or not match[1].startswith('/opt/toolchain/'):
            raise ValueError('unexpected SDK external toolchain path')
        compiler = Path(match[1]) / 'bin/riscv64-unknown-linux-gnu-gcc'
        if not toolchain_ready(compiler):
            if os.geteuid() != 0:
                raise ValueError('install the pinned SDK toolchain with sudo make toolchain_and_depend '
                                 'or use build-docker.sh')
            run(['make', '-C', sdk, 'toolchain_and_depend'])
        if not toolchain_ready(compiler):
            raise ValueError(f'upstream toolchain installation did not produce a working {compiler}')
        run([compiler, '--version'])
        bsp = Path(build['bsp_dir'])
        if not (bsp / '.config').exists():
            run(['make', '-C', sdk, f'CONF={DEFCONFIG}', DEFCONFIG])
        run(['make', '-C', sdk, f'CONF={DEFCONFIG}', f'BR2_JLEVEL={jobs}', 'all'])
        validation = validate_bsp(layer, bsp)
        record = {'format': 1, 'kind': 'source-built-bsp', 'recipe': recipe,
                  'built_at': datetime.now(timezone.utc).isoformat(),
                  'payload_sha256': payload_hashes(bsp), 'checks': validation,
                  'hardware_boot_test': False}
        save_json(bsp / 'SOURCE-BUILD.json', record)
        return bsp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'path', 'build', 'check', 'space'))
    parser.add_argument('--board-dir', type=Path)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument('--min-free-gib', type=int, default=MIN_FREE_GIB)
    args = parser.parse_args()
    try:
        if args.jobs < 1 or args.min_free_gib < 15:
            raise ValueError('jobs must be positive and min-free-gib must be at least 15')
        if args.action == 'space':
            check_space(args.work_dir, args.min_free_gib)
            return
        if args.board_dir is None:
            parser.error('--board-dir is required')
        build = plan(args.board_dir, args.work_dir)
        if args.action == 'plan':
            print(json.dumps(build, indent=2))
        elif args.action == 'path':
            print(build['bsp_dir'])
        elif args.action == 'check':
            print(f'Validated source BSP: {check_cached(args.board_dir, build)}')
        else:
            print(f'Built and validated BSP: {build_bsp(args.board_dir, args.work_dir, args.jobs, args.min_free_gib)}')
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'BSP source build: {error}\n')


if __name__ == '__main__':
    main()
