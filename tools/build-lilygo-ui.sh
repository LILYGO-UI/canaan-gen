#!/usr/bin/env bash
# Build each application as its own top-level project against the same Debian SDK.
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo "Run this cross-build helper on Linux or in a Linux container" >&2; exit 1; }
if [[ $# != 6 ]]; then
    echo 'Usage: build-lilygo-ui.sh APPKIT LAUNCHER TEMPLATE CALCULATOR DEBIAN_SYSROOT OUTPUT_DIR' >&2
    exit 2
fi
appkit=$(cd "$1" && pwd)
launcher=$(cd "$2" && pwd)
template=$(cd "$3" && pwd)
calculator=$(cd "$4" && pwd)
sysroot=$(cd "$5" && pwd)
mkdir -p "$6"
output=$(cd "$6" && pwd)
build=${UI_BUILD_DIR:-$(mktemp -d)}
mkdir -p "$build"
common=(-G Ninja -DCMAKE_TOOLCHAIN_FILE="$appkit/cmake/k230-debian-toolchain.cmake"
    -DLILYGO_UI_DEBIAN_SYSROOT="$sysroot" -DLilyGoUI_DIR="$appkit"
    -DLILYGO_UI_PLATFORM=k230 -DLILYGO_UI_TARGET_OS=debian
    -DLILYGO_UI_RENDERER=software -DCM0_SIMULATOR=OFF -DCM0_FETCH_LVGL=OFF
    -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_INSTALL_LIBDIR:STRING=lib)
for project in appkit launcher template calculator; do
    case "$project" in
        appkit) source_dir=$appkit ;;
        launcher) source_dir=$launcher ;;
        template) source_dir=$template ;;
        calculator) source_dir=$calculator ;;
    esac
    extra=()
    if [[ "$project" == launcher ]]; then
        extra+=(-DLAUNCHER_SYSTEM_SESSION=ON)
        if [[ -n "${LILYGO_UI_DATACHANNEL_SOURCE:-}" ]]; then
            extra+=(-DFETCHCONTENT_SOURCE_DIR_LILYGO_DATACHANNEL="$LILYGO_UI_DATACHANNEL_SOURCE")
        fi
    fi
    cmake -S "$source_dir" -B "$build/$project" "${common[@]}" "${extra[@]}"
    cmake --build "$build/$project" --parallel "${JOBS:-4}"
    cpack --config "$build/$project/CPackConfig.cmake" -G DEB -B "$output"
done
(cd "$output" && sha256sum ./*.deb > SHA256SUMS)
printf 'Debian packages: %s\nBuild tree: %s\n' "$output" "$build"
