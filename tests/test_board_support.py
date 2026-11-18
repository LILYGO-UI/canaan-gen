#!/usr/bin/env python3
"""Exercise shared board snapshots and installed payloads without a real image."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SOURCE_BOARD = PROJECT.parent / 'lilygo-linux-dtoverlays/boards/T-Display-K230'
SPEC = importlib.util.spec_from_file_location('board_support', PROJECT / 'scripts/board-support.py')
BOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOARD)


OTHER_SERVICES = ('lilygo-k230-peripherals.service', 'lilygo-k230-power.service')
BUILT_HELPER = 'usr/lib/lilygo-k230/libk230mmio.so'


class BoardSupportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='canaan-board-test-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / 'board checkout'
        self.board = self.repo / BOARD.BOARD_PATH
        shutil.copytree(SOURCE_BOARD, self.board,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        self.destination = self.directory / 'build snapshot'

    def rootfs(self):
        root = self.directory / 'rootfs'
        shutil.copytree(self.board / 'debian/rootfs', root)
        # Everything except the audio unit's link is in its built state.
        wants = root / 'etc/systemd/system/multi-user.target.wants'
        wants.mkdir(parents=True)
        for service in OTHER_SERVICES:
            (wants / service).symlink_to(f'/usr/lib/systemd/system/{service}')
        (root / BUILT_HELPER).write_bytes(b'\x7fELF fixture')
        return root, wants / 'lilygo-k230-audio.service'

    def test_snapshot_records_exact_sources_and_is_independent_of_later_edits(self):
        relative = 'debian/rootfs/etc/asound.conf'
        source = self.board / relative
        original = source.read_bytes()
        evidence = BOARD.snapshot(self.repo, self.destination)
        recorded = json.loads((self.destination / 'SOURCE-PROVENANCE.json').read_text())
        self.assertEqual(recorded, evidence)
        self.assertEqual(recorded['repository'], 'lilygo-linux-dtoverlays')
        self.assertEqual(recorded['board'], 't-display-k230')
        self.assertFalse(recorded['linux_patches_applied_by_generator'])
        self.assertEqual(recorded['files_sha256'][relative], hashlib.sha256(original).hexdigest())
        patch = 'linux/0059-asoc-canaan-add-external-i2s-output-switch.patch'
        self.assertEqual(recorded['files_sha256'][patch],
                         hashlib.sha256((self.board / patch).read_bytes()).hexdigest())
        self.assertNotIn('SOURCE-PROVENANCE.json', recorded['files_sha256'])
        source.write_bytes(original + b'\n# Later source edit\n')
        self.assertEqual((self.destination / relative).read_bytes(), original)
        self.assertEqual(BOARD.hashes(self.destination), recorded['files_sha256'])

    def test_modified_upstream_patch_stops_snapshot_before_output_creation(self):
        patch = self.board / 'linux/0059-asoc-canaan-add-external-i2s-output-switch.patch'
        patch.write_bytes(patch.read_bytes() + b'\nmodified patch\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            BOARD.snapshot(self.repo, self.destination)
        self.assertFalse(self.destination.exists())

    def test_missing_or_empty_runtime_payload_is_rejected(self):
        source = self.board / 'debian/rootfs/etc/asound.conf'
        for state in ('missing', 'empty'):
            with self.subTest(state=state):
                if state == 'missing':
                    source.unlink()
                else:
                    source.touch()
                with self.assertRaisesRegex(ValueError, 'missing board runtime file'):
                    BOARD.validate(self.board)

    def test_runtime_helpers_must_be_executable_in_source_and_rootfs(self):
        helper = 'usr/libexec/lilygo-k230-audio-init'
        source = self.board / 'debian/rootfs' / helper
        source.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'board runtime helper is not executable'):
            BOARD.validate(self.board)
        source.chmod(0o755)
        root, link = self.rootfs()
        link.symlink_to('/usr/lib/systemd/system/lilygo-k230-audio.service')
        (root / helper).chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'not executable in rootfs'):
            BOARD.check_rootfs(self.board, root)

    def test_rootfs_requires_runtime_files_and_enabled_service(self):
        root, link = self.rootfs()
        with self.assertRaisesRegex(ValueError, 'service is not enabled'):
            BOARD.check_rootfs(self.board, root)
        link.symlink_to('/usr/lib/systemd/system/missing.service')
        with self.assertRaisesRegex(ValueError, 'service link is broken'):
            BOARD.check_rootfs(self.board, root)
        link.unlink()
        link.symlink_to('/etc/asound.conf')
        with self.assertRaisesRegex(ValueError, 'unexpected target'):
            BOARD.check_rootfs(self.board, root)
        link.unlink()
        link.symlink_to('/usr/lib/systemd/system/lilygo-k230-audio.service')
        BOARD.check_rootfs(self.board, root)
        link.unlink()
        link.symlink_to('../../../../usr/lib/systemd/system/lilygo-k230-audio.service')
        BOARD.check_rootfs(self.board, root)
        (root / 'etc/asound.conf').unlink()
        with self.assertRaisesRegex(ValueError, 'runtime missing from rootfs: etc/asound.conf'):
            BOARD.check_rootfs(self.board, root)

    def test_every_board_service_must_be_enabled_and_helpers_built(self):
        root, link = self.rootfs()
        link.symlink_to('/usr/lib/systemd/system/lilygo-k230-audio.service')
        BOARD.check_rootfs(self.board, root)
        for service in OTHER_SERVICES:
            with self.subTest(service=service):
                enabled = link.parent / service
                enabled.unlink()
                with self.assertRaisesRegex(ValueError, f'not enabled in rootfs: {service}'):
                    BOARD.check_rootfs(self.board, root)
                enabled.symlink_to(f'/usr/lib/systemd/system/{service}')
        helper = root / BUILT_HELPER
        helper.write_text('not an object file')
        with self.assertRaisesRegex(ValueError, 'helper was not built in rootfs'):
            BOARD.check_rootfs(self.board, root)
        helper.unlink()
        with self.assertRaisesRegex(ValueError, 'helper was not built in rootfs'):
            BOARD.check_rootfs(self.board, root)

    def edit_profile(self, change):
        path = self.board / 'profile.json'
        profile = json.loads(path.read_text())
        change(profile['runtime'])
        path.write_text(json.dumps(profile))

    def test_profile_services_need_unit_files_and_include_the_audio_service(self):
        self.edit_profile(lambda runtime: runtime['services'].append('missing.service'))
        with self.assertRaisesRegex(ValueError, 'has no unit file: missing.service'):
            BOARD.validate(self.board)
        self.edit_profile(lambda runtime: runtime.update(services=list(OTHER_SERVICES)))
        with self.assertRaisesRegex(ValueError, 'including runtime.service'):
            BOARD.validate(self.board)
        self.edit_profile(lambda runtime: runtime.update(services=[]))
        with self.assertRaisesRegex(ValueError, 'nonempty list'):
            BOARD.validate(self.board)

    def test_profile_without_services_list_enables_only_the_audio_service(self):
        self.edit_profile(lambda runtime: runtime.pop('services'))
        profile = BOARD.validate(self.board)
        self.assertEqual(BOARD.runtime_services(profile), ['lilygo-k230-audio.service'])

    def test_build_steps_cannot_escape_usr_or_overwrite_payload_files(self):
        source = 'usr/lib/lilygo-k230/mmio.c'
        for output in ('/usr/lib/x.so', 'usr/../etc/x.so', 'etc/x.so', source):
            with self.subTest(output=output):
                self.edit_profile(lambda runtime: runtime.update(
                    build=[dict(source=source, output=output)]))
                with self.assertRaisesRegex(ValueError, 'build steps need'):
                    BOARD.validate(self.board)
        self.edit_profile(lambda runtime: runtime.update(
            build=[dict(source='usr/lib/missing.c', output='usr/lib/x.so')]))
        with self.assertRaisesRegex(ValueError, 'build steps need'):
            BOARD.validate(self.board)

    def test_stage_scripts_can_list_services_packages_and_builds(self):
        def listed(kind):
            result = subprocess.run([sys.executable, str(PROJECT / 'scripts/board-support.py'),
                '--board-dir', str(self.board), '--list', kind], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()
        self.assertEqual(listed('services'), ['lilygo-k230-audio.service', *OTHER_SERVICES])
        self.assertEqual(listed('build'), [f'usr/lib/lilygo-k230/mmio.c {BUILT_HELPER}'])
        self.assertIn('python3-smbus2', listed('packages'))

    def test_source_symlink_is_rejected_without_copying_external_files(self):
        external = self.directory / 'outside-source'
        external.write_text('must not be copied')
        (self.board / 'external-file').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'must not contain symlinks'):
            BOARD.snapshot(self.repo, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(external.read_text(), 'must not be copied')

    def test_payload_cannot_write_to_build_mounts_or_install_undeclared_files(self):
        payload = self.board / 'debian/rootfs'
        for name in ('dev', 'proc', 'sys', 'run', 'tmp'):
            with self.subTest(path=name):
                path = payload / name
                path.mkdir()
                with self.assertRaisesRegex(ValueError, 'must not contain top-level path'):
                    BOARD.validate(self.board)
                path.rmdir()
        (payload / 'etc/unlisted.conf').write_text('unlisted board file')
        with self.assertRaisesRegex(ValueError, 'must exactly match'):
            BOARD.validate(self.board)

    def test_existing_snapshot_is_never_overwritten(self):
        self.destination.mkdir()
        sentinel = self.destination / 'existing-output'
        sentinel.write_bytes(b'keep existing build files')
        with self.assertRaises(FileExistsError):
            BOARD.snapshot(self.repo, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'keep existing build files')


if __name__ == '__main__':
    unittest.main()
