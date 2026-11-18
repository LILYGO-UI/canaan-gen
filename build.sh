#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${BASE_DIR}/config"
CHECK_ONLY=0
ROOTFS_ONLY=0
BSP_ONLY=0
BSP_PLAN=0
while (($#)); do
    case "$1" in
        -c|--config) [[ $# -ge 2 ]] || { echo 'Missing config path' >&2; exit 2; }; CONFIG_FILE=$2; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        --rootfs-only) ROOTFS_ONLY=1; shift ;;
        --bsp-only) BSP_ONLY=1; shift ;;
        --plan-bsp) BSP_PLAN=1; shift ;;
        -h|--help) echo 'Usage: build.sh [-c config] [--check] [--rootfs-only] [--bsp-only] [--plan-bsp]'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -f "$CONFIG_FILE" ]] || { echo "Copy config.example to config or pass -c FILE" >&2; exit 1; }
# Config contains trusted shell code; do not accept unreviewed configurations.
source "$CONFIG_FILE"
source "${BASE_DIR}/scripts/common"
export BASE_DIR BOARD=${BOARD:-t-display-k230} RELEASE=${RELEASE:-trixie} ARCH=riscv64
[[ "$BOARD" == t-display-k230 ]] || die "unsupported board: $BOARD"
[[ "$RELEASE" == trixie ]] || die 'this profile currently supports Debian 13 (trixie)'
source "${BASE_DIR}/boards/${BOARD}/board.conf"
export LILYGO_DTOVERLAYS_DIR=${CANAAN_CONTAINER_DTOVERLAYS_DIR:-${LILYGO_DTOVERLAYS_DIR:-${BASE_DIR}/../lilygo-linux-dtoverlays}}
[[ -d "$LILYGO_DTOVERLAYS_DIR" ]] || die 'set LILYGO_DTOVERLAYS_DIR to the lilygo-linux-dtoverlays checkout'
LILYGO_DTOVERLAYS_DIR="$(cd -- "$LILYGO_DTOVERLAYS_DIR" && pwd)"
export LILYGO_BOARD_DIR="${LILYGO_DTOVERLAYS_DIR}/boards/T-Display-K230"
export BSP_MODE=${BSP_MODE:-prebuilt}
[[ "$BSP_MODE" == source || "$BSP_MODE" == prebuilt ]] || die 'BSP_MODE must be source or prebuilt'
if ((BSP_ONLY || BSP_PLAN)); then
    [[ "$BSP_MODE" == source ]] || die '--bsp-only/--plan-bsp require BSP_MODE=source'
fi
if ((BSP_PLAN)); then
    exec python3 "$BASE_DIR/scripts/build-bsp.py" plan --board-dir "$LILYGO_BOARD_DIR" \
        --work-dir "${BSP_WORK_DIR:-$BASE_DIR/.cache/bsp-source}"
