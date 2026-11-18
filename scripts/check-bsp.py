#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only checks for a matching T-Display-K230 Linux BSP payload."""
import argparse
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
import struct
import subprocess
import sys
import tempfile
import zlib

# Debian's systemd/udev need these kernel interfaces. Requiring them built in
# also ensures early userspace can start without a newly generated initramfs.
REQUIRED_BUILTIN = (
    '64BIT', 'RISCV', 'MMU', 'BINFMT_ELF', 'BLOCK', 'EXT4_FS',
    'DEVTMPFS', 'DEVTMPFS_MOUNT', 'CGROUPS', 'FHANDLE', 'EPOLL',
    'SIGNALFD', 'TIMERFD', 'INOTIFY_USER', 'PROC_FS', 'SYSFS', 'TMPFS',
    'TMPFS_XATTR', 'NET', 'UNIX', 'UNIX98_PTYS', 'SYSVIPC', 'POSIX_TIMERS',
    'NAMESPACES', 'UTS_NS', 'IPC_NS', 'PID_NS', 'NET_NS', 'USER_NS',
    'AUTOFS_FS', 'SECCOMP', 'SECCOMP_FILTER', 'TMPFS_POSIX_ACL',
    'MEMCG', 'CGROUP_PIDS', 'CGROUP_SCHED', 'MODULES', 'FW_LOADER',
)
MODULE_SUFFIXES = ('.ko', '.ko.gz', '.ko.xz', '.ko.zst')
# SDIO Wi-Fi drivers that the vendor image loads at boot. Its documentation
# names RTL8189FS as the fitted chip and RTL8723DS as the alternative.
REQUIRED_MODULES = ('8189fs', '8723ds')
CAMERA_MODULES = ('vvcam_isp', 'vvcam_mipi', 'vvcam_vb', 'vvcam_isp_subdev', 'vvcam_video', 'nonai2d')


def sibling(name):
    spec = importlib.util.spec_from_file_location('canaan_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FDT = sibling('fdt')


def parse_config(path):
    if not path.is_file():
        raise ValueError(f'built kernel configuration is missing: {path}; provide BSP_KERNEL_CONFIG from the exact Image build')
    values = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r'CONFIG_([A-Za-z0-9_]+)=(.*)', line)
        if match:
            values[match[1]] = match[2]
    if not values:
        raise ValueError(f'no kernel configuration values found: {path}')
    missing = [f'CONFIG_{key}=y' for key in REQUIRED_BUILTIN if values.get(key) != 'y']
    if missing:
        raise ValueError('vendor kernel lacks required Debian/systemd or early-boot support: ' + ', '.join(missing) + '; rebuild the BSP kernel and template together')
    if values.get('CMDLINE_FORCE') == 'y':
        command_line = values.get('CMDLINE', '')
        if 'init=' in command_line or 'systemd.unified_cgroup_hierarchy=0' in command_line:
            raise ValueError('forced kernel command line overrides Debian init/cgroup behavior; review and rebuild the BSP')
    return values


def module_bytes(path):
    if path.name.endswith('.gz'):
        return gzip.decompress(path.read_bytes())
    if path.name.endswith('.xz'):
        return lzma.decompress(path.read_bytes())
    if path.name.endswith('.zst'):
        try:
            return subprocess.check_output(['zstd', '-dc', str(path)], stderr=subprocess.PIPE)
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise ValueError(f'cannot inspect compressed module {path}: install zstd') from error
    return path.read_bytes()


def read_board_profile(path):
    """Read the shared board's requirements, without treating them as evidence."""
    try:
        profile = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f'cannot read board profile {path}: {error}') from error
    if (not isinstance(profile, dict) or type(profile.get('format')) is not int
            or profile['format'] != 1 or profile.get('board') != 't-display-k230'):
        raise ValueError(f'invalid board profile {path}: expected format=1 and board=t-display-k230')
    audio = profile.get('audio')
    if not isinstance(audio, dict):
        raise ValueError(f'invalid board profile {path}: audio must be an object')
    required = audio.get('required_config')
    if (not isinstance(required, list) or not required
            or any(not isinstance(key, str) or not re.fullmatch(r'[A-Z0-9_]+', key)
                   or key.startswith('CONFIG_')
                   for key in required) or len(set(required)) != len(required)):
        raise ValueError(f'invalid board profile {path}: audio.required_config must contain unique CONFIG names without the CONFIG_ prefix')
    drivers = audio.get('drivers')
    if not isinstance(drivers, list) or not drivers:
        raise ValueError(f'invalid board profile {path}: audio.drivers must be a nonempty list')
    configs = set()
    modules = set()
    for driver in drivers:
        if (not isinstance(driver, dict) or not isinstance(driver.get('config'), str)
                or not re.fullmatch(r'[A-Z0-9_]+', driver['config'])
                or driver['config'].startswith('CONFIG_')
                or not isinstance(driver.get('module'), str)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', driver['module'])):
            raise ValueError(f'invalid board profile {path}: each audio driver requires config and module names')
        name = driver['module'].replace('-', '_')
        if driver['config'] in configs or name in modules:
            raise ValueError(f'invalid board profile {path}: duplicate audio driver')
        configs.add(driver['config'])
        modules.add(name)
    if 'machine_control' in audio:
        control = audio['machine_control']
        if (not isinstance(control, dict)
                or not isinstance(control.get('config'), str)
                or not isinstance(control.get('module'), str)
                or not isinstance(control.get('name'), str)
                or not re.fullmatch(r'[ -~]{1,43}', control['name'])
                or control['name'] != control['name'].strip()
                or not any(control['config'] == driver['config']
                           and control['module'].replace('-', '_') == driver['module'].replace('-', '_')
                           for driver in drivers)):
            raise ValueError(f'invalid board profile {path}: audio.machine_control requires a matching audio driver config/module and a printable ASCII control name (1..43 characters)')
    return audio


def read_kernel_requirements(path):
    """Read the board runtime's kernel requirements; absent means none."""
    try:
        profile = json.loads(Path(path).read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f'cannot read board profile {path}: {error}') from error
    kernel = profile.get('kernel') if isinstance(profile, dict) else None
    if kernel is None:
        return None
    if not isinstance(kernel, dict):
        raise ValueError(f'invalid board profile {path}: kernel must be an object')

    def names(key):
        value = kernel.get(key, [])
        if (not isinstance(value, list)
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Z0-9_]+', name)
                                or name.startswith('CONFIG_') for name in value)
                or len(set(value)) != len(value)):
            raise ValueError(f'invalid board profile {path}: kernel.{key} must contain unique CONFIG names without the CONFIG_ prefix')
        return value

    optional = kernel.get('optional_config', [])
    if (not isinstance(optional, list)
            or any(not isinstance(entry, dict) or not isinstance(entry.get('config'), str)
                            or not re.fullmatch(r'[A-Z0-9_]+', entry['config'])
                            or not isinstance(entry.get('enables'), str) or not entry['enables'].strip()
                            for entry in optional)):
        raise ValueError(f'invalid board profile {path}: kernel.optional_config entries need config and enables')
    return {'required_config': names('required_config'),
            'forbidden_config': names('forbidden_config'), 'optional_config': optional}


