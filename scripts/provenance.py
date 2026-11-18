#!/usr/bin/env python3
"""Record image inputs without recording user credentials or local config."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


p = argparse.ArgumentParser(description=__doc__)
for name in ('base', 'bsp', 'kernel-config', 'template', 'artifact', 'packages', 'output'):
    p.add_argument('--' + name, required=True)
p.add_argument('--board-support', help='validated board source snapshot used by this build')
a = p.parse_args()
base = Path(a.base)
sources = {}
for pattern in ('*.sh', 'Dockerfile*', 'scripts/*', 'stage*/*', 'boards/**/*', 'keys/*'):
    for path in sorted(base.glob(pattern)):
        if path.is_file():
            sources[str(path.relative_to(base))] = digest(path)
try:
    revision = subprocess.check_output(['git', '-C', str(base), 'rev-parse', 'HEAD'],
                                       stderr=subprocess.DEVNULL, text=True).strip()
except (subprocess.CalledProcessError, FileNotFoundError):
    revision = None
result = {'format': 1, 'generator': 'canaan-gen', 'generated_at': datetime.now(timezone.utc).isoformat(),
          'architecture': 'riscv64', 'release': 'trixie', 'board': 't-display-k230',
          'generator_revision': revision, 'generator_source_sha256': sources,
          'build_settings': {key: os.environ.get(key) for key in
                             ('DEBIAN_MIRROR', 'LILYGO_UI_INSTALL', 'ACCOUNT_SETUP', 'TARGET_HOSTNAME',
                             'LOCALE', 'TIMEZONE', 'ENABLE_SSH', 'BSP_CAMERA_RUNTIME', 'BSP_MODE',
                             'LILYGO_UI_APT_URL', 'LILYGO_UI_PACKAGES')},
          'bsp_template': {'name': Path(a.template).name, 'sha256': digest(a.template)},
          'kernel_config_sha256': digest(a.kernel_config),
          'packages_sha256': digest(a.packages),
          'artifact': {'name': Path(a.artifact).name, 'bytes': Path(a.artifact).stat().st_size,
                       'sha256': digest(a.artifact)},
          'validation': {'hardware_boot_test': False}}
if os.environ.get('LILYGO_UI_INSTALL') == 'apt':
    result['ui_repository'] = {
        'url': os.environ['LILYGO_UI_APT_URL'], 'suite': 'stable', 'component': 'main',
        'architecture': 'riscv64',
        'requested_packages': os.environ['LILYGO_UI_PACKAGES'].split(),
        'key_sha256': digest(os.environ['LILYGO_UI_APT_KEY_FILE']),
        'local_packages_used': False,
    }
input_provenance = Path(a.bsp) / 'INPUT-PROVENANCE.json'
if input_provenance.is_file():
    result['bsp_import_provenance'] = json.loads(input_provenance.read_text())
source_build = Path(a.bsp) / 'SOURCE-BUILD.json'
if source_build.is_file():
    result['bsp_source_build'] = json.loads(source_build.read_text())
if a.board_support:
    board = Path(a.board_support)
    result['board_support'] = json.loads((board / 'SOURCE-PROVENANCE.json').read_text())
    result['board_support']['profile'] = json.loads((board / 'profile.json').read_text())
    result['board_support']['linux_upstream'] = json.loads((board / 'linux/upstream.json').read_text())
    result['board_support']['linux_patches_applied_by_generator'] = 'bsp_source_build' in result
Path(a.output).write_text(json.dumps(result, indent=2) + '\n')
