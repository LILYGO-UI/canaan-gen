# T-Display-K230 Debian 硬件支持对照

本文记录 Debian 镜像如何接管原厂 Buildroot 系统里的硬件处理，以及 LILYGO 应用
通过什么接口使用这些硬件。源码模式基于原厂 BSP 构建内核、设备树和启动链，并加入
本地键盘补丁。本文同时记录用户态服务和剩余内核缺口；实机验收步骤见文末。

## 依据

- 原厂源码 `Xinyuan-LilyGO/T-Display-K230` 提交
  `c27c58c07ca5e39d15f5dcf1b7103aead29282df`：`k230_launcher/k230_phone_ui/src/`
  （主要是 `ui_hardware.c`、`ui_audio.c`、`ui_cellular.c`、`ui_lora.cpp`）、
  `k230_bsp/docs/HARDWARE_PINMAP.md` 和 `k230_bsp/overlay/`。
- 原厂 v0.2.9 发布镜像：rootfs 的 `/etc/init.d/*`、`/etc/mdev_video.sh`、
  `/etc/sysctl.conf`，boot 分区的 `k230-canmv-rm69a10.dtb`，以及内核自带的配置。

原厂系统里，开机脚本只加载模块、启动 ISP 守护进程；其余硬件（功放、电池、充电、
键盘底板、模组供电）都由图形程序 `k230_phone_ui` 以 root 身份直接操作。Debian 上
应用以普通用户运行，所以这部分工作改由系统服务完成。

## 对照表

| 硬件 | 原厂做法 | Debian 镜像 | 应用使用的接口 |
| --- | --- | --- | --- |
| RM69A10 屏幕 | 内核 DRM | 同一内核 | `/dev/dri/card0` |
| 背光 | 写 `/sys/class/backlight/*/brightness` | udev 让 `video` 组可写 `rm69a10/brightness` | AppKit `Hardware::set_backlight` |
| GT9895 触摸 | 内核 `goodix_ts` | 同一内核；udev 建立 `/dev/input/lilygo-touchscreen` | AppKit 自动发现；NES 读 `NES_TOUCH_DEVICE` |
| 电源键 | UI 读 `KEY_POWER`，长按由内核关机 | logind 忽略短按，交给 Launcher | evdev `K230 PMU Power Key` |
| SDIO Wi-Fi | 开机 `modprobe 8189fs 8723ds`，UI 调 `wpa_supplicant`/`udhcpc` | 同样加载两个模块；NetworkManager 管理，扫描不使用随机 MAC | `nmcli`（Launcher、Settings） |
| USB Wi-Fi/蓝牙（AIC8800） | 开机 `modprobe aic_load_fw aic8800_fdrv aic_btusb` | BSP 含这些模块时同样加载 | NetworkManager、BlueZ |
| 网卡命名 | 固定 `wlan0`/`eth0`/`usb0` | `.link` 文件保留内核命名 | 同左（xiaozhi 等按名字读 MAC） |
| 扬声器功放 MAX98357A | UI 设 `External I2S Output Switch` 并持有 IO34 | `lilygo-k230-audio.service` | ALSA `default` |
| 音量 | 管道里的 `k230_pcm_volume` 软件缩放 | ALSA softvol，控件 `Master Playback Volume` | AppKit `Hardware::set_volume` |
| 麦克风/耳机 | 录音前切到内部路由并关功放 | `lilygo-k230-audio-route internal` | ALSA `default` 采集 |
| 电量计 BQ27220、充电 BQ25896 | UI 在 IO46/IO47 上模拟 I²C | `lilygo-k230-power.service`，发布到 `/run/lilygo-k230/power_supply` | `CM0_POWER_SUPPLY_DIR`（状态栏、Settings） |
| DRV2605 振动 | UI 在键盘底板 GPIO-I2C `0x5a` 选择 ERM 开环和 ROM library 1 | 内核 `drv260x-haptics`，键盘反馈默认关闭 | `FF_RUMBLE`、`keyboard_feedback_strength`（Settings） |
| XL9555 底板 LED | UI 操作 P03/P04/P05，低有效 | `gpio-pca953x` + `leds-gpio`，三灯默认关闭 | `/sys/class/leds/keyboard:{capslock,fn,status}/brightness` |
| 低电量关机 | 3600 mV，连续 3 次、间隔 30 s | 同一规则，`systemctl poweroff` | — |
| nRF9151 蜂窝/GNSS | UI 拉高 IO2，等 600 ms 后开 `/dev/ttyS3` | `lilygo-k230-peripherals.service` 持有 IO2 | `/dev/ttyS3`（`CELLULAR_PORT`、`LILYGO_UI_CELLULAR_DEVICE`） |
| LoRa SX1262/LR2021 | UI 拉高 IO44 后开 `/dev/spidev0.0` | 同一服务持有 IO44 | `/dev/spidev0.0`，`spi` 组 |
| nRF52840 | `/dev/ttyS1` 上的 AT 协议 | 串口属 `dialout` 组，系统不占用 | `/dev/ttyS1` |
| GC2093 摄像头 | `S31canaan_isp` 加载模块并启动 `isp_media_server` | `lilygo-k230-camera-bsp.service`（`BSP_CAMERA_RUNTIME=1`） | `/dev/video*`，`video` 组 |
| 视频节点别名 | `mdev_video.sh` | udev 建立 `video_nonai_2d`、`video_vpu`、`vvcam-*` | — |
| ISP 实时线程 | `sysctl kernel.sched_rt_runtime_us=-1` | 启用相机运行时时写入 `/etc/sysctl.d` | — |
| AHT20 温湿度 | hwmon，失败时退回模拟 I²C | 内核 hwmon | `/sys/class/hwmon` |
| CPU 温度 | thermal zone | 同一内核 | `/sys/class/thermal` |
| I²C、SPI、GPIO 节点 | root 直接访问 | udev 分给 `i2c`、`spi`、`gpio` 组 | `/dev/i2c-*`、`/dev/spidev*`、`/dev/gpiochip*` |

