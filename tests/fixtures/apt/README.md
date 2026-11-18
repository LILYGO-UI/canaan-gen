# Public APT signature fixture

`InRelease` and `key.gpg` are the public repository metadata and public key
downloaded from `https://lilygo-ui.github.io/packages/` on November 12, 2026.
The signed Release date is `Thu, 12 Nov 2026 12:26:51 GMT`.

- InRelease URL: `https://lilygo-ui.github.io/packages/dists/stable/InRelease`
- Key URL: `https://lilygo-ui.github.io/packages/apt-keyring.gpg`
- Signing fingerprint: `FAC75BB28CE621CB6D5353A1C309120B7DC98F4A`
- InRelease SHA-256: `e2dbe3c0d7b1790fc03d1601fce73c3828c9172d601f8ba234117e5490a869e6`
- Public key SHA-256: `837224f13c15cb6d5bc72da442e4ecf657c4ee8cd5f55ee2321ec439d34635de`
- Cleartext Release SHA-256: `f28594607d8404ecef470ce6bbfe5f3181ade83436cfb3053ee3957cc940c283`

These immutable fixtures test offline signature verification, tampering and
key isolation. They do not establish the current repository package inventory.
There is no private key, secret, package binary or network requirement.
