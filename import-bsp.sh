#!/usr/bin/env bash
# Import an existing T-Display-K230 image through a non-privileged Linux container.
set -euo pipefail
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INCLUDE_CAMERA_RUNTIME=0
if [[ "${1:-}" == --include-camera-runtime ]]; then
    INCLUDE_CAMERA_RUNTIME=1
    shift
fi
if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
    echo 'Usage: import-bsp.sh [--include-camera-runtime] vendor.img[.gz|.xz] NEW_BSP_DIR'
    exit 0
fi
[[ $# == 2 ]] || { echo 'Usage: import-bsp.sh [--include-camera-runtime] vendor.img[.gz|.xz] NEW_BSP_DIR' >&2; exit 2; }
[[ -f "$1" ]] || { echo 'Vendor image must be a regular file' >&2; exit 1; }
[[ ! -e "$2" && ! -L "$2" ]] || { echo 'BSP output must not already exist' >&2; exit 1; }
input="$(cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")"
parent="$(dirname -- "$2")"
mkdir -p "$parent"
parent="$(cd -- "$parent" && pwd)"
name="$(basename -- "$2")"
DOCKER=${DOCKER:-docker}
DOCKER_IMAGE=${DOCKER_IMAGE:-canaan-gen:trixie}
extra_args=()
if [[ "$INCLUDE_CAMERA_RUNTIME" == 1 ]]; then
    extra_args+=(--include-camera-runtime)
fi
"$DOCKER" build -t "$DOCKER_IMAGE" "$BASE_DIR"
"$DOCKER" run --rm --user "$(id -u):$(id -g)" --entrypoint python3 \
    --mount "type=bind,src=$input,dst=/input/$(basename -- "$input"),readonly" \
    --mount "type=bind,src=$parent,dst=/output" \
    "$DOCKER_IMAGE" /src/scripts/import-bsp.py \
    --image "/input/$(basename -- "$input")" --output "/output/$name" \
    "${extra_args[@]}"
