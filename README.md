# canaan-gen

为 **T-Display-K230** 生成 **Debian 13（trixie）riscv64** 系统镜像。
借鉴 pi-gen 的配置文件和阶段式构建方式，保留匹配板卡的 Canaan/LILYGO
BSP 启动链、Linux 内核、设备树和 boot 分区，用 Debian 替换 Buildroot 用户空间。
默认的 `BSP_MODE=source` 会先从固定提交编译 SDK/Buildroot，加入本地板级补丁，
生成并检查成套内核、设备树、模块和启动模板，再进入 Debian 构建。

当前默认清单从签名 APT 源安装 AppKit、Launcher 和 `stage3/packages` 中的 23 个应用。
仅 `lilygo-ui-imu` 按产品要求不发布 riscv64 包，保留在 `stage3/packages.pending`。
缺少任何已选包时预检停止，不导入本地 `.deb`。首次启动可进入图形
引导，依次配置地区、创建用户名和密码，再选择网络与更新；网络和更新可以跳过。
`ACCOUNT_SETUP=launcher` 模式不预建普通账号，没有固定默认用户名或密码。
使用此模式时，预检要求清单包含 `lilygo-ui-launcher` 且签名 APT 仓库已发布相应包。

基础 Debian 工具按本地 pi-gen 的 lite 阶段清单对齐，并移除 Raspberry Pi
专属包；具体的可移植包和排除项见 [pi-gen 包清单对齐说明](docs/pi-gen-package-parity.md)。

2026-11-16 已用源码 BSP 构建 Debian 镜像。发布前还需在 T-Display-K230 上验证冷启动、
显示、触摸和外设。生成器只输出文件。镜像校验和与各项测试结果见
[验证总览](docs/validation/README.md)，板级功能与配置见 [硬件支持对照](docs/hardware-support.md)。

## 先准备什么

- 源码模式会自动获取固定版本的 T-Display-K230 与 K230 SDK，不要求先下载固件。
  也可用 `BSP_MODE=prebuilt` 指定已完成且通过相同驱动检查的 BSP。
- `lilygo-linux-dtoverlays` checkout，包含 `boards/T-Display-K230`。默认使用
  与 `canaan-gen` 同级的目录，也可以设置 `LILYGO_DTOVERLAYS_DIR`。
- Docker 环境；源码阶段使用 Ubuntu 24.04 `linux/amd64`，Debian 阶段使用
  Debian 13。Apple Silicon 通过 Docker 的 amd64 模拟运行原厂 x86_64 工具链。
  原生源码构建需要 x86_64 Linux、SDK 构建依赖及 Debian 打包依赖。
- 非 RISC-V 构建机需要在 Linux 内核中注册 `qemu-riscv64` 的 `binfmt_misc`
  处理器，并启用 `F` 标志，使 Debian 的 RISC-V 程序能在 chroot 中执行。
- 源码完整构建默认要求至少 60 GiB 可用空间，缓存需要长期保留；只打包已有 BSP
  建议至少 15 GB。macOS 同时检查宿主机和 Docker 内部可用空间。

初版只支持 `t-display-k230`、`trixie`、`riscv64`。不安装 Raspberry Pi
固件、内核、Raspbian 包或 CM0 的 arm64 二进制。

## 从源码构建 BSP 和 Debian 镜像

```bash
cp config.example config       # 已有 config 时直接编辑，保留账号和软件源设置
./build-docker.sh --plan-bsp    # 查看固定提交、补丁哈希和输出路径，不下载或编译
./build-docker.sh --bsp-only    # 先完成 SDK/Buildroot 和驱动检查
./build-docker.sh              # 复用校验通过的 BSP，再生成 Debian 镜像
```

也可以直接运行最后一条，一次完成两阶段。`--check` 只检查已经完成的对应 BSP；
缓存不存在会明确停止，不会悄悄下载旧固件或回退到 v0.2.9。

源码步骤取自上游 `k230_bsp/scripts/apply.sh` 和 `k230_bsp/scripts/build.sh`：
固定提交检出 → 安装原厂 Xuantie 工具链 → 应用 BSP 和共享板级补丁 →
`make CONF=k230_canmv_t_display_rm69a10_defconfig ...defconfig` → `make ... all`。
固定版本来自共享板级目录的 `linux/upstream.json`；不跟随浮动 `main`。
上游原始补丁与 `linux-local/` 中新增的键盘、DRV2605 和 XL9555 板级补丁分别保留来源。

