# Buildroot patch recheck — 2026-11-16

The board's first keyboard integration put TCA8418, DRV2605 and XL9555 below
the hardware `&i2c4` controller. The pinned upstream layer shows why that is
wrong: patch `0058` keeps camera I2C4 on IO7/IO8 and adds IO46/IO47 as an
alternate expansion pair; the vendor `ui_hardware.c` switches IO46/IO47 to
GPIO function and bit-bangs the keyboard-base devices during each transaction.

The local layer now follows that boundary:

- `0101` creates `/keyboard-i2c` with `compatible = "i2c-gpio"`, GPIO1 SCL
  offset 14 / IO46 and SDA offset 15 / IO47, active-high open-drain flags and
  a 5 µs delay. TCA8418, AHT20, BQ27220 and BQ25896 are children of this bus.
- `0103` puts DRV2605 and XL9555 on `/keyboard-i2c`, removes AHT20 from the
  camera controller and leaves hardware I2C4 with camera IO7/IO8 plus GC2093
  and DW9714.
- `keyboard.fragment` now requires `CONFIG_I2C_GPIO=y`; the board profile and
  DTB checker require the same bus, GPIO lines, GPIO-alt0 pinctrl and camera
  preservation.
- `lilygo-k230-power` discovers the dynamic `i2c-gpio` adapter by DT identity.
  Older prebuilt images retain the legacy GPIO fallback.

The Buildroot input path is also now checked against the upstream board layout.
For the pinned T-Display-K230 commit `c27c58c07ca5e39d15f5dcf1b7103aead29282df`,
`k230_bsp/scripts/apply.sh` copies the complete `k230_bsp/overlay/` tree into
the pinned SDK. `stage-sdk.py` then replaces only the board Linux fragment with
the upstream fragment plus `keyboard.fragment`, and adds the separately
manifested local Linux patches. It does not replace the vendor Buildroot board
scripts or unrelated fragments.

The staging preflight now requires the files that the board defconfig consumes:

- `board/canaan/k230-soc/fragment/linux.fragment`
- `board/canaan/k230-soc/fragment/linux.led_btn`
- `board/canaan/k230-soc/fragment/busybox.fragment`
- `board/canaan/k230-soc/genimage_cfg/genimage.cfg`
- `board/canaan/k230-soc/post-build.sh`
- `board/canaan/k230-soc/post-image.sh`
- `board/canaan/k230-soc/rootfs_overlay/`

It checks the defconfig references for the rootfs overlay, post-build and
post-image scripts, both Linux fragments, the BusyBox fragment, and all three
T-Display DTBs. The pinned SDK baseline Git blobs are checked against SHA-1
`linux.led_btn=1d5c1ce21d932abb3a404a89c3ee36062592a299` and
`busybox.fragment=df9e5db42c11311374e469750ac994d805a55596`; this catches a
wrong or stale Buildroot base before the toolchain or kernel build starts.

## Evidence

- The corrected DTB was preprocessed with the pinned SDK/kernel bindings and
  compiled with dtc. Its SHA-256 is
  `72a6923ecae725a848d789c020487dcd6f4aa7eb424bebb8ac93c529ff819ebd`.
- A RISC-V generic-defconfig object build compiled `i2c-gpio.o` together with
  `drv260x.o`, `gpio-pca953x.o` and `leds-gpio.o`; all four objects are ELF64
  RISC-V. The log is `deploy/validation-peripheral-objects-20261116.log`.
- The production checker passed both keyboard and peripheral validation on that
  DTB. It confirmed the separate GPIO-I2C bus and camera I2C4 pinctrl.
- Board staging and source tests passed after the patch rewrite; the Linux
  board source suite ran 26 tests successfully, the full canaan-gen Linux
  suite ran 244 tests successfully, and the board Debian runtime suite ran 85
  tests successfully. The current macOS rerun of the canaan-gen suite reaches
  242 passing tests plus one skip; its two remaining errors are pre-existing
  macOS device-number compatibility cases in `test_grow_rootfs.py`.
- `stage-sdk.py --verify` still validates 36 unchanged upstream files and the
  separately recorded local layer. The upstream `linux/` manifest remains
  unchanged.
- The Buildroot staging regression suite passes 21 tests, including missing
  file, symlink, baseline-hash, defconfig-contract and repeated-staging cases.
- A byte-for-byte comparison against the checked-out upstream board reports all
  36 manifest files unchanged. The expected upstream board Buildroot files are
  present at the pinned commit; `linux.led_btn` and `busybox.fragment` remain
  SDK baseline inputs rather than being copied into the LILYGO overlay.

### Pending at the time of the patch recheck

The complete vendor SDK/Buildroot rebuild and hardware validation still need to
be run with sufficient storage. The next source build gets a new generation key
because the board files and local patch hashes changed; an old BSP cache must
not be reused.

## Source-built Debian image

Later on 2026-11-16 the vendor SDK/Buildroot build completed for
`k230_canmv_t_display_rm69a10_defconfig`. The build provenance identifies the
BSP as `source-built-bsp` and records Linux 6.6.36 with 288 modules. The boot
partition's `k230-canmv-rm69a10.dtb` places the keypad, haptics and GPIO
expander under `/keyboard-i2c`. Its SHA-256 is
`66dee909d5e2aa6173aca13e3af1292488477e294f99db82db6ce97c1fc1e8d1`.

Debian packaging exited 0 after the Launcher, board-runtime and ext4 checks.
The package manifest contains 684 installed packages. The image provenance
records `BSP_MODE=source`, `ACCOUNT_SETUP=launcher`, `ENABLE_SSH=1` and
`validation.hardware_boot_test=false`.

| Local build evidence in `deploy/` | Value |
| --- | --- |
| Compressed image | `canaan-debian-t-display-k230-lilygo-ui-20261116T104343Z.img.xz` (610,760,432 bytes) |
| Image SHA-256 | `f7a1ece025b2fa75763b5149970f24069becd847ed914ca8fdd0889005be1933` |
| Provenance and installed packages | Same image stem with `.json` and `.packages.tsv` |
| BSP and Debian build logs | `canaan-gen-20261116-134624-70951-bsp.log` and `canaan-gen-20261116-134624-70951.log` |

On 2026-11-18 the image SHA-256 matched the `.sha256` file and build provenance.
The SDK's `manifest.json` identifies the same source image by hash. All three
entries in its `SHA256SUMS` passed verification (sysroot archive, package list
and manifest).
