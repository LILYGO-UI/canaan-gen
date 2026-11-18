#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
source "${BASE_DIR:?}/scripts/common"
source "${BASE_DIR}/boards/${BOARD:?}/board.conf"

log 'Checking the vendor kernel and modules before importing board files'
camera_check=(--board-profile "${LILYGO_BOARD_DIR:?}/profile.json")
if [[ "${BSP_CAMERA_RUNTIME:-0}" == 1 ]]; then
    camera_check+=(--require-camera-userspace)
fi
python3 "${BASE_DIR}/scripts/check-bsp.py" --bsp "${BSP_DIR:?}" \
    --kernel-config "${BSP_KERNEL_CONFIG:?}" --template "${BSP_IMAGE:?}" "${camera_check[@]}"

# Debian uses merged /usr. Copy only hardware modules/firmware, never the
# Buildroot C library, dynamic loader or other userspace libraries.
install -d -m 0755 "${ROOTFS_DIR}/usr/lib/modules" "${ROOTFS_DIR}/usr/lib/firmware"
rsync -aH --chown=0:0 --safe-links --exclude='/*/build' --exclude='/*/source' \
    "${BSP_DIR}/target/lib/modules/" "${ROOTFS_DIR}/usr/lib/modules/"
if [[ -d "${BSP_DIR}/target/lib/firmware" ]]; then
    rsync -aH --chown=0:0 --safe-links "${BSP_DIR}/target/lib/firmware/" "${ROOTFS_DIR}/usr/lib/firmware/"
