# Source BSP integration validation — 2026-11-13

The configured default now uses `BSP_MODE=source`. The generator obtains the
pinned T-Display-K230 board overlay and K230 SDK, stages the independently
recorded local Linux additions, runs the vendor Buildroot defconfig/all flow,
validates the actual boot template and passes that BSP to Debian packaging.

## Completed verification

- Built the Debian trixie packaging container successfully.
- Built the Ubuntu 24.04 `linux/amd64` BSP host container successfully on the
  Apple Silicon Docker host. This verifies host dependency installation, not
  SDK/toolchain compilation.
- 222 canaan-gen tests passed in the Linux container, with both repositories
  mounted read-only and networking disabled. The output is retained in
  `deploy/validation-source-bsp-tests-20261113.log`.
- 21 board staging/patch/FN tests and 82 board-runtime tests passed.
- The fixed kernel includes, SDK DTS prerequisites, original board patches and
  new keyboard patch were preprocessed with Clang and compiled with dtc.
  The produced DTB passed the actual keyboard profile validator, including
  controller phandles, IRQ, reset, matrix and three keymaps. Its SHA-256 was
  `353f132eb093d6c3c3983a7da6518b0c8ae8b3cd8175b1d9016e5f41aa958487`.
  Existing SoC dtc warnings remain; no keyboard-node warning was reported.
- The actual v0.2.9 BSP is rejected because `CONFIG_KEYBOARD_TCA8418` is
  missing. It can no longer be packaged as a board-complete image.
- Source planning and shell/Python syntax checks passed. The manifest still
  validates 36 unchanged upstream files plus 3 separately recorded local
  additions.

Tests exercise failure before Debian starts, missing/altered driver or boot
payload, stale source caches, mid-build retry, generated SDK files, firmware
and camera-runtime integrity, and the different free-space budgets for a new
BSP versus reuse. The keyboard extraction tests include a real ext4/MBR
fixture with the boot DTB selected through `/k.dtb`.

## Environment and remaining work

The initial macOS run exposed existing Python 3.9/Linux device-number test
assumptions. The complete suite subsequently passed in its Linux environment.

The full `--bsp-only` invocation stopped at the configured 60 GiB free-space
guard before SDK or toolchain downloads. Host free space varied during the
session, remaining below that threshold. No complete kernel/Buildroot build,
new Debian image, flashing or hardware test was performed. The earlier
November 13 image still uses the old BSP and has not been replaced.

With sufficient storage, run `./build-docker.sh --bsp-only`, then
`./build-docker.sh`. Successful static acceptance must still be followed by
boot, keyboard/FN, detach/reattach and concurrent keyboard/battery polling
tests on the board. Optional peripherals such as DRV2605 and XL9555 are not
claimed complete by the new required keyboard checks.
