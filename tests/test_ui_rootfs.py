import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check-ui-rootfs.py'
spec = importlib.util.spec_from_file_location('check_ui_rootfs', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


BOARD_ROOTFS = SCRIPT.parents[2] / 'lilygo-linux-dtoverlays/boards/T-Display-K230/debian/rootfs'
DROP_IN = 'etc/systemd/system/lilygo-ui-launcher.service.d/' + module.BOARD_DROP_IN


class FirstBootRootfsTests(unittest.TestCase):
    def setUp(self):
        self.reader = patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout='(NEEDED) Shared library: [libpam.so.0]'))
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.put('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n')
        self.put('etc/shadow', 'root:!:20000:0:99999:7:::\n')
        data = bytearray(64); data[:6] = b'\x7fELF\x02\x01'; struct.pack_into('<H', data, 18, 243)
        self.put('usr/bin/lilygo-ui-launcher', data, 0o755)
        for name in ('first-boot', 'setup-region', 'console'):
            self.put('usr/lib/lilygo-ui-launcher/lilygo-ui-launcher-'+name, '#!/bin/sh\n', 0o755)
        self.put('etc/pam.d/lilygo-ui-launcher', 'auth required pam_unix.so\n')
        self.put('usr/lib/systemd/system/lilygo-ui-launcher.service', 'User=root\nExecStart=/usr/bin/lilygo-ui-launcher --display drm --input auto\n')
        link = self.root/'etc/systemd/system/multi-user.target.wants/lilygo-ui-launcher.service'
        link.parent.mkdir(parents=True); link.symlink_to('/usr/lib/systemd/system/lilygo-ui-launcher.service')
        self.put('usr/share/lilygo-ui/fonts/Inter.ttf', b'fixture')
        self.put('usr/share/lilygo-ui/fonts/SourceHan.otf', b'fixture')
        self.put('usr/share/doc/lilygo-ui-appkit-dev/OFL-1.1.txt', 'fixture')
    def put(self, name, value, mode=0o644):
        path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value); path.chmod(mode)
    def test_empty_accounts_are_ready_for_wizard(self):
        self.assertTrue(module.check(self.root))
    def test_locked_preset_user_would_bypass_wizard(self):
        with (self.root/'etc/passwd').open('a') as f: f.write('lilygo:x:1000:1000::/home/lilygo:/bin/bash\n')
        with self.assertRaisesRegex(ValueError, 'preset ordinary users'): module.check(self.root)
    def test_stale_setup_journal_is_rejected(self):
        for name in ('active-user', 'first-boot-pending', 'first-boot-new-user', 'first-boot-progress'):
            with self.subTest(name=name):
                self.put('var/lib/lilygo-ui-launcher/'+name, 'stale')
                with self.assertRaisesRegex(ValueError, 'stale account state'): module.check(self.root)
                (self.root/'var/lib/lilygo-ui-launcher'/name).unlink()
    def test_missing_service_enablement_is_rejected(self):
        (self.root/'etc/systemd/system/multi-user.target.wants/lilygo-ui-launcher.service').unlink()
        with self.assertRaisesRegex(ValueError, 'not enabled'): module.check(self.root)
    def test_wrong_architecture_is_rejected(self):
        self.put('usr/bin/lilygo-ui-launcher', b'host executable', 0o755)
        with self.assertRaisesRegex(ValueError, 'RISC-V'): module.check(self.root)
    def test_unlocked_root_is_rejected(self):
        self.put('etc/shadow', 'root::20000:0:99999:7:::\n')
        with self.assertRaisesRegex(ValueError, 'locked'): module.check(self.root)
    def test_non_executable_launcher_is_rejected(self):
        (self.root/'usr/bin/lilygo-ui-launcher').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'execute permission'): module.check(self.root)
    def test_binary_without_pam_cannot_skip_first_boot(self):
        with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout='')):
            with self.assertRaisesRegex(ValueError, 'PAM dependency'): module.check(self.root)
    def test_masked_service_is_rejected(self):
        (self.root/'etc/systemd/system/lilygo-ui-launcher.service').symlink_to('/dev/null')
        with self.assertRaisesRegex(ValueError, 'override or mask'): module.check(self.root)
    def test_wrong_enablement_target_is_rejected(self):
        link = self.root/'etc/systemd/system/multi-user.target.wants/lilygo-ui-launcher.service'
        link.unlink(); link.symlink_to('/missing/lilygo-ui-launcher.service')
        with self.assertRaisesRegex(ValueError, 'not enabled'): module.check(self.root)
    def test_insecure_state_directory_is_rejected(self):
        directory = self.root/'var/lib/lilygo-ui-launcher'
        directory.mkdir(parents=True); directory.chmod(0o777)
        with self.assertRaisesRegex(ValueError, '0700'): module.check(self.root)
    def test_enabled_ssh_is_rejected(self):
        (self.root/'etc/systemd/system/multi-user.target.wants/ssh.service').symlink_to('/usr/lib/systemd/system/ssh.service')
        with self.assertRaisesRegex(ValueError, 'SSH must remain disabled'): module.check(self.root)
    def test_enabled_ssh_is_allowed_when_requested(self):
        link = self.root/'etc/systemd/system/multi-user.target.wants/ssh.service'
        for target in ('/usr/lib/systemd/system/ssh.service', '/lib/systemd/system/ssh.service',
                       '../../../../usr/lib/systemd/system/ssh.service'):
            with self.subTest(target=target):
                link.symlink_to(target)
                self.assertTrue(module.check(self.root, enable_ssh=True))
                link.unlink()
    def test_requested_ssh_must_be_enabled(self):
        with self.assertRaisesRegex(ValueError, 'SSH service is not enabled'):
            module.check(self.root, enable_ssh=True)
    def test_requested_ssh_rejects_wrong_enablement_target(self):
        (self.root/'etc/systemd/system/multi-user.target.wants/ssh.service').symlink_to('/dev/null')
        with self.assertRaisesRegex(ValueError, 'SSH service is not enabled'):
            module.check(self.root, enable_ssh=True)
    def test_requested_ssh_rejects_masked_service(self):
        (self.root/'etc/systemd/system/multi-user.target.wants/ssh.service').symlink_to('/usr/lib/systemd/system/ssh.service')
        (self.root/'etc/systemd/system/ssh.service').symlink_to('/dev/null')
        with self.assertRaisesRegex(ValueError, 'SSH service has a local override or mask'):
            module.check(self.root, enable_ssh=True)
    def test_enabled_ssh_does_not_allow_preset_username(self):
        (self.root/'etc/systemd/system/multi-user.target.wants/ssh.service').symlink_to('/usr/lib/systemd/system/ssh.service')
        self.put('etc/ssh/sshd_config.d/local.conf', 'AllowUsers lilygo\n')
        with self.assertRaisesRegex(ValueError, 'AllowUsers'):
            module.check(self.root, enable_ssh=True)
    def test_preset_ssh_name_is_rejected(self):
        self.put('etc/ssh/sshd_config.d/local.conf', 'AllowUsers lilygo\n')
        with self.assertRaisesRegex(ValueError, 'AllowUsers'): module.check(self.root)
    def test_missing_pam_is_rejected(self):
        (self.root/'etc/pam.d/lilygo-ui-launcher').unlink()
        with self.assertRaisesRegex(ValueError, 'PAM'): module.check(self.root)
    def board_environment(self):
        for name in (DROP_IN, module.BOARD_ENVIRONMENT):
            self.put(name, (BOARD_ROOTFS/name).read_text())
    def test_shared_board_device_paths_are_the_one_accepted_drop_in(self):
        self.board_environment()
        self.assertTrue(module.check(self.root))
        variables = dict(line.split('=', 1) for line in (BOARD_ROOTFS/module.BOARD_ENVIRONMENT).read_text().splitlines()
            if line and not line.startswith('#'))
        self.assertEqual(variables['CM0_POWER_SUPPLY_DIR'], '/run/lilygo-k230/power_supply')
        self.assertEqual(variables['LILYGO_UI_CELLULAR_DEVICE'], '/dev/ttyS3')
    def test_other_launcher_drop_ins_are_still_rejected(self):
        self.board_environment()
        self.put('etc/systemd/system/lilygo-ui-launcher.service.d/20-local.conf', '[Service]\nUser=nobody\n')
        with self.assertRaisesRegex(ValueError, 'reviewed first-boot profile'): module.check(self.root)
    def test_board_drop_in_name_outside_etc_is_rejected(self):
        self.put('usr/lib/systemd/system/lilygo-ui-launcher.service.d/10-t-display-k230.conf',
            (BOARD_ROOTFS/DROP_IN).read_text())
        with self.assertRaisesRegex(ValueError, 'reviewed first-boot profile'): module.check(self.root)
    def test_board_drop_in_cannot_change_how_the_launcher_runs(self):
        self.board_environment()
        for extra in ('ExecStart=\nExecStart=/bin/sh\n', 'User=nobody\n', 'Environment=LD_PRELOAD=/tmp/x.so\n'):
            with self.subTest(extra=extra):
                self.put(DROP_IN, (BOARD_ROOTFS/DROP_IN).read_text() + extra)
                with self.assertRaisesRegex(ValueError, 'may only read the board environment file'):
                    module.check(self.root)
    def test_board_environment_may_only_name_application_device_paths(self):
        self.board_environment()
        original = (BOARD_ROOTFS/module.BOARD_ENVIRONMENT).read_text()
        for line in ('LD_PRELOAD=/usr/lib/evil.so', 'PATH=/tmp', 'CM0_POWER_SUPPLY_DIR=relative/path',
            'CM0_POWER_SUPPLY_DIR=/run/../etc', 'NES_TOUCH_DEVICE=/dev/input/event1 extra', 'export CM0_X=/dev/x'):
            with self.subTest(line=line):
                self.put(module.BOARD_ENVIRONMENT, original + line + '\n')
                with self.assertRaisesRegex(ValueError, 'may only set device paths'): module.check(self.root)
        (self.root/module.BOARD_ENVIRONMENT).unlink()
        with self.assertRaisesRegex(ValueError, 'environment file is missing'): module.check(self.root)

if __name__ == '__main__': unittest.main()
