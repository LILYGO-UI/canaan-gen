#!/usr/bin/env python3
"""Pinned BSP inputs, safe cache reuse and interrupted Buildroot recovery.

The expensive Git/toolchain/Buildroot steps are replaced by command doubles.
Planning, snapshots, payload hashes and success-record handling run unchanged.
These tests do not claim to compile a kernel or create a bootable image.
"""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build_bsp_source', PROJECT / 'scripts/build-bsp.py')
BSP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BSP)


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / 'board'
        (self.board / 'scripts').mkdir(parents=True)
        self.metadata = {
            'repository': 'https://example.invalid/board', 'commit': 'a' * 40,
            'sdk_repository': 'https://example.invalid/sdk', 'sdk_commit': 'b' * 40,
            'kernel_repository': 'https://example.invalid/linux', 'kernel_commit': 'c' * 40,
        }
        (self.board / 'metadata.json').write_text(json.dumps(self.metadata))
        (self.board / 'scripts/stage-sdk.py').write_text(
            'import json\n'
            'from pathlib import Path\n'
            'def read_sources(board):\n'
            '    return json.loads((Path(board) / "metadata.json").read_text())\n'
        )
        (self.board / 'profile.json').write_text('{"board": "t-display-k230"}\n')
        self.work = self.root / 'work'

    def plan(self):
        return BSP.plan(self.board, self.work)


class SourcePlanning(SourceFixture):
    def test_plan_pins_repositories_commits_and_defconfig(self):
        result = self.plan()
        recipe = result['recipe']
        self.assertEqual(recipe['board_commit'], self.metadata['commit'])
        self.assertEqual(recipe['sdk_commit'], self.metadata['sdk_commit'])
        self.assertEqual(recipe['kernel_commit'], self.metadata['kernel_commit'])
        self.assertEqual(recipe['defconfig'], BSP.DEFCONFIG)
        self.assertEqual(Path(result['bsp_dir']),
                         Path(result['generation']) / 'sdk/output' / BSP.DEFCONFIG)
        self.assertIn('scripts/stage-sdk.py', recipe['board_files_sha256'])
        self.assertEqual(len(recipe['builder_sha256']), 64)

    def test_source_change_selects_new_generation(self):
        before = self.plan()
        (self.board / 'profile.json').write_text('{"board":"t-display-k230","keyboard":true}\n')
        self.assertNotEqual(before['key'], self.plan()['key'])

    def test_invalid_peripheral_contract_fails_before_any_source_work(self):
        (self.board / 'profile.json').write_text('{"board":"t-display-k230","peripherals":{}}\n')
        with mock.patch.object(BSP, 'checkout') as checkout:
            with self.assertRaisesRegex(ValueError, 'invalid board profile.*peripherals'):
                BSP.build_bsp(self.board, self.work, 2, 60)
        checkout.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_checkout_location_does_not_change_recipe(self):
        other = self.root / 'another-board'
        shutil.copytree(self.board, other)
        self.assertEqual(self.plan()['recipe'], BSP.plan(other, self.work)['recipe'])
        self.assertEqual(self.plan()['key'], BSP.plan(other, self.work)['key'])

    def test_python_cache_and_previous_provenance_are_not_build_inputs(self):
        before = self.plan()
        cache = self.board / 'scripts/__pycache__'
        cache.mkdir(exist_ok=True)
        (cache / 'unrelated.pyc').write_bytes(b'cache')
        (self.board / 'SOURCE-PROVENANCE.json').write_text('{"old":true}')
        self.assertEqual(before['key'], self.plan()['key'])

    def test_abbreviated_or_moving_commit_is_rejected(self):
        for name in ('commit', 'sdk_commit', 'kernel_commit'):
            with self.subTest(name=name):
                changed = dict(self.metadata, **{name: 'main'})
                (self.board / 'metadata.json').write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'complete Git commit'):
                    self.plan()

    def test_insufficient_space_stops_before_checkout_or_cache_creation(self):
        with (mock.patch.object(BSP.platform, 'system', return_value='Linux'),
              mock.patch.object(BSP.platform, 'machine', return_value='x86_64'),
              mock.patch.object(BSP.shutil, 'disk_usage', return_value=SimpleNamespace(free=7 * 1024 ** 3)),
              mock.patch.object(BSP, 'checkout') as checkout):
            with self.assertRaisesRegex(ValueError, 'no sources have been downloaded'):
                BSP.build_bsp(self.board, self.work, 2, 60)
        checkout.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_native_non_x86_linux_is_rejected_before_download(self):
        with (mock.patch.object(BSP.platform, 'system', return_value='Linux'),
              mock.patch.object(BSP.platform, 'machine', return_value='aarch64'),
              mock.patch.object(BSP, 'checkout') as checkout):
            with self.assertRaisesRegex(ValueError, 'x86_64 Linux'):
                BSP.build_bsp(self.board, self.work, 2, 60)
        checkout.assert_not_called()


