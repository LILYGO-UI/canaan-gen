#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${BASE_DIR}/config"
BUILD_ARGS=(-c /config)
BSP_ONLY=0
BSP_PLAN=0
CHECK_ONLY=0
while (($#)); do
    case "$1" in
        -c|--config) [[ $# -ge 2 ]] || exit 2; CONFIG_FILE=$2; shift 2 ;;
        --check) CHECK_ONLY=1; BUILD_ARGS+=("$1"); shift ;;
        --rootfs-only) BUILD_ARGS+=("$1"); shift ;;
        --bsp-only) BSP_ONLY=1; shift ;;
        --plan-bsp) BSP_PLAN=1; shift ;;
        -h|--help) echo 'Usage: build-docker.sh [-c config] [--check] [--rootfs-only] [--bsp-only] [--plan-bsp]'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -f "$CONFIG_FILE" ]] || { echo 'Copy config.example to config or pass -c FILE' >&2; exit 1; }
CONFIG_FILE="$(cd -- "$(dirname -- "$CONFIG_FILE")" && pwd)/$(basename -- "$CONFIG_FILE")"
source "$CONFIG_FILE"
DOCKER=${DOCKER:-docker}
case "${LILYGO_UI_INSTALL:-0}" in
    0|apt) ;;
    local) echo 'LILYGO_UI_INSTALL=local is no longer supported; use apt with the signed LILYGO UI repository.' >&2; exit 1 ;;
    *) echo 'LILYGO_UI_INSTALL must be 0 or apt' >&2; exit 1 ;;