## 板级服务

三个服务的程序和配置都在
`lilygo-linux-dtoverlays/boards/T-Display-K230/debian/rootfs/`，`canaan-gen` 在
`stage2` 安装并启用。它们都在 Launcher 之前启动，也不依赖 Launcher。

### lilygo-k230-audio

选择音频路由并持续持有功放使能脚 IO34；首次启动时打开一次默认 PCM，让 softvol
建立 `Master Playback Volume` 控件，此后由 Debian 的 `alsa-restore` 保存音量。
内核自带的 `PCM Playback Volume` 只作用于内部编解码器，不影响外置功放，所以
Launcher 的音量滑块需要这个软件控件才起作用。

内核不允许在 PCM 打开期间切换路由，所以录音程序自己无法切换。`audio` 组成员可以
请求服务切换：

```sh
lilygo-k230-audio-route     # 显示当前路由
lilygo-k230-audio-route internal   # 内部编解码器：耳机输出和麦克风，功放关闭
lilygo-k230-audio-route external   # 外置扬声器
```

切换前要关闭所有音频流，否则服务会报告内核拒绝。开机默认值在
`/etc/default/lilygo-k230-audio`。声卡同一时间只接受一路播放，与原厂系统一致。

### lilygo-k230-peripherals

持有 nRF9151 使能脚 IO2 和 LoRa 供电脚 IO44 并拉高。停止服务会把两条线拉低。
`/etc/default/lilygo-k230-peripherals` 可以分别关闭；如果无线电守护进程自己配置了
使能脚，把 `LILYGO_K230_LORA_POWER` 设为 `off`。线已被其他程序占用时服务只记录
警告，不抢占。

