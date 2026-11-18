# Launcher image export — 2026-11-13

The default package list now includes AppKit, Launcher and 14 applications.
The other 10 unpublished packages remain in `stage3/packages.pending`.
The signed APT repository provides `lilygo-ui-launcher 0.1.3 riscv64`.
The existing `ACCOUNT_SETUP=launcher` and `ENABLE_SSH=1` configuration is retained.

## Failure and fixes

The earlier build installed 15 packages but stopped at the Launcher executable
check before image assembly. Preflight now rejects Launcher account setup when
the selected package list omits Launcher. Installation errors identify a missing
Launcher executable or service explicitly.

The subsequent complete build passed the Launcher, board-runtime and ext4 checks,
but the host ran out of disk space while exporting. OrbStack recorded
`StorageFull / No space left on device` and its Docker VM stopped. The complete
build output is retained in `deploy/build-launcher-20261113.log`.

The Docker wrapper now saves output through `tee` as it arrives. A Docker outage
can no longer replace the build log with an unsuccessful `docker logs` response.
It also distinguishes successful export, failed build and preflight-only runs.

## Exported artifact

- File: `deploy/canaan-debian-t-display-k230-lilygo-ui-20261113T015632Z.img.xz`
- Compressed bytes: `609684980` (approximately 582 MiB).
- Decompressed image bytes: `3091202048`.
- SHA-256: `8ae08a907e90863ec0f9364b83b5814c8281025510adbffafece7b46e78ae08c`.
- Installed package manifest: 684 packages, including Launcher `0.1.3 riscv64`.
- Adjacent `.sha256`, `.packages.tsv`, `.json`, `.layout.json` and
  `.validation.json` files record the checksum, packages, inputs and verification.

After Docker recovered, the retained `rootfs.ext4` passed `e2fsck -f -n` again.
The original export steps were resumed with
`.cache/recover-export-20261113.sh` in
`canaan-gen-export-recovery-20261113`, using the completed rootfs from
`canaan-gen-20261113-093514-40836` (`/work/run.oz2JbmSY`). No rootfs rebuilding or
credential changes were needed. The output provenance records this recovery.

## Verification

- Shell regression tests: 32 passed; APT tests: 18 passed.
- Both Launcher rootfs checks passed: RISC-V executable, PAM, fonts, enabled
  DRM service, SSH configuration and no preset ordinary account.
- Board-runtime checks and all five ext4 filesystem-check passes succeeded.
- The exported file matches its checksum, provenance hash and recorded size.
- The complete XZ stream was decompressed and checked without creating another
  large raw image. The bootloader and boot partition match the BSP template;
  only the root partition sector count changes in the MBR.
- The embedded rootfs matches the verified ext4 input byte for byte (SHA-256
  `f736d5b2008c7f0edbcc136f6c5e2004641fd65316c2e0fb601227308e3d3afb`).
- Hardware boot, display and touch remain untested.

## Retained recovery data and disk space

Two obsolete generator dependency caches were removed. The previous failed
rootfs at `/work/run.FEQDDMo8/rootfs` was archived with numeric ownership, ACLs
and xattrs, checked with `xz -t` and `tar --compare`, then its original directory
was released. Its backup and checksum remain as `rootfs.failed.tar.xz` and
`rootfs.failed.tar.xz.sha256` in that run directory, in volume
`b3bba71faccc1a5e0b992b74da773feb8e1dfd6bf66457124a9d6eecb28f09cd`.

Identical cached BSP images were replaced with APFS copy-on-write clones after
SHA-256 comparison, preserving their paths and contents. No BSP payload,
account configuration or completed image was discarded. Allow at least the
README's recommended 15 GB of free space before another complete build.