Docker 在 `canaan-gen-bsp-cache` 卷保存源码、下载缓存和 Buildroot 输出，在
`canaan-gen-bsp-cache-toolchain` 卷保存工具链。`BSP_CACHE_VOLUME` 可改卷名前缀，
`BSP_JOBS` 控制并行数。补丁或构建脚本改变会选用新的构建目录，旧结果保留；
同一组输入失败后可以重试，成功结果按内容哈希校验后复用。源码容器不使用特权模式。

键盘、DRV2605 和 XL9555 是必需项：检查实际内核驱动、FN 层、键位、IRQ/reset、
马达初始化模式、GPIO/LED 接线和最终启动分区的 DTB，
缺少任何一项就停止，不能把仅存在于源码里的补丁算成已经装入镜像。
编译与静态产物检查仍不能替代实机启动、按键和外设测试。
详细配置、缓存和验证范围见 [源码构建说明](docs/source-bsp.md)。

## 从原厂镜像导入已有 BSP

已有 `.img`、`.img.gz` 或 `.img.xz` 固件时，macOS/Linux 都可以运行：

```bash
./import-bsp.sh --include-camera-runtime /path/to/t-display-k230.img.gz /path/to/new-bsp
```

输出目录必须不存在。该步骤用非特权 Docker 容器读取 ext4 文件，不挂载磁盘、
不执行原镜像程序，也不烧录设备。`--include-camera-runtime` 会额外提取
GC2093 ISP 所需的 `isp_media_server`、`libvvcam.so`、`libmxml.so.1.6`、
`media-ctl`、`v4l2-sysfs-path` 和 `/etc/vvcam` 调参文件；它不会复制整个
Buildroot `/usr` 或替换 Debian 的 C 库。随后将 `BSP_DIR` 设为 `/path/to/new-bsp`。
导入器要求 boot 分区包含 `Image`，且内核保留可提取的 IKCONFIG；如果固件缺少
这些信息，请提供完整、匹配的 BSP 编译输出。来源记录会标明这是发布镜像导入，
不是源码重新编译。

Linux 已安装 `debugfs`（e2fsprogs）时也可直接运行：

```bash
python3 scripts/import-bsp.py --image /path/to/vendor.img.gz \
  --output /path/to/new-bsp --include-camera-runtime
```

## 配置与预检

当前工作目录的 `config` 已切换为源码模式；旧 `.cache/k230-v0.2.9` 保留供追溯，
不再作为默认构建输入。该旧内核没有键盘支持，不能通过新的必需项检查。
全新克隆时先建立配置：

```bash
cp config.example config
```

编辑 `config`，默认使用源码模式：

```bash
BSP_MODE=source
BSP_JOBS=4
BSP_CAMERA_RUNTIME=1
# Omit when lilygo-linux-dtoverlays is beside canaan-gen.
LILYGO_DTOVERLAYS_DIR=/absolute/path/lilygo-linux-dtoverlays
```

使用已有 BSP 时显式设置 `BSP_MODE=prebuilt` 和 `BSP_DIR`。
此模式的 `BSP_IMAGE` 默认取 `${BSP_DIR}/images/sysimage-sdcard.img`。如果构建输出有多个
内核目录，设置 `BSP_KERNEL_CONFIG` 指向这张镜像对应的内核 `.config`。
源码模式自动选择自己的输出，拒绝单独覆盖 `BSP_IMAGE` 或 `BSP_KERNEL_CONFIG`。
配置是受信任的 Bash 脚本片段；本地 `config` 已加入 Git 忽略列表。

先做不构建 rootfs 的预检：

```bash
./build.sh -c config --check
```

预检验证输入布局和 BSP 条件，并联网核验 APT 签名、索引哈希及所选应用清单是否有
riscv64/all 包；缺包会一次列出并停止。它不执行安装或实机启动，依赖解析由构建阶段
的 APT 完成。预检需要 Python 3 和 GnuPG，允许在 macOS 上执行；真正的构建需要 Linux。

图形首次设置配置：

```bash
ACCOUNT_SETUP=launcher
LILYGO_UI_INSTALL=apt
LILYGO_UI_APT_URL=https://lilygo-ui.github.io/packages
ENABLE_SSH=1
FIRST_USER_PASSWORD_HASH=''
SSH_AUTHORIZED_KEYS_FILE=''
```

