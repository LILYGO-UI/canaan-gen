#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify a signed stable/main repository has the requested K230 packages.

This checks publication and signatures before building a rootfs. APT still
resolves dependencies and verifies downloaded packages during installation.
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_RELEASE = 2 * 1024 * 1024
MAX_INDEX = 32 * 1024 * 1024
MAX_UNPACKED_INDEX = 128 * 1024 * 1024
PACKAGE = r'[a-z0-9][a-z0-9+.-]*'
VERSION = r'[A-Za-z0-9.+:~_-]+'


def repository_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(char.isspace() for char in value)):
        raise ValueError('APT repository URL must be HTTPS without credentials, query or fragment')
    return value.rstrip('/')


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        repository_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch(url, limit):
    opener = urllib.request.build_opener(HTTPSRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'canaan-gen-apt-preflight/1'})
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f'APT repository metadata exceeds the size limit: {url}')
            return data
        except (OSError, urllib.error.URLError) as error:
            permanent = isinstance(error, urllib.error.HTTPError) and not 500 <= error.code < 600
            if permanent or attempt == 2:
                raise ValueError(f'cannot read APT repository metadata {url}: {error}') from error
            time.sleep(attempt + 1)


def verify_inrelease(data, key, directory):
    """Use only the supplied key, never the user's GPG home or network keys."""
    key = Path(key)
    if not key.is_file() or not 0 < key.stat().st_size <= MAX_RELEASE:
        raise ValueError(f'APT public key is missing, empty or too large: {key}')
    home = directory / 'gnupg'
    home.mkdir(mode=0o700)
    keyring = directory / 'trusted.gpg'
    public_key = key.read_bytes()
    gpg = shutil.which('gpg')
    if public_key.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
        if not gpg:
            raise ValueError('gpg is required to read an ASCII-armored APT public key')
        converted = subprocess.run([gpg, '--no-options', '--homedir', str(home),
                                    '--batch', '--dearmor'], input=public_key,
                                   capture_output=True, timeout=30)
        if converted.returncode or not converted.stdout:
            raise ValueError('cannot decode the APT public key')
        public_key = converted.stdout
    keyring.write_bytes(public_key)
    signed = directory / 'InRelease'
    signed.write_bytes(data)
    gpgv = shutil.which('gpgv')
    if gpgv:
        command = [gpgv, '--homedir', str(home), '--keyring', str(keyring),
                   '--status-fd=1', str(signed)]
    elif gpg:
        command = [gpg, '--no-options', '--homedir', str(home), '--batch',
                   '--no-default-keyring', '--keyring', str(keyring),
                   '--no-auto-key-retrieve', '--auto-key-locate', 'clear',
                   '--status-fd=1', '--verify', str(signed)]
    else:
        raise ValueError('install gpgv or gpg to verify the APT repository signature')
    verified = subprocess.run(command, capture_output=True, text=True, timeout=30)
    statuses = [line.split()[1:] for line in verified.stdout.splitlines()
                if line.startswith('[GNUPG:] ')]
    invalid = {'BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG'}
    fingerprints = [parts[1] for parts in statuses if parts[0] == 'VALIDSIG']
    if verified.returncode or not fingerprints or any(parts[0] in invalid for parts in statuses):
        raise ValueError('APT InRelease signature verification failed with the configured public key')
    text = data.decode('utf-8').replace('\r\n', '\n')
    if not text.startswith('-----BEGIN PGP SIGNED MESSAGE-----\n') or '\n\n' not in text:
        raise ValueError('APT InRelease is not a clear-signed Release file')
    body = text.split('\n\n', 1)[1]
    body, separator, _ = body.partition('\n-----BEGIN PGP SIGNATURE-----\n')
    if not separator:
        raise ValueError('APT InRelease signature block is missing')
    release = '\n'.join(line[2:] if line.startswith('- ') else line
                        for line in body.splitlines()) + '\n'
    return release.encode('utf-8'), sorted(set(fingerprints))


def paragraphs(data):
    records, record, previous = [], {}, None
    for line in data.decode('utf-8').splitlines() + ['']:
        if not line:
            if record:
                records.append(record)
            record, previous = {}, None
        elif line.startswith((' ', '\t')):
            if previous is None:
                raise ValueError('APT metadata has a continuation without a field')
            record[previous] += '\n' + line.lstrip()
        else:
            name, separator, value = line.partition(':')
            if not separator or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', name):
                raise ValueError('APT metadata has an invalid field')
            if name in record:
                raise ValueError(f'APT metadata repeats field {name}')
            record[name], previous = value.strip(), name
    return records


