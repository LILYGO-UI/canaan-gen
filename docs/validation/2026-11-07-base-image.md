# 2026-11-07 构建验证记录

本次生成了 Debian 13 trixie / riscv64 基础系统测试镜像。未烧写或重启目标板，
尚未验证 Debian 的实机启动、显示、触摸、网络与其它外设。

## 输入

- 原厂发布固件：`lilygo-k230-v0.2.9.img.gz`。
- 压缩固件 SHA-256：`ea1b825c9afad82c51a742e3010265d681f6ca90df9ea8318ccc9924e17a2877`。
- 原始整盘镜像 SHA-256：`66c53f484c646ec2f175e1427f91e7bf800311feadfd82dbc6686e5678013702`。
- Linux：`6.6.36`；boot/Image 内嵌 IKCONFIG 通过 systemd/早期启动能力检查。
- 导入 288 个 ELF64 RISC-V 模块；vermagic 与内核版本匹配。
- `import-bsp.sh` 已通过非 root Docker 实际运行，364 个提取文件/链接的哈希核验通过。
- BSP 是从发布固件重构的输入，不是一次完整 SDK 源码重新编译。
- 本地输入缓存在 `.cache/k230-v0.2.9/`，详细来源见其 `INPUT-PROVENANCE.json`。

## 已通过的检查

- 58 项主机回归测试：MBR/文件系统边界、boot 保留、输入保护、BSP 架构与ABI、
  固件导入、Docker 参数、包清单转义以及 overlay 挂载边界。
- Docker Debian 13 构建环境，QEMU RISC-V foreign bootstrap 和 second stage。
- 实际 `dpkg --print-architecture` 为 `riscv64`；systemd `257.13-1~deb13u1` 能执行。
- APT 依赖检查通过，NetworkManager 与 systemd-resolved 已启用。
- 最终清单包含 183 个包，架构均为 `riscv64` 或 `all`。
- rootfs 完成五阶段 `e2fsck -f -n`，退出状态为 0。
- 新旧镜像的前 128 MiB 按字节比较一致，允许的唯一差异是 MBR 的 rootfs 长度字段。
- rootfs 起点保持扇区 262144，UUID 保持 `a023d06b-61c4-4131-b961-c23b20a13acc`。
- XZ 完整解压校验通过；镜像中 rootfs SHA-256 与生成的 ext4 文件相同。
- 最终 Docker 包装器将脚本固定进镜像快照，`build-docker.sh --check` 实际通过。

开发时一次并发源码更新使已运行的 shell 未加载新抽出的 helper，首轮在导出前停止。
复用完成 stage0–stage3 的 rootfs，以最终收尾/导出代码继续执行，成功生成下列产物。
随后 Docker 包装器改为固定源码快照，防止这种工作区编辑影响长时间构建。
最终快照的全新下载构建未再完整重复；已有的各阶段、导出和快照预检均按上述范围验证。

## 产物

- 文件：`canaan-debian-t-display-k230-validation-20261107T090506Z.img.xz`
- 压缩大小：119,196,048 字节（约 113.7 MiB）
- 原始整盘大小：1,143,996,416 字节
- 压缩镜像 SHA-256：`c2a321b4ebc3e4404d34c3af400de3b506535f990d04402d09941292b77e4e9d`
- 原始整盘 SHA-256：`e70d75f9ed13175054600639eaf6403fc58b5319b0307954ffb7e07f714b8afc`
- rootfs SHA-256：`005833cccb61d6fefebf9eaee21d0ae1fb5a528ea90e86bc12fc6c87fa23f722`

`deploy/` 中包含镜像、`.sha256`、包清单、来源 JSON、独立 `.inspection.json` 与构建日志。
本次测试镜像的 `lilygo` 与 root 密码均锁定，SSH 关闭，未嵌入共享 SSH 主机密钥。
它用于构建验证；需要交互登录时，在本地 `config` 设置密码哈希或 SSH 公钥后重建。
只有公钥、没有密码的用户仍需明确配置管理员权限，默认 sudo 不免密。

## 尚未覆盖

- 未实机启动 Debian；不等同于之前 Buildroot 上的 LILYGO UI 测试。
- 未安装或验证 Debian 版 LILYGO UI；必须先补齐 riscv64 `.deb`、依赖与 systemd 服务。
- 未迁移 ISP/camera、蓝牙等专有用户态服务；保留内核模块不保证其完整功能。
- 不自动扩容 rootfs，也不自动烧录；上板验证流程见 [验证计划](../validation.md)。
