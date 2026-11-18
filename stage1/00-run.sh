#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
source "${BASE_DIR:?}/scripts/common"

[[ "${FIRST_USER_NAME:?}" =~ ^[a-z][a-z0-9_-]{0,31}$ && "${FIRST_USER_NAME}" != root ]] ||
    die "FIRST_USER_NAME must be a non-root Linux username"
[[ "${TARGET_HOSTNAME:?}" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] || die "Invalid TARGET_HOSTNAME"
[[ "${ENABLE_SSH:-0}" =~ ^[01]$ ]] || die "ENABLE_SSH must be 0 or 1"
[[ "${TIMEZONE:?}" =~ ^[A-Za-z0-9_+/-]+$ && "${TIMEZONE}" != *..* ]] || die "Invalid TIMEZONE"
[[ "${LOCALE:?}" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "Invalid LOCALE"
account_setup="${ACCOUNT_SETUP:-preset}"
[[ "$account_setup" == preset || "$account_setup" == launcher ]] || die "Invalid ACCOUNT_SETUP"
password_hash="${FIRST_USER_PASSWORD_HASH:-}"
authorized_keys="${SSH_AUTHORIZED_KEYS_FILE:-}"
if [[ -n "${password_hash}" ]]; then
    [[ "${password_hash}" =~ ^\$[A-Za-z0-9]+\$[^:[:space:]]+$ ]] ||
        die "FIRST_USER_PASSWORD_HASH must be a crypt password hash, not plaintext"
fi
if [[ -n "${authorized_keys}" ]]; then
    [[ -s "${authorized_keys}" ]] || die "SSH_AUTHORIZED_KEYS_FILE is missing or empty"
fi
if [[ "$account_setup" == preset && "${ENABLE_SSH:-0}" == 1 && -z "${password_hash}" && -z "${authorized_keys}" ]]; then
    die "ENABLE_SSH=1 requires SSH_AUTHORIZED_KEYS_FILE or FIRST_USER_PASSWORD_HASH"
fi
if [[ "$account_setup" == launcher ]]; then
    [[ -z "$password_hash" && -z "$authorized_keys" ]] ||
        die "Launcher account setup cannot use preset credentials"
fi

packages=()
while IFS= read -r package || [[ -n "${package}" ]]; do
    package="${package%%#*}"
    [[ -n "${package//[[:space:]]/}" ]] || continue
    [[ "${package}" =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || die "Invalid package in stage1/packages"
    packages+=("${package}")
done < "${BASE_DIR}/stage1/packages"
[[ "${#packages[@]}" -gt 0 ]] || die "stage1/packages is empty"

# Package maintainer scripts must not start target daemons inside the builder.
policy="${ROOTFS_DIR}/usr/sbin/policy-rc.d"
policy_backup="$(mktemp -d "${WORK_DIR}/stage1-policy.XXXXXX")"
if [[ -f "${ROOTFS_DIR}/etc/resolv.conf" ]]; then
    cp -L "${ROOTFS_DIR}/etc/resolv.conf" "${policy_backup}/resolv.conf"
fi
if [[ -e "${policy}" || -L "${policy}" ]]; then
    cp -a "${policy}" "${policy_backup}/original"
fi
restore_policy() {
    rm -f "${policy}"
    if [[ -e "${policy_backup}/original" || -L "${policy_backup}/original" ]]; then
        mv "${policy_backup}/original" "${policy}"
    fi
    # systemd-resolved's postinst can replace this file with a runtime symlink.
    # The service is not running in the chroot, so retain build-time DNS until
    # build.sh creates the final runtime symlink after all APT stages.
    if [[ -f "${policy_backup}/resolv.conf" ]]; then
        rm -f "${ROOTFS_DIR}/etc/resolv.conf"
        install -m 0644 "${policy_backup}/resolv.conf" "${ROOTFS_DIR}/etc/resolv.conf"
        rm -f "${policy_backup}/resolv.conf"
    fi
    rmdir "${policy_backup}"
}
trap restore_policy EXIT
rm -f "${policy}"
printf '#!/bin/sh\nexit 101\n' > "${policy}"
chmod 0755 "${policy}"

log "Installing Debian base packages"
on_chroot -se -- "${packages[@]}" <<'CHROOT'
set -eu
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
apt-get -o Acquire::Retries=3 update
apt-get -o Acquire::Retries=3 install --no-install-recommends -y "$@"
CHROOT

[[ -f "${ROOTFS_DIR}/usr/share/zoneinfo/${TIMEZONE}" ]] || die "Unknown TIMEZONE: ${TIMEZONE}"
printf '%s\n' "${TIMEZONE}" > "${ROOTFS_DIR}/etc/timezone"
ln -snf "../usr/share/zoneinfo/${TIMEZONE}" "${ROOTFS_DIR}/etc/localtime"
if [[ "${LOCALE}" != C.UTF-8 ]]; then
    grep -Fqx "${LOCALE} UTF-8" "${ROOTFS_DIR}/usr/share/i18n/SUPPORTED" ||
        die "LOCALE must name an available UTF-8 locale"
    printf '%s UTF-8\n' "${LOCALE}" > "${ROOTFS_DIR}/etc/locale.gen"
    on_chroot -se <<'CHROOT'
locale-gen
CHROOT
fi
printf 'LANG=%s\n' "${LOCALE}" > "${ROOTFS_DIR}/etc/default/locale"
printf '%s\n' "${TARGET_HOSTNAME}" > "${ROOTFS_DIR}/etc/hostname"
cat > "${ROOTFS_DIR}/etc/hosts" <<EOF
127.0.0.1 localhost
127.0.1.1 ${TARGET_HOSTNAME}
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF

if [[ "$account_setup" == preset ]]; then
on_chroot -se -- "${FIRST_USER_NAME}" "${password_hash}" <<'CHROOT'
set -eu
first_user="$1"
password_hash="$2"
if id -u "$first_user" >/dev/null 2>&1; then
    user_id="$(id -u "$first_user")"
    if [ "$user_id" -lt 1000 ] || [ "$user_id" -ge 65534 ]; then
        printf '%s\n' 'Refusing to reuse a system account as FIRST_USER_NAME' >&2
        exit 1
    fi
else
    useradd --create-home --shell /bin/bash "$first_user"
fi
for group in adm audio dialout input netdev render sudo video i2c spi gpio; do
    getent group "$group" >/dev/null || groupadd --system "$group"
    usermod --append --groups "$group" "$first_user"
done
if [ -n "$password_hash" ]; then
    printf '%s:%s\n' "$first_user" "$password_hash" | chpasswd --encrypted
else
    usermod --lock "$first_user"
fi
CHROOT
else
    on_chroot <<'CHROOT'
# No ordinary user may bypass the launcher's first-use account wizard.
if awk -F: '$3 >= 1000 && $3 < 65534 { found=1 } END { exit !found }' /etc/passwd; then
    echo "Launcher first-boot image already contains an ordinary user" >&2
    exit 1
fi
for group in audio dialout input netdev render sudo video i2c spi gpio; do
    getent group "$group" >/dev/null || groupadd --system "$group"
done
CHROOT
fi
on_chroot <<'CHROOT'
usermod --lock root
CHROOT

if [[ -n "${authorized_keys}" ]]; then
    install -d -m 0700 "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh"
    install -m 0600 "${authorized_keys}" "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh/authorized_keys"
    on_chroot -se -- "${FIRST_USER_NAME}" <<'CHROOT'
set -eu
ssh-keygen -l -f "/home/$1/.ssh/authorized_keys" >/dev/null
chown -R "$1:$(id -gn "$1")" "/home/$1/.ssh"
CHROOT
fi

install -d -m 0755 "${ROOTFS_DIR}/etc/NetworkManager/conf.d"
cat > "${ROOTFS_DIR}/etc/NetworkManager/conf.d/10-canaan-gen.conf" <<'EOF'
[main]
dns=systemd-resolved

[ifupdown]
managed=true
EOF
# Keep the bootstrap DNS configuration while later stages install packages.
# build.sh replaces it with systemd-resolved's runtime link at finalisation.

install -d -m 0755 "${ROOTFS_DIR}/etc/ssh/sshd_config.d"
password_auth=no
[[ -z "${password_hash}" && "$account_setup" != launcher ]] || password_auth=yes
cat > "${ROOTFS_DIR}/etc/ssh/sshd_config.d/10-canaan-gen.conf" <<EOF
PermitRootLogin no
PermitEmptyPasswords no
PubkeyAuthentication yes
PasswordAuthentication ${password_auth}
KbdInteractiveAuthentication no
EOF
if [[ "$account_setup" == preset ]]; then
    printf 'AllowUsers %s\n' "$FIRST_USER_NAME" >> "${ROOTFS_DIR}/etc/ssh/sshd_config.d/10-canaan-gen.conf"
fi
install -d -m 0755 "${ROOTFS_DIR}/etc/systemd/system/ssh.service.d"
cat > "${ROOTFS_DIR}/etc/systemd/system/canaan-ssh-host-keys.service" <<'EOF'
[Unit]
Description=Generate unique SSH host keys
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes
EOF
cat > "${ROOTFS_DIR}/etc/systemd/system/ssh.service.d/10-canaan-gen.conf" <<'EOF'
[Unit]
Requires=canaan-ssh-host-keys.service
After=canaan-ssh-host-keys.service
EOF
rm -f "${ROOTFS_DIR}"/etc/ssh/ssh_host_*_key "${ROOTFS_DIR}"/etc/ssh/ssh_host_*_key.pub

on_chroot -se -- "${ENABLE_SSH:-0}" <<'CHROOT'
set -eu
systemctl enable NetworkManager.service systemd-resolved.service systemd-timesyncd.service
if [ "$1" = 1 ]; then
    systemctl enable ssh.service
else
    systemctl disable ssh.service
    if [ -f /usr/lib/systemd/system/ssh.socket ] || [ -f /lib/systemd/system/ssh.socket ]; then
        systemctl disable ssh.socket
    fi
fi
CHROOT
log "Debian userspace configured; SSH enabled: ${ENABLE_SSH:-0}"
