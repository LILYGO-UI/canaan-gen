#!/usr/bin/env python3
"""Regression coverage for safe, read-only vendor firmware imports."""
import contextlib
import gzip
import importlib.util
import io
import lzma
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('import_bsp', Path(__file__).resolve().parents[1] / 'scripts/import-bsp.py')
IMPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORT)


class ImportBspTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def kernel(self, config=b'CONFIG_RISCV=y\n', release='6.6.36'):
        image = self.root / 'Image'
        image.write_bytes(b'Linux version ' + release.encode() + b' test\0IKCFG_ST' + gzip.compress(config) + b'IKCFG_ED')
        return image

    def test_all_input_compressions_preserve_decompressed_bytes(self):
        data = bytes(range(256)) * 1000
        for suffix, pack in (('.img', lambda x: x), ('.img.gz', gzip.compress), ('.img.xz', lzma.compress)):
            with self.subTest(suffix=suffix):
                source = self.root / ('vendor' + suffix)
                source.write_bytes(pack(data))
                before = IMPORT.sha256(source)
                output = self.root / ('unpacked' + suffix)
                IMPORT.unpack_image(source, output, IMPORT.input_kind(source))
                self.assertEqual(output.read_bytes(), data)
                self.assertEqual(IMPORT.sha256(source), before)

    def test_device_is_rejected_before_open(self):
        with self.assertRaisesRegex(ValueError, 'not a device'):
            IMPORT.input_kind(Path('/dev/null'))

    def test_unsupported_extension(self):
        source = self.root / 'vendor.zip'
        source.write_bytes(b'not supported')
        with self.assertRaisesRegex(ValueError, 'filename must end'):
            IMPORT.input_kind(source)

    def test_existing_output_is_never_modified(self):
        source = self.root / 'vendor.img'
        source.write_bytes(b'image')
        output = self.root / 'existing'
        output.mkdir()
        sentinel = output / 'sentinel'
        sentinel.write_text('keep me')
        with self.assertRaisesRegex(ValueError, 'output already exists'):
            IMPORT.import_bsp(source, output, debugfs='unused')
        self.assertEqual(sentinel.read_text(), 'keep me')
        self.assertEqual(list(self.root.glob('.existing.import-*')), [])

    def test_dangling_output_symlink_is_never_followed(self):
        source = self.root / 'vendor.img'
        source.write_bytes(b'image')
        output = self.root / 'link'
        output.symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(ValueError, 'output already exists'):
            IMPORT.import_bsp(source, output, debugfs='unused')
        self.assertTrue(output.is_symlink())
        self.assertFalse((self.root / 'absent').exists())

    def test_missing_debugfs_fails_before_creating_staging(self):
        source = self.root / 'vendor.img'
        source.write_bytes(b'image')
        with mock.patch.object(IMPORT.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'debugfs is required'):
                IMPORT.import_bsp(source, self.root / 'bsp')
        self.assertEqual(list(self.root.glob('.bsp.import-*')), [])

    def test_invalid_image_retains_failure_staging_not_final_output(self):
        source = self.root / 'vendor.img'
        source.write_bytes(b'invalid image')
        before = IMPORT.sha256(source)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'multiple of 512'):
                IMPORT.import_bsp(source, self.root / 'bsp', debugfs='unused')
        stages = list(self.root.glob('.bsp.import-*'))
        self.assertEqual(len(stages), 1)
        self.assertTrue((stages[0] / 'IMPORT-FAILED.txt').is_file())
        self.assertFalse((self.root / 'bsp').exists())
        self.assertEqual(IMPORT.sha256(source), before)

    def test_kernel_configuration_comes_from_embedded_gzip(self):
        config = b'# Actual kernel configuration\nCONFIG_RISCV=y\nCONFIG_CGROUPS=y\n'
        self.assertEqual(IMPORT.kernel_configuration(self.kernel(config)), config.decode())

    def test_missing_embedded_config_is_actionable(self):
        kernel = self.root / 'Image'
        kernel.write_bytes(b'kernel without IKCONFIG')
        with self.assertRaisesRegex(ValueError, 'exact completed SDK output'):
            IMPORT.kernel_configuration(kernel)

    def test_incomplete_config(self):
        kernel = self.root / 'Image'
        kernel.write_bytes(b'IKCFG_ST' + gzip.compress(b'CONFIG_RISCV=y\n'))
        with self.assertRaisesRegex(ValueError, 'incomplete embedded'):
            IMPORT.kernel_configuration(kernel)

    def test_embedded_configuration_size_is_bounded(self):
        with mock.patch.object(IMPORT, 'MAX_CONFIG', 16):
            with self.assertRaisesRegex(ValueError, 'configuration exceeds'):
                IMPORT.kernel_configuration(self.kernel(b'CONFIG_RISCV=y\n' * 10))

    def test_corrupt_embedded_config_is_rejected(self):
        kernel = self.root / 'Image'
        kernel.write_bytes(b'IKCFG_STnot-gzipIKCFG_ED')
        with self.assertRaisesRegex(ValueError, 'readable embedded IKCONFIG'):
            IMPORT.kernel_configuration(kernel)

    def test_kernel_release_must_match_modules(self):
        modules = self.root / 'modules'
        (modules / '6.6.35').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            IMPORT.kernel_release(self.kernel(), modules)

    def test_debugfs_status_zero_does_not_hide_extraction_error(self):
        result = subprocess.CompletedProcess([], 0, stdout='', stderr='debugfs 1.47.0 (5-Feb-2023)\n/Image: File not found by ext2_lookup\n')
        with mock.patch.object(IMPORT.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'extraction failed'):
                IMPORT.run_debugfs('debugfs', self.root / 'boot.ext4', 'dump /Image Image', self.root, self.root / 'import.log')

    def test_unprivileged_chown_warning_is_recorded_and_allowed(self):
        text = ('debugfs 1.47.0 (5-Feb-2023)\n'
                'dump_file: Operation not permitted while changing ownership of target/lib/modules/6.6.36/8723ds.ko\n')
        result = subprocess.CompletedProcess([], 0, stdout='', stderr=text)
        log = self.root / 'import.log'
        with mock.patch.object(IMPORT.subprocess, 'run', return_value=result) as run:
            IMPORT.run_debugfs('debugfs', self.root / 'rootfs.ext4', 'rdump /lib/modules target/lib', self.root, log)
        self.assertIn('Operation not permitted', log.read_text())
        self.assertNotIn('-w', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['cwd'], self.root)


if __name__ == '__main__':
    unittest.main()