镜像会确认没有预建普通账号、Launcher 二进制链接 PAM、图形服务已启用、字体可用，
并检查没有残留首次设置状态。账号由屏幕上的向导创建，具有标准的 sudo 权限，
sudo 使用设置时的密码。示例配置开启 SSH 开机自启；在屏幕上创建账号并连网后，
使用 `ssh <设置的用户名>@<板卡IP>` 登录，密码与首次设置时一致。创建账号之前
没有可供 SSH 登录的普通用户；root 登录和空密码登录禁用。SSH 不限制用户名为
固定的 `lilygo`。如需关闭，设置 `ENABLE_SSH=0` 后重新构建。

制作不带 UI 的基础镜像时，使用 `ACCOUNT_SETUP=preset` 和 `LILYGO_UI_INSTALL=0`。
这时 `FIRST_USER_NAME` 默认为 `lilygo`；应提供用 `mkpasswd -m yescrypt` 生成并用
单引号包围的 `FIRST_USER_PASSWORD_HASH`，或启用 SSH 并提供
`SSH_AUTHORIZED_KEYS_FILE`。未提供凭据的 preset 账号仍会锁定。

## 共享板级驱动与硬件支持

`lilygo-linux-dtoverlays/boards/T-Display-K230` 维护固定提交的 Linux 补丁、
设备树修改、内核检查清单和 Debian 板级运行时。`canaan-gen` 预检会校验
上游补丁 SHA-256，读取 `profile.json` 检查实际 BSP，并在每次构建开始时保存
独立快照。Docker 只读挂载该仓库；输出来源记录包含板级文件实际哈希和来源提交。
仓库 revision 在 Git 可用时记录，否则为 `null`；上游补丁提交和实际文件哈希
仍完整记录。

核对过的 v0.2.9 BSP 已将 INNO codec、I²S 和 machine driver 内建进内核，
所以没有相应 `.ko` 属于正常情况。预检现在区分 `=y` 与 `=m`：前者核对
`modules.builtin`，后者核对实际模块及 vermagic；还会检查 `Image` 或 machine
模块中存在 `External I2S Output Switch` 控件，避免只配了 Kconfig 却漏掉音频补丁。
导入旧 BSP 时仍允许提取文件；不满足板级要求的输入会在镜像构建预检时报错。
预检还会核对板级服务必需的内核选项，并逐条列出这个内核没有提供的可选功能
（例如标准电量计驱动）。TCA8418、DRV2605 和 XL9555/LED 驱动已列为必需项。

原厂系统由图形程序以 root 直接操作硬件。Debian 镜像改用三个板级服务，它们在
Launcher 之前启动，也不依赖 Launcher：

| 服务 | 接管的原厂工作 |
| --- | --- |
| `lilygo-k230-audio` | 音频路由、MAX98357A 使能脚 IO34、软件音量控件 `Master`、路由切换 |
| `lilygo-k230-peripherals` | nRF9151 使能脚 IO2、LoRa 供电脚 IO44 |
| `lilygo-k230-power` | 键盘底板的 BQ27220 电量计和 BQ25896 充电芯片、低电量关机 |

镜像还安装设备组的 udev 规则、保留 `wlan0`/`eth0` 命名的 `.link` 文件、让
Launcher 处理电源键短按的 logind 设置，并通过 `/etc/default/lilygo-k230-ui` 把
本板的设备路径交给 LILYGO 应用（电池信息目录、蜂窝串口、触摸节点）。开机加载
原厂同样的 `8189fs`、`8723ds` 和 AIC8800 模块。逐项对照、设置说明和需要重建
内核才能补齐的缺口见 [硬件支持对照](docs/hardware-support.md)。

`lilygo-k230-power` 默认像原厂程序一样管理充电芯片，写入 704 mA 充电电流和
4288 mV 充电电压。如果电芯额定 4.2 V，在 `/etc/default/lilygo-k230-power` 把
`LILYGO_K230_CHARGE_VOLTAGE_MV` 改为 `4208`，或设 `LILYGO_K230_CHARGER_MANAGE=off`。

外置功放没有硬件音量；`/etc/asound.conf` 的 softvol 提供 `Master` 控件，Launcher
的音量滑块通过它生效。需要内置耳机/录音时运行 `lilygo-k230-audio-route internal`
（先关闭所有音频流），或在 `/etc/default/lilygo-k230-audio` 设置
`LILYGO_K230_AUDIO_OUTPUT=internal` 并重启服务。

板上只读诊断：

```bash
sudo lilygo-k230-audio-check
sudo lilygo-k230-peripherals-check
systemctl status 'lilygo-k230-*'
journalctl -b -u 'lilygo-k230-*'
ls /run/lilygo-k230/power_supply/
aplay -l
arecord -l
```

