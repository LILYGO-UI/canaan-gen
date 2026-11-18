"""APT publication failures must stop a build before creating a rootfs."""
import gzip
import hashlib
import importlib.util
import io
import lzma
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('check_apt', Path(__file__).resolve().parents[1] / 'scripts/check-apt.py')
APT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APT)


def package(name='lilygo-ui-launcher', version='0.1.3', architecture='riscv64'):
    return f'Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n\n'.encode()


def release(payloads, extra=''):
    text = 'Suite: stable\nCodename: stable\nComponents: main\nArchitectures: riscv64 all\n' + extra + 'SHA256:\n'
    for path, data in payloads.items():
        text += f' {hashlib.sha256(data).hexdigest()} {len(data)} {path}\n'
    return text.encode()


class AptPreflightTests(unittest.TestCase):
    def check(self, payloads, requested='lilygo-ui-launcher', release_data=None, fetched=None):
        metadata = release_data or release(payloads)
        downloads = fetched or payloads

        def fetch(url, limit):
            if url.endswith('/InRelease'):
                return b'test signed metadata'
            return downloads[url.split('/dists/stable/', 1)[1]]

        with mock.patch.object(APT, 'fetch', side_effect=fetch), \
                mock.patch.object(APT, 'verify_inrelease', return_value=(metadata, ['TEST-FINGERPRINT'])):
            return APT.check('https://packages.example/repo', Path('/unused/key'), requested)

    def test_accepts_riscv64_and_all_from_verified_compressed_indexes(self):
        shared = package('lilygo-ui-appkit-dev', '0.1.1', 'all')
        payloads = {'main/binary-riscv64/Packages.xz': lzma.compress(package() + shared),
                    'main/binary-all/Packages.gz': gzip.compress(shared)}
        result = self.check(payloads, 'lilygo-ui-launcher\nlilygo-ui-appkit-dev')
        self.assertEqual([p['request'] for p in result['packages']], ['lilygo-ui-launcher', 'lilygo-ui-appkit-dev'])
        self.assertEqual(result['packages'][1]['published'], [{'version': '0.1.1', 'architecture': 'all'}])
        self.assertFalse(result['dependency_resolution_checked'])

    def test_all_missing_packages_are_reported_together(self):
        with self.assertRaisesRegex(ValueError, 'lilygo-ui-launcher, lilygo-ui-music.*fallback is disabled'):
            self.check({'main/binary-riscv64/Packages': package('lilygo-ui-store')},
                       'lilygo-ui-launcher lilygo-ui-music')

    def test_exact_version_must_be_published(self):
        payloads = {'main/binary-riscv64/Packages': package(version='1:2.0~rc1-1')}
        self.assertEqual(self.check(payloads, 'lilygo-ui-launcher=1:2.0~rc1-1')['packages'][0]['request'],
                         'lilygo-ui-launcher=1:2.0~rc1-1')
        with self.assertRaisesRegex(ValueError, 'lilygo-ui-launcher=1.0'):
            self.check(payloads, 'lilygo-ui-launcher=1.0')

    def test_tampered_index_is_rejected_before_package_parsing(self):
        path = 'main/binary-riscv64/Packages'
        with self.assertRaisesRegex(ValueError, 'SHA256/size'):
            self.check({path: package()}, fetched={path: package().replace(b'0.1.3', b'0.1.4')})

    def test_wrong_architecture_does_not_satisfy_package(self):
        with self.assertRaisesRegex(ValueError, 'incompatible architecture arm64'):
            self.check({'main/binary-riscv64/Packages': package(architecture='arm64')})

    def test_absent_signed_architecture_index(self):
        with self.assertRaisesRegex(ValueError, 'no SHA256-verified riscv64/all'):
            self.check({'main/binary-arm64/Packages': package(architecture='arm64')})

    def test_expired_release_is_rejected(self):
        payloads = {'main/binary-riscv64/Packages': package()}
        with self.assertRaisesRegex(ValueError, 'has expired'):
            self.check(payloads, release_data=release(payloads, 'Valid-Until: Tue, 1 Jan 2002 00:00:00 UTC\n'))

    def test_unsigned_index_paths_are_not_used(self):
        for path in ('../Packages', '/Packages', 'https://evil.example/Packages', 'main/./Packages'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'unsafe index path'):
                APT.release_indexes(release({path: package()}))

    def test_conflicting_versions_and_shell_tokens_rejected_before_network(self):
        for value in ('', 'launcher=1 launcher=2', './local.deb', 'launcher;touch', 'launcher $(id)'):
            with self.subTest(value=value), mock.patch.object(APT, 'fetch') as fetch, self.assertRaises(ValueError):
                APT.check('https://packages.example', '/unused/key', value)
            fetch.assert_not_called()

    def test_https_source_and_redirects_required(self):
        for url in ('http://packages.example', 'file:///tmp/repo', 'https://user:pass@packages.example',
                    'https://packages.example/?key=secret', 'https://packages.example/#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                APT.repository_url(url)
        with self.assertRaises(ValueError):
            APT.HTTPSRedirect().redirect_request(None, None, 302, '', {}, 'http://packages.example/index')

    def test_bounded_decompression(self):
        data = b'x' * 100
        with mock.patch.object(APT, 'MAX_UNPACKED_INDEX', 50):
            for path, payload in [('Packages.gz', gzip.compress(data)), ('Packages.xz', lzma.compress(data))]:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    APT.unpack_index(payload, path)

    def test_bad_signature_stops_before_reading_indexes(self):
        with mock.patch.object(APT, 'fetch', return_value=b'signed') as fetch, \
                mock.patch.object(APT, 'verify_inrelease', side_effect=ValueError('bad signature')):
            with self.assertRaisesRegex(ValueError, 'bad signature'):
                APT.check('https://packages.example', '/unused/key', 'lilygo-ui-launcher')
        self.assertEqual(fetch.call_count, 1)

    def test_transient_network_and_server_failures_retry(self):
        opener = mock.Mock()
        opener.open.side_effect = [APT.urllib.error.URLError('TLS connection closed'),
                                   APT.urllib.error.HTTPError('https://packages.example', 503, 'busy', {}, None),
                                   io.BytesIO(b'valid metadata')]
        with mock.patch.object(APT.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(APT.time, 'sleep'):
            self.assertEqual(APT.fetch('https://packages.example', 100), b'valid metadata')
        self.assertEqual(opener.open.call_count, 3)

    def test_missing_metadata_and_size_failures_do_not_retry(self):
        for response in (APT.urllib.error.HTTPError('https://packages.example', 404, 'missing', {}, None),
                         io.BytesIO(b'oversized')):
            with self.subTest(response=response):
                opener = mock.Mock()
                if isinstance(response, Exception):
                    opener.open.side_effect = response
                else:
                    opener.open.return_value = response
                with mock.patch.object(APT.urllib.request, 'build_opener', return_value=opener), \
                        mock.patch.object(APT.time, 'sleep') as sleep, self.assertRaises(ValueError):
                    APT.fetch('https://packages.example', 2)
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_network_retries_are_bounded(self):
        opener = mock.Mock()
        opener.open.side_effect = APT.urllib.error.URLError('offline')
        with mock.patch.object(APT.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(APT.time, 'sleep'), self.assertRaisesRegex(ValueError, 'offline'):
            APT.fetch('https://packages.example', 100)
        self.assertEqual(opener.open.call_count, 3)


@unittest.skipUnless(shutil.which('gpgv') or shutil.which('gpg'), 'GPG verifier needed for signature integration')
class SignatureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='canaan-apt-signature-')
        cls.root = Path(cls.temp.name)
        cls.gpg = shutil.which('gpg')
        fixture = Path(__file__).parent / 'fixtures/apt'
        cls.key = fixture / 'key.gpg'
        cls.signed = (fixture / 'InRelease').read_bytes()
        cls.plain_sha256 = 'f28594607d8404ecef470ce6bbfe5f3181ade83436cfb3053ee3957cc940c283'
        cls.fingerprint = 'FAC75BB28CE621CB6D5353A1C309120B7DC98F4A'

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def verify(self, data, key=None):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            return APT.verify_inrelease(data, key or self.key, Path(directory))

    def test_valid_signature_and_tampering(self):
        plain, fingerprints = self.verify(self.signed)
        self.assertEqual(hashlib.sha256(plain).hexdigest(), self.plain_sha256)
        self.assertEqual(fingerprints, [self.fingerprint])
        with self.assertRaisesRegex(ValueError, 'signature verification failed'):
            self.verify(self.signed.replace(b'Suite: stable', b'Suite: forged'))

    def test_wrong_key_is_rejected(self):
        bad = self.root / 'invalid-key.gpg'
        bad.write_bytes(b'not a key')
        with self.assertRaisesRegex(ValueError, 'signature verification failed'):
            self.verify(self.signed, bad)

    @unittest.skipUnless(shutil.which('gpg'), 'gpg needed for fallback/armored public key test')
    def test_gpg_fallback_with_armored_key(self):
        home = self.root / 'exporter'
        home.mkdir(mode=0o700)
        armored = subprocess.run([self.gpg, '--no-options', '--homedir', str(home), '--batch',
                                  '--no-default-keyring', '--keyring', str(self.key), '--armor', '--export'],
                                 capture_output=True, check=True).stdout
        key = self.root / 'key.asc'
        key.write_bytes(armored)
        original_which = shutil.which
        with mock.patch.object(APT.shutil, 'which', side_effect=lambda name: None if name == 'gpgv' else original_which(name)):
            plain, fingerprints = self.verify(self.signed, key)
        self.assertEqual(hashlib.sha256(plain).hexdigest(), self.plain_sha256)
        self.assertEqual(fingerprints, [self.fingerprint])


if __name__ == '__main__':
    unittest.main()
