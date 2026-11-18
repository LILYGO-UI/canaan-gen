#!/usr/bin/env python3
"""BSP failures that must stop an image build before rootfs modification."""
import gzip
import importlib.util
import json
import lzma
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('check_bsp', Path(__file__).resolve().parents[1] / 'scripts/check-bsp.py')
BSP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BSP)


def module(release='6.6.36', machine=243):
    data = bytearray(64)
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', data, 18, machine)
    return bytes(data) + b'\0vermagic=' + release.encode() + b' SMP mod_unload riscv\0'


def cells(*values):
    return struct.pack('>' + 'I' * len(values), *values)


def dtb(nodes):
    """Encode an independently supplied property tree, without using the decoder."""
    names = bytearray()
    offsets = {}
    tree = bytearray()

    def string(value):
        data = value.encode() + b'\0'
        return data + bytes((-len(data)) % 4)

    def node(path, name):
        tree.extend(cells(1) + string(name))
        for key, value in nodes[path].items():
            if key not in offsets:
                offsets[key] = len(names)
                names.extend(key.encode() + b'\0')
            tree.extend(cells(3, len(value), offsets[key]) + value + bytes((-len(value)) % 4))
        for child in nodes:
            if child != '/' and (child.rsplit('/', 1)[0] or '/') == path:
                node(child, child.rsplit('/', 1)[1])
        tree.extend(cells(2))

    node('/', '')
    tree.extend(cells(9))
    header = cells(0xd00dfeed, 56 + len(tree) + len(names), 56, 56 + len(tree), 40,
                   17, 16, 0, len(names), len(tree))
    return header + bytes(16) + tree + names


class BspCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'build/linux-test/.config'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(''.join(f'CONFIG_{key}=y\n' for key in BSP.REQUIRED_BUILTIN))
        self.modules = self.root / 'target/lib/modules/6.6.36'
        self.modules.mkdir(parents=True)
        for name in BSP.REQUIRED_MODULES:
            (self.modules / f'{name}.ko').write_bytes(module())
        (self.root / 'target/lib/firmware').mkdir()

    def test_matching_kernel_and_modules(self):
        result = BSP.check(self.root, self.config)
        self.assertEqual(result['kernel_release'], '6.6.36')
        self.assertEqual(result['module_count'], len(BSP.REQUIRED_MODULES))
        self.assertEqual(result['required_modules'], ['8189fs', '8723ds'])

    def test_missing_configuration(self):
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, 'configuration is missing'):
            BSP.check(self.root, self.config)

    def test_systemd_support_cannot_be_module_or_unset(self):
        self.config.write_text(self.config.read_text().replace('CONFIG_FHANDLE=y', 'CONFIG_FHANDLE=m'))
        with self.assertRaisesRegex(ValueError, 'CONFIG_FHANDLE=y'):
            BSP.check(self.root, self.config)

    def test_wrong_module_architecture(self):
        (self.modules / '8723ds.ko').write_bytes(module(machine=183))
        with self.assertRaisesRegex(ValueError, 'RISC-V kernel module'):
            BSP.check(self.root, self.config)

    def test_mismatched_vermagic(self):
        (self.modules / '8723ds.ko').write_bytes(module(release='6.6.35'))
        with self.assertRaisesRegex(ValueError, 'vermagic'):
            BSP.check(self.root, self.config)

    def test_multiple_kernel_versions_are_ambiguous(self):
        (self.modules.parent / '6.6.35').mkdir()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            BSP.check(self.root, self.config)

    def test_built_kernel_release_mismatch(self):
        release = self.config.parent / 'include/config/kernel.release'
        release.parent.mkdir(parents=True)
        release.write_text('6.6.35\n')
        with self.assertRaisesRegex(ValueError, 'built kernel release'):
            BSP.check(self.root, self.config)

    def test_missing_board_wifi_module(self):
        for name in BSP.REQUIRED_MODULES:
            with self.subTest(module=name):
                (self.modules / f'{name}.ko').rename(self.modules / 'irrelevant.ko')
                with self.assertRaisesRegex(ValueError, f'board modules missing: {name}'):
                    BSP.check(self.root, self.config)
                (self.modules / 'irrelevant.ko').rename(self.modules / f'{name}.ko')

    def test_compressed_module(self):
        (self.modules / '8723ds.ko').unlink()
        (self.modules / '8723ds.ko.gz').write_bytes(gzip.compress(module()))
        self.assertEqual(BSP.check(self.root, self.config)['module_count'], len(BSP.REQUIRED_MODULES))

    def test_camera_runtime_requires_modules_and_allowlisted_files(self):
        for name in BSP.CAMERA_MODULES:
            (self.modules / f'{name}.ko').write_bytes(module())
        with self.assertRaisesRegex(ValueError, 'camera userspace missing'):
            BSP.check(self.root, self.config, require_camera_userspace=True)
        for path in (
            'usr/bin/isp_media_server', 'usr/bin/media-ctl', 'usr/bin/v4l2-sysfs-path',
            'usr/lib/libvvcam.so', 'usr/lib/libmxml.so.1.6',
        ):
            target = self.root / 'target' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'fixture')
        (self.root / 'target/etc/vvcam').mkdir(parents=True)
        (self.root / 'target/etc/vvcam/gc2093.json').write_text('{}')
        result = BSP.check(self.root, self.config, require_camera_userspace=True)
        self.assertEqual(result['kernel_release'], '6.6.36')

    def audio_profile(self, modes=('y', 'y', 'y', 'y')):
        """Build config and independent payload evidence for the shared profile."""
        audio = {
            'required_config': ['SOUND', 'SND', 'SND_PCM', 'SND_SOC',
                                'CANAAN_SND_DESIGNWARE_PCM', 'DMA_ENGINE', 'K230_PERIDMA',
                                'GPIOLIB', 'GPIO_CDEV', 'PINCTRL_K230_IOMUX'],
            'drivers': [
                {'config': 'SND_SOC_K230_INNO', 'module': 'snd-soc-inno-k230_codec'},
                {'config': 'CANAAN_SND_DESIGNWARE_I2S', 'module': 'canaan-designware_i2s'},
                {'config': 'SND_SOC_CANAAN_K230_AUDIO', 'module': 'snd-soc-canaan-k230-audio'},
                {'config': 'SND_SOC_CANAAN_K230_INNO', 'module': 'snd-soc-canaan-k230-inno'},
            ],
        }
        self.profile = self.root / 'profile.json'
        self.profile.write_text(json.dumps({'format': 1, 'board': 't-display-k230', 'audio': audio}))
        config = self.config.read_text() + ''.join(f'CONFIG_{key}=y\n' for key in audio['required_config'])
        builtins = []
        for driver, mode in zip(audio['drivers'], modes):
            config += f'CONFIG_{driver["config"]}={mode}\n'
            if mode == 'y':
                builtins.append(f'kernel/sound/{driver["module"]}.ko')
            elif mode == 'm':
                (self.modules / f'{driver["module"]}.ko').write_bytes(module())
        self.config.write_text(config)
        (self.modules / 'modules.builtin').write_text('\n'.join(builtins) + '\n')
        return audio

    def check_audio(self):
        return BSP.check(self.root, self.config, board_profile=self.profile)['audio']

    def audio_control_profile(self, mode='y'):
        self.audio_profile(('y', 'y', 'y', mode))
        profile = json.loads(self.profile.read_text())
        profile['audio']['machine_control'] = {
            'config': 'SND_SOC_CANAAN_K230_INNO',
            'module': 'snd-soc-canaan-k230-inno',
            'name': 'External I2S Output Switch',
        }
        self.profile.write_text(json.dumps(profile))
        return profile

    def test_audio_control_builtin_marker_is_recorded_without_claiming_dtb_verification(self):
        self.audio_control_profile()
        image = self.root / 'images/Image'
        image.parent.mkdir()
        image.write_bytes(b'kernel fixture\0External I2S Output Switch\0other data')
        result = self.check_audio()
        self.assertEqual(result['control_evidence'], {
            'name': 'External I2S Output Switch', 'mode': 'y',
            'path': 'images/Image', 'check': 'NUL-terminated control name in binary',
        })
        self.assertFalse(result['dtb_verified'])
        self.assertFalse(result['hardware_verified'])

    def test_audio_control_builtin_requires_image_and_exact_terminated_marker(self):
        self.audio_control_profile()
        with self.assertRaisesRegex(ValueError, 'audio route control cannot be verified: missing .*Image; rebuild'):
            self.check_audio()
        image = self.root / 'images/Image'
        image.parent.mkdir()
        for payload in (b'kernel without route patch', b'External I2S Output Switch',
                        b'External I2S Output Switch extra\0'):
            with self.subTest(payload=payload):
                image.write_bytes(payload)
                with self.assertRaisesRegex(ValueError, 'audio route control marker missing.*rebuild'):
                    self.check_audio()

    def test_audio_control_modular_marker_uses_checked_compressed_module(self):
        self.audio_control_profile('m')
        path = self.modules / 'snd-soc-canaan-k230-inno.ko'
        payload = path.read_bytes() + b'External I2S Output Switch\0'
        path.unlink()
        for suffix, compressor in (('', lambda value: value), ('.gz', gzip.compress), ('.xz', lzma.compress)):
            with self.subTest(suffix=suffix):
                candidate = path.with_name(path.name + suffix)
                candidate.write_bytes(compressor(payload))
                result = self.check_audio()['control_evidence']
                self.assertEqual(result['mode'], 'm')
                self.assertEqual(result['path'], str(candidate.relative_to(self.root)))
                candidate.unlink()
        self.assertFalse((self.root / 'images/Image').exists())

    def test_audio_control_cannot_be_supplied_by_unrelated_module_or_image(self):
        self.audio_control_profile('m')
        image = self.root / 'images/Image'
        image.parent.mkdir()
        marker = b'External I2S Output Switch\0'
        image.write_bytes(marker)
        (self.modules / 'unrelated.ko').write_bytes(module() + marker)
        path = self.modules / 'snd-soc-canaan-k230-inno.ko'
        path.with_name(path.name + '.gz').write_bytes(gzip.compress(path.read_bytes()))
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'audio route control marker missing.*inno.ko.gz.*rebuild'):
            self.check_audio()

    def test_audio_control_duplicate_module_candidates_are_rejected(self):
        self.audio_control_profile('m')
        path = self.modules / 'snd-soc-canaan-k230-inno.ko'
        path.write_bytes(path.read_bytes() + b'External I2S Output Switch\0')
        (self.modules / 'extra').mkdir()
        (self.modules / 'extra' / path.name).write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'multiple copies of module'):
            self.check_audio()

    def test_audio_control_profile_schema_rejects_types_and_mismatched_drivers(self):
        valid = self.audio_control_profile()
        control = valid['audio']['machine_control']
        cases = [None, False, [], {},
                 {**control, 'config': []}, {**control, 'config': 'SND_SOC_UNKNOWN'},
                 {**control, 'module': {}}, {**control, 'module': 'snd-soc-inno-k230_codec'},
                 {**control, 'name': None}, {**control, 'name': []},
                 {**control, 'name': ''}, {**control, 'name': 'x' * 44},
                 {**control, 'name': ' Audio route'}, {**control, 'name': 'Audio route '},
                 {**control, 'name': 'Audio\0route'}, {**control, 'name': 'Audio\nroute'},
                 {**control, 'name': '音频'}]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                profile = {**valid, 'audio': {**valid['audio'], 'machine_control': candidate}}
                self.profile.write_text(json.dumps(profile))
                with self.assertRaisesRegex(ValueError, 'invalid board profile.*machine_control'):
                    self.check_audio()

    def test_audio_builtin_drivers_do_not_require_standalone_modules(self):
        expected = self.audio_profile()
        audio = self.check_audio()
        self.assertEqual([driver['mode'] for driver in audio['drivers']], ['y'] * 4)
        self.assertEqual(audio['config']['SND'], 'y')
        self.assertEqual({driver['module'] for driver in audio['drivers']},
                         {driver['module'] for driver in expected['drivers']})
        self.assertFalse(audio['dtb_verified'])
        self.assertFalse(audio['hardware_verified'])

    def test_audio_modules_do_not_require_builtin_metadata(self):
        self.audio_profile(('m',) * 4)
        (self.modules / 'modules.builtin').unlink()
        audio = self.check_audio()
        self.assertEqual([driver['mode'] for driver in audio['drivers']], ['m'] * 4)
        self.assertEqual(audio['drivers'][0]['evidence'], 'ELF/vermagic-checked module')

    def test_audio_mixed_modes_and_normalized_module_names(self):
        self.audio_profile(('y', 'm', 'y', 'm'))
        builtin = self.modules / 'modules.builtin'
        builtin.write_text(builtin.read_text().replace('-', '_'))
        for path in self.modules.glob('*.ko'):
            path.rename(path.with_name(path.name.replace('-', '_')))
        self.assertEqual([driver['mode'] for driver in self.check_audio()['drivers']],
                         ['y', 'm', 'y', 'm'])

    def test_audio_required_configuration_can_be_modular(self):
        self.audio_profile(('m',) * 4)
        self.config.write_text(self.config.read_text().replace('CONFIG_SND=y', 'CONFIG_SND=m'))
        self.assertEqual(self.check_audio()['config']['SND'], 'm')

    def test_audio_unset_configuration_is_rejected_despite_present_payload(self):
        self.audio_profile()
        original = self.config.read_text()
        for key in ('SND', 'GPIO_CDEV', 'SND_SOC_K230_INNO'):
            for value in ('n', ''):
                with self.subTest(config=key, value=value):
                    self.config.write_text(original.replace(f'CONFIG_{key}=y', f'CONFIG_{key}={value}'))
                    with self.assertRaisesRegex(ValueError, f'audio kernel support missing: CONFIG_{key}'):
                        self.check_audio()

    def test_audio_builtin_metadata_missing_or_incomplete_is_rejected(self):
        self.audio_profile()
        builtin = self.modules / 'modules.builtin'
        builtin.unlink()
        with self.assertRaisesRegex(ValueError, 'built-in audio cannot be verified'):
            self.check_audio()
        builtin.write_text('kernel/sound/unrelated.ko\n')
        with self.assertRaisesRegex(ValueError, 'built-in audio driver missing'):
            self.check_audio()

    def test_audio_module_missing_is_not_satisfied_by_builtin_entry(self):
        audio = self.audio_profile(('m',) * 4)
        driver = audio['drivers'][0]['module']
        (self.modules / f'{driver}.ko').unlink()
        (self.modules / 'modules.builtin').write_text(f'kernel/sound/{driver}.ko\n')
        with self.assertRaisesRegex(ValueError, 'audio module missing'):
            self.check_audio()

    def test_audio_builtin_entry_is_not_satisfied_by_standalone_module(self):
        audio = self.audio_profile()
        builtin = self.modules / 'modules.builtin'
        driver = audio['drivers'][0]['module']
        builtin.write_text(builtin.read_text().replace(f'kernel/sound/{driver}.ko\n', ''))
        (self.modules / f'{driver}.ko').write_bytes(module())
        with self.assertRaisesRegex(ValueError, 'built-in audio driver missing'):
            self.check_audio()

    def test_compressed_audio_modules_are_verified(self):
        audio = self.audio_profile(('m',) * 4)
        for driver, suffix, compressor in zip(audio['drivers'], ('.gz', '.xz'),
                                               (gzip.compress, lzma.compress)):
            path = self.modules / f'{driver["module"]}.ko'
            path.with_name(path.name + suffix).write_bytes(compressor(path.read_bytes()))
            path.unlink()
        self.assertEqual([driver['mode'] for driver in self.check_audio()['drivers']], ['m'] * 4)

    def test_audio_modules_require_riscv_elf_and_matching_vermagic(self):
        audio = self.audio_profile(('m',) * 4)
        path = self.modules / f'{audio["drivers"][0]["module"]}.ko'
        for payload, message in ((b'not ELF', 'RISC-V kernel module'),
                                 (module(machine=183), 'RISC-V kernel module'),
                                 (module(release='6.6.35'), 'vermagic')):
            with self.subTest(message=message, payload=payload):
                path.write_bytes(payload)
                with self.assertRaisesRegex(ValueError, message):
                    self.check_audio()

    def test_board_profile_schema_errors_are_friendly(self):
        self.audio_profile()
        valid = json.loads(self.profile.read_text())
        cases = [None, [], {}, {**valid, 'format': True}, {**valid, 'format': 2},
                 {**valid, 'board': 'other'}, {**valid, 'audio': []},
                 {**valid, 'audio': {**valid['audio'], 'required_config': []}},
                 {**valid, 'audio': {**valid['audio'], 'required_config': [{}]}},
                 {**valid, 'audio': {**valid['audio'], 'required_config': ['CONFIG_SND']}},
                 {**valid, 'audio': {**valid['audio'], 'required_config': ['SND', 'SND']}},
                 {**valid, 'audio': {**valid['audio'], 'drivers': []}},
                 {**valid, 'audio': {**valid['audio'], 'drivers': [None]}},
                 {**valid, 'audio': {**valid['audio'], 'drivers': [{'config': 'SND', 'module': '../snd'}]}},
                 {**valid, 'audio': {**valid['audio'], 'drivers': valid['audio']['drivers'] * 2}}]
        for invalid in cases:
            with self.subTest(profile=invalid):
                self.profile.write_text(json.dumps(invalid))
                with self.assertRaisesRegex(ValueError, 'invalid board profile'):
                    self.check_audio()
        self.profile.write_text('{')
        with self.assertRaisesRegex(ValueError, 'cannot read board profile'):
            self.check_audio()
        self.profile.unlink()
        with self.assertRaisesRegex(ValueError, 'cannot read board profile'):
            self.check_audio()

    def test_no_profile_keeps_legacy_importer_behavior(self):
        result = BSP.check(self.root, self.config)
        self.assertNotIn('audio', result)
        self.assertNotIn('board_profile', result)

    def test_cli_board_profile_reports_modes_and_unverified_hardware(self):
        self.audio_profile()
        command = [sys.executable, str(Path(BSP.__file__)), '--bsp', str(self.root),
                   '--kernel-config', str(self.config), '--board-profile', str(self.profile)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('snd-soc-inno-k230_codec=y', result.stdout)
        self.assertIn('DTB and hardware behavior remain unverified', result.stdout)
        result = subprocess.run(command + ['--json'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['audio']['config']['SND'], 'y')
        self.profile.write_text('invalid json')
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot read board profile', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def kernel_profile(self, **kernel):
        self.audio_profile()
        profile = json.loads(self.profile.read_text())
        profile['kernel'] = kernel
        self.profile.write_text(json.dumps(profile))

    def check_kernel(self):
        return BSP.check(self.root, self.config, board_profile=self.profile)['kernel']

    def test_board_runtime_kernel_requirements_stop_the_build(self):
        self.kernel_profile(required_config=['DEVMEM', 'SPI_SPIDEV'])
        with self.assertRaisesRegex(ValueError, 'CONFIG_DEVMEM, CONFIG_SPI_SPIDEV'):
            self.check_kernel()
        self.config.write_text(self.config.read_text() + 'CONFIG_DEVMEM=y\nCONFIG_SPI_SPIDEV=m\n')
        self.assertEqual(self.check_kernel()['required_config'], dict(DEVMEM='y', SPI_SPIDEV='m'))

    def test_kernel_option_that_blocks_register_access_is_rejected(self):
        self.kernel_profile(forbidden_config=['IO_STRICT_DEVMEM'])
        self.assertEqual(self.check_kernel()['unavailable'], [])
        self.config.write_text(self.config.read_text() + 'CONFIG_IO_STRICT_DEVMEM=y\n')
        with self.assertRaisesRegex(ValueError, 'cannot work with: CONFIG_IO_STRICT_DEVMEM'):
            self.check_kernel()

    def test_optional_kernel_features_are_reported_not_required(self):
        self.kernel_profile(optional_config=[
            dict(config='KEYBOARD_TCA8418', enables='keyboard base keys'),
            dict(config='RFKILL', enables='radio kill switches')])
        self.config.write_text(self.config.read_text() + 'CONFIG_RFKILL=m\n')
        kernel = self.check_kernel()
        self.assertEqual(kernel['unavailable'], ['keyboard base keys'])
        self.assertEqual([entry['state'] for entry in kernel['optional_config']], ['n', 'm'])
        command = [sys.executable, str(Path(BSP.__file__)), '--bsp', str(self.root),
            '--kernel-config', str(self.config), '--board-profile', str(self.profile)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Not provided by this BSP kernel: keyboard base keys', result.stdout)
        self.assertNotIn('radio kill switches', result.stdout)

    def test_kernel_profile_schema_errors_are_friendly(self):
        for kernel in (['DEVMEM'], dict(required_config='DEVMEM'),
            dict(required_config=['CONFIG_DEVMEM']), dict(forbidden_config=['A', 'A']),
            dict(optional_config=[dict(config='RFKILL')]),
            dict(optional_config=[dict(config='rfkill', enables='radio')])):
            with self.subTest(kernel=kernel):
                self.audio_profile()
                profile = json.loads(self.profile.read_text())
                profile['kernel'] = kernel
                self.profile.write_text(json.dumps(profile))
                with self.assertRaisesRegex(ValueError, 'invalid board profile'):
                    self.check_kernel()

    def test_profile_without_kernel_section_is_still_accepted(self):
        self.audio_profile()
        self.assertNotIn('kernel', BSP.check(self.root, self.config, board_profile=self.profile))

    def test_shared_board_profile_requirements_match_the_checker_schema(self):
        shared = Path(BSP.__file__).resolve().parents[2] / 'lilygo-linux-dtoverlays/boards/T-Display-K230/profile.json'
        requirements = BSP.read_kernel_requirements(shared)
        self.assertIn('DEVMEM', requirements['required_config'])
        self.assertEqual(requirements['forbidden_config'], ['IO_STRICT_DEVMEM'])
        self.assertTrue(all(entry['enables'] for entry in requirements['optional_config']))

    def keyboard_profile(self, mode='y'):
        self.audio_profile()
        self.keyboard = {
            'config': 'KEYBOARD_TCA8418', 'module': 'tca8418_keypad',
            'compatible': 'lilygo,tca8418-keypad', 'image_marker': 'LILYGO TCA8418 keyboard ready',
            'dtb': 'k230-canmv-rm69a10.dtb', 'node_path': '/keyboard-i2c/keypad@34',
            'bus': {'node_path': '/keyboard-i2c', 'compatible': 'i2c-gpio', 'scl_gpio': 14,
                    'sda_gpio': 15, 'gpio_controller_path': '/soc/gpio@9140c000/gpio-port@0',
                    'pinctrl_path': '/soc/iomux@91105000/keyboard_i2c_gpio_pins'},
            'interrupt_parent_path': '/soc/gpio@9140c000/gpio-port@0',
            'reset_controller_path': '/soc/gpio@9140c000/gpio-port@0',
            'address': 52, 'rows': 7, 'columns': 10,
            'interrupts': [10, 2], 'reset_gpios': [11, 1],
            'keymap': [0x00000010, 0x0100001e], 'fn_keymap': [0x00000002],
            'fn_shift_keymap': [0x0100000c],
        }
        profile = json.loads(self.profile.read_text())
        profile['keyboard'] = self.keyboard
        self.profile.write_text(json.dumps(profile))
        self.config.write_text(self.config.read_text() + f'CONFIG_KEYBOARD_TCA8418={mode}\n')
        if mode == 'y':
            builtin = self.modules / 'modules.builtin'
            builtin.write_text(builtin.read_text() + 'kernel/drivers/input/keyboard/tca8418_keypad.ko\n')
        elif mode == 'm':
            (self.modules / 'tca8418_keypad.ko').write_bytes(
                module() + b'lilygo,tca8418-keypad\0LILYGO TCA8418 keyboard ready\n\0')
        self.nodes = {
            '/': {}, '/soc': {}, '/keyboard-i2c': {
                'compatible': b'i2c-gpio\0', 'status': b'okay\0',
                '#address-cells': cells(1), '#size-cells': cells(0),
                'scl-gpios': cells(77, 14, 2), 'sda-gpios': cells(77, 15, 2),
                'pinctrl-0': cells(88)},
            self.keyboard['node_path']: {
                'compatible': b'lilygo,tca8418-keypad\0', 'reg': cells(52),
                'keypad,num-rows': cells(7), 'keypad,num-columns': cells(10),
                'interrupt-parent': cells(77), 'interrupts': cells(10, 2),
                'reset-gpios': cells(77, 11, 1),
                'linux,keymap': cells(*self.keyboard['keymap']),
                'linux,fn-keymap': cells(*self.keyboard['fn_keymap']),
                'lilygo,fn-shift-keymap': cells(*self.keyboard['fn_shift_keymap']),
            },
            '/soc/gpio@9140c000': {},
            self.keyboard['interrupt_parent_path']: {
                'phandle': cells(77), 'gpio-controller': b'', '#gpio-cells': cells(2),
                'interrupt-controller': b'', '#interrupt-cells': cells(2),
            },
            '/soc/iomux@91105000/keyboard_i2c_gpio_pins': {'phandle': cells(88)},
            '/soc/iomux@91105000/keyboard_i2c_gpio_pins/scl': {
                'pins': b'io46\0', 'function': b'alt0\0'},
            '/soc/iomux@91105000/keyboard_i2c_gpio_pins/sda': {
                'pins': b'io47\0', 'function': b'alt0\0'},
            '/soc/i2c@91409000': {'pinctrl-0': cells(99)},
            '/soc/iomux@91105000': {},
            '/soc/iomux@91105000/cam_i2c4_pins': {'phandle': cells(99)},
            '/soc/i2c@91409000/gc2093@37': {},
            '/soc/i2c@91409000/dw9714@0c': {},
        }
        self.image = (self.root / 'images/Image')
        self.image.parent.mkdir()
        self.image.write_bytes(b'kernel\0LILYGO TCA8418 keyboard ready\n\0IKCFG_ST' +
                               gzip.compress(self.config.read_bytes()) + b'IKCFG_ED')
        self.extraction = mock.patch.object(BSP, 'boot_keyboard_payload',
                                          side_effect=lambda *_: {'Image': self.image.read_bytes(),
                                                                  self.keyboard['dtb']: dtb(self.nodes)})
        self.extract = self.extraction.start()
        self.addCleanup(self.extraction.stop)

    def check_keyboard(self, **kwargs):
        return BSP.check(self.root, self.config, board_profile=self.profile, **kwargs)['keyboard']

    def test_keyboard_requires_binary_and_real_boot_dtb_evidence(self):
        self.keyboard_profile()
        result = self.check_keyboard(template=self.root / 'selected.img')
        self.extract.assert_called_once_with(self.root / 'selected.img', 'k230-canmv-rm69a10.dtb')
        self.assertTrue(result['dtb']['wiring_and_keymap_verified'])
        self.assertFalse(result['hardware_verified'])
        self.assertEqual(result['mode'], 'y')

    def test_keyboard_module_support_is_checked_with_its_lilygo_compatible(self):
        self.keyboard_profile(mode='m')
        self.assertEqual(self.check_keyboard()['mode'], 'm')
        (self.modules / 'tca8418_keypad.ko').write_bytes(module())
        with self.assertRaisesRegex(ValueError, 'FN-layer driver'):
            self.check_keyboard()

    def test_keyboard_cannot_use_present_binary_with_disabled_config(self):
        self.keyboard_profile()
        self.config.write_text(self.config.read_text().replace('CONFIG_KEYBOARD_TCA8418=y', '# disabled'))
        with self.assertRaisesRegex(ValueError, 'keyboard support missing'):
            self.check_keyboard()
        self.extract.assert_not_called()

    def test_keyboard_builtin_metadata_and_marker_are_both_required(self):
        self.keyboard_profile()
        builtin = self.modules / 'modules.builtin'
        original = builtin.read_text()
        builtin.write_text(original.replace('tca8418_keypad.ko', 'other.ko'))
        with self.assertRaisesRegex(ValueError, 'missing from modules.builtin'):
            self.check_keyboard()
        builtin.write_text(original)
        self.image.write_bytes(self.image.read_bytes().replace(b'LILYGO TCA8418 keyboard ready', b'old driver'))
        with self.assertRaisesRegex(ValueError, 'FN-layer driver marker'):
            self.check_keyboard()

    def test_keyboard_rejects_stale_image_and_config_next_to_new_template(self):
        self.keyboard_profile()
        self.extract.side_effect = None
        self.extract.return_value = {'Image': b'different boot kernel', self.keyboard['dtb']: dtb(self.nodes)}
        with self.assertRaisesRegex(ValueError, 'differs from the kernel'):
            self.check_keyboard()
        self.extract.return_value['Image'] = self.image.read_bytes()
        self.config.write_text(self.config.read_text() + 'CONFIG_AN_UNRELATED_OPTION=y\n')
        with self.assertRaisesRegex(ValueError, 'does not match.*embedded'):
            self.check_keyboard()

    def test_keyboard_rejects_missing_or_disabled_node_and_ancestors(self):
        self.keyboard_profile()
        for path in ('/', '/soc', '/keyboard-i2c', self.keyboard['node_path'],
                     '/soc/gpio@9140c000', self.keyboard['interrupt_parent_path']):
            with self.subTest(path=path):
                previous = self.nodes[path].get('status')
                self.nodes[path]['status'] = b'disabled\0'
                with self.assertRaisesRegex(ValueError, 'ancestor is disabled'):
                    self.check_keyboard()
                if previous is None:
                    del self.nodes[path]['status']
                else:
                    self.nodes[path]['status'] = previous
        del self.nodes[self.keyboard['node_path']]
        with self.assertRaisesRegex(ValueError, 'node missing'):
            self.check_keyboard()

    def test_keyboard_rejects_wrong_bus_address_matrix_keymap_and_irq(self):
        self.keyboard_profile()
        node = self.nodes[self.keyboard['node_path']]
        cases = {'reg': cells(53), 'keypad,num-rows': cells(8), 'keypad,num-columns': cells(9),
                 'linux,keymap': cells(0x000000ff), 'linux,fn-keymap': b'',
                 'lilygo,fn-shift-keymap': b'', 'interrupts': cells(42, 2),
                 'interrupt-parent': cells(999), 'reset-gpios': cells(77, 43, 1)}
        for key, bad in cases.items():
            with self.subTest(property=key):
                previous = node[key]
                node[key] = bad
                with self.assertRaisesRegex(ValueError, 'TCA8418 keyboard DTB'):
                    self.check_keyboard()
                node[key] = previous

    def test_keyboard_profile_schema_rejects_unsafe_dtb_names_and_invalid_cells(self):
        self.keyboard_profile()
        original = json.loads(self.profile.read_text())
        for key, value in (('dtb', '../other.dtb'), ('dtb', 'x.dtb output'), ('config', 'OTHER'),
                           ('rows', True), ('rows', 9), ('keymap', [-1]), ('interrupts', [10]),
                           ('node_path', '/soc/../keyboard'), ('image_marker', 'x\0y')):
            with self.subTest(key=key, value=value):
                candidate = {**original, 'keyboard': {**original['keyboard'], key: value}}
                self.profile.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError, 'invalid board profile.*keyboard'):
                    BSP.read_keyboard_requirements(self.profile)


class FdtChecks(unittest.TestCase):
    def test_decodes_nested_tree_and_cells(self):
        tree = {'/': {'model': b'K230\0'}, '/soc': {}, '/soc/keyboard@34': {'reg': cells(52)}}
        self.assertEqual(BSP.FDT.parse(dtb(tree)), tree)
        self.assertEqual(BSP.FDT.cells(tree['/soc/keyboard@34']['reg']), [52])

    def test_rejects_truncated_blob_and_invalid_offsets(self):
        data = dtb({'/': {}, '/soc': {}})
        invalid = [b'', data[:39], data[:-1]]
        bad = bytearray(data)
        struct.pack_into('>I', bad, 8, len(data) + 32)
        invalid.append(bytes(bad))
        for candidate in invalid:
            with self.subTest(size=len(candidate)), self.assertRaises(ValueError):
                BSP.FDT.parse(candidate)

    def test_rejects_unknown_tokens_unterminated_tree_and_misaligned_cells(self):
        data = bytearray(dtb({'/': {}}))
        struct.pack_into('>I', data, 56, 99)
        with self.assertRaisesRegex(ValueError, 'unknown FDT token'):
            BSP.FDT.parse(data)
        data = bytearray(dtb({'/': {}}))
        data[-4:] = cells(4)
        with self.assertRaisesRegex(ValueError, 'missing FDT end'):
            BSP.FDT.parse(data)
        with self.assertRaisesRegex(ValueError, 'partial cell'):
            BSP.FDT.cells(b'\0\1')


@unittest.skipUnless(shutil.which('debugfs') and shutil.which('mke2fs'), 'requires e2fsprogs')
class BootKeyboardFilesystemTests(unittest.TestCase):
    def test_reads_actual_boot_files_and_rejects_a_different_selected_dtb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contents = root / 'contents'
            contents.mkdir()
            (contents / 'Image').write_bytes(b'actual kernel image')
            name = 'k230-canmv-rm69a10.dtb'
            data = dtb({'/': {}, '/soc': {}})
            (contents / name).write_bytes(data)
            (contents / 'k.dtb').symlink_to(name)
            boot = root / 'boot.ext4'

            def image():
                subprocess.run(['mke2fs', '-q', '-F', '-t', 'ext4', '-d', str(contents), str(boot), '8192'],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                start, count = 2048, boot.stat().st_size // 512
                mbr = bytearray(512)
                for i, sector in enumerate((start, start + count)):
                    mbr[446 + i * 16 + 4] = 0x83
                    struct.pack_into('<II', mbr, 446 + i * 16 + 8, sector, count)
                mbr[510:512] = b'\x55\xaa'
                result = root / 'template.img'
                with result.open('wb') as stream:
                    stream.write(mbr)
                    stream.seek(start * 512)
                    stream.write(boot.read_bytes())
                    stream.write(boot.read_bytes())
                return result

            result = BSP.boot_keyboard_payload(image(), name)
            self.assertEqual(result, {'Image': b'actual kernel image', name: data})
            (contents / 'k.dtb').unlink()
            (contents / 'k.dtb').symlink_to('old-board.dtb')
            with self.assertRaisesRegex(ValueError, 'does not select'):
                BSP.boot_keyboard_payload(image(), name)


if __name__ == '__main__':
    unittest.main()