esac
LILYGO_DTOVERLAYS_DIR=${LILYGO_DTOVERLAYS_DIR:-${BASE_DIR}/../lilygo-linux-dtoverlays}
[[ -f "$LILYGO_DTOVERLAYS_DIR/boards/T-Display-K230/profile.json" ]] || {
    echo 'Set LILYGO_DTOVERLAYS_DIR to a checkout with boards/T-Display-K230/profile.json' >&2; exit 1;
}
BSP_MODE=${BSP_MODE:-prebuilt}
[[ "$BSP_MODE" == source || "$BSP_MODE" == prebuilt ]] || { echo 'BSP_MODE must be source or prebuilt' >&2; exit 1; }
if [[ "$BSP_MODE" == source ]]; then
    [[ -z "${BSP_IMAGE:-}" && -z "${BSP_KERNEL_CONFIG:-}" ]] || {
        echo 'BSP_MODE=source must use its own matching Image/config/template; unset BSP_IMAGE and BSP_KERNEL_CONFIG' >&2; exit 1;
    }
    BSP_CACHE_VOLUME=${BSP_CACHE_VOLUME:-canaan-gen-bsp-cache}
    [[ "$BSP_CACHE_VOLUME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo 'Invalid BSP_CACHE_VOLUME' >&2; exit 1; }
    BSP_DOCKER_IMAGE=${BSP_DOCKER_IMAGE:-canaan-gen-bsp:ubuntu24.04}
    BSP_MIN_FREE_GIB=${BSP_MIN_FREE_GIB:-60}
    BSP_JOBS=${BSP_JOBS:-4}
    BSP_ARGS=(--board-dir /inputs/lilygo-linux-dtoverlays/boards/T-Display-K230 --work-dir /bsp-work
        --jobs "$BSP_JOBS" --min-free-gib "$BSP_MIN_FREE_GIB")
    BSP_DIR="$(python3 "$BASE_DIR/scripts/build-bsp.py" path \
        --board-dir "$LILYGO_DTOVERLAYS_DIR/boards/T-Display-K230" --work-dir /bsp-work)"
    if ((BSP_PLAN)); then
        exec python3 "$BASE_DIR/scripts/build-bsp.py" plan \
            --board-dir "$LILYGO_DTOVERLAYS_DIR/boards/T-Display-K230" --work-dir /bsp-work
    fi
    # macOS Docker disks are sparse files: guest free space alone is not enough.
    # Check the host before Docker builds/downloads, and the guest in the builder.
    if [[ "$(uname -s)" == Darwin && "$CHECK_ONLY" == 0 ]]; then
        bsp_cached=0
        if "$DOCKER" image inspect "$BSP_DOCKER_IMAGE" >/dev/null 2>&1 && \
           "$DOCKER" volume inspect "$BSP_CACHE_VOLUME" >/dev/null 2>&1; then
            if "$DOCKER" run --rm --pull=never --network=none --platform linux/amd64 \
                --entrypoint /usr/bin/test \
                --mount "type=volume,src=$BSP_CACHE_VOLUME,dst=/bsp-work,readonly" \
                "$BSP_DOCKER_IMAGE" -s "$BSP_DIR/SOURCE-BUILD.json"; then
                bsp_cached=1
            fi
        fi
        # The marker only selects the space budget; the builder still verifies
        # every sealed artifact before reuse. A malformed cache never falls back.
        if ((bsp_cached == 0)); then
            python3 "$BASE_DIR/scripts/build-bsp.py" space --work-dir "$BASE_DIR" --min-free-gib "$BSP_MIN_FREE_GIB"
        elif ((BSP_ONLY == 0)); then
            python3 "$BASE_DIR/scripts/build-bsp.py" space --work-dir "$BASE_DIR" --min-free-gib 15
        fi
    fi
else
    ((BSP_ONLY == 0 && BSP_PLAN == 0)) || { echo '--bsp-only/--plan-bsp require BSP_MODE=source' >&2; exit 1; }
    [[ -d "${BSP_DIR:-}/target" ]] || { echo 'BSP_DIR must contain a completed target/' >&2; exit 1; }
    BSP_DIR="$(cd -- "$BSP_DIR" && pwd)"
fi
"$DOCKER" info >/dev/null
CONTAINER_NAME=${CONTAINER_NAME:-canaan-gen-$(date +%Y%m%d-%H%M%S)-$$}
DOCKER_IMAGE=${DOCKER_IMAGE:-canaan-gen:trixie}
DEPLOY_DIR=${DEPLOY_DIR:-${BASE_DIR}/deploy}
mkdir -p "$DEPLOY_DIR"
DEPLOY_DIR="$(cd -- "$DEPLOY_DIR" && pwd)"
if [[ "$BSP_MODE" == source ]]; then
    LILYGO_DTOVERLAYS_DIR="$(cd -- "$LILYGO_DTOVERLAYS_DIR" && pwd)"
    "$DOCKER" build --platform linux/amd64 -f "$BASE_DIR/Dockerfile.bsp" -t "$BSP_DOCKER_IMAGE" "$BASE_DIR"
    bsp_action=build
    ((CHECK_ONLY == 0)) || bsp_action=check
    BSP_CONTAINER_NAME="${CONTAINER_NAME}-bsp"
    trap '"$DOCKER" stop -t 10 "$BSP_CONTAINER_NAME" >/dev/null || true' INT TERM
    set +e
    "$DOCKER" run --name "$BSP_CONTAINER_NAME" --platform linux/amd64 \
        --mount "type=volume,src=$BSP_CACHE_VOLUME,dst=/bsp-work" \
        --mount "type=volume,src=${BSP_CACHE_VOLUME}-toolchain,dst=/opt/toolchain" \
        --mount "type=bind,src=$LILYGO_DTOVERLAYS_DIR,dst=/inputs/lilygo-linux-dtoverlays,readonly" \
        "$BSP_DOCKER_IMAGE" "$bsp_action" "${BSP_ARGS[@]}" 2>&1 | tee "$DEPLOY_DIR/${BSP_CONTAINER_NAME}.log"
    bsp_result=${PIPESTATUS[0]}
    set -e
    if [[ "$bsp_result" != 0 ]]; then
        printf 'BSP build/check failed (exit %s); Debian packaging was not started. Log: %s\n' \
            "$bsp_result" "$DEPLOY_DIR/${BSP_CONTAINER_NAME}.log" >&2
        exit "$bsp_result"
    fi
    if ((BSP_ONLY)); then
        printf 'BSP build/check succeeded. Output: volume %s, %s\n' "$BSP_CACHE_VOLUME" "$BSP_DIR"
        exit 0
    fi
fi
# Work stays on a Linux filesystem. The container is deliberately retained so
# interrupted builds, logs and root-owned filesystem metadata remain inspectable.
DOCKER_ARGS=(run --name "$CONTAINER_NAME" --privileged
    --mount "type=bind,src=$CONFIG_FILE,dst=/config,readonly"
    --mount type=volume,dst=/work
    -e CANAAN_CONTAINER_WORK_DIR=/work
    -e CANAAN_CONTAINER_DEPLOY_DIR=/deploy)
if [[ "$BSP_MODE" == source ]]; then
    DOCKER_ARGS+=(--mount "type=volume,src=$BSP_CACHE_VOLUME,dst=/bsp-work,readonly"
        -e "CANAAN_CONTAINER_BSP_DIR=$BSP_DIR" -e CANAAN_CONTAINER_BSP_WORK_DIR=/bsp-work)
else
    DOCKER_ARGS+=(--mount "type=bind,src=$BSP_DIR,dst=/bsp,readonly" -e CANAAN_CONTAINER_BSP_DIR=/bsp)
fi
add_input() {
    local source=$1 destination=$2 variable=$3
    [[ -e "$source" ]] || { echo "Missing input: $source" >&2; exit 1; }
    source="$(cd -- "$(dirname -- "$source")" && pwd)/$(basename -- "$source")"
    DOCKER_ARGS+=(--mount "type=bind,src=$source,dst=$destination,readonly" -e "$variable=$destination")
}
[[ -z "${BSP_IMAGE:-}" ]] || add_input "$BSP_IMAGE" /inputs/template.img CANAAN_CONTAINER_BSP_IMAGE
[[ -z "${BSP_KERNEL_CONFIG:-}" ]] || add_input "$BSP_KERNEL_CONFIG" /inputs/kernel.config CANAAN_CONTAINER_KERNEL_CONFIG
[[ -z "${SSH_AUTHORIZED_KEYS_FILE:-}" ]] || add_input "$SSH_AUTHORIZED_KEYS_FILE" /inputs/authorized_keys CANAAN_CONTAINER_SSH_KEYS
if [[ "${LILYGO_UI_INSTALL:-0}" == apt && -n "${LILYGO_UI_APT_KEY_FILE:-}" ]]; then
    add_input "$LILYGO_UI_APT_KEY_FILE" /inputs/archive-keyring.gpg CANAAN_CONTAINER_APT_KEY
fi
[[ -z "${ROOTFS_OVERLAY:-}" ]] || add_input "$ROOTFS_OVERLAY" /inputs/overlay CANAAN_CONTAINER_OVERLAY
add_input "$LILYGO_DTOVERLAYS_DIR" /inputs/lilygo-linux-dtoverlays CANAAN_CONTAINER_DTOVERLAYS_DIR
"$DOCKER" build -t "$DOCKER_IMAGE" "$BASE_DIR"
trap '"$DOCKER" stop -t 10 "$CONTAINER_NAME" >/dev/null || true' INT TERM
set +e
# Preserve output as it arrives, even if the Docker daemon exits during a build.
"$DOCKER" "${DOCKER_ARGS[@]}" "$DOCKER_IMAGE" "${BUILD_ARGS[@]}" 2>&1 | tee "$DEPLOY_DIR/${CONTAINER_NAME}.log"
result=${PIPESTATUS[0]}
set -e
if [[ "$result" == 0 ]]; then
    # --check creates no deploy directory.
    if [[ " ${BUILD_ARGS[*]} " != *' --check '* ]]; then
        "$DOCKER" cp "$CONTAINER_NAME:/deploy/." "$DEPLOY_DIR/"
        printf 'Build succeeded; image artifacts exported to: %s\n' "$DEPLOY_DIR"
    else
        printf 'Preflight passed; --check does not generate an image.\n'
    fi
else
    printf 'Build failed (exit %s); no image was exported. See log: %s\n' \
        "$result" "$DEPLOY_DIR/${CONTAINER_NAME}.log" >&2
fi
printf 'Container retained: %s\nLogs/output: %s\n' "$CONTAINER_NAME" "$DEPLOY_DIR"
exit "$result"
