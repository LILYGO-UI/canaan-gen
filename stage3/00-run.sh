#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
source "${BASE_DIR:?}/scripts/common"

mode="${LILYGO_UI_INSTALL:-0}"
case "${mode}" in
    0) log "Skipping optional LILYGO UI packages"; exit 0 ;;
    apt) ;;
    *) die "LILYGO_UI_INSTALL must be 0 or apt; local DEB installation is no longer supported" ;;
esac

[[ "${LILYGO_UI_APT_URL:-}" =~ ^https://[^[:space:]]+$ ]] || die "Set an HTTPS LILYGO_UI_APT_URL"
[[ -s "${LILYGO_UI_APT_KEY_FILE:-}" ]] || die "Set LILYGO_UI_APT_KEY_FILE to the repository public key"
[[ -n "${LILYGO_UI_PACKAGES:-}" ]] || die "Set an explicit LILYGO_UI_PACKAGES list"
read -r -a packages <<< "${LILYGO_UI_PACKAGES//$'\n'/ }"
for package in "${packages[@]}"; do
    [[ "${package}" =~ ^[a-z0-9][a-z0-9+.-]*(=[A-Za-z0-9.+:~_-]+)?$ ]] ||
        die "Invalid name or version in LILYGO_UI_PACKAGES"
done
[[ "${#packages[@]}" -gt 0 ]] || die "LILYGO_UI_PACKAGES is empty"
command -v gpg >/dev/null || die "gpg is required to validate the APT repository key"
gpg --batch --show-keys "${LILYGO_UI_APT_KEY_FILE}" >/dev/null 2>&1 || die "Invalid LILYGO UI APT public key"
install -d -m 0755 "${ROOTFS_DIR}/usr/share/keyrings" "${ROOTFS_DIR}/etc/apt/sources.list.d"
keyring="${ROOTFS_DIR}/usr/share/keyrings/lilygo-ui-archive-keyring.gpg"
if grep -q '^-----BEGIN PGP PUBLIC KEY BLOCK-----' "${LILYGO_UI_APT_KEY_FILE}"; then
    gpg --batch --yes --dearmor --output "${keyring}" "${LILYGO_UI_APT_KEY_FILE}"
else
    install -m 0644 "${LILYGO_UI_APT_KEY_FILE}" "${keyring}"
fi
chmod 0644 "${keyring}"
cat > "${ROOTFS_DIR}/etc/apt/sources.list.d/lilygo-ui.sources" <<EOF
Types: deb
URIs: ${LILYGO_UI_APT_URL%/}
Suites: stable
Components: main
Architectures: riscv64
Signed-By: /usr/share/keyrings/lilygo-ui-archive-keyring.gpg
EOF

policy="${ROOTFS_DIR}/usr/sbin/policy-rc.d"
policy_backup="$(mktemp -d "${WORK_DIR}/stage3-policy.XXXXXX")"
if [[ -e "${policy}" || -L "${policy}" ]]; then
    cp -a "${policy}" "${policy_backup}/original"
fi
cleanup() {
    rm -f "${policy}"
    if [[ -e "${policy_backup}/original" || -L "${policy_backup}/original" ]]; then
        mv "${policy_backup}/original" "${policy}"
    fi
    rmdir "${policy_backup}"
}
trap cleanup EXIT
rm -f "${policy}"
printf '#!/bin/sh\nexit 101\n' > "${policy}"
chmod 0755 "${policy}"

log "Installing ${#packages[@]} LILYGO UI packages from ${LILYGO_UI_APT_URL}"
on_chroot -se -- "${packages[@]}" <<'CHROOT'
set -eu
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update
apt-get -o Acquire::Retries=3 install --no-install-recommends -y "$@"
apt-get check
for package in "$@"; do
    name=${package%%=*}
    test "$(dpkg-query -W -f='${Status}' "$name")" = 'install ok installed'
    case "$(dpkg-query -W -f='${Architecture}' "$name")" in
        riscv64|all) ;;
        *) echo "Unexpected architecture for $name" >&2; exit 1 ;;
    esac
done
CHROOT
if [[ "${ACCOUNT_SETUP:-preset}" == launcher ]]; then
    [[ -x "$ROOTFS_DIR/usr/bin/lilygo-ui-launcher" ]] ||
        die 'ACCOUNT_SETUP=launcher requires an executable /usr/bin/lilygo-ui-launcher; it is missing after APT installation'
    [[ -f "$ROOTFS_DIR/usr/lib/systemd/system/lilygo-ui-launcher.service" ]] ||
        die 'ACCOUNT_SETUP=launcher requires lilygo-ui-launcher.service; it is missing after APT installation'
    on_chroot <<'CHROOT'
systemctl enable lilygo-ui-launcher.service
CHROOT
    python3 "$BASE_DIR/scripts/check-ui-rootfs.py" --root "$ROOTFS_DIR" --enable-ssh "${ENABLE_SSH:-0}"
fi
log "Optional LILYGO UI package installation completed"
