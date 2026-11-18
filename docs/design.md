# Debian on T-Display-K230

`canaan-gen` builds a Debian userspace around a board-specific Linux BSP. It is
an independent image project. It does not add applications to the launcher
repository and does not rename their existing package or application IDs.

The first profile is T-Display-K230 with Debian 13 (`trixie`) and `riscv64`.
The upstream T-Display-K230 image uses Buildroot. Its bootloader, device tree,
kernel and hardware integration remain the starting point; replacing them with
a generic Debian kernel is outside this generator's initial scope.

## Relationship to pi-gen

The local pi-gen checkout supplies the organizational model: a configuration
file, ordered stages, a container entry point and a deployment directory. The
generator scripts here are an independent implementation. They do not reuse
Raspberry Pi firmware, Raspbian repositories, Raspberry Pi kernel packages,
first-boot tools, device-tree overlays or BCM-specific graphics libraries.

An image build has four responsibilities:

1. Bootstrap Debian packages for the requested architecture.
2. Configure users, systemd, networking, locale and SSH.
3. Add the matching BSP's kernel modules and firmware, optionally import the
   allowlisted GC2093 ISP runtime, and optionally install separately published
   LILYGO UI packages.
4. Export a root filesystem and replace the original image's rootfs partition
   while retaining its boot chain.

## Boot-image contract

The template is the completed BSP artifact:

```text
k230_linux_sdk/output/k230_canmv_t_display_rm69a10_defconfig/images/sysimage-sdcard.img
```

The inspected T-Display-K230 `genimage.cfg` defines this layout. Offsets below
are MiB unless expressed as hexadecimal bytes.

| Area | Offset | Size | MBR entry |
| --- | ---: | ---: | --- |
| SPL copy 1 | 1 | From BSP | None |
| SPL copy 2 | 1.5 | From BSP | None |
| U-Boot environment | `0x1e0000` | `0x20000` bytes | None |
| U-Boot | 2 | From BSP | None |
| Environment copy | `0x380000` | From BSP | None |
| Boot ext4 filesystem | 30 | 80 | 1 |
| Root ext4 filesystem | 128 | From BSP | 2 |

These values document the inspected profile; they are not a substitute for
validating each input image. A profile for a different board or BSP revision
must explicitly describe and verify its layout and boot arguments.

The image exporter preserves the template's bytes before the selected rootfs
start, including unpartitioned bootloader slots, the disk identifier, the boot
partition and its filesystem UUID. Only the rootfs partition's length field in
the MBR changes. Its start sector and partition number stay fixed so that the
BSP's `root=/dev/mmcblk...p2` or PARTUUID-based boot selection can remain valid.
The initial exporter accepts exactly two Linux (`0x83`) primary MBR
partitions: boot at p1 and rootfs at p2. Rootfs must be physically last. GPT,
extended partitions, additional partitions, overlapping partitions and
out-of-bounds entries are unsupported and must fail before export.

A build must confirm the actual boot arguments. The exporter also requires the
replacement rootfs UUID to match the template, and the build script creates the
filesystem with that UUID. This preserves `root=UUID=...` boot selection as well
as partition-based selection. A custom BSP with other boot-time assumptions
still needs a reviewed board profile. Do not mount the template and replacement
filesystems simultaneously by UUID, because they deliberately share one.

The input image is never rewritten. Export to a new regular file; device nodes
are not valid outputs. The generator has no flashing or automatic reboot step.

## Root filesystem growth on the device

`stage2` installs and enables `canaan-grow-rootfs.service` before `sysinit.target`,
after local filesystems are mounted and the root filesystem is writable. The
helper resolves the mounted root's device number through sysfs, so it does not
depend on MMC enumeration or a `/dev/root` alias. It accepts only the exporter's
two-primary-Linux-partition MBR layout, 512-byte logical sectors and ext4 p2.

`growpart --fudge 0` extends p2, then `resize2fs` grows ext4 online. The helper
checks that the kernel sees the updated partition size before resizing; it
uses `partx --update` if needed. The root start, disk identity and boot partition
are preserved. No filesystem resize follows a partitioning error or unexpected
MBR change. Temporary partitioning files go into `/run`, allowing recovery even
when the original root filesystem is full.

There is no permanent completion marker. Every boot rechecks the card, including
when the partition has already grown but the filesystem has not, or when an
installation was cloned to a larger card. Failures remain visible in the journal
and are retried at the next boot. The service does not reboot automatically or
depend on cloud-init seed data or the optional UI account setup.

## BSP and Debian boundary

`lilygo-linux-dtoverlays/boards/T-Display-K230` owns the Linux source patch
layer, audio requirements and Debian board runtime. The generator reads that
profile and takes a per-build snapshot before installing its files. Docker
mounts the source checkout read-only. Image provenance records the snapshot
hashes. In `BSP_MODE=source`, a separate Ubuntu amd64 stage builds the pinned
SDK with those board patches first. The source builder seals the matching
kernel, DTBs, modules and boot template in `SOURCE-BUILD.json`, then Debian
uses that output read-only. A source change selects a new build generation;
failed builds resume only with unchanged inputs. In `prebuilt` mode, the
generator does not claim that an externally supplied BSP was built from its
source checkout. Both modes must pass the same required driver checks.

The audio preflight accepts built-in or modular drivers and checks their
actual artifacts. It also requires the machine driver's external I²S mixer
control in `images/Image` or the corresponding module. This is static evidence,
not a device-tree binding or playback test. Debian installs `alsa-utils` and
the shared amplifier service, which retains the GPIO character-device request
for IO34 throughout its lifetime. It replaces the old vendor UI's amplifier
initialization, while Debian retains ownership of mixer-state restoration.

