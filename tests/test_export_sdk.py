"""Regressions for the release-to-SDK trust, package selection and publication boundary."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import export_sdk as sdk


class ExportSDKTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.image = self.work / 'release.img.xz'
        self.image.write_bytes(b'fixture')
        self.inventory = self.work / 'release.packages.tsv'
        self.inventory.write_text('libssl3t64\t1\triscv64\n')
        self.status = self.work / 'status'
        self.status.write_text('Package: libssl3t64\nVersion: 1\nSource: openssl\n'
                               'Architecture: riscv64\nStatus: install ok installed\n\n')
        self.seal()

    def seal(self):
        (self.work / 'release.json').write_text(json.dumps(dict(
            generator='canaan-gen', architecture='riscv64', board='t-display-k230', release='trixie',
            packages_sha256=sdk.sha256(self.inventory), artifact=dict(name=self.image.name,
            bytes=self.image.stat().st_size, sha256=sdk.sha256(self.image)))))

    def test_changed_image_rejected_even_at_same_length(self):
        self.image.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'image does not match'):
            sdk.release_inputs(self.image)

    def test_sealed_inventory(self):
        self.inventory.write_text('libssl3t64\t2\triscv64\n')
        with self.assertRaisesRegex(ValueError, 'inventory SHA-256'):
            sdk.release_inputs(self.image)
        with self.assertRaisesRegex(ValueError, 'rootfs dpkg status'):
            sdk.inventory_versions(self.status, self.inventory)

    def test_source_pin_covers_missing_dev_and_binnmu(self):
        self.status.write_text(self.status.read_text().replace('Version: 1', 'Version: 1+b2')
                               .replace('Source: openssl', 'Source: openssl (1)'))
        pins = sdk.package_pins(self.status, {'libssl3t64': ('1+b2', 'riscv64')})
        self.assertIn('Pin: version 1+b2\n', pins)
        self.assertIn('Package: src:openssl:any\nPin: source-version 1\n', pins)
        self.assertIn('Package: src:openssl:any\nPin: version *\nPin-Priority: -1', pins)

    def test_existing_release_is_preserved(self):
        previous = self.work / 'release.sdk'
        previous.mkdir()
        (previous / 'marker').write_text('previous')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            sdk.export(self.image, self.work)
        self.assertEqual((previous / 'marker').read_text(), 'previous')

    def test_failed_resolution_does_not_publish_or_mutate_build_root(self):
        db = self.work / 'root/var/lib/dpkg'
        db.mkdir(parents=True)
        (db / 'status').write_bytes(self.status.read_bytes())
        with patch.object(sdk, 'resolve_debs', side_effect=ValueError('unavailable')):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                sdk.export(self.image, self.work, self.work / 'root')
        self.assertFalse((self.work / 'release.sdk').exists())
        self.assertFalse(list(self.work.glob('.sdk-*')))
        self.assertEqual((db / 'status').read_bytes(), self.status.read_bytes())

    def test_rootfs_ext4_sidecars(self):
        self.image = self.image.rename(self.work / 'release.rootfs.ext4.xz')
        self.seal()
        self.assertEqual(sdk.release_inputs(self.image)[:2], ('release', 'rootfs.ext4'))


if __name__ == '__main__':
    unittest.main()
