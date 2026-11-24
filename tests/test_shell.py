"""Exercise shell boundaries with fake commands; no Docker/root/network needed."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
BASH = "/bin/bash"


class ShellBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="canaan-shell-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bsp = self.directory / "bsp with spaces"
        (self.bsp / "target").mkdir(parents=True)
        self.deploy = self.directory / "deployment files"
        self.config = self.directory / "local config"
        self.call_log = self.directory / "calls.jsonl"
        self.fake_bin = self.directory / "bin"
        self.fake_bin.mkdir()
        self.docker = self.fake_bin / "docker"
        self.docker.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALL_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['image', 'inspect'] and os.environ.get('TEST_NO_BSP_CACHE') == '1':\n"
            "    sys.exit(1)\n"
            "if sys.argv[1] == 'logs':\n"
            "    if os.environ.get('TEST_LOGS_UNAVAILABLE') == '1':\n"
            "        print('Cannot connect to the Docker daemon', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    print('mock build diagnostic')\n"
            "if sys.argv[1] == 'run':\n"
            "    print('mock build diagnostic')\n"
            "    print('mock stderr diagnostic', file=sys.stderr)\n"
            "    if any(arg.endswith('-bsp') for arg in sys.argv):\n"
            "        sys.exit(int(os.environ.get('TEST_BSP_RUN_EXIT', '0')))\n"
            "    sys.exit(int(os.environ.get('TEST_RUN_EXIT', '0')))\n"
        )
        self.docker.chmod(0o755)
        self.environment = os.environ.copy()
        for name in ("BSP_IMAGE", "BSP_KERNEL_CONFIG", "SSH_AUTHORIZED_KEYS_FILE",
                     "LILYGO_UI_INSTALL", "LILYGO_UI_APT_URL", "LILYGO_UI_PACKAGES",
                     "LILYGO_UI_APT_KEY_FILE", "LILYGO_UI_DEB_DIR", "ROOTFS_OVERLAY",
                     "LILYGO_DTOVERLAYS_DIR", "CANAAN_CONTAINER_DTOVERLAYS_DIR"):
            self.environment.pop(name, None)
        self.environment.update({"DOCKER": str(self.docker), "TEST_CALL_LOG": str(self.call_log),
                                 "CONTAINER_NAME": "canaan-unit-test", "DOCKER_IMAGE": "canaan-gen:trixie"})
        self.write_config()

    def write_config(self, extra=""):
        self.config.write_text(
            f"BSP_DIR={shlex.quote(str(self.bsp))}\n"
            f"DEPLOY_DIR={shlex.quote(str(self.deploy))}\n" + extra
        )

    def run_wrapper(self, *arguments, status=0):
        result = subprocess.run([BASH, str(PROJECT / "build-docker.sh"),
                                 "-c", str(self.config), *arguments],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        return [json.loads(line) for line in self.call_log.read_text().splitlines()]

    def test_default_docker_build_works_without_optional_flags(self):
        # macOS /bin/bash 3.2 raises nounset on an empty "${array[@]}".
        calls = self.run_wrapper()
        run = next(call for call in calls if call[0] == "run")
        self.assertEqual(run[-3:], ["canaan-gen:trixie", "-c", "/config"])
        self.assertTrue(any(call[0] == "cp" for call in calls))
        self.assertIn("mock build diagnostic", (self.deploy / "canaan-unit-test.log").read_text())

    def test_default_board_checkout_is_read_only_sibling(self):
        calls = self.run_wrapper('--check')
        run = next(call for call in calls if call[0] == 'run')
        sibling = PROJECT.parent / 'lilygo-linux-dtoverlays'
        self.assertIn(f'type=bind,src={sibling},dst=/inputs/lilygo-linux-dtoverlays,readonly', run)
        self.assertIn('CANAAN_CONTAINER_DTOVERLAYS_DIR=/inputs/lilygo-linux-dtoverlays', run)

    def test_explicit_board_checkout_with_spaces_is_mapped_as_one_argument(self):
        checkout = self.directory / 'custom board checkout'
        board = checkout / 'boards/T-Display-K230'
        board.mkdir(parents=True)
        (board / 'profile.json').write_text('{}')
        self.write_config(f'LILYGO_DTOVERLAYS_DIR={shlex.quote(str(checkout))}\n')
        calls = self.run_wrapper('--check')
        run = next(call for call in calls if call[0] == 'run')
        self.assertIn(f'type=bind,src={checkout},dst=/inputs/lilygo-linux-dtoverlays,readonly', run)
        self.assertIn('CANAAN_CONTAINER_DTOVERLAYS_DIR=/inputs/lilygo-linux-dtoverlays', run)
        self.assertFalse(any(str(PROJECT.parent / 'lilygo-linux-dtoverlays') in arg for arg in run))

    def test_missing_board_checkout_fails_before_any_docker_call(self):
        checkout = self.directory / 'missing board checkout'
        for exists in (False, True):
            with self.subTest(directory_exists=exists):
                if exists:
                    checkout.mkdir()
                self.write_config(f'LILYGO_DTOVERLAYS_DIR={shlex.quote(str(checkout))}\n')
                result = subprocess.run([BASH, str(PROJECT / 'build-docker.sh'),
                                         '-c', str(self.config), '--check'],
                                        env=self.environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Set LILYGO_DTOVERLAYS_DIR', result.stderr)
                self.assertFalse(self.call_log.exists(), 'must not even query Docker before rejecting missing board input')

    def test_check_is_forwarded_and_does_not_copy_outputs(self):
        calls = self.run_wrapper("--check")
        run = next(call for call in calls if call[0] == "run")
        self.assertEqual(run[-3:], ["-c", "/config", "--check"])
        self.assertFalse(any(call[0] == "cp" for call in calls))

    def test_rootfs_only_is_forwarded(self):
        calls = self.run_wrapper("--rootfs-only")
        run = next(call for call in calls if call[0] == "run")
        self.assertEqual(run[-3:], ["-c", "/config", "--rootfs-only"])

    def test_failed_build_preserves_status_and_logs_without_export(self):
        self.environment["TEST_RUN_EXIT"] = "17"
        calls = self.run_wrapper(status=17)
        self.assertFalse(any(call[0] == "cp" for call in calls))
        self.assertTrue((self.deploy / "canaan-unit-test.log").is_file())

    def test_daemon_failure_keeps_run_output_when_logs_are_unavailable(self):
        self.environment['TEST_RUN_EXIT'] = '125'
        self.environment['TEST_LOGS_UNAVAILABLE'] = '1'
        calls = self.run_wrapper(status=125)
        saved = (self.deploy / 'canaan-unit-test.log').read_text()
        self.assertIn('mock build diagnostic', saved)
        self.assertIn('mock stderr diagnostic', saved)
        self.assertNotIn('Cannot connect', saved)
        self.assertFalse(any(call[0] == 'logs' for call in calls))
        self.assertFalse(any(call[0] == 'cp' for call in calls))

    def test_source_mode_builds_amd64_bsp_before_debian_and_mounts_cache_read_only(self):
        self.write_config('BSP_MODE=source\nBSP_CACHE_VOLUME=canaan-test-cache\n')
        calls = self.run_wrapper('--check')
        runs = [call for call in calls if call[0] == 'run']
        self.assertEqual(len(runs), 2)
        bsp, debian = runs
        self.assertIn('linux/amd64', bsp)
        self.assertIn('check', bsp)
        self.assertIn('type=volume,src=canaan-test-cache,dst=/bsp-work', bsp)
        self.assertIn('type=volume,src=canaan-test-cache-toolchain,dst=/opt/toolchain', bsp)
        self.assertNotIn('--privileged', bsp)
        self.assertIn('type=volume,src=canaan-test-cache,dst=/bsp-work,readonly', debian)
        self.assertNotIn(f'type=bind,src={self.bsp},dst=/bsp,readonly', debian)
        self.assertTrue(any(arg.startswith('CANAAN_CONTAINER_BSP_DIR=/bsp-work/builds/') for arg in debian))

    def test_failed_source_build_stops_before_debian_or_export(self):
        self.write_config('BSP_MODE=source\n')
        self.environment['TEST_BSP_RUN_EXIT'] = '29'
        calls = self.run_wrapper('--check', status=29)
        self.assertEqual(len([call for call in calls if call[0] == 'run']), 1)
        self.assertFalse(any(call[0] == 'cp' for call in calls))
        self.assertFalse(any(call[:2] == ['build', '-t'] for call in calls))

    def test_bsp_only_never_starts_debian_packaging(self):
        self.write_config('BSP_MODE=source\n')
        calls = self.run_wrapper('--check', '--bsp-only')
        self.assertEqual(len([call for call in calls if call[0] == 'run']), 1)
        self.assertFalse(any(call[0] == 'cp' for call in calls))

    def test_source_mode_rejects_unrelated_template_before_docker(self):
        self.write_config('BSP_MODE=source\nBSP_IMAGE=/old/vendor.img\n')
        result = subprocess.run([BASH, str(PROJECT / 'build-docker.sh'), '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('matching Image/config/template', result.stderr)
        self.assertFalse(self.call_log.exists())

    def test_source_plan_does_not_access_docker_or_require_completed_bsp(self):
        self.write_config('BSP_MODE=source\nBSP_DIR=/does/not/exist\n')
        result = subprocess.run([BASH, str(PROJECT / 'build-docker.sh'), '-c', str(self.config), '--plan-bsp'],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['recipe']['defconfig'], 'k230_canmv_t_display_rm69a10_defconfig')
        self.assertFalse(self.call_log.exists())

    def prepare_source_space_check(self):
        self.write_config('BSP_MODE=source\n')
        uname = self.fake_bin / 'uname'
        uname.write_text('#!/bin/sh\nprintf "Darwin\\n"\n')
        uname.chmod(0o755)
        python = self.fake_bin / 'python3'
        python.write_text(
            '#!' + sys.executable + '\n'
            'import json, os, sys\n'
            "if len(sys.argv) > 2 and sys.argv[2] == 'space':\n"
            "    with open(os.environ['TEST_CALL_LOG'], 'a') as stream:\n"
            "        stream.write(json.dumps(['space-check'] + sys.argv[3:]) + '\\n')\n"
            "    sys.exit(int(os.environ.get('TEST_SPACE_EXIT', '0')))\n"
            f'os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])\n')
        python.chmod(0o755)
        self.environment['PATH'] = str(self.fake_bin) + os.pathsep + self.environment['PATH']

    def test_completed_source_cache_uses_only_debian_space_budget(self):
        self.prepare_source_space_check()
        calls = self.run_wrapper()
        space = next(call for call in calls if call[0] == 'space-check')
        self.assertEqual(space[-2:], ['--min-free-gib', '15'])
        probe = next(call for call in calls if '/usr/bin/test' in call)
        self.assertIn('--pull=never', probe)
        self.assertTrue(any('dst=/bsp-work,readonly' in arg for arg in probe))
        self.assertTrue(any(call[0] == 'cp' for call in calls))

    def test_uncached_source_space_failure_prevents_download_and_compile(self):
        self.prepare_source_space_check()
        self.environment.update(TEST_NO_BSP_CACHE='1', TEST_SPACE_EXIT='7')
        calls = self.run_wrapper(status=7)
        space = next(call for call in calls if call[0] == 'space-check')
        self.assertEqual(space[-2:], ['--min-free-gib', '60'])
        self.assertFalse(any(call[0] in ('build', 'run', 'cp') for call in calls))

    def test_config_bsp_and_optional_inputs_are_read_only(self):
        keys = self.directory / "authorized keys"
        keys.write_text("test fixture\n")
        overlay = self.directory / "overlay files"
        overlay.mkdir()
        self.write_config(f"SSH_AUTHORIZED_KEYS_FILE={shlex.quote(str(keys))}\n"
                          f"ROOTFS_OVERLAY={shlex.quote(str(overlay))}\n")
        calls = self.run_wrapper()
        run = next(call for call in calls if call[0] == "run")
        mounts = [run[index + 1] for index, value in enumerate(run) if value == "--mount"]
        binds = [mount for mount in mounts if mount.startswith("type=bind,")]
        self.assertEqual(len(binds), 5)
        self.assertFalse(any("dst=/src" in mount for mount in mounts))
        self.assertTrue(all(mount.endswith(",readonly") for mount in binds))
        self.assertIn(f"type=bind,src={self.bsp},dst=/bsp,readonly", binds)
        self.assertIn(f"type=bind,src={keys},dst=/inputs/authorized_keys,readonly", binds)
        self.assertIn("type=volume,dst=/work", mounts)

    def test_apt_does_not_mount_or_require_a_stale_local_deb_directory(self):
        stale = self.directory / "removed local packages"
        self.write_config("LILYGO_UI_INSTALL=apt\n"
                          f"LILYGO_UI_DEB_DIR={shlex.quote(str(stale))}\n")
        calls = self.run_wrapper('--check')
        run = next(call for call in calls if call[0] == 'run')
        self.assertFalse(any(str(stale) in arg for arg in run))
        self.assertFalse(any('dst=/inputs/debs' in arg for arg in run))
        self.assertFalse(any('CANAAN_CONTAINER_DEB_DIR=' in arg for arg in run))

    def test_explicit_apt_key_is_mapped_read_only(self):
        key = self.directory / 'reviewed archive key.gpg'
        key.write_bytes(b'public key fixture')
        self.write_config("LILYGO_UI_INSTALL=apt\n"
                          f"LILYGO_UI_APT_KEY_FILE={shlex.quote(str(key))}\n")
        calls = self.run_wrapper('--check')
        run = next(call for call in calls if call[0] == 'run')
        self.assertIn(f'type=bind,src={key},dst=/inputs/archive-keyring.gpg,readonly', run)
        self.assertIn('CANAAN_CONTAINER_APT_KEY=/inputs/archive-keyring.gpg', run)

    def test_local_mode_is_rejected_before_querying_docker(self):
        self.write_config('LILYGO_UI_INSTALL=local\n')
        result = subprocess.run([BASH, str(PROJECT / 'build-docker.sh'),
                                 '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local', result.stderr)
        self.assertIn('apt', result.stderr)
        self.assertFalse(self.call_log.exists(), 'local mode must fail before any Docker action')

    def prepare_apt_preflight(self, extra=''):
        kernel = self.bsp / 'build/linux-test/.config'
        kernel.parent.mkdir(parents=True)
        kernel.write_text('kernel configuration fixture\n')
        key = self.directory / 'reviewed archive key.gpg'
        key.write_bytes(b'public key fixture')
        self.work = self.directory / 'uncreated build work'
        self.write_config('ACCOUNT_SETUP=preset\nENABLE_SSH=0\n'
                          'LILYGO_UI_INSTALL=apt\n'
                          'LILYGO_UI_APT_URL=https://packages.example.test/repository\n'
                          f'LILYGO_UI_APT_KEY_FILE={shlex.quote(str(key))}\n'
                          f'WORK_DIR={shlex.quote(str(self.work))}\n' + extra)
        python = self.fake_bin / 'python3'
        python.write_text(
            '#!' + sys.executable + '\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            "with open(os.environ['TEST_CALL_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if Path(sys.argv[1]).name == 'check-apt.py':\n"
            "    code = int(os.environ.get('TEST_APT_EXIT', '0'))\n"
            "    if code: print('APT package preflight fixture failed', file=sys.stderr)\n"
            '    sys.exit(code)\n'
        )
        python.chmod(0o755)
        self.environment['PATH'] = str(self.fake_bin) + os.pathsep + self.environment['PATH']
        return key

    def test_native_check_validates_the_selected_repository_key_and_packages(self):
        packages = 'lilygo-ui-launcher lilygo-ui-settings=0.1.2'
        key = self.prepare_apt_preflight(f'LILYGO_UI_PACKAGES={shlex.quote(packages)}\n')
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.call_log.read_text().splitlines()]
        apt = next(call for call in calls if Path(call[0]).name == 'check-apt.py')
        self.assertEqual(apt[apt.index('--url') + 1], 'https://packages.example.test/repository')
        self.assertEqual(apt[apt.index('--key') + 1], str(key))
        self.assertEqual(apt[apt.index('--packages') + 1].split(), packages.split())
        self.assertFalse(self.work.exists())

    def test_native_default_package_selection_excludes_unpublished_imu(self):
        self.prepare_apt_preflight()
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.call_log.read_text().splitlines()]
        apt = next(call for call in calls if Path(call[0]).name == 'check-apt.py')
        selected = set(apt[apt.index('--packages') + 1].split())
        self.assertLessEqual({'lilygo-ui-appkit-dev', 'lilygo-ui-launcher', 'lilygo-ui-store', 'lilygo-ui-settings',
                             'lilygo-ui-calculator', 'lilygo-ui-camera', 'lilygo-ui-cellular',
                             'lilygo-ui-meshtastic', 'lilygo-ui-monitor', 'lilygo-ui-music',
                             'lilygo-ui-radio', 'lilygo-ui-sensors', 'lilygo-ui-terminal',
                             'lilygo-ui-xiaozhi', 'lilygo-ui-youtube'}, selected)
        self.assertNotIn('lilygo-ui-imu', selected)
        self.assertNotIn('lilygo-ui-mesa-vc4', selected)

    def test_missing_apt_packages_stop_before_creating_a_rootfs(self):
        self.prepare_apt_preflight()
        self.environment['TEST_APT_EXIT'] = '23'
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config)],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn('APT package preflight fixture failed', result.stderr)
        self.assertFalse(self.work.exists(), 'failed APT preflight must not create build/rootfs state')

    def test_launcher_without_selected_package_stops_before_preflight_or_rootfs(self):
        self.prepare_apt_preflight('ACCOUNT_SETUP=launcher\n')
        cases = ('lilygo-ui-appkit-dev lilygo-ui-settings',
                 'lilygo-ui-launcher-extra', 'other-lilygo-ui-launcher',
                 'lilygo-ui-launcher=')
        for packages in cases:
            for flags in ((), ('--check',)):
                with self.subTest(packages=packages, flags=flags):
                    environment = self.environment.copy()
                    environment['LILYGO_UI_PACKAGES'] = packages
                    result = subprocess.run([BASH, str(PROJECT / 'build.sh'),
                                             '-c', str(self.config), *flags],
                                            env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('ACCOUNT_SETUP=launcher requires lilygo-ui-launcher', result.stderr)
                    self.assertIn('ACCOUNT_SETUP=preset', result.stderr)
                    self.assertFalse(self.call_log.exists(), 'configuration conflict must stop before BSP/APT checks')
                    self.assertFalse(self.work.exists(), 'configuration conflict must not create a rootfs')

    def test_launcher_package_selection_accepts_exact_and_versioned_requests(self):
        self.prepare_apt_preflight('ACCOUNT_SETUP=launcher\n')
        for packages in ('lilygo-ui-launcher',
                         'lilygo-ui-appkit-dev\nlilygo-ui-launcher=1:0.1.3~rc1-2\tlilygo-ui-store'):
            with self.subTest(packages=packages):
                environment = self.environment.copy()
                environment['LILYGO_UI_PACKAGES'] = packages
                result = subprocess.run([BASH, str(PROJECT / 'build.sh'),
                                         '-c', str(self.config), '--check'],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = [json.loads(line) for line in self.call_log.read_text().splitlines()]
                apt = next(call for call in reversed(calls) if Path(call[0]).name == 'check-apt.py')
                self.assertEqual(apt[apt.index('--packages') + 1].split(), packages.split())
                self.assertFalse(self.work.exists())

    def test_selected_but_unpublished_launcher_still_fails_apt_preflight(self):
        self.prepare_apt_preflight('ACCOUNT_SETUP=launcher\nLILYGO_UI_PACKAGES=lilygo-ui-launcher\n')
        self.environment['TEST_APT_EXIT'] = '23'
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config)],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn('APT package preflight fixture failed', result.stderr)
        self.assertFalse(self.work.exists())

    def test_native_build_also_rejects_local_mode(self):
        self.prepare_apt_preflight('LILYGO_UI_INSTALL=local\n')
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local', result.stderr)
        self.assertIn('apt', result.stderr)
        self.assertFalse(self.call_log.exists())

    def test_base_image_mode_skips_apt_preflight(self):
        self.prepare_apt_preflight('LILYGO_UI_INSTALL=0\nACCOUNT_SETUP=preset\n')
        self.environment['TEST_APT_EXIT'] = '23'
        result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config), '--check'],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.call_log.read_text().splitlines()]
        self.assertFalse(any(Path(call[0]).name == 'check-apt.py' for call in calls))
        self.assertFalse(self.work.exists())

    def test_package_manifest_preserves_dpkg_format_variables(self):
        source = (PROJECT / "build.sh").read_text()
        match = re.search(r"on_chroot[^\n]*packages\.tsv[^\n]*<<'CHROOT'\n(.*?)\nCHROOT", source, re.S)
        self.assertIsNotNone(match, "package manifest must use an isolated chroot script")
        dpkg = self.fake_bin / "dpkg-query"
        dpkg.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALL_LOG'], 'w') as stream:\n"
            "    json.dump(sys.argv[1:], stream)\n"
            "print('bash\\t5.2\\triscv64')\n"
        )
        dpkg.chmod(0o755)
        self.environment["PATH"] = str(self.fake_bin) + os.pathsep + self.environment["PATH"]
        for name in ("Package", "Version", "Architecture"):
            self.environment.pop(name, None)
        result = subprocess.run([BASH, "-e", "-u", "-o", "pipefail", "-s"],
                                input=match.group(1), env=self.environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.call_log.read_text()),
                         ["-W", "-f=${Package}\\t${Version}\\t${Architecture}\\n"])
        self.assertEqual(result.stdout, "bash\t5.2\triscv64\n")

    def test_ssh_preflight_account_modes(self):
        kernel = self.bsp / 'build/linux-test/.config'
        kernel.parent.mkdir(parents=True)
        kernel.touch()
        # BSP/image validation is unrelated to account policy; record that the
        # preflight reached those checks without needing a real BSP image.
        python = self.fake_bin / 'python3'
        python.write_text('#!/bin/sh\nprintf "BSP check reached\\n"\n')
        python.chmod(0o755)
        self.environment['PATH'] = str(self.fake_bin) + os.pathsep + self.environment['PATH']
        cases = (
            ('launcher', '1', '', '', None),
            ('launcher', '0', '', '', None),
            ('launcher', '1', '$6$test$fixture', '', 'leave password/key unset'),
            ('launcher', '1', '', str(kernel), 'leave password/key unset'),
            ('preset', '1', '', '', 'requires an SSH authorized_keys file or password hash'),
            ('preset', '1', '$6$test$fixture', '', None),
            ('preset', '1', '', str(kernel), None),
            ('preset', '0', '', '', None),
        )
        kernel.write_text('test fixture\n')
        apt_key = self.directory / 'archive-keyring.gpg'
        apt_key.write_bytes(b'public key fixture')
        for mode, ssh, password, keys, error in cases:
            with self.subTest(mode=mode, ssh=ssh, password=bool(password), keys=bool(keys)):
                self.write_config(
                    f'ACCOUNT_SETUP={mode}\nENABLE_SSH={ssh}\n'
                    f'FIRST_USER_PASSWORD_HASH={shlex.quote(password)}\n'
                    f'SSH_AUTHORIZED_KEYS_FILE={shlex.quote(keys)}\n'
                    'LILYGO_UI_INSTALL=apt\n'
                    'LILYGO_UI_PACKAGES=lilygo-ui-launcher\n'
                    'LILYGO_UI_APT_URL=https://packages.example.test/repository\n'
                    f'LILYGO_UI_APT_KEY_FILE={shlex.quote(str(apt_key))}\n'
                )
                result = subprocess.run([BASH, str(PROJECT / 'build.sh'), '-c', str(self.config), '--check'],
                                        env=self.environment, capture_output=True, text=True)
                if error:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
                    self.assertNotIn('BSP check reached', result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('BSP check reached', result.stdout)

    def test_mount_refuses_symlink_runtime_directories_before_mounting(self):
        rootfs = self.directory / "rootfs"
        rootfs.mkdir()
        sentinel = self.directory / "must-not-mount"
        for point in ("dev", "proc", "sys", "run", "tmp"):
            with self.subTest(point=point):
                link = rootfs / point
                link.symlink_to(self.directory / "missing-target")
                script = """
