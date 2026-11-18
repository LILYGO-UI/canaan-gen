#!/usr/bin/env bash
set -euo pipefail
if [[ "$(uname -m)" != riscv64 ]]; then
    if [[ ! -e /proc/sys/fs/binfmt_misc/register ]]; then
        mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
    fi
    if [[ ! -r /proc/sys/fs/binfmt_misc/qemu-riscv64 ]] ||
       ! grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-riscv64; then
        update-binfmts --enable qemu-riscv64
    fi
fi
exec /src/build.sh "$@"
