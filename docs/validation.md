# Validation and release gate

Use this checklist to qualify a Debian image on T-Display-K230. The source-built
image exported on 2026-11-16 passed offline checks. Board qualification begins
with a serial capture from reset through login. For the dated results by
subsystem, see the [validation summary](validation/README.md).

## Host checks

Run syntax validation for shell/Python entry points and the unit tests in
`tests/`. Image tests use generated fixtures, temporary directories and regular
files. They must not require root, network access, loop devices or physical
media.

The image exporter checks must cover:

- Valid MBR inspection and selection of the last Linux rootfs partition.
- Rejection of an invalid signature, GPT/protective MBR, extended partitions,
  overlapping entries, zero or out-of-bounds ranges and a non-final rootfs.
- Rejection of the source image as an output path, including aliases.
- Rejection of a replacement that cannot satisfy the export contract.
- Exact preservation of all bytes before rootfs except its four-byte MBR
  sector-count field, and preservation of other partition entries.
- Placement of the new filesystem at the original rootfs offset, correct new
  image length and partition length, and no modifications to either input.
- A failed export must not overwrite an existing destination.

The runtime root-growth tests must cover larger cards, unchanged partitions,
repeated boots, a partition already larger than ext4, partitioning/filesystem
failures, stale kernel partition sizes and rejection of unsupported layouts.
Use disposable disk-image files for integration checks with real `growpart` and
`resize2fs`; verify that existing data, the boot region and filesystem UUID stay
intact. These checks do not replace online growth on the board's vendor kernel.

```bash
python3 -m unittest discover -s tests -v
# Linux; requires cloud-guest-utils, fdisk, e2fsprogs and Python.
# systemd-analyze, when available, also verifies the service unit.
python3 tests/integration_grow_rootfs.py
```

On 2026-11-12, all 131 unit tests and the integration script passed. The latter
ran in a disposable, unprivileged Debian trixie container: 96/160 MiB images grew
successfully, repeated runs reported no partition change, and a 128 MiB image
recovered a partition expanded before its filesystem. File contents, ext4 UUID
and boot content were preserved; all three filesystems passed `e2fsck -f -n`.
`systemd-analyze verify` passed with the executable redirected to a temporary
stub. Device discovery and kernel-size refresh were mocked; K230 online root
growth and a complete firmware rebuild were not performed in this check.

The 2026-11-12 hardware-support change added tests for the board services and
their integration: 151 generator tests and 79 board-runtime tests passed, the
latter also inside the generated riscv64 root filesystem. See
[the hardware validation record](validation/2026-11-12-hardware.md).

Use an actual generated ext4 filesystem for integration checks, then inspect it
with `e2fsck -fn` and `dumpe2fs` before mounting or writing it to media.

## Build evidence

Record:

| Item | Required evidence |
| --- | --- |
| Generator | Commit or source-tree digest and full configuration without secrets |
| BSP | Repository revision, defconfig, kernel release and template SHA-256 |
| Debian | Release, mirror or snapshot, package inventory and keyring |
| Exports | Rootfs ext4 or complete disk image and their SHA-256 hashes |
| Boot preservation | Binary comparison of pre-rootfs bytes allowing only the rootfs size field |
| Filesystem | Clean offline ext4 check and available space |
| Modules | Kernel/module release match and required driver availability |
| Identity | `/etc/os-release`, `dpkg --print-architecture`, hostname and account policy |
| Services | Enabled systemd units and independent network startup |

Regular Debian mirrors and an unpinned container tag change over time. They are
useful for development but do not give bit-for-bit reproducibility. Release
builds need a retained package snapshot, pinned build environment and input
artifacts in addition to the saved configuration.

## First board boot

Use separate test media and retain the known-working Buildroot image. The
generator only creates files; media selection and flashing are a separate,
explicit operation.

Capture serial output from reset through login and verify:

1. The expected SPL, U-Boot and kernel boot without a changed boot partition.
2. The kernel mounts the correct root partition read/write and starts systemd
   without emergency mode or unsupported-kernel errors.
3. The intended administrator can log in using the configured SSH key or serial
   credentials; the image has no baked-in SSH host keys shared across devices.
4. A cold boot acquires a network address with the graphical UI stopped. Check
   Ethernet and each configured Wi-Fi path separately.
5. `apt-get update` verifies repository signatures and a small package can be
   installed, upgraded and removed.
6. DRM display, Goodix touch, backlight, storage, audio, USB and relevant device
   nodes work with the imported modules and firmware.
7. Time synchronization, shutdown and at least three cold boots work. Record
   boot time, memory use, rootfs space and any persistent journal errors.
8. Flash to a card larger than the exported image. Check
   `journalctl -b -u canaan-grow-rootfs.service`, `lsblk` and `df -h /`: both p2
   and ext4 must use the available card space. Reboot and confirm unchanged
   capacity, successful service status and intact files. Repeat with another
   card capacity and check that `/boot` and the root UUID are preserved.
9. All three board services are active and their read-only checks pass:
   `lilygo-k230-audio-check`, `lilygo-k230-peripherals-check` and, with the
   service stopped, `lilygo-k230-power-check`. The speaker plays, the launcher's
   volume slider changes the level through the `Master` control and the level
   survives a reboot. `lilygo-k230-audio-route internal` permits recording.
10. With the keyboard base attached, `/run/lilygo-k230/power_supply` holds the
    gauge and charger, the status bar shows the charge, and plugging USB power
    changes the status. Record which `LILYGO_K230_POWER_BUS` value works. The
    charger reports the configured charge current and voltage.
11. A short press of the power key is handled by the launcher and does not
    power the board off; a long press still does. Interfaces are named `wlan0`
    and, for a USB adapter, `eth0`.

The power service reads the keyboard base's fuel gauge and charger. With the
source-built kernel, test ordinary and FN keys during battery polling, detach
and reattach the base, exercise haptic feedback and toggle each of the three
LEDs. For commands and expected behavior, see the
[board acceptance checklist](hardware-support.md#上板验收).

## Optional UI qualification

After rebuilding Debian packages, validate their architecture, shared-library
dependencies and font license contents before adding them to the signed APT
repository. Install AppKit, Launcher and a representative independent app such
as Calculator through APT. Verify portrait/landscape, touch, application return,
brightness and network recovery after reboot. Keep the network service
independent of whether the launcher starts successfully.

Only claim tested functionality backed by those results. Keep known gaps in the
release notes alongside the image checksum.
