#!/usr/bin/env python3
"""Reject images that cannot enter the launcher's first-use account wizard."""
import argparse
from pathlib import Path
import struct
import os
import re
import stat
import subprocess


# The board runtime may hand device paths to the launcher and its applications.
# Nothing else about the service is changed by this drop-in.
BOARD_DROP_IN = '10-t-display-k230.conf'
BOARD_ENVIRONMENT = 'etc/default/lilygo-k230-ui'
BOARD_VARIABLE = re.compile(r'(?:CM0|LILYGO_UI|LILYGO_MAPS|LILYGO_SENSORS|CELLULAR|NES)_[A-Z0-9_]+')


def check_board_environment(root, drop_in):
    settings = [line.strip() for line in drop_in.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    if settings != ['[Service]', f'EnvironmentFile=-/{BOARD_ENVIRONMENT}']:
        raise ValueError('Launcher board drop-in may only read the board environment file')
    environment = root / BOARD_ENVIRONMENT
    if environment.is_symlink() or not environment.is_file():
        raise ValueError('Launcher board environment file is missing')
    for line in environment.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if (not separator or not BOARD_VARIABLE.fullmatch(name)
                or not re.fullmatch(r'/[A-Za-z0-9._/-]+', value) or '..' in value.split('/')):
            raise ValueError(f'Launcher board environment may only set device paths: {line}')


def check(root, enable_ssh=False):
    root = Path(root)
    ordinary = []
    for line in (root / 'etc/passwd').read_text().splitlines():
        fields = line.split(':')
        if len(fields) == 7 and 1000 <= int(fields[2]) < 65534:
            ordinary.append(fields[0])
    if ordinary:
        raise ValueError('Launcher setup requires no preset ordinary users: ' + ', '.join(ordinary))
    shadows = {line.split(':', 1)[0]: line.split(':')[1]
               for line in (root / 'etc/shadow').read_text().splitlines()}
    if not shadows.get('root', '').startswith(('!', '*')):
        raise ValueError('root password must remain locked before first setup')
    executable = root / 'usr/bin/lilygo-ui-launcher'
    if not executable.stat().st_mode & 0o111:
        raise ValueError('Launcher executable has no execute permission')
    data = executable.read_bytes()
    if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 243:
        raise ValueError('Launcher is not an ELF64 RISC-V executable')
    dynamic = subprocess.run(['readelf', '-d', str(executable)], check=True,
                             capture_output=True, text=True).stdout
    if '[libpam.so.0]' not in dynamic:
        raise ValueError('Launcher has no PAM dependency; build with LAUNCHER_SYSTEM_SESSION=ON')
    for name in ('lilygo-ui-launcher-first-boot', 'lilygo-ui-launcher-setup-region',
                 'lilygo-ui-launcher-console'):
        path = root / 'usr/lib/lilygo-ui-launcher' / name
        if not path.is_file() or not path.stat().st_mode & 0o111:
            raise ValueError(f'missing executable launcher helper: {name}')
    if not (root / 'etc/pam.d/lilygo-ui-launcher').is_file():
        raise ValueError('Launcher PAM policy is missing')
    unit = root / 'usr/lib/systemd/system/lilygo-ui-launcher.service'
    systemd = root / 'etc/systemd/system'
    override = systemd / unit.name
    if override.exists() or override.is_symlink():
        raise ValueError('Launcher service has a local override or mask')
    for directory in (systemd, root / 'usr/lib/systemd/system'):
        for drop_in in sorted((directory / (unit.name + '.d')).glob('*.conf')):
            if directory != systemd or drop_in.name != BOARD_DROP_IN:
                raise ValueError('Launcher service drop-ins require a reviewed first-boot profile')
            check_board_environment(root, drop_in)
    contents = unit.read_text()
    if 'User=root' not in contents or '--input auto' not in contents or '--display drm' not in contents:
        raise ValueError('Launcher systemd service lacks the Debian DRM first-boot configuration')
    enabled = root / 'etc/systemd/system/multi-user.target.wants/lilygo-ui-launcher.service'
    if not enabled.is_symlink():
        raise ValueError('Launcher service is not enabled for boot')
    target = str(enabled.readlink())
    logical_parent = '/' + str(enabled.parent.relative_to(root))
    logical_target = os.path.normpath(target if target.startswith('/') else logical_parent + '/' + target)
    if logical_target not in ('/usr/lib/systemd/system/' + unit.name, '/lib/systemd/system/' + unit.name):
        raise ValueError('Launcher service is not enabled for boot')
    for name in ('active-user', 'first-boot-pending', 'first-boot-new-user', 'first-boot-progress'):
        path = root / 'var/lib/lilygo-ui-launcher' / name
        if path.exists() or path.is_symlink():
            raise ValueError(f'first-boot image contains stale account state: {name}')
    state_dir = root / 'var/lib/lilygo-ui-launcher'
    if state_dir.exists() or state_dir.is_symlink():
        metadata = state_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('Launcher state directory must be root-owned mode 0700')
    for target_dir in list(systemd.glob('*.wants')) + list(systemd.glob('*.requires')):
        for name in ('ssh.service', 'sshd.service', 'ssh.socket'):
            path = target_dir / name
            if not enable_ssh and (path.exists() or path.is_symlink()):
                raise ValueError('SSH must remain disabled when ENABLE_SSH=0')
    if enable_ssh:
        enabled_ssh = systemd / 'multi-user.target.wants/ssh.service'
        if not enabled_ssh.is_symlink():
            raise ValueError('SSH service is not enabled for boot with ENABLE_SSH=1')
        target = str(enabled_ssh.readlink())
        logical_parent = '/' + str(enabled_ssh.parent.relative_to(root))
        logical_target = os.path.normpath(target if target.startswith('/') else logical_parent + '/' + target)
        if logical_target not in ('/usr/lib/systemd/system/ssh.service', '/lib/systemd/system/ssh.service'):
            raise ValueError('SSH service is not enabled for boot with ENABLE_SSH=1')
        override = systemd / 'ssh.service'
        if override.exists() or override.is_symlink():
            raise ValueError('SSH service has a local override or mask')
    ssh_configs = [root / 'etc/ssh/sshd_config', *(root / 'etc/ssh/sshd_config.d').glob('*.conf')]
    for path in ssh_configs:
        if path.is_file() and any(line.strip().lower().startswith('allowusers ') for line in path.read_text().splitlines()):
            raise ValueError('Preset SSH AllowUsers would restrict the account chosen in the wizard')
    fonts = root / 'usr/share/lilygo-ui/fonts'
    if not fonts.is_dir() or not any(fonts.glob('*.ttf')) or not any(fonts.glob('*.otf')):
        raise ValueError('AppKit runtime fonts are missing')
    if not (root / 'usr/share/doc/lilygo-ui-appkit-dev/OFL-1.1.txt').is_file():
        raise ValueError('AppKit font license is missing')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--enable-ssh', choices=('0', '1'), default='0')
    args = parser.parse_args()
    try:
        check(args.root, enable_ssh=args.enable_ssh == '1')
    except (ValueError, OSError, IndexError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'canaan-gen: {error}\n')
    print('Launcher first-boot image checks passed: no preset account, RISC-V executable, PAM, fonts and enabled DRM service')
