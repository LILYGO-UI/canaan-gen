#!/usr/bin/env python3
"""Reject incomplete or mismatched DRV2605/XL9555 BSP outputs."""
import copy
import gzip
import json
from pathlib import Path
import unittest

import test_bsp as fixtures


BSP = fixtures.BSP
cells = fixtures.cells


class PeripheralBspTests(unittest.TestCase):
    # Reuse the independent kernel/module/keyboard fixtures without rerunning
    # the keyboard test class as part of this suite.
    audio_profile = fixtures.BspCheck.audio_profile

    def setUp(self):
        fixtures.BspCheck.setUp(self)
        fixtures.BspCheck.keyboard_profile(self)
        self.requirements = {
            'dtb': self.keyboard['dtb'],
            'bus': self.keyboard['bus'],
            'drivers': [
                {'config': 'INPUT_DRV260X_HAPTICS', 'module': 'drv260x',
                 'image_markers': ['LILYGO DRV260x haptics ready', 'keyboard_feedback_strength']},
                {'config': 'GPIO_PCA953X', 'module': 'gpio-pca953x', 'image_markers': ['nxp,pca9555']},
                {'config': 'LEDS_GPIO', 'module': 'leds-gpio', 'image_markers': ['gpio-leds']},
            ],
            'haptics': {
                'node_path': '/keyboard-i2c/haptics@5a', 'compatible': 'ti,drv2605', 'address': 90,
                'properties': {'mode': [2], 'library-sel': [1], 'lilygo,erm-open-loop': True},
                'absent_properties': ['enable-gpio', 'enable-gpios', 'vbat-supply',
                                      'vib-rated-mv', 'vib-overdrive-mv'],
                'feedback_node_paths': [self.keyboard['node_path']],
            },
            'expander': {
                'node_path': '/keyboard-i2c/gpio@20', 'compatible': 'nxp,pca9555',
                'address': 32, 'ngpios': 16, 'line_names': [f'P{bank}{line}' for bank in (0, 1) for line in range(8)],
                'leds': {'node_path': '/keyboard-leds', 'compatible': 'gpio-leds', 'children': [
                    {'node_path': f'/keyboard-leds/led{line}', 'line': line, 'active_low': True,
                     'label': f'keyboard:led{line}', 'default_state': 'off'} for line in (3, 4, 5)]},
            },
        }
        profile = json.loads(self.profile.read_text())
        profile['peripherals'] = self.requirements
        self.profile.write_text(json.dumps(profile))
        self.modes = {driver['config']: 'y' for driver in self.requirements['drivers']}
        with self.config.open('a') as config, (self.modules / 'modules.builtin').open('a') as builtin:
            for driver in self.requirements['drivers']:
                config.write(f'CONFIG_{driver["config"]}=y\n')
                builtin.write(f'kernel/drivers/{driver["module"]}.ko\n')
        self.refresh_image()
        self.nodes[self.requirements['haptics']['node_path']] = {
            'compatible': b'ti,drv2605\0', 'reg': cells(90), 'mode': cells(2), 'library-sel': cells(1),
            'lilygo,erm-open-loop': b'', 'phandle': cells(90),
        }
        self.nodes[self.keyboard['node_path']]['lilygo,haptic-feedback'] = b''
        self.nodes[self.requirements['expander']['node_path']] = {
            'compatible': b'nxp,pca9555\0', 'reg': cells(32), 'gpio-controller': b'',
            '#gpio-cells': cells(2), 'ngpios': cells(16), 'phandle': cells(89),
            'gpio-line-names': ('\0'.join(self.requirements['expander']['line_names']) + '\0').encode(),
        }
        self.nodes['/keyboard-leds'] = {'compatible': b'gpio-leds\0'}
        for led in self.requirements['expander']['leds']['children']:
            self.nodes[led['node_path']] = {'gpios': cells(89, led['line'], 1),
                                           'label': led['label'].encode() + b'\0', 'default-state': b'off\0'}

    def refresh_image(self):
        markers = ['LILYGO TCA8418 keyboard ready']
        for driver in self.requirements['drivers']:
            if self.modes[driver['config']] == 'y':
                markers.extend(driver['image_markers'])
        self.image.write_bytes(('\0'.join(markers) + '\0').encode() + b'IKCFG_ST' +
                               gzip.compress(self.config.read_bytes()) + b'IKCFG_ED')

    def check(self):
        return BSP.check(self.root, self.config, board_profile=self.profile)

    def test_all_board_devices_share_one_actual_boot_payload(self):
        result = self.check()
        self.extract.assert_called_once()
        peripherals = result['peripherals']
        self.assertEqual(peripherals['boot_image_sha256'], result['keyboard']['boot_image_sha256'])
        self.assertTrue(peripherals['dtb']['wiring_verified'])
        self.assertFalse(peripherals['hardware_verified'])

    def test_each_driver_needs_config_and_independent_builtin_evidence(self):
        config = self.config.read_text()
        builtin = (self.modules / 'modules.builtin').read_text()
        for driver in self.requirements['drivers']:
            with self.subTest(driver=driver['module']):
                self.config.write_text(config.replace(f'CONFIG_{driver["config"]}=y\n', ''))
                self.refresh_image()
                with self.assertRaisesRegex(ValueError, 'peripheral driver missing: CONFIG_' + driver['config']):
                    self.check()
                self.config.write_text(config)
                self.refresh_image()
                (self.modules / 'modules.builtin').write_text(builtin.replace(driver['module'] + '.ko', 'other.ko'))
                with self.assertRaisesRegex(ValueError, 'built-in driver missing.*' + driver['module']):
                    self.check()
                (self.modules / 'modules.builtin').write_text(builtin)

    def test_modules_need_their_own_markers_even_if_the_boot_image_has_them(self):
        for driver in self.requirements['drivers']:
            key = driver['config']
            self.modes[key] = 'm'
            self.config.write_text(self.config.read_text().replace(f'CONFIG_{key}=y', f'CONFIG_{key}=m'))
            path = self.modules / (driver['module'] + '.ko')
            path.write_bytes(fixtures.module() + ('\0'.join(driver['image_markers']) + '\0').encode())
        # Keep unused builtin records to ensure module mode does not accept them
        # instead of checking the actual .ko file.
        self.refresh_image()
        self.assertEqual([driver['mode'] for driver in self.check()['peripherals']['drivers']], ['m'] * 3)
        drv = self.modules / 'drv260x.ko'
        drv.write_bytes(fixtures.module())
        self.image.write_bytes(b'LILYGO DRV260x haptics ready\0keyboard_feedback_strength\0' + self.image.read_bytes())
        with self.assertRaisesRegex(ValueError, 'driver marker missing from drv260x'):
            self.check()

    def test_builtin_markers_cannot_come_from_an_adjacent_dtb_or_module(self):
        for driver in self.requirements['drivers']:
            with self.subTest(driver=driver['module']):
                self.refresh_image()
                marker = driver['image_markers'][0].encode()
                self.image.write_bytes(self.image.read_bytes().replace(marker, b'older_driver'))
                (self.modules / (driver['module'] + '.ko')).write_bytes(fixtures.module() + marker + b'\0')
                with self.assertRaisesRegex(ValueError, 'peripheral driver marker missing'):
                    self.check()

    def test_motor_mode_library_and_open_loop_flag_must_match_vendor(self):
        motor = self.nodes[self.requirements['haptics']['node_path']]
        for name, value in (('reg', cells(91)), ('mode', cells(0)), ('library-sel', cells(6)),
                            ('lilygo,erm-open-loop', cells(1))):
            with self.subTest(property=name):
                previous = motor[name]
                motor[name] = value
                with self.assertRaisesRegex(ValueError, 'DRV2605 DTB'):
                    self.check()
                motor[name] = previous

    def test_unverified_motor_voltages_supply_and_enable_gpio_are_rejected(self):
        motor = self.nodes[self.requirements['haptics']['node_path']]
        for name in self.requirements['haptics']['absent_properties']:
            with self.subTest(property=name):
                motor[name] = cells(3200)
                with self.assertRaisesRegex(ValueError, 'must not guess.*' + name):
                    self.check()
                del motor[name]

    def test_keyboard_feedback_must_explicitly_enable_the_boolean_input_flag(self):
        consumer = self.nodes[self.keyboard['node_path']]
        consumer['lilygo,haptic-feedback'] = cells(89)
        with self.assertRaisesRegex(ValueError, 'feedback DTB must explicitly enable'):
            self.check()
        del consumer['lilygo,haptic-feedback']
        with self.assertRaisesRegex(ValueError, 'feedback DTB must explicitly enable'):
            self.check()

    def test_gpio_address_compatible_width_and_line_names_are_verified(self):
        gpio = self.nodes[self.requirements['expander']['node_path']]
        for name, value in (('reg', cells(33)), ('compatible', b'nxp,pca9535\0'),
                            ('ngpios', cells(8)), ('#gpio-cells', cells(3)),
                            ('gpio-line-names', b'only-one-line\0')):
            with self.subTest(property=name):
                previous = gpio[name]
                gpio[name] = value
                with self.assertRaisesRegex(ValueError, 'XL9555 DTB'):
                    self.check()
                gpio[name] = previous
        del gpio['gpio-controller']
        with self.assertRaisesRegex(ValueError, 'not configured as a GPIO provider'):
            self.check()

    def test_leds_reject_wrong_pin_provider_polarity_or_boot_state(self):
        led = self.nodes['/keyboard-leds/led3']
        for name, value in (('gpios', cells(88, 3, 1)), ('gpios', cells(89, 2, 1)),
                            ('gpios', cells(89, 3, 0)), ('default-state', b'on\0'),
                            ('label', b'unknown-led\0')):
            with self.subTest(property=name, value=value):
                previous = led[name]
                led[name] = value
                with self.assertRaisesRegex(ValueError, 'XL9555 LED DTB'):
                    self.check()
                led[name] = previous

    def test_extra_led_or_gpio_hog_cannot_claim_unassigned_expander_lines(self):
        extra = '/keyboard-leds/led6'
        self.nodes[extra] = {'gpios': cells(89, 6, 1)}
        with self.assertRaisesRegex(ValueError, 'children differ'):
            self.check()
        del self.nodes[extra]
        self.nodes[self.requirements['expander']['node_path'] + '/reserved'] = {'gpio-hog': b''}
        with self.assertRaisesRegex(ValueError, 'GPIO hogs'):
            self.check()

    def test_disabled_missing_or_unselected_devices_fail_even_with_correct_driver(self):
        for path in (self.requirements['haptics']['node_path'], self.requirements['expander']['node_path'],
                     '/keyboard-leds', '/keyboard-leds/led3'):
            with self.subTest(path=path):
                self.nodes[path]['status'] = b'disabled\0'
                with self.assertRaisesRegex(ValueError, 'node or ancestor is disabled'):
                    self.check()
                del self.nodes[path]['status']
        del self.nodes[self.requirements['haptics']['node_path']]
        with self.assertRaisesRegex(ValueError, 'DRV2605 node missing'):
            self.check()

    def test_peripherals_profile_is_optional_for_legacy_imports(self):
        profile = json.loads(self.profile.read_text())
        del profile['peripherals']
        self.profile.write_text(json.dumps(profile))
        self.assertNotIn('peripherals', self.check())

    def test_peripheral_checks_also_validate_boot_without_a_keyboard_profile(self):
        profile = json.loads(self.profile.read_text())
        del profile['keyboard']
        self.profile.write_text(json.dumps(profile))
        result = self.check()
        self.assertNotIn('keyboard', result)
        self.assertTrue(result['peripherals']['dtb']['wiring_verified'])
        self.extract.assert_called_once()

    def test_shared_board_profile_matches_the_peripheral_checker_schema(self):
        shared = Path(BSP.__file__).resolve().parents[2] / 'lilygo-linux-dtoverlays/boards/T-Display-K230/profile.json'
        requirements = BSP.read_peripheral_requirements(shared)
        self.assertEqual(requirements['haptics']['properties']['lilygo,erm-open-loop'], True)
        self.assertEqual(requirements['expander']['ngpios'], 16)

    def test_peripheral_profile_rejects_incomplete_or_invented_hardware(self):
        original = json.loads(self.profile.read_text())
        edits = [lambda p: p.update(dtb='../other.dtb'),
                 lambda p: p.update(drivers=p['drivers'][:2]),
                 lambda p: p['haptics'].update(address=0x5b, node_path='/keyboard-i2c/haptics@5b'),
                 lambda p: p['haptics'].update(node_path='/soc/i2c@91408000/haptics@5a'),
                 lambda p: p['expander'].update(address=0x28, node_path='/keyboard-i2c/gpio@28'),
                 lambda p: p['expander'].update(address=0x21),
                 lambda p: p['expander'].update(node_path='/soc/i2c@91408000/gpio@20'),
                 lambda p: p['haptics']['properties'].update(mode=[0]),
                 lambda p: p['haptics']['properties'].update({'library-sel': [True]}),
                 lambda p: p['haptics'].update(absent_properties=[]),
                 lambda p: p['haptics'].update(supply={'microvolt': 3300000}),
                 lambda p: p['expander'].update(ngpios=8),
                 lambda p: p['expander']['leds']['children'][0].update(active_low=False),
                 lambda p: p['expander']['leds']['children'][0].update(line=6)]
        for edit in edits:
            candidate = copy.deepcopy(original)
            edit(candidate['peripherals'])
            with self.subTest(profile=candidate['peripherals']):
                self.profile.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError, 'invalid board profile.*peripherals'):
                    BSP.read_peripheral_requirements(self.profile)

    def test_expander_strap_variant_requires_a_matching_i2c4_node(self):
        profile = json.loads(self.profile.read_text())
        profile['peripherals']['expander'].update(address=0x27, node_path='/keyboard-i2c/gpio@27')
        self.profile.write_text(json.dumps(profile))
        requirements = BSP.read_peripheral_requirements(self.profile)
        self.assertEqual(requirements['expander']['address'], 0x27)

    def test_keyboard_and_peripherals_cannot_be_verified_in_different_dtbs(self):
        profile = json.loads(self.profile.read_text())
        profile['peripherals']['dtb'] = 'other.dtb'
        self.profile.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ValueError, 'same boot DTB'):
            self.check()
        self.extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
