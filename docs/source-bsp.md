# BSP source build

`BSP_MODE=source` runs the vendor Buildroot flow before Debian packaging. The
board and SDK revisions are read from the shared board's `linux/upstream.json`.
The kernel revision in the vendor defconfig must match that manifest. The local
Linux layer adds the keyboard, DRV2605 feedback and XL9555 LED support that
the pinned upstream BSP lacks.

## Entry points

```sh
./build-docker.sh --plan-bsp
./build-docker.sh --bsp-only
./build-docker.sh --check
./build-docker.sh
```

`--plan-bsp` is read-only and offline. `--bsp-only` builds and validates the BSP;
it does not install Debian or export a Debian image. `--check` requires a
completed cache for these exact source inputs and does not compile the SDK.
The regular build runs both phases. All commands accept `-c CONFIG`.

The native `build.sh` accepts the same flags. Its source stage requires x86_64
Linux and the vendor dependencies; Debian packaging additionally needs the
packages in `depends`, root and riscv64 binfmt support. Docker is the supported
way to obtain both host environments on macOS. Its source container is Ubuntu
24.04 amd64 and unprivileged; its Debian container needs mount/chroot privileges.

## Configuration and storage

| Setting | Default | Purpose |
| --- | --- | --- |
| `BSP_MODE` | `source` in `config.example` | Select source compilation or an explicit `prebuilt` BSP |
| `BSP_JOBS` | `4` | Buildroot compiler parallelism |
| `BSP_MIN_FREE_GIB` | `60` | Available-space guard; minimum accepted override is 15 |
| `BSP_CACHE_VOLUME` | `canaan-gen-bsp-cache` | Docker source/build/download volume; toolchain uses this name plus `-toolchain` |
| `BSP_WORK_DIR` | `.cache/bsp-source` | Native-only cache directory |
| `BSP_CAMERA_RUNTIME` | `1` for source mode | Install the vendor camera runtime into Debian |

Older configs without `BSP_MODE` retain prebuilt behavior, but must pass the new
keyboard checks. Source mode ignores a stale `BSP_DIR` and rejects independently
overridden `BSP_IMAGE`/`BSP_KERNEL_CONFIG`. It never falls back to a release image.

The default free-space threshold is an operational guard, not a measured upper
bound. Buildroot compiles the original board defconfig, including OpenCV and AI
packages, and retains intermediate output. Allocate enough disk for this and
Debian export. On macOS the wrapper checks host storage before any Docker build;
the source builder also checks the Linux filesystem. When a completed BSP is
reused, Debian packaging uses the existing 15 GiB host budget instead of the
60 GiB source-build budget; the cache still passes all content checks. Moving Docker storage to a
larger disk may require adjusting the host guard to that disk's available space.

## Cache, retries and provenance

The cache generation key includes fixed commits, all board input hashes and the
builder recipe. The builder snapshots local board files before downloading.
Different patches select a new SDK checkout/build directory instead of relying
on `linux-rebuild`, which does not reapply Linux patches. Source archives and the
toolchain are shared across generations. The builder does not run `clean`,
`linux-dirclean`, or the vendor image script against a user's existing SDK.

The upstream `apply.sh` runs only during preparation, before compilation. It
copies the complete `k230_bsp/overlay/` tree, including the `board/canaan`
Buildroot scripts and fragments. `stage-sdk.py` then checks the pinned board
defconfig references, the `linux.led_btn` and BusyBox baseline fragments, the
image-generation scripts, and the three T-Display DTBs before it stages the
local Linux additions. Failed builds retain their output and log; rerunning
resumes the same generation. A nonblocking lock prevents concurrent source
builds in one cache. Prepared SDK inputs are checked before resuming; manual
changes stop the build. Completed generations are read-only inputs to Debian
and reused only after content checks.

`SOURCE-BUILD.json` is written only after Buildroot, BSP validation and template
validation succeed. It records source inputs, kernel/config/DTB/module hashes, firmware and camera
runtime hashes, the complete template hash, and validation results. Debian's adjacent provenance
JSON includes that record and identifies the BSP as built from source. Hardware
boot validation is recorded as false until separately performed.

## Driver acceptance

The board profile defines the hardware contract, rather than treating successful
`make` as proof that every peripheral works. Missing keyboard, audio, DRV2605
or XL9555/LED support stops packaging. The keyboard checks inspect configuration, actual driver
payload, FN support and parsed device-tree properties from the boot partition
of the selected template, including matrix/keymaps, interrupt and reset wiring.
The Image in that partition must match the inspected kernel. DRV2605 acceptance
requires the Settings strength ABI and the vendor ERM open-loop configuration;
the board preserves voltage registers and does not invent an enable GPIO or
regulator. XL9555 uses the PCA9555 driver at the vendor default 0x20 address,
with P03/P04/P05 as active-low GPIO LEDs, initially off. Address-strapped board
variants require matching DT/profile inputs. The three LED interfaces are
provided; automatic Caps/Fn state mirroring by the desktop is a separate feature.
The keyboard/base devices use a dedicated `i2c-gpio` DT bus on GPIO1 offsets
14/15 (IO46/IO47), while the camera keeps hardware I2C4 on IO7/IO8. The checker
rejects a DTB that places the keyboard children on the camera controller or
that omits the GPIO-I2C pinctrl/open-drain wiring.

Remaining optional hardware gaps are still reported. This change provides the
source build path and required keyboard/base-peripheral integration; it does not claim that all
optional peripherals or physical behavior have been tested.

## Build validation

On 2026-11-13 the 60 GiB space guard stopped the initial source build before
downloads. Separate checks compiled the RISC-V driver objects and the DTB. See
the [source build record](validation/2026-11-13-source-bsp.md) and the
[peripheral record](validation/2026-11-13-peripherals.md).

On 2026-11-16 the vendor Buildroot build completed and Debian packaging exported
a matching image. The build inspected the drivers and the DTB in the boot
partition. Physical boot and peripheral tests are pending. See the
[source image and checksum](validation/2026-11-16-buildroot-recheck.md#source-built-debian-image).
