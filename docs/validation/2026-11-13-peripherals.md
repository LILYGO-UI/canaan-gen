# DRV2605 and XL9555 integration — 2026-11-13

The required board profile now includes `INPUT_DRV260X_HAPTICS`,
`GPIO_PCA953X` and `LEDS_GPIO`. The source layer stages local patches `0102`
and `0103` after the existing keyboard patches. The immutable upstream patch
files are unchanged.

## Hardware decisions

The pinned vendor `ui_haptic.c` uses the keyboard-base GPIO-I2C bus at address `0x5a`, ERM mode, library 1
and open-loop operation. It does not write rated/overdrive voltage registers
or control an EN GPIO. The new board-specific open-loop branch preserves
those voltage registers, does not start calibration, and leaves supply
management to hardware. It starts automatic keyboard feedback at 0, matching
the vendor default. Settings can set `keyboard_feedback_strength` to 0–100.

No motor voltage or supply wiring was inferred from the different CM0 board.
The explicit `lilygo,erm-open-loop` DT property selects this behavior. Other
drv260x configurations retain their calibration flow, with a bounded timeout.
The driver exposes `drv260x:haptics`/FF_RUMBLE and observes the keyboard only
when its DT node contains `lilygo,haptic-feedback`.

The XL9555 datasheet register layout matches the 16-line PCA9555 driver. The
board DT selects the vendor software's default address `0x20`. The vendor also
probes `0x20`–`0x27`, so other address-strapped hardware needs a corresponding
DT/profile. P03/P04/P05 are active-low and are exposed as
`keyboard:capslock`, `keyboard:fn`, and `keyboard:status`, initially off. Other
lines are unclaimed. Automatic desktop Caps/Fn LED synchronization is not part
of this driver integration.

Udev grants the existing `input` group write access to feedback strength on
both add and bind, and the `gpio` group write access to the three LED
brightness attributes. Applications continue to run as ordinary users.

## Completed compile checks

- Downloaded the exact kernel revision
  `7d4e1f444f461dbe3833bd99a4640e7b6c2cd529` and applied the real `0102` patch.
- With Ubuntu 24.04 GCC 13 `riscv64-linux-gnu-`, the generic RISC-V defconfig
  plus the required driver options compiled `drv260x.o`, `gpio-pca953x.o`
  and `leds-gpio.o`. All three are ELF64 RISC-V objects, and contain the
  binary markers used by the production preflight.
- The log is `deploy/validation-peripheral-objects-20261113.log`. This is an
  API/object compilation check, not a complete K230 build with the vendor
  Xuantie toolchain. The pinned generic defconfig's existing formatting and
  duplicate vdso target warnings were retained in the log.
- The complete K230 DTS, reconstructed from the pinned kernel/SDK/board
  layers and all local patches, passed Clang preprocessing and dtc.
  The resulting DTB passed both the keyboard and peripheral validators:
  The corrected DTB now keeps camera hardware I2C4 on IO7/IO8 and places the
  keyboard-base devices on a separate `i2c-gpio` bus using GPIO1 offsets 14/15
  (IO46/IO47). Its SHA-256 is
  `72a6923ecae725a848d789c020487dcd6f4aa7eb424bebb8ac93c529ff819ebd`.
  Existing SoC dtc warnings remain; none refer to the new peripherals.

## Acceptance and limits

The complete Linux regression run passed 240 canaan-gen tests, 25 board
source/staging tests and 84 board-runtime tests (349 total). The retained
logs are `deploy/validation-peripheral-tests-20261113.log` and
`deploy/validation-peripheral-board-tests-20261113.log`.

The generator verifies config plus actual built-in/module payload, and checks
the selected boot partition DTB rather than a loose adjacent file. Kernel,
embedded configuration and template must agree. Keyboard and peripherals
share one boot extraction. Missing hardware contracts, wrong motor mode,
unverified voltage/GPIO/supply settings, wrong LED lines/polarity/default
state, or missing drivers stop the build. A malformed peripheral profile is
rejected before downloading SDK sources.

No new flashable image or physical test was completed. The source build's
60 GiB free-space guard remains in force; the previously exported v0.2.9 image
remains unchanged.