驱动/DTB 有修改时，源码模式会按新的输入哈希重新构建成套 `Image`、DTB、模块和
`sysimage-sdcard.img`。使用外部 BSP 时仍需自行完成这一步，再设置 `BSP_DIR`。
Debian 打包保留通过验证的 boot 分区；实机播放、录音和按键仍需板上验证。

## 使用 Docker 构建

```bash
./build-docker.sh -c config
```

容器构建需要 Linux 的 chroot 和 mount 能力。包装脚本使用特权构建容器，将 BSP
作为只读输入挂载，构建脚本在 Docker 镜像创建时固定快照；构建期间编辑工作区
不会改变已启动任务的脚本。工作 rootfs 放在 Linux Docker 卷内，成功后导出到本机
`deploy/`。这也避免将 Linux 所有权、符号链接和特殊文件写到 macOS 文件系统。

Docker 入口会在需要时挂载 `binfmt_misc` 并启用 `qemu-riscv64` 注册，再由构建
脚本检查 `F` 标志。这项注册作用于 Docker 引擎共享的 Linux 内核。若引擎策略不
允许注册或已有处理器缺少 `F` 标志，构建会停止，需要先修正该 Linux 环境配置。

容器和 Linux 工作卷会保留，终端输出其名称，日志同时保存到 `deploy/`。可通过
`docker logs <容器名>` 查看；确认不再需要其中的工作文件后，用
`docker rm -v <容器名>` 清理。

只输出 Debian 根文件系统时：

```bash
./build-docker.sh -c config --rootfs-only
```

`--rootfs-only` 仍需要匹配的 BSP 输入，用于内核模块、固件、预检和 rootfs UUID。
它输出 ext4 文件系统，不是可以直接启动的整盘镜像。

## 在 Debian Linux 原生构建

构建机依赖列在 `depends`。安装依赖并注册 QEMU 的典型方式：

```bash
sudo apt-get update
sudo apt-get install bash binfmt-support ca-certificates coreutils debootstrap \
  debian-archive-keyring dosfstools e2fsprogs file gpg kmod python3 qemu-user-static \
  qemu-user-binfmt rsync util-linux xz-utils zstd binutils
sudo update-binfmts --enable qemu-riscv64
./build.sh -c config --check
sudo ./build.sh -c config
```

原生 RISC-V Linux 构建机不需要 QEMU。原生构建的工作目录和输出目录默认为
`work/`、`deploy/`，可在配置中用 `WORK_DIR`、`DEPLOY_DIR` 覆盖。每次构建创建
新的工作目录；失败的目录与日志保留用于诊断，不自动从旧 rootfs 继续执行。

## 构建阶段和产物

| 阶段 | 内容 |
| --- | --- |
| BSP 源码 | 固定版本 SDK/Buildroot、板级补丁、内核/DTB/模块和完整启动模板；验证后交给 Debian |
| `stage0` | 使用 Debian 官方仓库和 debootstrap 创建 riscv64 基础系统 |
| `stage1` | systemd、NetworkManager、账号配置模式、locale、时区与可选 SSH |
| `stage2` | 导入匹配 BSP 的模块和固件，安装共享板级配置并启用音频、外设使能、电源三个板级服务 |
| `stage3` | 安装独立发布的 LILYGO UI Debian 包并验证图形首次开机配置 |
| 导出 | 应用 overlay，清理机器身份信息，创建 ext4 并装配镜像 |

`deploy/` 中输出带 UTC 时间戳的镜像、包版本清单、来源记录和 SHA-256 校验文件。
默认压缩为 `.img.xz`；`--rootfs-only` 输出 `.rootfs.ext4.xz`。设置
`DEPLOY_COMPRESSION=none` 可保留未压缩文件。`ROOTFS_EXTRA_MB` 默认 512 MiB，
构建还会按已用空间增加余量。启动时 `canaan-grow-rootfs.service` 会识别实际
SD 卡，把最后的根分区 p2 和 ext4 文件系统扩展到卡上的可用空间；无需调整
`ROOTFS_EXTRA_MB` 来匹配不同容量的卡。

扩容在系统初始化阶段执行，不依赖 Launcher 设置账号或 cloud-init seed。
每次启动都会检查，因此重复启动、扩容中断后重试，以及以后克隆到更大卡都能
继续处理。仅支持本项目生成的 512 字节扇区、MBR p1/p2、ext4 根分区布局；
不移动分区起点、不缩小分区。可在设备上检查结果：