def check_kernel(requirements, values):
    """Fail on what the board services cannot work without; report the rest."""
    missing = [f'CONFIG_{key}' for key in requirements['required_config']
            if values.get(key) not in ('y', 'm')]
    if missing:
        raise ValueError('T-Display-K230 board runtime needs kernel support that is missing: '
                    + ', '.join(missing) + '; rebuild the kernel, DTB, modules and boot template together')
    forbidden = [f'CONFIG_{key}' for key in requirements['forbidden_config']
                if values.get(key) in ('y', 'm')]
    if forbidden:
        raise ValueError('T-Display-K230 board runtime cannot work with: ' + ', '.join(forbidden)
                    + '; rebuild the kernel, DTB, modules and boot template together')
    optional = [{'config': entry['config'], 'enables': entry['enables'],
                    'state': values.get(entry['config'], 'n')}
                for entry in requirements['optional_config']]
    return {'required_config': {key: values[key] for key in requirements['required_config']},
            'optional_config': optional,
            'unavailable': [entry['enables'] for entry in optional if entry['state'] == 'n']}


def check_audio(requirements, values, version_dir, module_names):
    keys = dict.fromkeys(requirements['required_config'] +
                         [driver['config'] for driver in requirements['drivers']])
    missing = [f'CONFIG_{key}' for key in keys if values.get(key) not in ('y', 'm')]
    if missing:
        raise ValueError('T-Display-K230 audio kernel support missing: ' + ', '.join(missing)
                         + '; enable these as y or m and rebuild the kernel, DTB, modules and boot template together')
    builtin_names = set()
    if any(values[driver['config']] == 'y' for driver in requirements['drivers']):
        builtin = version_dir / 'modules.builtin'
        if not builtin.is_file():
            raise ValueError(f'T-Display-K230 built-in audio cannot be verified: missing {builtin}')
        builtin_names = {Path(line.strip()).name[:-3].replace('-', '_')
                         for line in builtin.read_text().splitlines() if line.strip().endswith('.ko')}
    drivers = []
    for driver in requirements['drivers']:
        mode = values[driver['config']]
        name = driver['module'].replace('-', '_')
        if mode == 'y' and name not in builtin_names:
            raise ValueError(f'T-Display-K230 built-in audio driver missing from modules.builtin: {driver["module"]} (CONFIG_{driver["config"]}=y)')
        if mode == 'm' and name not in module_names:
            raise ValueError(f'T-Display-K230 audio module missing: {driver["module"]} (CONFIG_{driver["config"]}=m)')
        drivers.append({'config': driver['config'], 'module': driver['module'], 'mode': mode,
                        'evidence': 'modules.builtin' if mode == 'y' else 'ELF/vermagic-checked module'})
    return {'config': {key: values[key] for key in keys}, 'drivers': drivers,
            'dtb_verified': False, 'hardware_verified': False}


def check_audio_control(control, values, bsp, module_paths):
    """Check a control-name marker, not ABI, DTB or template consistency."""
    mode = values[control['config']]
    rebuild = '; rebuild the kernel, DTB, modules and boot template together with the T-Display-K230 audio patches'
    if mode == 'y':
        path = bsp / 'images/Image'
        if not path.is_file():
            raise ValueError(f'T-Display-K230 audio route control cannot be verified: missing {path}' + rebuild)
        payload = path.read_bytes()
    else:
        paths = module_paths[control['module'].replace('-', '_')]
        if len(paths) != 1:
            raise ValueError(f'T-Display-K230 audio route control cannot be verified: multiple copies of module {control["module"]}' + rebuild)
        path = paths[0]
        # These files have already passed the ELF/vermagic checks. Inspect
        # the decompressed contents too, including .gz/.xz/.zst payloads.
        payload = module_bytes(path)
    marker = control['name'].encode('ascii') + b'\0'
    if marker not in payload:
        raise ValueError(f'T-Display-K230 audio route control marker missing from {path}: {control["name"]}' + rebuild)
    return {'name': control['name'], 'mode': mode, 'path': str(path.relative_to(bsp)),
            'check': 'NUL-terminated control name in binary'}


