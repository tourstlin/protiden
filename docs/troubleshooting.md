# 排坑记录（板端实战）

## 显示相关

**开机分辨率掉回 1280×800**
显示器 EDID 的"首选模式"往往不是 1080p，重启后就回落。程序侧会检测屏幕尺寸变化并重新全屏，
但最好在桌面级也锁死：

```bash
# ~/.config/autostart/lamp-resolution.desktop
Exec=xrandr --output HDMI-1 --mode 1920x1080 --rate 60
```

调试期用 `LAMP_KIOSK=0 ./scripts/protaiden-start.sh` 保留标题栏，方便看窗口尺寸。

**中文字体变马赛克**
conda 版 Python 的 Tk 没编 Xft，所有字体退化成位图字体。借系统 Tk 即可：

```bash
export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libtk8.6.so:/usr/lib/aarch64-linux-gnu/libtcl8.6.so"
```

启动脚本已自动检测并设置。

**触屏要隐藏鼠标指针**
Tk 的 `cursor="none"` 可用（板端 Tk 8.6 实测 OK）；`@透明位图.xbm` 那套写法在板端会报
`bad cursor spec`。注意还要递归覆盖 CTk 控件自带的 `hand2`。
`LAMP_HIDE_CURSOR=0` 可临时找回指针。

**任务栏盖在界面上**
用 `root.attributes("-fullscreen", True)`：EWMH fullscreen 层在 lxpanel 之上，
任务栏与标题栏都会被盖住。不需要 `overrideredirect`。

## 摄像头

**MIPI（OV5648）开机没画面**
sensor 上电时序问题，probe 失败。重绑一次：

```bash
sudo sh -c 'echo 4-0036 > /sys/bus/i2c/drivers/ov5648/unbind; sleep 0.2;
            echo 4-0036 > /sys/bus/i2c/drivers/ov5648/bind'
```

启动脚本会自动尝试（需 sudo 免密），失败会明确提示。

**USB 摄像头帧率只有 6fps**
不指定格式时走 YUYV，USB2.0 带宽不够。必须切 MJPG：

```bash
export CAM_INDEX=9 CAM_FOURCC=MJPG CAM_BUFSIZE=2
```

**MIPI 只有 5fps**
`CAP_PROP_BUFFERSIZE` 太小。RKISP 路径要 **≥3** 才能跑满 sensor 的 15fps
（实测 1 → 5fps，2 → 10fps，4 → 15fps）：

```bash
export CAM_BUFSIZE=4
```

**画面偏绿**
3A 服务没起来（该镜像只有 v1 XML IQ，服务只认 v2 JSON）。对手势识别无影响，
只是预览难看；要修得补 IQ 配置文件。

## 蓝牙 / 网络

**蓝牙 MAC 每次重启都变，配对丢失**
控制器用了随机地址。固定它（详见 `scripts/fix_bt_addr.sh`）：

```bash
sudo btmgmt --index 0 public-addr AA:BB:CC:DD:EE:FF
```

**内核没有 `/dev/rfcomm0`**
`CONFIG_BT_RFCOMM_TTY` 未编译。不用重新编内核——直接用 **RFCOMM socket 连 MAC**
（主程序支持 `bt:AA:BB:CC:DD:EE:FF` 写法），或走 `bt_bridge.py` 桥接。

**蓝牙连接总被判失败**
寻呼 + 认证要 3~6 秒，而串口默认超时 1 秒。连接超时必须 **≥10 秒**。

**WiFi 与蓝牙抢天线**
蓝牙 inquiry 扫描期间 WiFi 会掉。所以不要扫描，用固定 MAC 直连；
WiFi 掉线后重连（NetworkManager 的 `autoconnect=yes` + 无限重试即可）。

**板子时钟不准（无外网时无 NTP）**
时钟偏差会让 `tar` 报"时间戳在未来"、TLS 失败。手动校准 + 落盘：

```bash
sudo date -s "2026-09-13 15:08:00" && sudo hwclock --systohc
sudo systemctl enable --now systemd-timesyncd   # 有网后自动保持
```

## 音乐（mpv）

**重启后出现两个 mpv 同时出声**
旧实例的 mpv 因为 socket 文件被新实例 unlink 变成孤儿。`music_player.py` 的处理：
启动前先尝试**接管**已有 socket（问一次 `mpv-version` 确认是 mpv），
不能接管才 `pkill -f input-ipc-server=<sock>` 清孤儿再起新的。

**播放列表点一下跳两首**
`loadfile replace` 也会发 `end-file` 事件。自动下一曲必须过滤
`event == "end-file" and reason == "eof"`。

## 性能 / 其他

**手势占用一个核**
推理搬到 NPU 后降到约半个核（见 `docs/npu-migration.md`）。

**CPU 频率会降**
默认 governor 可能不是 performance，装 `scripts/cpu-performance.service` 固化：

```bash
sudo cp scripts/cpu-performance.service /etc/systemd/system/
sudo systemctl enable --now cpu-performance
```

**v4l2-ctl 一用就死机**
本板 `v4l2-ctl` 会触发内核 oops 并把摄像头节点锁死，**不要用它**调试摄像头。