class SourceBuildRecovery(SourceFixture):
    PRUNED_SOURCES = (
        'buildroot-overlay/board/canaan/k230-soc/rootfs_overlay/etc/init.d/S00resizemmc',
        'buildroot-overlay/board/canaan/k230-soc/rootfs_overlay/first_boot_flag',
    )

    def setUp(self):
        super().setUp()
        self.commands = []
        self.checkouts = []
        self.fail_step = None
        self.build = self.plan()
        self.bsp = Path(self.build['bsp_dir'])
        self.generation = Path(self.build['generation'])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, attribute, value in (
            (BSP.platform, 'system', 'Linux'),
            (BSP.platform, 'machine', 'x86_64'),
            (BSP, 'check_space', 80),
            (BSP, 'toolchain_ready', True),
        ):
            self.stack.enter_context(mock.patch.object(target, attribute, return_value=value))
        self.stack.enter_context(mock.patch.object(BSP, 'checkout', side_effect=self.checkout))
        self.stack.enter_context(mock.patch.object(BSP, 'require_clean_checkout'))
        self.stack.enter_context(mock.patch.object(BSP, 'run', side_effect=self.run_command))
        self.validation = self.stack.enter_context(
            mock.patch.object(BSP, 'validate_bsp', return_value={'kernel_release': '6.6.36'}))

    def checkout(self, repository, commit, path, sparse=None):
        self.checkouts.append((repository, commit, sparse))
        path.mkdir(parents=True, exist_ok=True)
        if sparse:
            script = path / 'k230_bsp/scripts/apply.sh'
            script.parent.mkdir(parents=True, exist_ok=True)
            if not script.exists():
                script.write_text('#!/bin/sh\n')
        else:
            if not (path / '.git').exists():
                subprocess.run(['git', 'init', '--quiet', str(path)], check=True)
                (path / '.gitignore').write_text('/output\n/dl\n.last_conf\n')
                (path / 'Makefile').write_text('all:\n\ttrue\n')
                for name in self.PRUNED_SOURCES:
                    source = path / name
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text('upstream first-boot resize fixture\n')
                subprocess.run(['git', '-C', str(path), 'add', '.gitignore', 'Makefile',
                                *self.PRUNED_SOURCES], check=True)
            config = path / 'buildroot-overlay/configs' / BSP.DEFCONFIG
            config.parent.mkdir(parents=True, exist_ok=True)
            if not config.exists():
                config.write_text('BR2_TOOLCHAIN_EXTERNAL_PATH="/opt/toolchain/test-xuantie/"\n')

    def run_command(self, command, **kwargs):
        command = list(map(str, command))
        self.commands.append(command)
        if command[0] == 'bash':
            step = 'apply'
        elif command[-1] == 'all':
            step = 'compile'
        elif command[-1] == BSP.DEFCONFIG:
            step = 'configure'
        elif any(arg.endswith('stage-sdk.py') for arg in command):
            step = 'stage'
        else:
            step = 'toolchain'
        if step == self.fail_step:
            raise subprocess.CalledProcessError(1, command)
        if step == 'apply':
            # The pinned vendor apply.sh intentionally removes these tracked
            # SDK inputs before the builder saves its prepared fingerprint.
            for name in self.PRUNED_SOURCES:
                (self.generation / 'sdk' / name).unlink(missing_ok=True)
        elif step == 'configure':
            self.bsp.mkdir(parents=True, exist_ok=True)
            (self.bsp / '.config').write_text('BR2_riscv=y\n')
        elif step == 'compile':
            # Small placeholders exercise sealing only, never kernel validation.
            for name, data in {
                'images/sysimage-sdcard.img': b'nonbootable image fixture',
                'images/Image': b'kernel fixture',
                'images/k230-canmv-rm69a10.dtb': b'device-tree fixture',
                'build/linux-test/.config': b'CONFIG_RISCV=y\n',
                'target/lib/modules/6.6.36/tca8418_keypad.ko': b'module fixture',
                'target/lib/firmware/wifi.bin': b'Wi-Fi firmware fixture',
                'target/usr/bin/isp_media_server': b'camera daemon fixture',
                'target/usr/bin/media-ctl': b'media control fixture',
                'target/usr/bin/v4l2-sysfs-path': b'video path fixture',
                'target/usr/lib/libvvcam.so': b'camera library fixture',
                'target/usr/lib/libmxml.so.1.6': b'XML library fixture',
                'target/etc/vvcam/gc2093.xml': b'<camera>original tuning</camera>\n',
            }.items():
                path = self.bsp / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def execute(self):
        return BSP.build_bsp(self.board, self.work, 3, 60)

    def applies(self):
        return [command for command in self.commands if command[0] == 'bash']

    def test_success_records_actual_payload_after_validation(self):
        self.assertEqual(self.execute(), self.bsp)
        saved = json.loads((self.bsp / 'SOURCE-BUILD.json').read_text())
        self.assertEqual(saved['recipe'], self.build['recipe'])
        self.assertEqual(saved['payload_sha256'], BSP.payload_hashes(self.bsp))
        self.assertFalse(saved['hardware_boot_test'])
        self.assertEqual(len(self.applies()), 1)
        self.assertIn('BR2_JLEVEL=3', self.commands[-1])
        self.validation.assert_called_once_with(self.generation / 'board-layer', self.bsp)

    def test_compilation_failure_has_no_success_record_and_resumes_without_reapply(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertTrue((self.generation / 'prepared.json').is_file())
        self.assertFalse((self.bsp / 'SOURCE-BUILD.json').exists())
        self.fail_step = None
        self.execute()
        self.assertEqual(len(self.applies()), 1)
        configure = [command for command in self.commands if command[-1] == BSP.DEFCONFIG]
        self.assertEqual(len(configure), 1)
        self.assertTrue((self.bsp / 'SOURCE-BUILD.json').is_file())

    def test_vendor_pruned_sources_are_recorded_and_resume_without_reapply(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        saved = json.loads((self.generation / 'prepared.json').read_text())
        for name in self.PRUNED_SOURCES:
            self.assertFalse((self.generation / 'sdk' / name).exists())
            self.assertIn(name, saved['sdk_inputs'])
            self.assertIsNone(saved['sdk_inputs'][name])
        self.fail_step = None
        self.assertEqual(self.execute(), self.bsp)
        self.assertEqual(len(self.applies()), 1)

    def test_unexpected_source_deletion_after_preparation_rejects_resume(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        (self.generation / 'sdk/Makefile').unlink()
        self.fail_step = None
        before = list(self.commands)
        with self.assertRaisesRegex(ValueError, 'SDK inputs changed'):
            self.execute()
        self.assertEqual(self.commands, before)
        self.assertFalse((self.bsp / 'SOURCE-BUILD.json').exists())

    def test_restored_vendor_pruned_source_rejects_resume(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        (self.generation / 'sdk' / self.PRUNED_SOURCES[0]).write_text(
            'upstream first-boot resize fixture\n')
        self.fail_step = None
        before = list(self.commands)
        with self.assertRaisesRegex(ValueError, 'SDK inputs changed'):
            self.execute()
        self.assertEqual(self.commands, before)
        self.assertFalse((self.bsp / 'SOURCE-BUILD.json').exists())

    def test_apply_failure_is_not_marked_prepared(self):
        self.fail_step = 'apply'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertFalse((self.generation / 'prepared.json').exists())
        self.assertFalse((self.bsp / 'SOURCE-BUILD.json').exists())
        self.fail_step = None
        self.execute()
        self.assertEqual(len(self.applies()), 2)

    def test_validation_failure_cannot_publish_success(self):
        self.validation.side_effect = ValueError('keyboard DTB missing')
        with self.assertRaisesRegex(ValueError, 'keyboard DTB missing'):
            self.execute()
        self.assertFalse((self.bsp / 'SOURCE-BUILD.json').exists())

    def test_completed_generation_is_revalidated_without_rebuilding(self):
        self.execute()
        commands = list(self.commands)
        checkouts = list(self.checkouts)
        self.execute()
        self.assertEqual(self.commands, commands)
        self.assertEqual(self.checkouts, checkouts)
        self.assertEqual(self.validation.call_count, 2)

    def test_modified_completed_kernel_is_rejected(self):
        self.execute()
        (self.bsp / 'images/Image').write_bytes(b'other kernel')
        with self.assertRaisesRegex(ValueError, 'payload changed'):
            self.execute()
        self.assertEqual(len(self.applies()), 1)

    def test_modified_firmware_is_rejected_by_cache_validation(self):
        self.execute()
        (self.bsp / 'target/lib/firmware/wifi.bin').write_bytes(b'other Wi-Fi firmware')
        with self.assertRaisesRegex(ValueError, 'payload changed'):
            BSP.check_cached(self.board, self.build)
        self.assertEqual(self.validation.call_count, 1)

    def test_modified_camera_daemon_or_tuning_is_rejected_by_cache_validation(self):
        self.execute()
        for relative in ('target/usr/bin/isp_media_server', 'target/etc/vvcam/gc2093.xml'):
            with self.subTest(relative=relative):
                path = self.bsp / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(b'changed camera runtime')
                    with self.assertRaisesRegex(ValueError, 'payload changed'):
                        BSP.check_cached(self.board, self.build)
                finally:
                    path.write_bytes(original)
        self.assertEqual(self.validation.call_count, 1)

    def test_modified_success_recipe_is_rejected(self):
        self.execute()
        record = self.bsp / 'SOURCE-BUILD.json'
        saved = json.loads(record.read_text())
        saved['recipe']['kernel_commit'] = 'd' * 40
        record.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'inputs or payload changed'):
            self.execute()

    def test_modified_prepared_sdk_is_rejected_before_resuming(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        config = self.generation / 'sdk/buildroot-overlay/configs' / BSP.DEFCONFIG
        config.write_text(config.read_text() + 'BR2_LINUX_KERNEL=n\n')
        self.fail_step = None
        before = list(self.commands)
        with self.assertRaisesRegex(ValueError, 'SDK inputs changed'):
            self.execute()
        self.assertEqual(self.commands, before)

    def test_modified_board_snapshot_is_rejected_before_resuming(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        (self.generation / 'board-layer/profile.json').write_text('{}\n')
        self.fail_step = None
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.execute()

    def test_generated_outputs_do_not_invalidate_prepared_sdk(self):
        self.fail_step = 'compile'
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        sdk = self.generation / 'sdk'
        for relative in ('output/buildroot/.overlay_sync', 'dl/linux.tar.xz', '.last_conf'):
            path = sdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('generated cache\n')
        self.fail_step = None
        self.execute()
        self.assertEqual(len(self.applies()), 1)


class SdkInputTracking(SourceFixture):
    def setUp(self):
        super().setUp()
        self.sdk = self.root / 'sdk'
        self.sdk.mkdir()
        subprocess.run(['git', 'init', '--quiet', str(self.sdk)], check=True)
        (self.sdk / '.gitignore').write_text('/output\n/dl\n.last_conf\n'
                                           'buildroot-overlay/package/ai_demo/out\n')
        tracked = self.sdk / 'Makefile'
        tracked.write_text('all:\n\ttrue\n')
        subprocess.run(['git', '-C', str(self.sdk), 'add', '.gitignore', 'Makefile'], check=True)

    def test_generated_ignored_directory_and_download_symlink_are_excluded(self):
        before = BSP.sdk_inputs(self.sdk)
        generated = self.sdk / 'buildroot-overlay/package/ai_demo/out/result.bin'
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b'build output')
        (self.sdk / 'dl').symlink_to(self.root, target_is_directory=True)
        (self.sdk / '.last_conf').write_text('CONF=test\n')
        self.assertEqual(BSP.sdk_inputs(self.sdk), before)

    def test_untracked_board_patch_is_hashed_and_changes_are_detected(self):
        before = BSP.sdk_inputs(self.sdk)
        patch = self.sdk / 'buildroot-overlay/linux/0101-keyboard.patch'
        patch.parent.mkdir(parents=True)
        patch.write_text('first board patch\n')
        after = BSP.sdk_inputs(self.sdk)
        self.assertNotEqual(before, after)
        name = str(patch.relative_to(self.sdk))
        self.assertEqual(after[name], BSP.digest(patch))
        patch.write_text('changed board patch\n')
        self.assertNotEqual(after, BSP.sdk_inputs(self.sdk))

    def test_removed_tracked_source_remains_in_fingerprint(self):
        before = BSP.sdk_inputs(self.sdk)
        tracked = self.sdk / 'Makefile'
        original = tracked.read_text()
        tracked.unlink()
        removed = BSP.sdk_inputs(self.sdk)
        self.assertIn('Makefile', removed)
        self.assertIsNone(removed['Makefile'])
        self.assertNotEqual(before, removed)
        tracked.write_text(original)
        self.assertEqual(BSP.sdk_inputs(self.sdk), before)

    def test_tracked_source_replaced_by_directory_is_rejected(self):
        tracked = self.sdk / 'Makefile'
        tracked.unlink()
        tracked.mkdir()
        with self.assertRaises(ValueError):
            BSP.sdk_inputs(self.sdk)


if __name__ == '__main__':
    unittest.main()
