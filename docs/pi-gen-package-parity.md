# pi-gen package parity

The comparison baseline is the local pi-gen **lite** image: the package files
under `stage0` through `stage2`. pi-gen's `stage3`-`stage5` files describe the
Raspberry Pi desktop/full image and are not the baseline for this K230 image.

`stage1/packages` contains the portable Debian package names from that lite
baseline, plus the packages required by the Canaan BSP and the Debian boot,
network, and recovery setup. The package installer still uses the Debian
riscv64 repositories configured by canaan-gen.

The following pi-gen entries are intentionally excluded because they belong to
the Raspberry Pi archive, ARM Raspberry Pi kernel, or Raspberry Pi hardware:

- `raspi-config`, `raspi-firmware`, `linux-image-rpi-*`, and
  `linux-headers-rpi-*`
- `raspberrypi-*`, `raspi-*`, `rpi-*`, `rpicam-apps-lite`, `rpifwcrypto`, and
  `python3-rpi-lgpio`
- `rpd-*`, `code-the-classics*`, `scratch3`, and `libreoffice-pi` from the
  full desktop image
- `rpi-cloud-init-mods`, because it is Raspberry Pi specific. The generic
  `cloud-init` package remains in the list for pi-gen package parity, but
  canaan-gen does not install pi-gen's Raspberry Pi cloud-init seed files.
  Root expansion instead uses the generator's own systemd service, with explicit
  `cloud-guest-utils`, `fdisk` and `e2fsprogs` runtime dependencies.
- `kms++-utils` and `libpam-chksshpwd`, which are supplied by the Raspberry Pi
  archive rather than the Debian riscv64 repositories used here

`firmware-*` packages that are portable Debian firmware packages remain in the
list. The board-specific kernel modules and firmware are still copied from the
matching Canaan BSP in `stage2`.

The UI package example in `stage3/packages.example` now follows pi-gen's
explicit LILYGO UI manifest. The checked-in build configuration still selects
only the four Debian packages currently present in the local package cache.
