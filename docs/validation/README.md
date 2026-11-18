# T-Display-K230 验证总览

截至 2026-11-18，已用源码 BSP 构建 Debian 13（trixie）riscv64 镜像并核对 SHA-256。发布前还缺 T-Display-K230 上从冷启动到登录的串口日志，以及显示、触摸和外设的实测结果。[上板验收清单](../validation.md#first-board-boot)

## 当前镜像

`canaan-debian-t-display-k230-lilygo-ui-20261116T104343Z.img.xz` 使用 11 月 16 日编译的 Linux 6.6.36、模块和设备树。构建日志记录了 Launcher 首次设置检查、板级服务检查和 ext4 检查通过。11 月 13 日恢复导出的镜像使用原厂 v0.2.9 BSP，核对键盘底板支持时应以 11 月 16 日的产物为准。[源码镜像、校验和与日志](2026-11-16-buildroot-recheck.md#source-built-debian-image)

## 按功能查证

| 项目 | 已验证 | 待验证 | 记录 |
| --- | --- | --- | --- |
| 引导区、rootfs 与扩容 | 基础镜像逐字节保留 rootfs 之前的内容，仅更新 MBR 中的 rootfs 长度。Linux 临时磁盘镜像上的扩容保留了文件与 UUID。 | 用两种容量的 SD 卡验证首次启动在线扩容和重启后的容量。 | [基础镜像](2026-11-07-base-image.md) · [扩容集成测试](../validation.md#host-checks) |
| 首次设置与 SSH | 在 riscv64 rootfs 中验证了创建账号、中断恢复和密码认证 sudo。开启 SSH 的镜像在 QEMU 下通过 `sshd -t` 和 `sshd -T`。 | 用触屏完成向导，联网后用新账号从另一台主机登录。 | [UI 与账号](2026-11-07-ui.md) · [SSH 镜像](2026-11-09-ssh.md) |
| APT 与应用 | 11 月 12 日，26 个请求包中缺 11 个，预检按预期停止。11 月 13 日 Launcher 发布后，镜像从签名源安装了 AppKit、Launcher 和 14 个应用。 | 另外 10 个包列在 `stage3/packages.pending`。板上还要验证签名更新、应用启动与返回 Launcher。 | [APT 预检](2026-11-12-apt.md) · [Launcher 镜像导出](2026-11-13-launcher-image.md) |
| 音频、电源与外设服务 | BSP 检查确认内建音频驱动。容器验证了 ALSA 配置，riscv64 rootfs 验证了服务、udev 规则及板级运行时。 | 验证扬声器、录音、音量保存、电量与充电状态、Wi-Fi 重连和相机预览。 | [音频](2026-11-12-audio.md) · [板级服务](2026-11-12-hardware.md) |
| 源码 BSP 与键盘底板 | TCA8418 键盘、DRV2605 振动和 XL9555 扩展口已通过最终启动分区的设备树检查。三个器件使用 IO46/IO47 的 GPIO-I²C 总线，相机保留硬件 I²C4。 | 接上底板测试普通键与 FN、插拔、电量轮询时的按键、振动强度及三颗指示灯。 | [键盘接入](2026-11-13-source-bsp.md) · [振动与 LED](2026-11-13-peripherals.md) · [总线复核与完整编译](2026-11-16-buildroot-recheck.md) |

11 月 18 日，在 macOS 上使用 Homebrew Python 3.14 和 e2fsprogs，`cc2a872` 的 250 项生成器测试全部通过。同日核验了应用开发 SDK 的 `SHA256SUMS`，其 manifest 记录的来源镜像哈希与上面的镜像一致。[主机测试命令](../validation.md#host-checks) · [SDK 复核记录](2026-11-16-buildroot-recheck.md#source-built-debian-image)

新增验证记录沿用 `YYYY-MM-DD-主题.md` 命名，并在相应功能行更新结论和链接。
