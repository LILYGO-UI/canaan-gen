# LILYGO UI APT signing key

`lilygo-ui-archive-keyring.gpg` is the public signing key downloaded from
`https://lilygo-ui.github.io/packages/apt-keyring.gpg` on 2026-11-12. It verified
the repository's `stable/InRelease` signature before being added here.

- Fingerprint: `FAC75BB28CE621CB6D5353A1C309120B7DC98F4A`
- File SHA-256: `837224f13c15cb6d5bc72da442e4ecf657c4ee8cd5f55ee2321ec439d34635de`

The build uses this checked-in public key and installs it in the generated
image under `/usr/share/keyrings/lilygo-ui-archive-keyring.gpg`. It does not
download and trust a new key on each build. A key rotation requires reviewing
and updating this file, or explicitly setting `LILYGO_UI_APT_KEY_FILE`.