The vendor image leaves most board bring-up to its root graphical shell. The
shared board directory therefore carries three services that do that work for
Debian, each started before and independently of the launcher:
`lilygo-k230-audio` (route, amplifier line, the softvol `Master` control and a
socket for route requests), `lilygo-k230-peripherals` (nRF9151 enable IO2 and
LoRa power IO44) and `lilygo-k230-power` (BQ27220 gauge, BQ25896 charger and
the empty-battery power-off of the keyboard base). `stage2` enables exactly the
units listed in the board profile and compiles the profile's one native helper
inside the target root filesystem. GPIO lines are always located by controller
label, never by `gpiochip` number.

The gauge and charger remain managed by `lilygo-k230-power`. Its automatic
bus selection uses `i2c-dev` when the DT contains the kernel-owned TCA8418
keyboard. The keyboard base is modeled as a dedicated `i2c-gpio` adapter on
IO46/IO47, so the service never remuxes those GPIOs underneath live kernel
transfers. Hardware I2C4 remains the camera bus on IO7/IO8.
The older v0.2.9 DT retains the GPIO fallback for compatibility. It publishes readings under
`/run/lilygo-k230/power_supply` in the `power_supply` class layout rather than
inventing a new interface. The LILYGO applications already accept an
alternative power-supply directory, modem port and touch node through
environment variables; `/etc/default/lilygo-k230-ui` sets them and the one
reviewed launcher drop-in loads that file. Applications are not patched.
The keyboard matrix, DRV2605 and XL9555/LED support are mandatory: preflight
checks their actual drivers and parses the DTB selected by `/k.dtb` in the
boot template. DRV2605 preserves the vendor's ERM open-loop voltage registers
and adds the Settings strength ABI. XL9555 uses the PCA9555 driver, exposing
three active-low LEDs and leaving the other GPIOs unclaimed. BOOT0 still needs
its own integration. All physical peripheral behavior needs device tests.
See [hardware support](hardware-support.md).

Keep kernel modules from the exact kernel build embedded in the template.
Copy the matching module tree and firmware from the BSP target directory and
regenerate module dependency information for that tree. A same-version kernel
from a different configuration is not necessarily ABI-compatible.

Do not copy the entire Buildroot `/usr`, `/etc`, C library, init scripts or
package database into Debian. Debian owns its dynamic loader, libraries,
systemd, network management, SSH and package updates. When
`BSP_CAMERA_RUNTIME=1` is enabled, the importer copies only the camera daemon,
its sensor library, its XML/JSON tuning directory, and two small diagnostics.
`stage2` installs a native systemd unit which loads `vvcam_isp`, `vvcam_mipi`,
`vvcam_vb`, `vvcam_isp_subdev`, `vvcam_video` (with `mcm_mask=1`) and `nonai2d`
before starting `isp_media_server`. With the camera runtime the image also
carries the vendor's `kernel.sched_rt_runtime_us = -1`, which the daemon's
realtime threads need under `CONFIG_RT_GROUP_SCHED`; images without the camera
runtime keep realtime throttling. Other binary-only vendor services still
need a separate ABI and licensing review before being added.

Debian userspace availability does not prove the board's kernel configuration
is ready for systemd. Validate namespaces, cgroups, device nodes, filesystem
features and the required kernel interfaces before release. Wi-Fi firmware,
interface naming and peripheral initialization also require board testing.

Networking must start independently of the graphical shell. The earlier
Buildroot integration needed a separate Wi-Fi reconnection service because the
stock graphical application owned that work. Debian uses its own network
manager; do not carry over the Buildroot UI's startup dependency. The board
runtime keeps the kernel interface names (`wlan0`, `eth0`, `usb0`), because the
preserved boot chain fixes the kernel command line and the applications refer
to those names, and it stops NetworkManager from scanning with a random MAC
address, which the vendor's Realtek drivers were never run with.

## LILYGO UI boundary

Optional UI installation is disabled by default. The existing K230 build
artifacts are Buildroot-targeted `.tar.gz` archives, not Debian packages. They
cannot establish that Debian has all ABI-compatible libraries or package
dependencies. Rebuild AppKit and every desired application against the Debian
`riscv64` sysroot before enabling a signed APT repository for this image.

Use the shared package names `lilygo-ui-*`, executable names `/usr/bin/lilygo-ui-*`
and IDs `cc.lilygo.ui.*`. The board is a platform profile, not an application
identity. Each application remains a separate source repository and Debian
package; the generator only installs published packages.

AppKit and LVGL link statically into each application. Install the shared
runtime font assets and their licenses once through `lilygo-ui-appkit-dev`;
applications must not add duplicate font copies. Preserve the AppKit font
`NOTICE.md` and OFL 1.1 license whenever those assets are distributed.

## Upstream references

- T-Display-K230 source: <https://github.com/Xinyuan-LilyGO/T-Display-K230>
- Board image layout: `k230_bsp/overlay/buildroot-overlay/board/canaan/k230-soc/genimage_cfg/genimage.cfg`
  in that repository.
- Canaan Linux SDK: <https://github.com/kendryte/k230_linux_sdk>
- Debian 13 release notes: <https://www.debian.org/releases/trixie/releasenotes>
- Debian architectures: <https://www.debian.org/ports/>
- pi-gen: <https://github.com/RPi-Distro/pi-gen>

The MIT license covers this generator's original code. Debian packages, kernel
and BSP code, firmware and fonts retain their own licenses. Shipping an image
requires preserving their notices and satisfying source-distribution duties
where applicable.