def release_indexes(data):
    records = paragraphs(data)
    if len(records) != 1:
        raise ValueError('APT Release must contain exactly one record')
    release = records[0]
    if 'stable' not in (release.get('Suite'), release.get('Codename')):
        raise ValueError('APT repository does not identify the stable suite')
    if 'main' not in release.get('Components', '').split():
        raise ValueError('APT repository has no main component')
    if release.get('Valid-Until'):
        try:
            expiry = parsedate_to_datetime(release['Valid-Until'])
            if expiry.tzinfo is None:
                raise ValueError('missing timezone')
        except (ValueError, TypeError) as error:
            raise ValueError('APT Release has invalid Valid-Until') from error
        if expiry <= datetime.now(timezone.utc):
            raise ValueError('APT Release has expired; wait for a fresh signed publication')
    hashes = {}
    for line in release.get('SHA256', '').splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]) or not parts[1].isdigit():
            raise ValueError('APT Release contains an invalid SHA256 entry')
        digest, size, path = parts
        relative = PurePosixPath(path)
        if (relative.is_absolute() or any(part in ('', '.', '..') for part in path.split('/'))
                or not re.fullmatch(r'[A-Za-z0-9+._/-]+', path)):
            raise ValueError('APT Release contains an unsafe index path')
        if path in hashes:
            raise ValueError(f'APT Release repeats index {path}')
        hashes[path] = (digest.lower(), int(size))
    indexes = []
    for architecture in ('riscv64', 'all'):
        if architecture not in release.get('Architectures', '').split():
            continue
        prefix = f'main/binary-{architecture}/Packages'
        for suffix in ('.xz', '.gz', ''):
            path = prefix + suffix
            if path in hashes:
                indexes.append((architecture, path, *hashes[path]))
                break
    if not indexes:
        raise ValueError('APT Release has no SHA256-verified riscv64/all package index')
    return release, indexes


def unpack_index(data, path):
    if path.endswith('.xz'):
        decompressor = lzma.LZMADecompressor(memlimit=128 * 1024 * 1024)
        result = decompressor.decompress(data, max_length=MAX_UNPACKED_INDEX + 1)
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError(f'APT package index is oversized, truncated or has trailing data: {path}')
    elif path.endswith('.gz'):
        # GzipFile.read is bounded; gzip.decompress would allocate without a limit.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            result = stream.read(MAX_UNPACKED_INDEX + 1)
    else:
        result = data
    if len(result) > MAX_UNPACKED_INDEX:
        raise ValueError(f'APT package index exceeds the uncompressed size limit: {path}')
    return result


def requested_packages(value):
    requests = {}
    for item in value.split():
        if not re.fullmatch(PACKAGE + '(?:=' + VERSION + ')?', item):
            raise ValueError(f'invalid requested APT package: {item}')
        name, _, version = item.partition('=')
        if name in requests and requests[name] != version:
            raise ValueError(f'conflicting requested versions of APT package: {name}')
        requests[name] = version
    if not requests:
        raise ValueError('set an explicit, nonempty LILYGO_UI_PACKAGES list')
    return requests


def check(url, key, packages):
    url = repository_url(url)
    requests = requested_packages(packages)
    with tempfile.TemporaryDirectory(prefix='canaan-apt-') as temporary:
        signed = fetch(url + '/dists/stable/InRelease', MAX_RELEASE)
        release_data, fingerprints = verify_inrelease(signed, key, Path(temporary))
    release, indexes = release_indexes(release_data)
    found, evidence = {}, []
    for architecture, path, digest, size in indexes:
        if size > MAX_INDEX:
            raise ValueError(f'APT package index exceeds the download size limit: {path}')
        payload = fetch(url + '/dists/stable/' + path, min(size, MAX_INDEX))
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f'APT package index SHA256/size does not match signed Release: {path}; retry after publication completes')
        evidence.append({'path': path, 'sha256': digest, 'bytes': size})
        for package in paragraphs(unpack_index(payload, path)):
            name, version, arch = (package.get(field, '') for field in ('Package', 'Version', 'Architecture'))
            if not re.fullmatch(PACKAGE, name) or not re.fullmatch(VERSION, version):
                raise ValueError(f'APT package index has an invalid name/version: {path}')
            allowed = ('riscv64', 'all') if architecture == 'riscv64' else ('all',)
            if arch not in allowed:
                raise ValueError(f'APT {architecture} index contains incompatible architecture {arch}: {name}')
            found.setdefault(name, set()).add((version, arch))
    missing, selected = [], []
    for name, version in requests.items():
        candidates = sorted((v, a) for v, a in found.get(name, ()) if not version or v == version)
        if not candidates:
            missing.append(name + ('=' + version if version else ''))
        else:
            selected.append({'request': name + ('=' + version if version else ''),
                             'published': [{'version': v, 'architecture': a} for v, a in candidates]})
    if missing:
        raise ValueError('APT repository lacks requested riscv64/all packages: ' + ', '.join(missing)
                         + '; publish these packages before building the image. Local .deb fallback is disabled')
    return {'url': url, 'suite': 'stable', 'component': 'main', 'architecture': 'riscv64',
            'release_date': release.get('Date'), 'signing_fingerprints': fingerprints,
            'inrelease_sha256': hashlib.sha256(signed).hexdigest(),
            'indexes': evidence, 'packages': selected,
            'dependency_resolution_checked': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--key', required=True, type=Path)
    parser.add_argument('--packages', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = check(args.url, args.key, args.packages)
    except (ValueError, OSError, UnicodeError, EOFError, lzma.LZMAError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'canaan-gen: {error}\n')
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"APT signature/index checks passed: {len(result['packages'])} requested packages published for riscv64/all; APT will resolve dependencies during installation")


if __name__ == '__main__':
    main()
