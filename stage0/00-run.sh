#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
source "${BASE_DIR:?}/scripts/common"

[[ "${ARCH:?}" == riscv64 ]] || die "stage0 supports ARCH=riscv64 only"
[[ "${RELEASE:?}" =~ ^[a-z][a-z0-9-]*$ ]] || die "Invalid Debian release"
[[ "${DEBIAN_MIRROR:?}" =~ ^https?://[^[:space:]]+$ ]] || die "Invalid DEBIAN_MIRROR"
[[ "${ROOTFS_DIR:?}" != / ]] || die "Refusing to bootstrap into /"
mkdir -p "${ROOTFS_DIR}"

if [[ -x "${ROOTFS_DIR}/debootstrap/debootstrap" ]]; then
    log "Completing the existing Debian bootstrap"
elif [[ -s "${ROOTFS_DIR}/var/lib/dpkg/status" && -x "${ROOTFS_DIR}/bin/bash" ]]; then
    log "Using the existing Debian root filesystem"
else
    [[ -z "$(find "${ROOTFS_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]] ||
        die "Root filesystem is incomplete; use a clean work directory"
    log "Bootstrapping Debian ${RELEASE} for ${ARCH}"
    debootstrap --foreign --arch="${ARCH}" --variant=minbase \
        --include=ca-certificates,debian-archive-keyring \
        "${RELEASE}" "${ROOTFS_DIR}" "${DEBIAN_MIRROR%/}"
fi

if [[ -x "${ROOTFS_DIR}/debootstrap/debootstrap" ]]; then
    # on_chroot runs bash through the host's registered RISC-V binfmt handler.
    on_chroot -se <<'CHROOT'
/debootstrap/debootstrap --second-stage
CHROOT
fi

on_chroot -se -- "${ARCH}" "${RELEASE}" <<'CHROOT'
set -eu
test "$(dpkg --print-architecture)" = "$1"
. /etc/os-release
test "${ID:-}" = debian
test "${VERSION_CODENAME:-}" = "$2"
CHROOT

install -d -m 0755 "${ROOTFS_DIR}/etc/apt/sources.list.d"
: > "${ROOTFS_DIR}/etc/apt/sources.list"
cat > "${ROOTFS_DIR}/etc/apt/sources.list.d/debian.sources" <<EOF
Types: deb
URIs: ${DEBIAN_MIRROR%/}
Suites: ${RELEASE} ${RELEASE}-updates
Components: main non-free-firmware
Architectures: riscv64
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://security.debian.org/debian-security
Suites: ${RELEASE}-security
Components: main non-free-firmware
Architectures: riscv64
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
chmod 0644 "${ROOTFS_DIR}/etc/apt/sources.list.d/debian.sources"
log "Debian ${RELEASE} riscv64 bootstrap is ready"