fi
export DEBIAN_MIRROR=${DEBIAN_MIRROR:-https://deb.debian.org/debian}
export TARGET_HOSTNAME=${TARGET_HOSTNAME:-t-display-k230}
export ACCOUNT_SETUP=${ACCOUNT_SETUP:-preset}
export FIRST_USER_NAME=${FIRST_USER_NAME:-lilygo}
export FIRST_USER_PASSWORD_HASH=${FIRST_USER_PASSWORD_HASH:-}
export SSH_AUTHORIZED_KEYS_FILE=${CANAAN_CONTAINER_SSH_KEYS:-${SSH_AUTHORIZED_KEYS_FILE:-}}
export ENABLE_SSH=${ENABLE_SSH:-0} TIMEZONE=${TIMEZONE:-Asia/Shanghai} LOCALE=${LOCALE:-en_US.UTF-8}
export LILYGO_UI_INSTALL=${LILYGO_UI_INSTALL:-0}
export LILYGO_UI_APT_URL=${LILYGO_UI_APT_URL:-https://lilygo-ui.github.io/packages}
export LILYGO_UI_APT_KEY_FILE=${CANAAN_CONTAINER_APT_KEY:-${LILYGO_UI_APT_KEY_FILE:-${BASE_DIR}/keys/lilygo-ui-archive-keyring.gpg}}
export LILYGO_UI_PACKAGES=${LILYGO_UI_PACKAGES:-$(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "${BASE_DIR}/stage3/packages")}
export ROOTFS_OVERLAY=${CANAAN_CONTAINER_OVERLAY:-${ROOTFS_OVERLAY:-}}
if [[ "$BSP_MODE" == source ]]; then
    export BSP_CAMERA_RUNTIME=${BSP_CAMERA_RUNTIME:-1}
else
    export BSP_CAMERA_RUNTIME=${BSP_CAMERA_RUNTIME:-0}
fi
export WORK_DIR=${CANAAN_CONTAINER_WORK_DIR:-${WORK_DIR:-${BASE_DIR}/work}}
export DEPLOY_DIR=${CANAAN_CONTAINER_DEPLOY_DIR:-${DEPLOY_DIR:-${BASE_DIR}/deploy}}
IMG_NAME=${IMG_NAME:-canaan-debian-${BOARD}}
ROOTFS_EXTRA_MB=${ROOTFS_EXTRA_MB:-512}
DEPLOY_COMPRESSION=${DEPLOY_COMPRESSION:-xz}
EXPORT_SDK=${EXPORT_SDK:-1}
[[ "$EXPORT_SDK" == 0 || "$EXPORT_SDK" == 1 ]] || die 'EXPORT_SDK must be 0 or 1'
[[ "$IMG_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || die 'invalid IMG_NAME'
[[ "$TARGET_HOSTNAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] || die 'invalid TARGET_HOSTNAME'
[[ "$FIRST_USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,30}$ && "$FIRST_USER_NAME" != root ]] || die 'invalid FIRST_USER_NAME'
[[ "$ACCOUNT_SETUP" == preset || "$ACCOUNT_SETUP" == launcher ]] || die 'ACCOUNT_SETUP must be preset or launcher'
[[ "$ENABLE_SSH" == 0 || "$ENABLE_SSH" == 1 ]] || die 'ENABLE_SSH must be 0 or 1'
[[ "$BSP_CAMERA_RUNTIME" == 0 || "$BSP_CAMERA_RUNTIME" == 1 ]] || die 'BSP_CAMERA_RUNTIME must be 0 or 1'
[[ "$ROOTFS_EXTRA_MB" =~ ^[0-9]+$ ]] && ((ROOTFS_EXTRA_MB >= 128)) || die 'ROOTFS_EXTRA_MB must be >= 128'
[[ "$DEPLOY_COMPRESSION" == xz || "$DEPLOY_COMPRESSION" == none ]] || die 'DEPLOY_COMPRESSION must be xz or none'
if [[ -n "$SSH_AUTHORIZED_KEYS_FILE" ]]; then
    [[ -s "$SSH_AUTHORIZED_KEYS_FILE" ]] || die 'SSH_AUTHORIZED_KEYS_FILE is missing/empty'
fi
if [[ "$ACCOUNT_SETUP" == preset && "$ENABLE_SSH" == 1 && -z "$SSH_AUTHORIZED_KEYS_FILE" && -z "$FIRST_USER_PASSWORD_HASH" ]]; then
    die 'ENABLE_SSH=1 requires an SSH authorized_keys file or password hash'
fi
if [[ "$ACCOUNT_SETUP" == launcher ]]; then
    [[ "$LILYGO_UI_INSTALL" != 0 ]] || die 'Launcher account setup requires LILYGO UI installation'
    [[ -z "$FIRST_USER_PASSWORD_HASH" && -z "$SSH_AUTHORIZED_KEYS_FILE" ]] ||
        die 'Launcher account setup creates credentials on the device; leave password/key unset'
fi
case "$LILYGO_UI_INSTALL" in
    0) ;;
    apt) [[ -n "$LILYGO_UI_APT_URL" && -s "$LILYGO_UI_APT_KEY_FILE" ]] || die 'apt mode requires URL and pinned local keyring' ;;
    local) die 'LILYGO_UI_INSTALL=local is no longer supported; use apt with the signed LILYGO UI repository.' ;;
    *) die 'LILYGO_UI_INSTALL must be 0 or apt' ;;
esac
if [[ "$ACCOUNT_SETUP" == launcher ]]; then
    [[ "$LILYGO_UI_PACKAGES" =~ (^|[[:space:]])lilygo-ui-launcher(=[A-Za-z0-9.+:~_-]+)?($|[[:space:]]) ]] ||
        die 'ACCOUNT_SETUP=launcher requires lilygo-ui-launcher in LILYGO_UI_PACKAGES; add its published riscv64 package or use ACCOUNT_SETUP=preset with your intended account configuration'
fi
[[ -z "$ROOTFS_OVERLAY" || -d "$ROOTFS_OVERLAY" ]] || die 'ROOTFS_OVERLAY directory does not exist'
python3 "${BASE_DIR}/scripts/board-support.py" --repo "$LILYGO_DTOVERLAYS_DIR"
if [[ "$BSP_MODE" == source ]]; then
    [[ -z "${BSP_IMAGE:-}" && -z "${BSP_KERNEL_CONFIG:-}" && -z "${CANAAN_CONTAINER_BSP_IMAGE:-}" && -z "${CANAAN_CONTAINER_KERNEL_CONFIG:-}" ]] ||
        die 'BSP_MODE=source must use its matching Image/config/template; unset BSP_IMAGE and BSP_KERNEL_CONFIG'
    bsp_work=${CANAAN_CONTAINER_BSP_WORK_DIR:-${BSP_WORK_DIR:-$BASE_DIR/.cache/bsp-source}}
    bsp_args=(--board-dir "$LILYGO_BOARD_DIR" --work-dir "$bsp_work" --jobs "${BSP_JOBS:-4}" --min-free-gib "${BSP_MIN_FREE_GIB:-60}")
    if ((CHECK_ONLY)) || [[ -n "${CANAAN_CONTAINER_BSP_DIR:-}" ]]; then
        python3 "$BASE_DIR/scripts/build-bsp.py" check "${bsp_args[@]}"
    else
        python3 "$BASE_DIR/scripts/build-bsp.py" build "${bsp_args[@]}"
    fi
    export BSP_DIR
    BSP_DIR="$(python3 "$BASE_DIR/scripts/build-bsp.py" path "${bsp_args[@]}")"
else
    export BSP_DIR=${CANAAN_CONTAINER_BSP_DIR:-${BSP_DIR:-}}
fi
[[ -d "$BSP_DIR/target" ]] || die 'BSP_DIR must contain a completed target/ directory'
BSP_DIR="$(cd -- "$BSP_DIR" && pwd)"
export BSP_IMAGE=${CANAAN_CONTAINER_BSP_IMAGE:-${BSP_IMAGE:-${BSP_DIR}/images/sysimage-sdcard.img}}
export BSP_KERNEL_CONFIG=${CANAAN_CONTAINER_KERNEL_CONFIG:-${BSP_KERNEL_CONFIG:-}}
if [[ -z "$BSP_KERNEL_CONFIG" ]]; then
    shopt -s nullglob
    kernel_configs=("$BSP_DIR"/build/linux-*/.config)
    [[ ${#kernel_configs[@]} -eq 1 ]] || die 'set BSP_KERNEL_CONFIG to the exact built kernel .config'
    BSP_KERNEL_CONFIG=${kernel_configs[0]}
fi
[[ -f "$BSP_KERNEL_CONFIG" ]] || die 'built kernel configuration is missing'
python3 "${BASE_DIR}/scripts/image.py" inspect --image "$BSP_IMAGE" --root-partition "$ROOT_PARTITION" >/dev/null
camera_check=(python3 "${BASE_DIR}/scripts/check-bsp.py" --bsp "$BSP_DIR" --kernel-config "$BSP_KERNEL_CONFIG"
    --template "$BSP_IMAGE" --board-profile "$LILYGO_BOARD_DIR/profile.json")
if [[ "$BSP_CAMERA_RUNTIME" == 1 ]]; then
    camera_check+=(--require-camera-userspace)
fi
"${camera_check[@]}"
if ((BSP_ONLY)); then
    log "Source BSP is ready: $BSP_DIR"
    exit 0
fi
if [[ "$LILYGO_UI_INSTALL" == apt ]]; then
    python3 "${BASE_DIR}/scripts/check-apt.py" --url "$LILYGO_UI_APT_URL" \
        --key "$LILYGO_UI_APT_KEY_FILE" --packages "$LILYGO_UI_PACKAGES"
fi
if ((CHECK_ONLY)); then
    log 'Inputs and image layout passed preflight; no rootfs build or boot test performed.'
    exit 0
fi
[[ "$(uname -s)" == Linux ]] || die 'build requires Linux; on macOS use build-docker.sh'
[[ $EUID -eq 0 ]] || die 'native build requires root for chroot/mount; use sudo or build-docker.sh'
for command in debootstrap chroot mount umount mountpoint rsync mke2fs e2fsck depmod xz sha256sum; do
    command -v "$command" >/dev/null || die "missing build tool: $command (see depends)"
done
if [[ "$(uname -m)" != riscv64 ]]; then
    [[ -r /proc/sys/fs/binfmt_misc/qemu-riscv64 ]] || die 'enable qemu-riscv64 binfmt on the Linux host first (see README)'
    grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-riscv64 || die 'qemu-riscv64 binfmt is disabled'
    grep -q '^flags:.*F' /proc/sys/fs/binfmt_misc/qemu-riscv64 || die 'qemu-riscv64 binfmt requires the F flag for chroot'
fi
umask 022
mkdir -p "$WORK_DIR" "$DEPLOY_DIR"
RUN_DIR="$(mktemp -d "${WORK_DIR}/run.XXXXXXXX")"
export RUN_DIR ROOTFS_DIR="${RUN_DIR}/rootfs"
mkdir "$ROOTFS_DIR"
# Keep the board payload and its recorded hashes fixed throughout this build.
python3 "${BASE_DIR}/scripts/board-support.py" --repo "$LILYGO_DTOVERLAYS_DIR" \
    --snapshot "$RUN_DIR/board-support"
export LILYGO_BOARD_DIR="$RUN_DIR/board-support"
LOG_FILE="${RUN_DIR}/build.log"
exec > >(tee -a "$LOG_FILE") 2>&1
cleanup() {
    local result=$?
    trap - EXIT INT TERM
    if ! unmount_rootfs; then
        log "Unmount failed; preserve $RUN_DIR and unmount it before cleanup."
        result=1
    fi
    log "Build exit $result; work retained at $RUN_DIR"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
log "Building $BOARD / Debian $RELEASE / $ARCH"
# A single rootfs avoids copying multi-GB trees between stages. Each invocation
# starts in a new work directory; failed runs are retained for diagnosis.
"${BASE_DIR}/stage0/00-run.sh"
mount_rootfs
for stage in stage1 stage2 stage3; do
    log "Begin $stage"
    "${BASE_DIR}/${stage}/00-run.sh"
done
apply_rootfs_overlay
on_chroot <<'CHROOT_GROW'
test -x /usr/sbin/canaan-grow-rootfs
command -v growpart
command -v partx
command -v resize2fs
command -v findmnt
systemctl is-enabled canaan-grow-rootfs.service
CHROOT_GROW
python3 "${BASE_DIR}/scripts/board-support.py" --board-dir "$LILYGO_BOARD_DIR" --rootfs "$ROOTFS_DIR"
board_packages="$(python3 "${BASE_DIR}/scripts/board-support.py" --board-dir "$LILYGO_BOARD_DIR" --list packages)"
board_services="$(python3 "${BASE_DIR}/scripts/board-support.py" --board-dir "$LILYGO_BOARD_DIR" --list services)"
# shellcheck disable=SC2086 # package and unit names contain no whitespace or globs
on_chroot -se -- $board_packages <<'CHROOT_BOARD_PACKAGES'
dpkg-query -W -f='${Package} ${Status}\n' "$@" | grep -c ' install ok installed$' | grep -qx "$#"
CHROOT_BOARD_PACKAGES
# shellcheck disable=SC2086
on_chroot -se -- $board_services <<'CHROOT_BOARD'
command -v aplay
command -v arecord
command -v amixer
command -v alsactl
systemctl is-enabled "$@"
# -B: do not leave bytecode that no package owns in the image.
python3 -B -c 'import gpiod; assert hasattr(gpiod, "request_lines"), "libgpiod v2 is required"'
python3 -B -c 'import smbus2, lilygo_k230_board'
python3 -B -c 'import ctypes; ctypes.CDLL("/usr/lib/lilygo-k230/libk230mmio.so").k230_mmio_write32'
for helper in lilygo-k230-audio-init lilygo-k230-peripherals lilygo-k230-power; do
    python3 -B "/usr/libexec/$helper" --help >/dev/null
done
# Groups that the board udev rules assign and the first user joins.
getent group audio dialout gpio i2c input spi video >/dev/null
CHROOT_BOARD
on_chroot <<'CHROOT'
apt-get clean
rm -f /etc/ssh/ssh_host_*
rm -f /var/lib/systemd/random-seed
rm -f /var/lib/dbus/machine-id
: > /etc/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +
rm -f /etc/resolv.conf
ln -s /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
CHROOT
rm -f "$ROOTFS_DIR/usr/sbin/policy-rc.d"
if [[ "$ACCOUNT_SETUP" == launcher ]]; then
    python3 "$BASE_DIR/scripts/check-ui-rootfs.py" --root "$ROOTFS_DIR" --enable-ssh "$ENABLE_SSH"
fi
# Capture exact installed versions before detaching the rootfs.
on_chroot > "$RUN_DIR/packages.tsv" <<'CHROOT'
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n'
CHROOT
unmount_rootfs
python3 "${BASE_DIR}/scripts/image.py" inspect --image "$BSP_IMAGE" > "$RUN_DIR/template.json"
root_uuid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["partitions"][1]["filesystem"]["uuid"])' "$RUN_DIR/template.json")"
used_mb="$(du -sm "$ROOTFS_DIR" | cut -f1)"
root_mb=$((used_mb + used_mb / 5 + ROOTFS_EXTRA_MB))
root_image="$RUN_DIR/rootfs.ext4"
truncate -s "${root_mb}M" "$root_image"
mke2fs -q -t ext4 -F -L rootfs -U "$root_uuid" -m 1 -d "$ROOTFS_DIR" "$root_image"
e2fsck -f -n "$root_image"
artifact_prefix="${IMG_NAME}-$(date -u +%Y%m%dT%H%M%SZ)"
if ((ROOTFS_ONLY)); then
    image_path="$RUN_DIR/${artifact_prefix}.rootfs.ext4"
    mv "$root_image" "$image_path"
else
    image_path="$RUN_DIR/${artifact_prefix}.img"
    python3 "${BASE_DIR}/scripts/image.py" assemble --image "$BSP_IMAGE" --rootfs "$root_image" \
        --output "$image_path" --root-partition "$ROOT_PARTITION" > "$RUN_DIR/output-layout.json"
fi
if [[ "$DEPLOY_COMPRESSION" == xz ]]; then
    xz -T2 -1 "$image_path"
    image_path="${image_path}.xz"
fi
cp "$RUN_DIR/packages.tsv" "$DEPLOY_DIR/${artifact_prefix}.packages.tsv"
python3 "${BASE_DIR}/scripts/provenance.py" --base "$BASE_DIR" --bsp "$BSP_DIR" --kernel-config "$BSP_KERNEL_CONFIG" \
    --template "$BSP_IMAGE" --artifact "$image_path" --packages "$RUN_DIR/packages.tsv" \
    --board-support "$LILYGO_BOARD_DIR" \
    --output "$DEPLOY_DIR/${artifact_prefix}.json"
cp "$image_path" "$DEPLOY_DIR/"
(cd "$DEPLOY_DIR" && sha256sum "$(basename "$image_path")" > "${artifact_prefix}.sha256")
log "Created $DEPLOY_DIR/$(basename "$image_path")"
if [[ "$EXPORT_SDK" == 1 ]]; then
    TMPDIR="$RUN_DIR" python3 "$BASE_DIR/scripts/export_sdk.py" \
        "$DEPLOY_DIR/$(basename "$image_path")" "$DEPLOY_DIR" --build-rootfs "$ROOTFS_DIR"
fi