原厂引脚表给出的 LoRa 连接：`/dev/spidev0.0`（片选 IO14 由控制器驱动）、复位
IO5、BUSY IO19、中断 IO20，均在标签为 `9140b000.gpio` 的控制器上；供电 IO44 在
`9140c000.gpio` 上。上游 `meshtasticd` 的 Debian 13 软件源没有 riscv64 构建（见
`launcher/k230-build-logs/meshtastic-dependency-review.txt`），镜像没有预装无线电
守护进程。

### lilygo-k230-power

键盘底板上的 BQ27220 和 BQ25896 由服务读取。默认 `auto` 检测设备树里的内核
键盘：新源码镜像使用内核 `i2c-gpio` 适配器，通过 `/dev/i2c-*` 访问 IO46/IO47；
硬件 I2C4 继续服务摄像头 IO7/IO8。旧 v0.2.9 设备树才保留原厂 GPIO 模拟 I²C
回退；新镜像不会再用用户态 `/dev/mem` 重映射内核键盘的 GPIO。

读数按 `/sys/class/power_supply` 的格式发布：

```text
/run/lilygo-k230/power_supply/bq27220-battery/{type,present,status,capacity,voltage_now,current_now,...}
/run/lilygo-k230/power_supply/bq25890-charger/{type,online,status,health,charge_type,...}
```

没有接底板时目录为空，状态栏显示 `--`。文件只读，所以 Settings 电池页只显示、不
提供修改控件。

`/etc/default/lilygo-k230-power` 的默认值与原厂一致，需要注意两项：

- **充电参数**：`LILYGO_K230_CHARGER_MANAGE=on` 时，服务像原厂程序一样关闭充电
  芯片的看门狗，并在芯片设置不同时写入 704 mA 充电电流和 **4288 mV** 充电电压。
  这是原厂针对其电池底板的默认值。如果接的是额定 4.2 V 的电芯，把
  `LILYGO_K230_CHARGE_VOLTAGE_MV` 改为 `4208`；设为 `off` 则服务只读取，不写入
  充电芯片。
- **低电量关机**：电压不高于 `LILYGO_K230_BATTERY_SHUTDOWN_MV`（默认 3600）、
  没有在充电、也没有 USB 供电，连续三次检查（间隔 30 s）都成立时执行
  `systemctl poweroff`。设为 `0` 关闭。

`LILYGO_K230_POWER_BUS=i2c-dev` 可显式选择键盘底板的 GPIO-I2C 适配器（Linux 动态
编号的 `/dev/i2c-*`），
不切换引脚功能，也不使用 `/dev/mem`。新键盘设备树下的 `auto` 会自动选择该模式。
键盘与电池轮询同时工作的可靠性仍需要上板确认。

电量计的容量参数（原厂首次开机写入 6000 mAh）保存在电量计内部，服务不改写。

## 应用接口适配

LILYGO 应用最初为 T-DisplayCM0 编写，部分默认路径是树莓派的。镜像不改应用，而是
通过它们已有的环境变量给出本板路径。变量集中在 `/etc/default/lilygo-k230-ui`：

| 变量 | 值 | 使用者 |
| --- | --- | --- |
| `CM0_POWER_SUPPLY_DIR` | `/run/lilygo-k230/power_supply` | AppKit 状态栏、Settings 电池页 |
| `LILYGO_UI_CELLULAR_DEVICE` | `/dev/ttyS3` | Settings 蜂窝页（默认 `/dev/ttyACM0`） |
| `CELLULAR_PORT` | `/dev/ttyS3` | Cellular、Maps |
| `NES_TOUCH_DEVICE` | `/dev/input/lilygo-touchscreen` | NES 多点触控（默认是树莓派路径） |

`lilygo-ui-launcher.service` 通过 drop-in 读取该文件，Launcher 启动的应用继承这些
变量；`/etc/profile.d/lilygo-k230-ui.sh` 让 SSH 或串口登录后手动启动的应用得到同样
的值。构建时 `check-ui-rootfs.py` 只接受这一个 drop-in，并且要求文件里只有设备
路径。