set -euo pipefail
source "$1"
ROOTFS_DIR="$2"
MOUNT_SENTINEL="$3"
mount() { touch "$MOUNT_SENTINEL"; }
mount_rootfs
"""
                result = subprocess.run([BASH, "-s", "--", str(PROJECT / "scripts/common"),
                                         str(rootfs), str(sentinel)], input=script,
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing symlink mount point", result.stderr)
                self.assertFalse(sentinel.exists())
                link.unlink()

    def run_overlay(self, overlay, unmount_status=0, rsync_status=0):
        event_log = self.directory / "overlay-events"
        event_log.unlink(missing_ok=True)
        rootfs = self.directory / "overlay-rootfs"
        rootfs.mkdir(exist_ok=True)
        script = """
set -euo pipefail
source "$1"
ROOTFS_OVERLAY="$2"
ROOTFS_DIR="$3"
EVENT_LOG="$4"
UNMOUNT_STATUS="$5"
RSYNC_STATUS="$6"
unmount_rootfs() { printf 'unmount\\n' >> "$EVENT_LOG"; return "$UNMOUNT_STATUS"; }
rsync() { printf 'rsync %s %s %s %s\\n' "$@" >> "$EVENT_LOG"; return "$RSYNC_STATUS"; }
mount_rootfs() { printf 'mount\\n' >> "$EVENT_LOG"; }
apply_rootfs_overlay
"""
        result = subprocess.run([BASH, "-s", "--", str(PROJECT / "scripts/common"),
                                 str(overlay), str(rootfs), str(event_log),
                                 str(unmount_status), str(rsync_status)],
                                input=script, capture_output=True, text=True)
        events = event_log.read_text().splitlines() if event_log.exists() else []
        return result, events

    def test_overlay_unmounts_before_copy_and_remounts_afterwards(self):
        overlay = self.directory / "overlay"
        overlay.mkdir()
        result, events = self.run_overlay(overlay)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["unmount",
                                  f"rsync -aH --safe-links {overlay}/ {self.directory}/overlay-rootfs/",
                                  "mount"])

    def test_overlay_rejects_runtime_directories_and_dangling_symlinks(self):
        overlay = self.directory / "overlay"
        overlay.mkdir()
        for point in ("dev", "proc", "sys", "run", "tmp"):
            for kind in ("directory", "dangling symlink"):
                with self.subTest(point=point, kind=kind):
                    path = overlay / point
                    if kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(overlay / "absent")
                    result, events = self.run_overlay(overlay)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("must not contain the runtime path", result.stderr)
                    self.assertEqual(events, [])
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()

    def test_overlay_never_copies_if_unmount_fails(self):
        overlay = self.directory / "overlay"
        overlay.mkdir()
        result, events = self.run_overlay(overlay, unmount_status=37)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(events, ["unmount"])

    def test_overlay_copy_failure_aborts_before_remount(self):
        overlay = self.directory / "overlay"
        overlay.mkdir()
        result, events = self.run_overlay(overlay, rsync_status=38)
        self.assertEqual(result.returncode, 38)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], "unmount")
        self.assertTrue(events[1].startswith("rsync "))

    def test_absent_overlay_performs_no_mount_or_copy(self):
        result, events = self.run_overlay("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [])


class BoardStageTests(unittest.TestCase):
    """The stage scripts, board profile and shared board payload must agree."""

    BOARD_ROOTFS = PROJECT.parent / "lilygo-linux-dtoverlays/boards/T-Display-K230/debian/rootfs"

    def board_variable(self, name):
        script = f'source "$1" && printf %s "${name}"'
        return subprocess.run([BASH, "-c", script, "bash",
            str(PROJECT / "boards/t-display-k230/board.conf")],
            capture_output=True, text=True, check=True).stdout

    def test_modules_loaded_at_boot_are_the_ones_preflight_requires(self):
        script = (PROJECT / "scripts/check-bsp.py").read_text()
        required = re.search(r"^REQUIRED_MODULES = \((.*)\)$", script, re.M).group(1)
        self.assertEqual(self.board_variable("{BSP_MODULES}").split(),
            re.findall(r"'([^']+)'", required))
        # The remainder of the vendor image's boot-time modprobe list.
        self.assertEqual(self.board_variable("{BSP_OPTIONAL_MODULES}").split(),
            ["aic_load_fw", "aic8800_fdrv", "aic_btusb"])

    def test_both_account_modes_create_every_group_used_by_the_board_udev_rules(self):
        rules = (self.BOARD_ROOTFS / "etc/udev/rules.d/70-t-display-k230.rules").read_text()
        needed = set(re.findall(r'GROUP="([^"]+)"', rules)) | set(re.findall(r"chgrp (\S+)", rules))
        stage = (PROJECT / "stage1/00-run.sh").read_text()
        loops = re.findall(r"^for group in (.+); do$", stage, re.M)
        self.assertEqual(len(loops), 2)
        for groups in loops:
            self.assertLessEqual(needed, set(groups.split()))
            self.assertIn("gpio", groups.split())

    def test_stage2_takes_services_and_helpers_from_the_board_profile(self):
        stage = (PROJECT / "stage2/00-run.sh").read_text()
        self.assertIn("--list services", stage)
        self.assertIn("--list build", stage)
        self.assertNotIn("70-lilygo-k230-access.rules", stage)
        self.assertFalse((PROJECT / "stage2/70-lilygo-k230-access.rules").exists())
        # Realtime throttling is only relaxed together with the vendor ISP daemon.
        camera = stage.split('if [[ "${BSP_CAMERA_RUNTIME:-0}" == 1 ]]; then', 2)[2].split("\nfi\n", 1)[0]
        self.assertIn("kernel.sched_rt_runtime_us = -1", camera)
        self.assertEqual(stage.count("sched_rt_runtime_us"), 1)

    def test_base_packages_cover_the_board_runtime(self):
        packages = (PROJECT / "stage1/packages").read_text().split()
        profile = json.loads((self.BOARD_ROOTFS.parents[1] / "profile.json").read_text())
        for package in profile["runtime"]["packages"]:
            self.assertIn(package, packages)
        # The register helper is compiled inside the image.
        self.assertIn("build-essential", packages)

    def test_login_profile_adds_administrative_binary_directories(self):
        packages = (PROJECT / "stage1/packages").read_text().split()
        self.assertIn("net-tools", packages)
        script_path = PROJECT / "stage1/files/etc/profile.d/10-canaan-gen-path.sh"
        stage = (PROJECT / "stage1/00-run.sh").read_text()
        self.assertIn("stage1/files/etc/profile.d/10-canaan-gen-path.sh", stage)
        self.assertTrue(script_path.is_file())
        script = script_path.read_text()
        self.assertIn("/usr/sbin", script)
        self.assertIn("/sbin", script)

        cases = {
            "": "/usr/sbin:/sbin",
            "/usr/bin:/bin": "/usr/bin:/bin:/usr/sbin:/sbin",
            "/usr/sbin:/usr/bin:/sbin:/bin": "/usr/sbin:/usr/bin:/sbin:/bin",
        }
        for initial, expected in cases.items():
            with self.subTest(initial=initial):
                result = subprocess.run(
                    [BASH, "-c", 'PATH="$1"; export PATH; . "$2"; printf "%s" "$PATH"',
                     "bash", initial, str(script_path)],
                    capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
