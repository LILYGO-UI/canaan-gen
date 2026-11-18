#!/usr/bin/env python3
"""Export a relocatable K230 application sysroot from a trusted canaan-gen release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import image as disk

ARCH = 'riscv64'
TRIPLET = 'riscv64-linux-gnu'
PACKAGES = '''libc6-dev linux-libc-dev libgcc-14-dev libstdc++-14-dev libdrm-dev
libfreetype-dev libpng-dev zlib1g-dev libbrotli-dev libbz2-dev libasound2-dev
libjson-c-dev libjpeg62-turbo-dev libcurl4-openssl-dev libssl-dev libopus-dev
libpam0g-dev libglib2.0-dev libsqlite3-dev libpolkit-agent-1-dev'''.split()
SDK_PATHS = ('usr/include', f'usr/lib/{TRIPLET}', f'usr/lib/gcc/{TRIPLET}',
             'usr/lib/linux', 'usr/lib/pkgconfig', 'usr/share/pkgconfig', 'usr/share/aclocal',
             'usr/share/cmake', 'usr/share/common-licenses', 'usr/lib/os-release',
             'usr/lib/ld-linux-riscv64-lp64d.so.1')


def sha256(path):
    with path.open('rb') as f:
        digest = hashlib.sha256()
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            digest.update(block)
        return digest.hexdigest()


def fields(paragraph):
    return dict(line.split(': ', 1) for line in paragraph.splitlines()
                if line and not line[0].isspace() and ': ' in line)


def release_inputs(artifact):
    match = re.fullmatch(r'([a-zA-Z0-9][\w.-]*)\.(img|rootfs\.ext4)(?:\.xz)?', artifact.name)
    if not match or not artifact.is_file():
        raise ValueError('expected a regular canaan-gen .img[.xz] or .rootfs.ext4[.xz]')
    stem, kind = match.groups()
    manifest = json.loads(artifact.with_name(stem + '.json').read_text())
    inventory = artifact.with_name(stem + '.packages.tsv')
    if (manifest.get('generator'), manifest.get('board'), manifest.get('release'),
            manifest.get('architecture')) != ('canaan-gen', 't-display-k230', 'trixie', ARCH):
        raise ValueError('expected canaan-gen / t-display-k230 / trixie / riscv64 provenance')
    if manifest['artifact'] != dict(name=artifact.name, bytes=artifact.stat().st_size, sha256=sha256(artifact)):
        raise ValueError('image does not match provenance (name, length or SHA-256)')
    if sha256(inventory) != manifest['packages_sha256']:
        raise ValueError('package inventory SHA-256 mismatch')
    return stem, kind, manifest, inventory


def extract_inputs(artifact, kind, work):
    raw = artifact
    if artifact.suffix == '.xz':
        raw = work / 'unpacked'
        with raw.open('xb') as output:
            subprocess.run(['xz', '-dc', '-T2', str(artifact)], stdout=output, check=True)
    if kind == 'img':
        part = disk.inspect(raw)['partitions'][1]
        fs = work / 'partition.ext4'
        with raw.open('rb') as src, fs.open('xb') as dst:
            src.seek(part['start_sector'] * disk.SECTOR)
            disk.copy_bytes(src, dst, part['filesystem']['bytes'])
        if raw != artifact:
            raw.unlink()
    else:
        fs = raw
        with fs.open('rb') as src:
            disk.ext4_info(src, 0, fs.stat().st_size)
    root = work / 'inputs'
    for item in ['usr', 'var/lib/apt/lists', 'var/lib/dpkg', 'etc/apt/sources.list.d/debian.sources']:
        parent = root / Path(item).parent
        parent.mkdir(parents=True, exist_ok=True)
        command = f'rdump /{item} .' if not item.endswith('.sources') else f'dump /{item} debian.sources'
        result = subprocess.run(['debugfs', '-R', command, str(fs)], cwd=parent, text=True,
                                capture_output=True, check=True)
        errors = [line for line in (result.stdout + result.stderr).splitlines() if line and
                  not re.fullmatch(r'debugfs [0-9].*|(?:rdump|dump_file): Operation not permitted while changing ownership of .*', line)]
        if errors or not (root / item).exists():
            raise ValueError(f'cannot extract /{item}: {errors[:2]}')
    return root


def inventory_versions(status, inventory):
    records = [fields(p) for p in status.read_text().split('\n\n') if p.strip()]
    installed = {p['Package']: (p['Version'], p['Architecture']) for p in records
                 if p.get('Status', '').endswith(' ok installed')}
    expected = {name: (version, arch) for name, version, arch in
                (line.split('\t') for line in inventory.read_text().splitlines())}
    if installed != expected or any(a not in (ARCH, 'all') for _, a in installed.values()):
        raise ValueError('rootfs dpkg status differs from release inventory or has a foreign architecture')
    return installed


def package_pins(status, installed):
    pins = [f'Package: {name}\nPin: version {version}\nPin-Priority: 1001\n'
            for name, (version, _) in installed.items()]
    sources = {}
    for paragraph in status.read_text().split('\n\n'):
        rec = fields(paragraph)
        if rec.get('Status', '').endswith(' ok installed'):
            source = rec.get('Source', rec['Package']).split()
            version = source[1].strip('()') if len(source) > 1 else rec['Version']
            sources.setdefault(source[0], set()).add(version)
    for source, versions in sorted(sources.items()):
        for version in sorted(versions):
            pins.append(f'Package: src:{source}:any\nPin: source-version {version}\nPin-Priority: 1000\n')
        pins.append(f'Package: src:{source}:any\nPin: version *\nPin-Priority: -1\n')
    return '\n'.join(pins)


def resolve_debs(root, installed, work):
    # APT reads only the package indexes sealed in the image. A temporary state
    # directory keeps its locks, caches and preferences off the source rootfs.
    apt = work / 'apt'
    apt.mkdir()
    archives = apt / 'archives'
    (archives / 'partial').mkdir(parents=True)
    # APT can chown lists/auxfiles even for a download-only transaction.
    # Only the index bytes belong to the image; keep APT's mutable state private.
    lists = apt / 'lists'
    (lists / 'partial').mkdir(parents=True)
    for index in (root / 'var/lib/apt/lists').iterdir():
        if index.is_file() and ('_Packages' in index.name or index.name.endswith(('_InRelease', '_Release', '_Release.gpg'))):
            shutil.copyfile(index, lists / index.name)
    prefs = apt / 'preferences'
    prefs.write_text(package_pins(root / 'var/lib/dpkg/status', installed))
    empty = apt / 'empty'
    empty.mkdir()
    opts = {'Dir::Etc::main': '/dev/null', 'Dir::Etc::parts': empty,
            'Dir::Etc::preferences': prefs, 'Dir::Etc::preferencesparts': empty,
            'Dir::Etc::sourcelist': root / 'etc/apt/sources.list.d/debian.sources',
            'Dir::Etc::sourceparts': empty, 'Dir::State': apt,
            'Dir::State::status': root / 'var/lib/dpkg/status', 'Dir::State::lists': lists,
            'Dir::Cache::archives': archives, 'Dir::Cache::pkgcache': '', 'Dir::Cache::srcpkgcache': '',
            'Debug::NoLocking': 'true', 'APT::Architecture': ARCH, 'Acquire::Languages': 'none'}
    config = apt / 'apt.conf'
    config.write_text(''.join(f'{k} {json.dumps(str(v))};\n' for k, v in opts.items()) +
                      'APT::Architectures { "riscv64"; };\n')
    env = dict(os.environ, LC_ALL='C', APT_CONFIG=str(config))
    command = ['apt-get', '--no-install-recommends', '--no-remove', 'install', *PACKAGES]
    plan = subprocess.check_output(command[:1] + ['-s'] + command[1:], env=env, text=True)
    additions = set()
    for line in plan.splitlines():
        if line.startswith('Inst '):
            name = line.split()[1].split(':')[0]
            if name in installed:
                raise ValueError(f'SDK would change image package: {line}')
            additions.add(name)
    subprocess.run(command[:1] + ['-y', '--download-only', '-o', 'Acquire::Retries=2'] + command[1:],
                   env=env, check=True)
    files = sorted(archives.glob('*.deb'))
    found = {subprocess.check_output(['dpkg-deb', '-f', p, 'Package'], text=True).strip() for p in files}
    if found != additions:
        raise ValueError(f'downloaded packages differ from APT plan: {found ^ additions}')
    return files


def copy_tree(source, dest):
    if source.is_symlink():
        if dest.is_symlink() and os.readlink(source) == os.readlink(dest):
            return
        dest.symlink_to(os.readlink(source))
    elif source.is_dir():
        if dest.is_symlink():
            raise ValueError(f'cannot overlay a directory on a symlink: {dest}')
        dest.mkdir(exist_ok=True)
        for child in source.iterdir():
            copy_tree(child, dest / child.name)
    elif source.is_file():
        if dest.is_symlink():
            raise ValueError(f'cannot overlay a file on a symlink: {dest}')
        shutil.copy2(source, dest)


def copy_payload(src, dst):
    paths = list(SDK_PATHS)
    paths.extend(str(p.relative_to(src)) for p in (src / 'usr/lib').glob('*.so*'))
    paths.extend(str(p.relative_to(src)) for p in (src / 'usr/share/doc').glob('*/copyright')
                 if not p.parent.is_symlink())
    for item in dict.fromkeys(paths):
        source, dest = src / item, dst / item
        if source.exists() or source.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(source, dest)


def add_deb(package, sysroot, work):
    with tempfile.TemporaryDirectory(dir=work) as temp:
        data = Path(temp) / 'data'
        control = Path(temp) / 'control'
        subprocess.run(['dpkg-deb', '-e', package, control], check=True)
        paragraph = (control / 'control').read_text().strip()
        record = fields(paragraph)
        name, version, arch = (record[k] for k in ('Package', 'Version', 'Architecture'))
        if arch not in (ARCH, 'all'):
            raise ValueError(f'foreign package: {name}/{arch}')
        subprocess.run(['dpkg-deb', '-x', package, data], check=True)
        copy_payload(data, sysroot)
        db = sysroot / 'var/lib/dpkg'
        key = name + (':' + arch if record.get('Multi-Arch') == 'same' else '')
        listing = sorted('/' + str(p.relative_to(data)) for p in data.rglob('*'))
        (db / 'info' / (key + '.list')).write_text('\n'.join(listing) + '\n')
        for field in ('shlibs', 'symbols'):
            if (control / field).is_file():
                shutil.copyfile(control / field, db / 'info' / (key + '.' + field))
        with (db / 'status').open('a') as f:
            f.write('\n' + paragraph + '\nStatus: install ok installed\n')
    return dict(package=name, version=version, architecture=arch, sha256=sha256(package))


def build_sysroot(root, sysroot, archives, work):
    sysroot.mkdir()
    copy_payload(root, sysroot)
    (sysroot / 'lib').symlink_to('usr/lib')
    db = sysroot / 'var/lib/dpkg'
    (db / 'info').mkdir(parents=True)
    original = root / 'var/lib/dpkg'
    shutil.copyfile(original / 'status', db / 'status')
    for p in (original / 'info').iterdir():
        if p.suffix in ('.list', '.shlibs', '.symbols') or p.name == 'format':
            shutil.copyfile(p, db / 'info' / p.name)
    (db / 'arch').write_text(ARCH + '\n')
    overlay = [add_deb(p, sysroot, work) for p in archives]
    for parent, dirs, files in os.walk(sysroot):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                target = os.readlink(path)
                if target.startswith('/'):
                    path.unlink()
                    path.symlink_to(os.path.relpath(sysroot / target.lstrip('/'), parent))
                if not path.resolve().is_relative_to(sysroot):
                    raise ValueError(f'symlink escapes sysroot: {path}')
    for item in ('usr/include/stdio.h', 'usr/include/alsa/asoundlib.h', 'usr/include/xf86drm.h',
                 'lib/ld-linux-riscv64-lp64d.so.1', f'usr/lib/{TRIPLET}/crt1.o',
                 f'usr/lib/{TRIPLET}/libc.so', f'usr/lib/{TRIPLET}/pkgconfig/polkit-agent-1.pc'):
        if not (sysroot / item).is_file():
            raise ValueError(f'incomplete SDK: missing {item}')
    return overlay


def export(artifact, output, rootfs=None):
    stem, kind, provenance, inventory = release_inputs(artifact)
    output.mkdir(parents=True, exist_ok=True)
    release = output / (stem + '.sdk')
    if release.exists() or release.is_symlink():
        raise ValueError(f'SDK already exists: {release}')
    with tempfile.TemporaryDirectory(prefix='k230-sdk-') as temp, \
            tempfile.TemporaryDirectory(prefix='.sdk-', dir=output) as staging:
        work = Path(temp)
        print('Reading Debian rootfs and release package indexes', flush=True)
        root = rootfs or extract_inputs(artifact, kind, work)
        installed = inventory_versions(root / 'var/lib/dpkg/status', inventory)
        archives = resolve_debs(root, installed, work)
        sdk = work / 'sysroot'
        overlay = build_sysroot(root, sdk, archives, work)
        bundle = Path(staging) / release.name
        bundle.mkdir()
        meta = dict(format=1, board=provenance['board'], architecture=ARCH, release='trixie',
                    source_image=provenance['artifact'], packages_sha256=provenance['packages_sha256'],
                    requested_packages=PACKAGES, development_overlay=overlay)
        manifest = bundle / 'manifest.json'
        manifest.write_text(json.dumps(meta, indent=2) + '\n')
        sdk_meta = sdk / 'usr/share/canaan-sdk'
        sdk_meta.mkdir(parents=True)
        shutil.copyfile(manifest, sdk_meta / manifest.name)
        shutil.copyfile(inventory, bundle / 'image-packages.tsv')
        archive = bundle / (stem + '.sysroot.tar.gz')
        print('Archiving sysroot', flush=True)
        subprocess.run(['tar', '--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner',
                        '-czf', archive, '-C', work, 'sysroot'], check=True)
        (bundle / 'SHA256SUMS').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in sorted(bundle.iterdir())))
        bundle.rename(release)
    print(f'SDK ready: {release}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--build-rootfs', type=Path, help='same-build staging root (internal image build hook)')
    args = parser.parse_args()
    try:
        export(args.image.absolute(), args.output.absolute(), args.build_rootfs)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'export-sdk: {exc}\n')