```bash
systemctl status canaan-grow-rootfs.service
journalctl -u canaan-grow-rootfs.service -b
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
df -h /
```

此前生成的镜像不会自动获得这个服务。可以重新构建和烧录；也可以将
`stage2/canaan-grow-rootfs.py` 和 `stage2/canaan-grow-rootfs.service` 复制到旧系统，
在设备上进入这两个文件所在目录后执行：

```bash
sudo apt-get update
sudo apt-get install cloud-guest-utils fdisk e2fsprogs python3 util-linux
sudo install -m 0755 canaan-grow-rootfs.py /usr/sbin/canaan-grow-rootfs
sudo install -m 0644 canaan-grow-rootfs.service /etc/systemd/system/canaan-grow-rootfs.service
sudo systemctl daemon-reload
sudo systemctl enable --now canaan-grow-rootfs.service
df -h /
```

若内核未能刷新分区大小，服务会报告失败并在下一次开机重试，不会自动重启。

Debian 软件包版本、BSP 输入和构建环境都会影响结果。来源记录有助于追踪构建，
但使用滚动更新的软件仓库不保证字节级可复现。

## 导出应用开发 SDK

镜像构建默认在 `deploy/` 同时发布 `<镜像名>.sdk/`。`EXPORT_SDK=0` 可仅构建镜像。
SDK 也支持单独从已发布的 `.img[.xz]` 或 `.rootfs.ext4[.xz]` 导出。将原始镜像、
同名 `.json` 和 `.packages.tsv` 放在一起，在 `canaan-gen` 目录运行：

```sh
./export-sdk deploy/canaan-debian-t-display-k230-lilygo-ui-20261116T104343Z.img.xz
```

第二个位置参数可以指定其他输出目录。导出器检查镜像与清单的 SHA-256，核对 rootfs
实际安装的包、版本和架构。它使用镜像保留的 APT 索引选择开发包，通过镜像对应的
Debian 仓库下载并校验 `.deb`。解析依赖时锁定已有二进制包和相应源码包版本。
若仓库已移除所需文件，或依赖要求升级板上的库，导出会报错。建议保存已发布的 SDK 归档。

独立导出在 Debian 13 Linux 上使用 Python 3、APT、dpkg、e2fsprogs、GNU tar、xz
和 Debian archive keyring。macOS 入口使用 Docker，默认镜像名为 `canaan-gen:trixie`，
也可通过 `SDK_DOCKER_IMAGE` 指定有相同工具的本地 Debian 13 镜像。准备约 10 GiB
临时空间。构建时导出直接读取本次构建的 rootfs，省去镜像解压和分区提取。

`.sdk/` 里包含 `*.sysroot.tar.gz`、`image-packages.tsv`、`manifest.json` 和 `SHA256SUMS`。
manifest 记录来源镜像的 SHA-256 和新增开发包的版本、架构、SHA-256。
归档包含目标头文件、库、pkg-config 文件和 `dpkg-shlibdeps` 需要的文件归属及 ABI 元数据。
在 `.sdk/` 中校验、解压一次，让所有应用共用同一个绝对路径：

```sh
sha256sum -c SHA256SUMS            # macOS 可用 shasum -a 256 -c SHA256SUMS
sdk_dir="$HOME/sdk/k230/20261116T104343Z"
mkdir -p "$sdk_dir"
tar -xzf ./*.sysroot.tar.gz -C "$sdk_dir"
export LILYGO_UI_DEBIAN_SYSROOT="$sdk_dir/sysroot"
```

进入应用源码目录后，运行以下命令。显式指定路径也可以切换已有 CMake 缓存所用的 SDK。

```sh
cmake --preset k230-debian -DLILYGO_UI_DEBIAN_SYSROOT="$LILYGO_UI_DEBIAN_SYSROOT"
lpm pack --arch riscv64
```

## 镜像如何保持原板启动链

初版接受 **MBR 中恰好两个 Linux 分区**的布局：p1 为 boot，p2 为最后的 rootfs。
已检查的 T-Display-K230 BSP 将 boot 放在 30 MiB、rootfs 放在 128 MiB。
工具读取并验证实际镜像，不依赖写死的 rootfs 偏移。

所有 rootfs 起点之前的字节都复制自输入，包括未列入分区表的 SPL、U-Boot、环境
和完整 boot 分区；只更新 MBR 中 rootfs 的长度字段。rootfs 分区起点、磁盘标识、
其他分区表字段和原 rootfs UUID 保持。GPT、扩展分区、额外分区、分区重叠或越界均
拒绝导出。输入镜像和已有输出不会被覆盖。