如果以后的 BSP 内核自带电量计驱动，应删除 `CM0_POWER_SUPPLY_DIR` 一行并停用
`lilygo-k230-power.service`，让应用直接读 `/sys/class/power_supply`。

向导创建的用户会加入 `video`、`audio`、`input`、`dialout`、`i2c`、`spi`、`gpio`
等组（`stage1` 预先建立这些组）。`/dev/mem` 仍然只有 root 可以访问。

## 键盘和底板外设源码集成

`BSP_MODE=source` 现在先编译固定版本 SDK/Buildroot，并加入共享板级目录
`linux-local/` 的 TCA8418 驱动、7×10 矩阵/FN 键位、GPIO42 中断和 GPIO43 复位。
保持内核驱动名 `tca8418_keypad`，供现有 AppKit 和 Launcher 使用。
键盘从可选项改为必需项：检查配置、驱动二进制，以及最终 boot 分区里
`/k.dtb` 选中的设备树。旧 v0.2.9 BSP 会被拒绝，不能继续生成缺键盘的新镜像。
2026-11-16 已完成全量 BSP 编译并导出 Debian 镜像。键盘与底板外设还需上板验收。
[源码镜像与校验和](validation/2026-11-16-buildroot-recheck.md#source-built-debian-image)

DRV2605 和 XL9555 也已加入必需清单。DRV2605 使用键盘底板 GPIO-I2C `0x5a`，提供标准
`FF_RUMBLE` 和 Settings 已使用的 `keyboard_feedback_strength`（0–100）。
默认 0 保持原厂“按键振动关闭”行为，设置为非零后按键触发 50 ms 反馈。
其初始化保留原厂 ERM 开环/library 1，不覆盖未获硬件资料确认的电机电压寄存器，
不启动自动校准；没有凭空声明 EN 引脚或供电电压。

XL9555 通过兼容的 PCA9555 驱动提供 16 根 GPIO，其中 P03/P04/P05 交给 LED
子系统，其余线路不占用。默认采用原厂软件初始地址 `0x20`；原厂也支持
`0x21`–`0x27` 的地址绑带变体，这些板需要同步修改设备树 `reg`、节点路径及
`profile.json` 后更新本地补丁哈希并重编。三颗灯默认熄灭，`gpio` 组可写亮度；
名称表达其原厂用途，目前没有增加桌面 Caps/Fn 状态自动同步。

电源服务默认 `auto`：新键盘设备树使用 `i2c-dev` 访问专用 GPIO-I2C 总线，避免 GPIO
模拟访问抢占内核键盘的引脚；旧设备树才保留原 GPIO 回退。不能在内核键盘启用时
强制选择 GPIO 模式。

预检会拒绝缺少以上必需驱动的 BSP，并列出仍未提供的可选功能
（`profile.json` 的 `kernel.optional_config`）。旧 v0.2.9 与新源码接入的区别如下：

| 功能 | 缺少的内核支持 | 现状 |
| --- | --- | --- |
| 键盘底板按键（TCA8418） | 旧 v0.2.9 缺 `CONFIG_KEYBOARD_TCA8418` 和设备树节点 | 源码镜像已通过驱动、设备树和 FN 键位检查，按键与插拔待实测 |
| 电量计/充电芯片的标准驱动 | `CONFIG_BATTERY_BQ27XXX`、`CONFIG_CHARGER_BQ25890` | 由 `lilygo-k230-power` 替代 |
| DRV2605 触觉反馈 | 旧 BSP 缺 `CONFIG_INPUT_DRV260X_HAPTICS` | 源码镜像已通过驱动、设备树和强度接口检查，按键振动待实测 |
| XL9555 扩展口（底板 LED） | 旧 BSP 缺 `CONFIG_GPIO_PCA953X`、`CONFIG_LEDS_GPIO` | 源码镜像已通过扩展口驱动和三颗 LED 的设备树检查，点灯待实测 |
| BOOT0 按键 | 设备树里没有 `gpio-keys` 节点 | 不产生输入事件 |
| 硬件时钟 | `CONFIG_RTC_DRV_K230` 未启用 | 时间来自 `systemd-timesyncd` |
| rfkill | `CONFIG_RFKILL` | Wi-Fi 开关由 NetworkManager 完成，没有 `/dev/rfkill` |

这些改动属于共享板级目录的内核层；新增本地补丁放在 `linux-local/`，保留
`linux/` 上游原始文件。源码模式负责在 SDK 里重建成套的内核、设备树、模块和启动镜像。

## 没有迁移的原厂功能

- USB 设备模式（ADB/MTP、U 盘、ACM、RNDIS）：原厂默认不启动，只在 UI 里手动开启；
  镜像没有对应的应用入口。
- HDMI 输出切换（把 `/boot/k.dtb` 换成 HDMI 设备树）、nRF52840 固件升级、键盘
  背光 PWM、风扇控制：都是原厂 UI 内部功能，现有 LILYGO 应用没有对应接口。
- RTL8723DS 的蓝牙串口：原厂 UI 只有在设置环境变量
  `K230_BLE_ENABLE_UART_HCI=1` 时才在 `/dev/ttyS1` 上运行 `hciattach`，开机脚本
  没有设置它，UART1 也只接了 TX/RX。蓝牙使用 USB 适配器，或 nRF52840 的 AT 协议。
- 原厂的 telnet、root 空密码和允许空密码的 SSH：有意不带入。

## 上板验收

```sh
systemctl status lilygo-k230-audio lilygo-k230-peripherals lilygo-k230-power
sudo lilygo-k230-audio-check   # 声卡、路由、Master 控件、IO34
sudo lilygo-k230-peripherals-check    # IO2、IO44 的占用者和方向
sudo systemctl stop lilygo-k230-power && sudo lilygo-k230-power-check; sudo systemctl start lilygo-k230-power
ls /run/lilygo-k230/power_supply/*/ && cat /run/lilygo-k230/power_supply/bq27220-battery/capacity
amixer -c K230I2SINNO sget Master
ip -br link       # wlan0，USB 网卡为 eth0
nmcli device wifi list
ls -l /dev/input/lilygo-touchscreen /dev/video_* 2>/dev/null
journalctl -b -u 'lilygo-k230-*'
```

需要逐项确认：

1. 扬声器出声，Launcher 音量滑块改变实际音量，重启后音量保持。
2. `lilygo-k230-audio-route internal` 后能录音，切回 `external` 后扬声器恢复。
3. 接上键盘底板后状态栏显示电量；插拔 USB 时充电状态变化；
   `constant_charge_current`、`constant_charge_voltage` 为 704000 和 4288000。
4. 短按电源键由 Launcher 处理而不是直接关机；长按仍能关机。
5. Wi-Fi 能扫描、连接，冷启动后自动重连。
6. Cellular 应用能与 nRF9151 通信；停止 `lilygo-k230-peripherals` 后模组断电。
7. 启用相机运行时的镜像里 `lilygo-k230-camera-bsp.service` 运行，Camera 应用有预览。
8. 确认新镜像电源服务使用 `i2c-dev`；同时轮询电量并测试普通键、FN 符号和底板插拔，
   核对 evdev 输入及 Launcher 恢复功能。
9. Settings 键盘振动强度从 0 改为非零后按键应有短反馈，改回 0 后无按键反馈；
   检查设备名 `drv260x:haptics`。不存在马达的底板不应误报为已实测可用。
10. 分别写 `/sys/class/leds/keyboard:{capslock,fn,status}/brightness` 为 1/0，
    确认各自点亮/关闭且其余灯不受影响，重启后三灯默认熄灭。