def read_keyboard_requirements(path):
    profile = json.loads(Path(path).read_text())
    if 'keyboard' not in profile:
        return None
    keyboard = profile['keyboard']
    invalid = f'invalid board profile {path}: keyboard'
    if not isinstance(keyboard, dict):
        raise ValueError(invalid + ' must be an object')
    exact = {'config': 'KEYBOARD_TCA8418', 'module': 'tca8418_keypad',
             'compatible': 'lilygo,tca8418-keypad'}
    if any(keyboard.get(key) != value for key, value in exact.items()):
        raise ValueError(invalid + ' requires the LILYGO TCA8418 config, module and compatible')
    for key in ('node_path', 'interrupt_parent_path', 'reset_controller_path'):
        value = keyboard.get(key)
        if (not isinstance(value, str) or not value.startswith('/')
                or not re.fullmatch(r'/[A-Za-z0-9,._@+/-]+', value)
                or any(part in ('', '.', '..') for part in value.split('/')[1:])):
            raise ValueError(invalid + f'.{key} must be an absolute device-tree node path')
    bus = keyboard.get('bus')
    if (not isinstance(bus, dict) or bus.get('node_path') != '/keyboard-i2c'
            or bus.get('compatible') != 'i2c-gpio'
            or bus.get('scl_gpio') != 14 or bus.get('sda_gpio') != 15
            or bus.get('gpio_controller_path') != '/soc/gpio@9140c000/gpio-port@0'
            or bus.get('pinctrl_path') != '/soc/iomux@91105000/keyboard_i2c_gpio_pins'):
        raise ValueError(invalid + '.bus must describe the GPIO-I2C keyboard bus on GPIO1 offsets 14/15')
    if (not isinstance(keyboard.get('dtb'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.dtb', keyboard['dtb'])):
        raise ValueError(invalid + '.dtb must name the boot partition DTB')
    marker = keyboard.get('image_marker')
    if not isinstance(marker, str) or not re.fullmatch(r'[ -~]{1,120}', marker):
        raise ValueError(invalid + '.image_marker must be printable ASCII')
    for key, minimum, maximum in (('address', 1, 127), ('rows', 1, 8), ('columns', 1, 10)):
        value = keyboard.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(invalid + f'.{key} is out of range')
    for key in ('interrupts', 'reset_gpios', 'keymap', 'fn_keymap', 'fn_shift_keymap'):
        value = keyboard.get(key)
        if (not isinstance(value, list) or not value
                or any(type(cell) is not int or not 0 <= cell <= 0xffffffff for cell in value)
                or (key in ('interrupts', 'reset_gpios') and len(value) != 2)):
            raise ValueError(invalid + f'.{key} must contain unsigned 32-bit cells')
    return keyboard


def read_peripheral_requirements(path):
    profile = json.loads(Path(path).read_text())
    if 'peripherals' not in profile:
        return None
    requirements = profile['peripherals']
    invalid = f'invalid board profile {path}: peripherals'
    if not isinstance(requirements, dict):
        raise ValueError(invalid + ' must be an object')
    bus = requirements.get('bus')
    if (not isinstance(bus, dict) or bus.get('node_path') != '/keyboard-i2c'
            or bus.get('compatible') != 'i2c-gpio'
            or bus.get('scl_gpio') != 14 or bus.get('sda_gpio') != 15
            or bus.get('gpio_controller_path') != '/soc/gpio@9140c000/gpio-port@0'
            or bus.get('pinctrl_path') != '/soc/iomux@91105000/keyboard_i2c_gpio_pins'):
        raise ValueError(invalid + '.bus must describe the GPIO-I2C keyboard bus on GPIO1 offsets 14/15')

    def node_path(value):
        return (isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9,._@+/-]+', value)
                and not any(part in ('', '.', '..') for part in value.split('/')[1:]))

    def cells(value, length=None):
        return (isinstance(value, list) and value and (length is None or len(value) == length)
                and all(type(cell) is int and 0 <= cell <= 0xffffffff for cell in value))

    if (not isinstance(requirements.get('dtb'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.dtb', requirements['dtb'])):
        raise ValueError(invalid + '.dtb must name the boot partition DTB')
    drivers = requirements.get('drivers')
    if not isinstance(drivers, list) or not drivers:
        raise ValueError(invalid + '.drivers must be a nonempty list')
    seen_configs, seen_modules = set(), set()
    for driver in drivers:
        if (not isinstance(driver, dict) or not isinstance(driver.get('config'), str)
                or not re.fullmatch(r'[A-Z0-9_]+', driver['config'])
                or driver['config'].startswith('CONFIG_')
                or not isinstance(driver.get('module'), str)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', driver['module'])
                or not isinstance(driver.get('image_markers'), list) or not driver['image_markers']
                or any(not isinstance(marker, str) or not re.fullmatch(r'[ -~]{1,120}', marker)
                       for marker in driver['image_markers'])):
            raise ValueError(invalid + '.drivers entries need config, module and printable image_markers')
        name = driver['module'].replace('-', '_')
        if driver['config'] in seen_configs or name in seen_modules:
            raise ValueError(invalid + '.drivers contains duplicate config/module')
        seen_configs.add(driver['config'])
        seen_modules.add(name)
    required_drivers = {'INPUT_DRV260X_HAPTICS': 'drv260x', 'GPIO_PCA953X': 'gpio_pca953x',
                        'LEDS_GPIO': 'leds_gpio'}
    actual_drivers = {driver['config']: driver['module'].replace('-', '_') for driver in drivers}
    if any(actual_drivers.get(key) != name for key, name in required_drivers.items()):
        raise ValueError(invalid + '.drivers must include DRV260x, PCA953x and gpio-leds')
    for key, compatible in (('haptics', 'ti,drv2605'), ('expander', 'nxp,pca9555')):
        device = requirements.get(key)
        if (not isinstance(device, dict) or not node_path(device.get('node_path'))
                or device.get('compatible') != compatible
                or type(device.get('address')) is not int or not 0 < device['address'] < 128):
            raise ValueError(invalid + f'.{key} requires the board node, compatible and I2C address')
        unit = 'haptics' if key == 'haptics' else 'gpio'
        address = device['address']
        if ((key == 'haptics' and address != 0x5a)
                or (key == 'expander' and not 0x20 <= address <= 0x27)
                or device['node_path'] != f'/keyboard-i2c/{unit}@{address:x}'):
            raise ValueError(invalid + f'.{key} must use the keyboard GPIO-I2C bus and matching address')
    haptics = requirements['haptics']
    properties = haptics.get('properties')
    if (not isinstance(properties, dict)
            or properties != {'mode': [2], 'library-sel': [1], 'lilygo,erm-open-loop': True}
            or not cells(properties['mode'], 1) or not cells(properties['library-sel'], 1)
            or properties['lilygo,erm-open-loop'] is not True):
        raise ValueError(invalid + '.haptics.properties must preserve the vendor ERM open-loop mode/library')
    absent = haptics.get('absent_properties')
    expected_absent = {'enable-gpio', 'enable-gpios', 'vbat-supply', 'vib-rated-mv', 'vib-overdrive-mv'}
    if (not isinstance(absent, list) or any(not isinstance(name, str) for name in absent)
            or set(absent) != expected_absent or len(absent) != len(expected_absent)):
        raise ValueError(invalid + '.haptics.absent_properties must exclude unverified enable/supply/motor voltages')
    feedback = haptics.get('feedback_node_paths')
    if (not isinstance(feedback, list) or not feedback or any(not node_path(value) for value in feedback)
            or len(feedback) != len(set(feedback))):
        raise ValueError(invalid + '.haptics.feedback_node_paths must name the keyboard feedback consumers')
    if haptics.get('supply') is not None or haptics.get('enable_gpio') is not None:
        raise ValueError(invalid + '.haptics cannot invent an unverified regulator or enable GPIO')
    expander = requirements['expander']
    line_names = expander.get('line_names')
    if (type(expander.get('ngpios')) is not int or expander['ngpios'] != 16
            or not isinstance(line_names, list) or len(line_names) != 16
            or any(not isinstance(name, str) or not re.fullmatch(r'[ -~]{0,80}', name) for name in line_names)):
        raise ValueError(invalid + '.expander must describe 16 GPIO lines')
    leds = expander.get('leds')
    if (not isinstance(leds, dict) or not node_path(leds.get('node_path'))
            or leds.get('compatible') != 'gpio-leds'
            or not isinstance(leds.get('children'), list) or len(leds['children']) != 3):
        raise ValueError(invalid + '.expander.leds must describe the three keyboard LEDs')
    lines, paths, labels = set(), set(), set()
    for led in leds['children']:
        if (not isinstance(led, dict) or not node_path(led.get('node_path'))
                or led['node_path'].rsplit('/', 1)[0] != leds['node_path']
                or type(led.get('line')) is not int or led['line'] not in (3, 4, 5)
                or led.get('active_low') is not True or led.get('default_state') != 'off'
                or not isinstance(led.get('label'), str) or not re.fullmatch(r'[ -~]{1,80}', led['label'])):
            raise ValueError(invalid + '.expander.leds children must use active-low P03/P04/P05, initially off')
        if led['line'] in lines or led['node_path'] in paths or led['label'] in labels:
            raise ValueError(invalid + '.expander.leds contains duplicate lines/paths/labels')
        lines.add(led['line'])
        paths.add(led['node_path'])
        labels.add(led['label'])
    return requirements


def boot_keyboard_payload(template, dtb):
    """Extract the files actually shipped in p1 without mounting or running them."""
    debugfs = shutil.which('debugfs')
    if not debugfs:
        raise ValueError('debugfs is required to verify the keyboard in the boot template; install e2fsprogs')
    layout = sibling('image').inspect(template)
    boot = next(part for part in layout['partitions'] if part['number'] == 1)
    with tempfile.TemporaryDirectory(prefix='canaan-keyboard-check-') as directory:
        directory = Path(directory)
        boot_image = directory / 'boot.ext4'
        with Path(template).open('rb') as source, boot_image.open('wb') as target:
            source.seek(boot['start_sector'] * 512)
            remaining = boot['sectors'] * 512
            while remaining:
                block = source.read(min(remaining, 4 * 1024 * 1024))
                if not block:
                    raise ValueError('truncated boot partition in BSP template')
                target.write(block)
                remaining -= len(block)
        payload = {}
        for source_name, destination in (('Image', 'Image'), (dtb, 'keyboard.dtb')):
            result = subprocess.run([debugfs, '-R', f'dump /{source_name} {destination}', str(boot_image)],
                                    cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            extracted = directory / destination
            if result.returncode or not extracted.is_file() or not extracted.stat().st_size:
                raise ValueError(f'BSP boot template does not contain /{source_name}')
            payload[source_name] = extracted.read_bytes()
        # U-Boot loads /k.dtb. The SDK normally makes it a short symlink to
        # the LCD DTB; validating only an unused adjacent DTB is insufficient.
        result = subprocess.run([debugfs, '-R', 'stat /k.dtb', str(boot_image)],
                                cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={**os.environ, 'LC_ALL': 'C'})
        if 'Type: symlink' in result.stdout:
            link = re.search(r'^Fast link dest: "([^"]+)"$', result.stdout, re.MULTILINE)
            if not link or link[1] not in (dtb, '/' + dtb):
                raise ValueError('BSP boot /k.dtb does not select the required keyboard DTB')
        elif 'Type: regular' in result.stdout:
            dumped = subprocess.run([debugfs, '-R', 'dump /k.dtb selected.dtb', str(boot_image)],
                                    cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selected = directory / 'selected.dtb'
            if dumped.returncode or not selected.is_file() or selected.read_bytes() != payload[dtb]:
                raise ValueError('BSP boot /k.dtb differs from the required keyboard DTB')
        else:
            raise ValueError('BSP boot template has no usable /k.dtb')
        return payload


def check_expansion_i2c_bus(requirements, tree, phandles):
    """Verify the GPIO-bitbanged keyboard-base bus and preserve camera I2C4."""
    bus_req = requirements['bus']
    bus_path = bus_req['node_path']
    if bus_path not in tree:
        raise ValueError(f'keyboard expansion I2C bus missing from boot DTB: {bus_path}')
    bus = tree[bus_path]
    if 'i2c-gpio' not in FDT.strings(bus.get('compatible', b'')):
        raise ValueError('keyboard expansion bus must use the i2c-gpio driver')
    if FDT.cells(bus.get('#address-cells', b'')) != [1] or FDT.cells(bus.get('#size-cells', b'')) != [0]:
        raise ValueError('keyboard expansion i2c-gpio bus has invalid address/size cells')
    for prop, line in (('scl-gpios', bus_req['scl_gpio']), ('sda-gpios', bus_req['sda_gpio'])):
        cells = FDT.cells(bus.get(prop, b''))
        if (len(cells) != 3 or phandles.get(cells[0]) != bus_req['gpio_controller_path']
                or cells[1] != line or not (cells[2] & 0x2)):
            raise ValueError(f'keyboard expansion bus {prop} must use GPIO1 line {line} as open-drain')
    pinctrl = FDT.cells(bus.get('pinctrl-0', b''))
    if len(pinctrl) != 1 or phandles.get(pinctrl[0]) != bus_req['pinctrl_path']:
        raise ValueError('keyboard expansion bus must select the GPIO46/47 pinctrl group')
    pins = tree[bus_req['pinctrl_path']]
    for child, expected_pin, expected_function in (
            ('scl', 'io46', 'alt0'), ('sda', 'io47', 'alt0')):
        path = bus_req['pinctrl_path'] + '/' + child
        if path not in tree:
            raise ValueError(f'keyboard expansion pinctrl missing {child} state')
        props = tree[path]
        if FDT.strings(props.get('pins', b'')) != [expected_pin] or \
                FDT.strings(props.get('function', b'')) != [expected_function]:
            raise ValueError(f'keyboard expansion pinctrl {child} does not select GPIO {expected_pin}')
    camera_path = '/soc/i2c@91409000'
    if camera_path not in tree:
        raise ValueError('hardware camera I2C4 controller missing from boot DTB')
    camera_pinctrl = FDT.cells(tree[camera_path].get('pinctrl-0', b''))
    camera_nodes = [phandles.get(value) for value in camera_pinctrl]
    if '/soc/iomux@91105000/cam_i2c4_pins' not in camera_nodes or \
            '/soc/iomux@91105000/aht20_i2c4_pins' in camera_nodes:
        raise ValueError('camera I2C4 must select only cam_i2c4_pins; expansion IO46/47 belongs to i2c-gpio')
    for node in ('/soc/i2c@91409000/gc2093@37', '/soc/i2c@91409000/dw9714@0c'):
        if node not in tree or ('status' in tree[node] and FDT.strings(tree[node]['status']) not in (['okay'], ['ok'])):
            raise ValueError(f'camera I2C4 node missing or disabled: {node}')


def check_keyboard_dtb(requirements, data, tree=None):
    tree = FDT.parse(data) if tree is None else tree
    path = requirements['node_path']
    if path not in tree:
        raise ValueError(f'TCA8418 keyboard node missing from boot DTB: {path}')
    def enabled(current):
        if current not in tree:
            raise ValueError(f'TCA8418 keyboard DTB controller missing: {current}')
        while True:
            properties = tree[current]
            if 'status' in properties and FDT.strings(properties['status']) not in (['okay'], ['ok']):
                raise ValueError(f'TCA8418 keyboard node or ancestor is disabled: {current}')
            if current == '/':
                break
            current = current.rsplit('/', 1)[0] or '/'

    enabled(path)
    properties = tree[path]
    if requirements['compatible'] not in FDT.strings(properties.get('compatible', b'')):
        raise ValueError('TCA8418 keyboard DTB compatible does not select the LILYGO driver')
    bus = tree[path.rsplit('/', 1)[0]]
    if FDT.cells(bus.get('#address-cells', b'')) != [1] or FDT.cells(bus.get('#size-cells', b'')) != [0]:
        raise ValueError('TCA8418 keyboard DTB parent does not encode an I2C address')
    expected = {'reg': [requirements['address']], 'keypad,num-rows': [requirements['rows']],
                'keypad,num-columns': [requirements['columns']], 'interrupts': requirements['interrupts'],
                'linux,keymap': requirements['keymap'], 'linux,fn-keymap': requirements['fn_keymap'],
                'lilygo,fn-shift-keymap': requirements['fn_shift_keymap']}
    for name, value in expected.items():
        if FDT.cells(properties.get(name, b'')) != value:
            raise ValueError(f'TCA8418 keyboard DTB {name} does not match the board wiring/keymap')
    phandles = {}
    for node, props in tree.items():
        candidates = [FDT.cells(props[name]) for name in ('phandle', 'linux,phandle') if name in props]
        if not candidates:
            continue
        if any(len(value) != 1 or not 0 < value[0] < 0xffffffff for value in candidates):
            raise ValueError('invalid DTB phandle')
        if len(candidates) == 2 and candidates[0] != candidates[1]:
            raise ValueError('conflicting DTB phandle aliases')
        value = candidates[0][0]
        if value in phandles:
            raise ValueError('duplicate DTB phandle')
        phandles[value] = node
    check_expansion_i2c_bus(requirements, tree, phandles)
    parent = FDT.cells(properties.get('interrupt-parent', b''))
    if len(parent) != 1 or phandles.get(parent[0]) != requirements['interrupt_parent_path']:
        raise ValueError('TCA8418 keyboard DTB interrupt-parent selects the wrong GPIO controller')
    reset = FDT.cells(properties.get('reset-gpios', b''))
    if (len(reset) != 3 or phandles.get(reset[0]) != requirements['reset_controller_path']
            or reset[1:] != requirements['reset_gpios']):
        raise ValueError('TCA8418 keyboard DTB reset-gpios does not match the board wiring')
    enabled(requirements['interrupt_parent_path'])
    enabled(requirements['reset_controller_path'])
    controller = tree[requirements['interrupt_parent_path']]
    if 'interrupt-controller' not in controller or FDT.cells(controller.get('#interrupt-cells', b'')) != [2]:
        raise ValueError('TCA8418 keyboard DTB GPIO controller has no usable interrupt interface')
    controller = tree[requirements['reset_controller_path']]
    if 'gpio-controller' not in controller or FDT.cells(controller.get('#gpio-cells', b'')) != [2]:
        raise ValueError('TCA8418 keyboard DTB reset controller has no usable GPIO interface')
    return {'node': path, 'sha256': hashlib.sha256(data).hexdigest(), 'wiring_and_keymap_verified': True}


def checked_boot_payload(bsp, values, template, dtb, cache=None):
    """Verify the selected kernel/config once, sharing extraction across devices."""
    template = Path(template) if template is not None else bsp / 'images/sysimage-sdcard.img'
    key = (str(template), dtb)
    if cache is not None and key in cache:
        return cache[key]
    payload = boot_keyboard_payload(template, dtb)
    image = payload['Image']
    standalone = bsp / 'images/Image'
    if not standalone.is_file() or hashlib.sha256(standalone.read_bytes()).digest() != hashlib.sha256(image).digest():
        raise ValueError('BSP images/Image differs from the kernel in the boot template')
    start = image.find(b'IKCFG_ST')
    end = image.find(b'IKCFG_ED', start + 8)
    if start < 0 or end < 0:
        raise ValueError('BSP boot kernel lacks embedded IKCONFIG needed to verify its configuration')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(image[start + 8:end])) as compressed:
            embedded_bytes = compressed.read(8 * 1024 * 1024 + 1)
        if len(embedded_bytes) > 8 * 1024 * 1024:
            raise ValueError('BSP boot kernel embedded IKCONFIG exceeds the 8 MiB limit')
        embedded = embedded_bytes.decode('ascii')
    except (OSError, EOFError, UnicodeError, zlib.error) as error:
        raise ValueError('BSP boot kernel has invalid embedded IKCONFIG') from error
    actual_values = dict(re.findall(r'^CONFIG_([A-Za-z0-9_]+)=(.*)$', embedded, re.MULTILINE))
    if actual_values != values:
        raise ValueError('BSP kernel .config does not match the configuration embedded in the boot Image')
    result = {'payload': payload, 'template': str(template),
              'image_sha256': hashlib.sha256(image).hexdigest(), 'tree': FDT.parse(payload[dtb])}
    if cache is not None:
        cache[key] = result
    return result


def check_keyboard(requirements, values, bsp, version_dir, module_paths, template=None, boot_cache=None):
    mode = values.get(requirements['config'])
    if mode not in ('y', 'm'):
        raise ValueError('TCA8418 keyboard support missing: enable CONFIG_KEYBOARD_TCA8418 and rebuild the BSP')
    name = requirements['module']
    if mode == 'y':
        builtin = version_dir / 'modules.builtin'
        if (not builtin.is_file() or not any(Path(line).name == name + '.ko'
                                            for line in builtin.read_text().splitlines())):
            raise ValueError('TCA8418 built-in driver missing from modules.builtin')
    else:
        candidates = module_paths.get(name, [])
        if len(candidates) != 1:
            raise ValueError('TCA8418 requires exactly one matching keyboard module')
        module_payload = module_bytes(candidates[0])
        if (requirements['compatible'].encode() + b'\0' not in module_payload
                or requirements['image_marker'].encode() not in module_payload):
            raise ValueError('TCA8418 module does not contain the LILYGO FN-layer driver')
    boot = checked_boot_payload(bsp, values, template, requirements['dtb'], boot_cache)
    if mode == 'y' and requirements['image_marker'].encode() not in boot['payload']['Image']:
        raise ValueError('BSP boot kernel is missing the LILYGO TCA8418 FN-layer driver marker')
    evidence = check_keyboard_dtb(requirements, boot['payload'][requirements['dtb']], boot['tree'])
    return {'config': requirements['config'], 'module': name, 'mode': mode,
            'template': boot['template'], 'boot_dtb': requirements['dtb'], 'dtb': evidence,
            'boot_image_sha256': boot['image_sha256'], 'hardware_verified': False}


def check_peripheral_dtb(requirements, data, tree=None):
    """Check the actual boot DTB's motor, supply, expander and LED wiring."""
    tree = FDT.parse(data) if tree is None else tree

    def enabled(path, description):
        current = path
        if current not in tree:
            raise ValueError(f'{description} node missing from boot DTB: {path}')
        while True:
            properties = tree[current]
            if 'status' in properties and FDT.strings(properties['status']) not in (['okay'], ['ok']):
                raise ValueError(f'{description} node or ancestor is disabled: {current}')
            if current == '/':
                return tree[path]
            current = current.rsplit('/', 1)[0] or '/'

    def expect_cells(properties, name, expected, description):
        if FDT.cells(properties.get(name, b'')) != expected:
            raise ValueError(f'{description} DTB {name} does not match the board wiring/configuration')

    def i2c(device, description):
        properties = enabled(device['node_path'], description)
        if device['compatible'] not in FDT.strings(properties.get('compatible', b'')):
            raise ValueError(f'{description} DTB compatible does not select the required driver')
        expect_cells(properties, 'reg', [device['address']], description)
        bus = tree[device['node_path'].rsplit('/', 1)[0]]
        expect_cells(bus, '#address-cells', [1], description + ' I2C parent')
        expect_cells(bus, '#size-cells', [0], description + ' I2C parent')
        return properties

    phandles = {}
    for path, properties in tree.items():
        candidates = [FDT.cells(properties[name]) for name in ('phandle', 'linux,phandle') if name in properties]
        if not candidates:
            continue
        if (any(len(value) != 1 or not 0 < value[0] < 0xffffffff for value in candidates)
                or (len(candidates) == 2 and candidates[0] != candidates[1])):
            raise ValueError('invalid or conflicting peripheral DTB phandle')
        if candidates[0][0] in phandles:
            raise ValueError('duplicate peripheral DTB phandle')
        phandles[candidates[0][0]] = path

    check_expansion_i2c_bus(requirements, tree, phandles)

    haptics = requirements['haptics']
    motor = i2c(haptics, 'DRV2605')
    for name, expected in haptics['properties'].items():
        if expected is True:
            if motor.get(name) != b'':
                raise ValueError(f'DRV2605 DTB must enable the boolean {name} mode')
        else:
            expect_cells(motor, name, expected, 'DRV2605')
    for name in haptics['absent_properties']:
        if name in motor:
            raise ValueError(f'DRV2605 DTB must not guess unverified hardware property {name}')
    for path in haptics['feedback_node_paths']:
        consumer = enabled(path, 'DRV2605 keyboard feedback consumer')
        if consumer.get('lilygo,haptic-feedback') != b'':
            raise ValueError(f'DRV2605 keyboard feedback DTB must explicitly enable the boolean input flag: {path}')

    expander = requirements['expander']
    gpio = i2c(expander, 'XL9555')
    if 'gpio-controller' not in gpio:
        raise ValueError('XL9555 DTB is not configured as a GPIO provider')
    expect_cells(gpio, '#gpio-cells', [2], 'XL9555')
    # PCA9555's match data defines 16 GPIOs. Also reject a conflicting optional
    # ngpios limit and require all 16 line names, so its consumers use real pins.
    if 'ngpios' in gpio:
        expect_cells(gpio, 'ngpios', [expander['ngpios']], 'XL9555')
    if FDT.strings(gpio.get('gpio-line-names', b'')) != expander['line_names']:
        raise ValueError('XL9555 DTB does not describe all 16 board GPIO line names')
    if any('gpio-hog' in properties for path, properties in tree.items()
           if path.startswith(expander['node_path'] + '/')):
        raise ValueError('XL9555 DTB must leave non-LED lines available instead of claiming GPIO hogs')
    leds = expander['leds']
    led_parent = enabled(leds['node_path'], 'XL9555 LEDs')
    if leds['compatible'] not in FDT.strings(led_parent.get('compatible', b'')):
        raise ValueError('XL9555 LEDs DTB does not select gpio-leds')
    expected_children = {led['node_path'] for led in leds['children']}
    actual_children = {path for path in tree if path.rsplit('/', 1)[0] == leds['node_path']}
    if actual_children != expected_children:
        raise ValueError('XL9555 LEDs DTB children differ from the three board LEDs')
    for led in leds['children']:
        properties = enabled(led['node_path'], 'XL9555 LED')
        reference = FDT.cells(properties.get('gpios', b''))
        if (len(reference) != 3 or phandles.get(reference[0]) != expander['node_path']
                or reference[1:] != [led['line'], 1 if led['active_low'] else 0]):
            raise ValueError(f'XL9555 LED DTB gpios has the wrong provider, line or polarity: {led["node_path"]}')
        if FDT.strings(properties.get('label', b'')) != [led['label']]:
            raise ValueError(f'XL9555 LED DTB label differs from the board mapping: {led["node_path"]}')
        if FDT.strings(properties.get('default-state', b'')) != [led['default_state']]:
            raise ValueError(f'XL9555 LED DTB must initially be off: {led["node_path"]}')
    return {'haptics_node': haptics['node_path'], 'expander_node': expander['node_path'],
            'led_nodes': sorted(expected_children), 'sha256': hashlib.sha256(data).hexdigest(),
            'wiring_verified': True}


def check_peripherals(requirements, values, bsp, version_dir, module_paths, template=None, boot_cache=None):
    drivers = []
    builtin = version_dir / 'modules.builtin'
    builtin_names = ({Path(line).name[:-3].replace('-', '_') for line in builtin.read_text().splitlines()
                      if line.endswith('.ko')} if builtin.is_file() else set())
    for driver in requirements['drivers']:
        mode = values.get(driver['config'])
        name = driver['module'].replace('-', '_')
        if mode not in ('y', 'm'):
            raise ValueError(f'board peripheral driver missing: CONFIG_{driver["config"]}; rebuild the BSP')
        if mode == 'y' and name not in builtin_names:
            raise ValueError(f'board peripheral built-in driver missing from modules.builtin: {driver["module"]}')
        if mode == 'm' and len(module_paths.get(name, [])) != 1:
            raise ValueError(f'board peripheral requires exactly one matching module: {driver["module"]}')
        drivers.append({'config': driver['config'], 'module': driver['module'], 'mode': mode})
    boot = checked_boot_payload(bsp, values, template, requirements['dtb'], boot_cache)
    for driver, evidence in zip(requirements['drivers'], drivers):
        payload = (boot['payload']['Image'] if evidence['mode'] == 'y'
                   else module_bytes(module_paths[driver['module'].replace('-', '_')][0]))
        missing = [marker for marker in driver['image_markers'] if marker.encode('ascii') not in payload]
        if missing:
            raise ValueError(f'board peripheral driver marker missing from {driver["module"]}: ' + ', '.join(missing))
    evidence = check_peripheral_dtb(requirements, boot['payload'][requirements['dtb']], boot['tree'])
    return {'drivers': drivers, 'template': boot['template'], 'boot_dtb': requirements['dtb'],
            'dtb': evidence, 'boot_image_sha256': boot['image_sha256'], 'hardware_verified': False}


def check(bsp, config, require_camera_userspace=False, board_profile=None, template=None):
    target = bsp / 'target'
    if not target.is_dir():
        raise ValueError(f'missing BSP target directory: {target}; build the matching SDK first')
    values = parse_config(config)
    audio_requirements = read_board_profile(Path(board_profile)) if board_profile is not None else None
    kernel_requirements = read_kernel_requirements(board_profile) if board_profile is not None else None
    keyboard_requirements = read_keyboard_requirements(board_profile) if board_profile is not None else None
    peripheral_requirements = read_peripheral_requirements(board_profile) if board_profile is not None else None
    if (keyboard_requirements is not None and peripheral_requirements is not None
            and keyboard_requirements['dtb'] != peripheral_requirements['dtb']):
        raise ValueError('board keyboard and peripherals must use the same boot DTB')
    modules = target / 'lib/modules'
    if not modules.is_dir():
        raise ValueError(f'missing vendor kernel modules: {modules}')
    versions = sorted(path for path in modules.iterdir() if path.is_dir())
    if len(versions) != 1:
        raise ValueError('BSP target must contain exactly one matching kernel release under lib/modules')
    version_dir = versions[0]
    version = version_dir.name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', version):
        raise ValueError(f'invalid kernel release directory: {version}')
    # A completed kernel tree provides an unambiguous release. An extracted
    # embedded configuration may not; module vermagic is still checked below.
    release_file = config.parent / 'include/config/kernel.release'
    if release_file.is_file() and release_file.read_text().strip() != version:
        raise ValueError('built kernel release does not match target/lib/modules')
    files = [p for p in version_dir.rglob('*') if p.is_file() and p.name.endswith(MODULE_SUFFIXES)]
    if not files:
        raise ValueError(f'no kernel modules found for {version}')
    names = set()
    module_paths = {}
    for path in files:
        data = module_bytes(path)
        if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 243:
            raise ValueError(f'not an ELF64 little-endian RISC-V kernel module: {path}')
        match = re.search(rb'(?:\x00|^)vermagic=([^ \x00]+)', data)
        if not match or match[1].decode('ascii', errors='replace') != version:
            raise ValueError(f'kernel module vermagic does not match {version}: {path}')
        name = path.name.split('.ko', 1)[0].replace('-', '_')
        names.add(name)
        module_paths.setdefault(name, []).append(path)
    absent = [name for name in REQUIRED_MODULES if name not in names]
    if absent:
        raise ValueError('T-Display-K230 board modules missing: ' + ', '.join(absent))
    if require_camera_userspace:
        camera_absent = [name for name in CAMERA_MODULES if name not in names]
        if camera_absent:
            raise ValueError('T-Display-K230 camera modules missing: ' + ', '.join(camera_absent))
        camera_files = (
            target / 'usr/bin/isp_media_server',
            target / 'usr/bin/media-ctl',
            target / 'usr/bin/v4l2-sysfs-path',
            target / 'usr/lib/libvvcam.so',
            target / 'usr/lib/libmxml.so.1.6',
        )
        missing_files = [str(path.relative_to(target)) for path in camera_files if not path.is_file()]
        if missing_files:
            raise ValueError('T-Display-K230 camera userspace missing: ' + ', '.join(missing_files))
        tuning = target / 'etc/vvcam'
        if not tuning.is_dir() or not any(tuning.iterdir()):
            raise ValueError('T-Display-K230 camera tuning data missing: target/etc/vvcam')
    firmware = target / 'lib/firmware'
    if not firmware.is_dir():
        raise ValueError(f'missing BSP firmware directory: {firmware}')
    result = {'kernel_release': version, 'module_count': len(files),
              'kernel_config': str(config), 'target': str(target),
              'required_modules': list(REQUIRED_MODULES)}
    if kernel_requirements is not None:
        result['kernel'] = check_kernel(kernel_requirements, values)
    if audio_requirements is not None:
        result['board_profile'] = str(board_profile)
        result['audio'] = check_audio(audio_requirements, values, version_dir, names)
        if 'machine_control' in audio_requirements:
            result['audio']['control_evidence'] = check_audio_control(
                audio_requirements['machine_control'], values, bsp, module_paths)
    boot_cache = {}
    if keyboard_requirements is not None:
        result['keyboard'] = check_keyboard(keyboard_requirements, values, bsp, version_dir, module_paths, template, boot_cache)
    if peripheral_requirements is not None:
        result['peripherals'] = check_peripherals(peripheral_requirements, values, bsp, version_dir, module_paths,
                                                 template, boot_cache)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bsp', required=True, type=Path)
    parser.add_argument('--kernel-config', required=True, type=Path)
    parser.add_argument('--board-profile', type=Path,
                        help='shared board profile JSON; verify its audio kernel configuration and driver payload')
    parser.add_argument('--template', type=Path, help='actual boot template; defaults to BSP images/sysimage-sdcard.img')
    parser.add_argument('--require-camera-userspace', action='store_true',
                        help='require the GC2093 ISP modules, daemon, libraries and tuning data')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = check(args.bsp, args.kernel_config,
                       require_camera_userspace=args.require_camera_userspace,
                       board_profile=args.board_profile, template=args.template)
    except (ValueError, OSError, EOFError, lzma.LZMAError) as error:
        parser.exit(1, f'canaan-gen: {error}\n')
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"BSP checks passed: kernel {result['kernel_release']}, {result['module_count']} RISC-V modules; hardware boot remains unverified")
        if 'audio' in result:
            modes = ', '.join(f'{driver["module"]}={driver["mode"]}' for driver in result['audio']['drivers'])
            print(f'Audio kernel checks passed: {modes}; DTB and hardware behavior remain unverified')
        if 'keyboard' in result:
            print('TCA8418 keyboard driver and boot DTB wiring/keymaps verified; hardware behavior remains unverified')
        if 'peripherals' in result:
            print('DRV2605 haptics, XL9555 GPIO and LEDs verified in boot kernel/DTB; hardware behavior remains unverified')
        for feature in result.get('kernel', dict()).get('unavailable', []):
            print(f'Not provided by this BSP kernel: {feature}')


if __name__ == '__main__':
    main()