可以单独检查模板：

```bash
python3 scripts/image.py inspect --image /path/to/sysimage-sdcard.img
```

如果已有采用相同 UUID 的 Debian ext4，可独立装配为新文件：

```bash
python3 scripts/image.py assemble \
  --image /path/to/sysimage-sdcard.img \
  --rootfs /path/to/debian-rootfs.ext4 \
  --output /path/to/new-debian.img
```

模板的内核是否满足 Debian/systemd 要求、Wi-Fi 和外围设备是否可用，仍须经过
实际构建与串口启动验证。详见 [设计边界](docs/design.md) 和
[验证计划](docs/validation.md)。

## 加入 LILYGO UI

K230 硬件 profile 与发行版集成已分离。Debian 构建使用
`LILYGO_UI_PLATFORM=k230`、`LILYGO_UI_TARGET_OS=debian` 和
`LAUNCHER_SYSTEM_SESSION=ON`，输出 riscv64 Debian 包，带 PAM、systemd 与通用
Debian 首次设置。Buildroot 的 TGZ/SysV 构建入口继续保留。不要混用 Buildroot
用户态库或 CM0 arm64 包。

镜像统一使用 LILYGO UI 的签名 APT 仓库：

```bash
LILYGO_UI_INSTALL=apt
LILYGO_UI_APT_URL=https://lilygo-ui.github.io/packages
# 公钥默认使用项目内 keys/lilygo-ui-archive-keyring.gpg。
# 省略 LILYGO_UI_PACKAGES 时使用 stage3/packages 的当前 25 个包。
```

当前清单包含 AppKit、Launcher 和 23 个应用；按产品要求不发布 riscv64 包的
`lilygo-ui-imu` 保存在 `stage3/packages.pending`。
清单不包含树莓派专用的 Mesa VC4，`meshtasticd` 为可选 riscv64 软件包，板载电台选择见[硬件支持](docs/hardware-support.md)。
需要精简镜像时可以显式设置
`LILYGO_UI_PACKAGES`，例如：

```bash
LILYGO_UI_PACKAGES='lilygo-ui-appkit-dev lilygo-ui-launcher lilygo-ui-template lilygo-ui-calculator'
```

源使用 `stable/main` 和 `riscv64` 架构。`keys/README.md` 记录已核验的公钥指纹；
自定义公钥可用 `LILYGO_UI_APT_KEY_FILE` 指向本地文件，Docker 将只读挂载它。
构建前验证 `InRelease` 和索引哈希，检查清单中的每个包；只有 arm64 版本不算满足。
包未发布或网络、签名验证失败时停止，不跳过应用，不回退到 `.cache/ui-debs`。
旧配置中的 `LILYGO_UI_INSTALL=local` 会明确报错，`LILYGO_UI_DEB_DIR` 不再使用。

生成的系统保留软件源和独立签名密钥，后续可通过 APT 更新应用。来源记录包含源地址、
公钥哈希和请求的包清单；旁边的 `.packages.tsv` 记录实际安装版本。应用源码仍位于
各自独立仓库，修改源码需要先构建并发布相应的 riscv64 包，镜像生成器不会编译应用。
APT 更新应用不会替换 BSP 内核；TCA8418 驱动由前面的源码 BSP 阶段提供。

网络由 NetworkManager 独立管理，不依赖 Launcher 进程。Wi-Fi 配置可以通过
`ROOTFS_OVERLAY` 提供 NetworkManager 配置文件；最终校验仍须确认模块、固件和
无线连接能在没有图形界面时启动。overlay 在所有阶段之后应用，并保留配置文件的
权限；不要将设备密码或私钥提交到仓库。

overlay 不允许包含顶层 `dev/`、`proc/`、`sys/`、`run/` 或 `tmp/`，包括指向
这些目录的符号链接。复制前会卸载临时挂载点，复制后再挂载，避免改动构建机的
设备节点或将文件写进随后被卸载的临时文件系统。

## 测试和许可证

```bash
python3 -m unittest discover -s tests -v
```

镜像测试使用合成 MBR/ext4 元数据，验证布局拒绝、引导字节保持、UUID、边界和
输入文件保护；不访问设备、联网、挂载或烧录，也不能代替实际 ext4 和板卡测试。

生成器原创代码采用 [MIT](LICENSE)。镜像中的 Debian 包、BSP、固件和字体保持
各自许可证；发布时须保留对应的许可证及适用的源码提供义务。