fi
# Buildroot's build/source links point outside the rootfs; do not ship them.
for modules_dir in "${ROOTFS_DIR}"/usr/lib/modules/*; do
    [[ -d "$modules_dir" ]] || continue
    for link in build source; do
        [[ ! -L "${modules_dir}/${link}" ]] || rm -f "${modules_dir}/${link}"
    done
    depmod -b "${ROOTFS_DIR}" -a "$(basename "$modules_dir")"
done

# Board-owned Debian configuration is shared with the Linux patch source: the
# services that take over the vendor shell's hardware work (audio route and
# amplifier, modem/LoRa enable lines, battery gauge and charger), udev access
# rules, and the device paths handed to the LILYGO applications.
rsync -aH --chown=0:0 --safe-links "${LILYGO_BOARD_DIR}/debian/rootfs/" "${ROOTFS_DIR}/"
board_support=(python3 "${BASE_DIR}/scripts/board-support.py" --board-dir "${LILYGO_BOARD_DIR}")
# Helpers that must be native code are compiled against Debian's own libc.
board_builds="$("${board_support[@]}" --list build)"
while read -r source output; do
    [[ -n "$source" ]] || continue
    on_chroot -se -- "/$source" "/$output" <<'CHROOT_BOARD_BUILD'
cc -shared -fPIC -O2 -Wall -Wextra -Werror -o "$2" "$1"
chmod 0644 "$2"
CHROOT_BOARD_BUILD
done <<< "$board_builds"
board_services="$("${board_support[@]}" --list services)"
# shellcheck disable=SC2086 # unit names contain no whitespace or globs
on_chroot -se -- $board_services <<'CHROOT_BOARD_SERVICES'
systemctl enable "$@"
CHROOT_BOARD_SERVICES

if [[ "${BSP_CAMERA_RUNTIME:-0}" == 1 ]]; then
    log 'Importing the allowlisted T-Display-K230 camera runtime'
    install -d -m 0755 \
        "${ROOTFS_DIR}/usr/bin" "${ROOTFS_DIR}/usr/lib" "${ROOTFS_DIR}/etc/vvcam" \
        "${ROOTFS_DIR}/etc/udev/rules.d" "${ROOTFS_DIR}/etc/systemd/system" \
        "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants"
    install -m 0755 "${BSP_DIR}/target/usr/bin/isp_media_server" "${ROOTFS_DIR}/usr/bin/isp_media_server"
    install -m 0755 "${BSP_DIR}/target/usr/bin/media-ctl" "${ROOTFS_DIR}/usr/bin/media-ctl"
    install -m 0755 "${BSP_DIR}/target/usr/bin/v4l2-sysfs-path" "${ROOTFS_DIR}/usr/bin/v4l2-sysfs-path"
    install -m 0755 "${BSP_DIR}/target/usr/lib/libvvcam.so" "${ROOTFS_DIR}/usr/lib/libvvcam.so"
    install -m 0755 "${BSP_DIR}/target/usr/lib/libmxml.so.1.6" "${ROOTFS_DIR}/usr/lib/libmxml.so.1.6"
    ln -sfn libmxml.so.1.6 "${ROOTFS_DIR}/usr/lib/libmxml.so.1"
    ln -sfn libmxml.so.1.6 "${ROOTFS_DIR}/usr/lib/libmxml.so"
    rsync -aH --chown=0:0 --safe-links "${BSP_DIR}/target/etc/vvcam/" "${ROOTFS_DIR}/etc/vvcam/"

    install -d -m 0755 "${ROOTFS_DIR}/usr/lib/lilygo-k230-camera"
    install -m 0644 "${BASE_DIR}/stage2/isp-pthread-compat.c" \
        "${ROOTFS_DIR}/usr/lib/lilygo-k230-camera/isp-pthread-compat.c"
    on_chroot <<'CHROOT_CAMERA_COMPAT'
cc -shared -fPIC -O2 -Wall -Wextra -pthread \
    /usr/lib/lilygo-k230-camera/isp-pthread-compat.c -ldl \
    -o /usr/lib/lilygo-k230-camera/isp-pthread-compat.so
CHROOT_CAMERA_COMPAT

    # The vendor image uses a Buildroot init script. Recreate only its camera
    # part as a native systemd unit; do not carry over mdev, vo_init or the
    # rest of the Buildroot boot sequence.
    cat > "${ROOTFS_DIR}/etc/systemd/system/lilygo-k230-camera-bsp.service" <<'EOF_CAMERA_SERVICE'
[Unit]
Description=LILYGO T-Display-K230 camera ISP
After=systemd-modules-load.service systemd-sysctl.service systemd-udev-settle.service
Wants=systemd-udev-settle.service
Before=lilygo-ui-launcher.service
ConditionPathExists=/usr/bin/isp_media_server
ConditionPathExists=/usr/lib/libvvcam.so

[Service]
Type=simple
Environment=ISP_MEDIA_SENSOR_DRIVER=/usr/lib/libvvcam.so
Environment=LD_PRELOAD=/usr/lib/lilygo-k230-camera/isp-pthread-compat.so
ExecStartPre=/sbin/modprobe vvcam_isp
ExecStartPre=/sbin/modprobe vvcam_mipi
ExecStartPre=/sbin/modprobe vvcam_vb
ExecStartPre=/sbin/modprobe vvcam_isp_subdev
ExecStartPre=/sbin/modprobe vvcam_video mcm_mask=1
ExecStartPre=/sbin/modprobe nonai2d
ExecStart=/usr/bin/isp_media_server
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF_CAMERA_SERVICE
    # From the vendor image's sysctl.conf: isp_media_server creates SCHED_RR
    # threads, which CONFIG_RT_GROUP_SCHED refuses outside the root cgroup
    # while realtime throttling is enabled. Device access and the vendor's
    # /dev/video_* names come from the board's udev rules.
    install -d -m 0755 "${ROOTFS_DIR}/etc/sysctl.d"
    cat > "${ROOTFS_DIR}/etc/sysctl.d/60-lilygo-k230-camera.conf" <<'EOF_CAMERA_SYSCTL'
# Allow SCHED_RR for isp_media_server (setting of the vendor BSP image).
kernel.sched_rt_runtime_us = -1
EOF_CAMERA_SYSCTL
    ln -sfn /etc/systemd/system/lilygo-k230-camera-bsp.service \
        "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/lilygo-k230-camera-bsp.service"
fi

install -d -m 0755 "${ROOTFS_DIR}/etc/modules-load.d" "${ROOTFS_DIR}/boot"
printf '%s\n' '# Board drivers required by T-Display-K230.' > "${ROOTFS_DIR}/etc/modules-load.d/t-display-k230.conf"
for module in ${BSP_MODULES}; do
    printf '%s\n' "$module" >> "${ROOTFS_DIR}/etc/modules-load.d/t-display-k230.conf"
done
# The vendor image also loads these whenever its BSP was built with them.
for module in ${BSP_OPTIONAL_MODULES:-}; do
    if find "${ROOTFS_DIR}/usr/lib/modules" -name "${module}.ko*" -print -quit | grep -q .; then
        printf '%s\n' "$module" >> "${ROOTFS_DIR}/etc/modules-load.d/t-display-k230.conf"
    fi
done

# Keep /boot mounted for diagnostics and deliberate BSP upgrades. The vendor
# bootloader identifies p2 by partition number; the generated rootfs keeps its
# UUID too, and userspace mounts use UUIDs rather than Linux MMC enumeration.
python3 - "${BASE_DIR}" "${BSP_IMAGE:?}" "${ROOTFS_DIR}" <<'PY'
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location('canaan_image', Path(sys.argv[1]) / 'scripts/image.py')
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)
layout = image.inspect(sys.argv[2])
parts = {entry['number']: entry for entry in layout['partitions']}
root_uuid = parts[2]['filesystem']['uuid']
boot_uuid = parts[1]['filesystem']['uuid']
(Path(sys.argv[3]) / 'etc/fstab').write_text(
    '# Generated by canaan-gen. Vendor boot/root filesystem identities are preserved.\n'
    f'UUID={root_uuid} / ext4 defaults,noatime,errors=remount-ro 0 1\n'
    f'UUID={boot_uuid} /boot ext4 defaults,noatime 0 2\n'
)
PY

# Expand the exported p2 and ext4 to the actual card at boot. No persistent
# completion marker: retries also recover a partially completed expansion.
install -d -m 0755 "${ROOTFS_DIR}/usr/sbin" "${ROOTFS_DIR}/etc/systemd/system"
install -m 0755 "${BASE_DIR}/stage2/canaan-grow-rootfs.py" \
    "${ROOTFS_DIR}/usr/sbin/canaan-grow-rootfs"
install -m 0644 "${BASE_DIR}/stage2/canaan-grow-rootfs.service" \
    "${ROOTFS_DIR}/etc/systemd/system/canaan-grow-rootfs.service"
on_chroot <<'CHROOT_GROW'
systemctl enable canaan-grow-rootfs.service
CHROOT_GROW

# Serial login is the recovery path before the optional UI packages are ready.
# Do not use the stale console_port environment value: the BSP's actual boot
# arguments and DTB stdout-path select ttyS0 at 115200 baud.
on_chroot -se -- "${SERIAL_CONSOLE}" <<'CHROOT'
systemctl enable "serial-getty@$1.service"
CHROOT
install -d -m 0755 "${ROOTFS_DIR}/etc/systemd/system/serial-getty@${SERIAL_CONSOLE}.service.d"
cat > "${ROOTFS_DIR}/etc/systemd/system/serial-getty@${SERIAL_CONSOLE}.service.d/10-canaan-gen.conf" <<EOF_GETTY
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -- \\u' --keep-baud ${SERIAL_BAUD},57600,38400,9600 - \$TERM
EOF_GETTY
# Retain the exact configuration for future BSP checks without pretending that
# this is a Debian kernel package. No automatic generic-kernel installation.
install -d -m 0755 "${ROOTFS_DIR}/usr/share/canaan-gen"
install -m 0644 "${BSP_KERNEL_CONFIG}" "${ROOTFS_DIR}/usr/share/canaan-gen/kernel.config"
log 'Imported vendor modules/firmware and configured boot mount, Wi-Fi and serial recovery'
