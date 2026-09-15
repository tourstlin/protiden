"""
ProTaiden 智能台灯控制台（磨砂玻璃版）
========================================
- UI 参考 Weather Dashboard 深色卡片风格：左侧图标侧栏 + 亮度 Hero 大卡
  + 指标卡组（温度折线 / 湿度柱状 / 亮度半圆仪表）+ 摄像头预览 + 控制卡 + 可折叠日志
- 左侧：温度 / 湿度 / 亮度实时显示，手动滑块调光（10~90%，与单片机死区一致）
- 右侧：摄像头预览 + MediaPipe 手势调光（拇指-食指距离映射亮度）
- 手势：拇指-食指捏合距离 = 调光；中指抬起 = 锁定亮度（张开手掌=暂停调光，
  不再触发退出；退出手势模式请用界面上的「手动滑块」按钮）
- 串口协议：
    下发（HEX 帧）：0xAA [亮度0~100] 0x55
    接收（文本行）：Humi:45%RH Temp:26C  /  Light:50%  /  DHT11 Read Error
- UI 框架：CustomTkinter（pip install customtkinter）

运行：D:/anaconda3/envs/yolo/python.exe desk_lamp_gui.py

【Linux / ARM 板版本（Purple Pi OH + Ubuntu）】本文件为上述脚本的跨平台适配版，改动仅限：
  1) Windows 专属 API（DWM 暗色标题栏）在 Linux 下跳过
  2) 中文字体候选加入 Noto CJK / 文泉驿（需 apt install fonts-noto-cjk）
  3) 摄像头：Linux 走 V4L2 并切 MJPG、缓冲 1 帧
  4) 串口：过滤板载调试口 /dev/ttyS*，保留 /dev/rfcomm*、/dev/ttyUSB*、/dev/ttyACM*
  5) 性能：手势推理降到 CAM_PROC_W 宽 + 隔帧推理（中间帧复用关键点）+ lite 模型
     —— 手势模型仍为「切入手势模式才加载、退出即释放」，滑块模式不占算力
  UI 布局、交互逻辑、串口协议（0xAA 亮度 0x55）与原版完全一致。

  可用环境变量微调（不改代码）：
    CAM_INDEX=0         摄像头索引
    CAM_FPS=30          采集帧率
    CAM_CAP_W=640       采集分辨率宽（板子上 640 最省算力）
    CAM_CAP_H=480       采集分辨率高
    CAM_FOURCC=         像素格式；留空=不指定（MIPI/RKISP 推荐），USB 可设 MJPG
    CAM_BUFSIZE=4       V4L2 缓冲区数；★RKISP 必须 ≥3 才跑满 15fps★（1→5fps, 2→10fps）
    CAM_PROC_W=320      手势推理图宽度（越小越快，标清手部 320 足够）
    GEST_DETECT_EVERY=3 每 N 帧推理一次（1=逐帧，最费）
    GEST_MODEL_COMPLEXITY=0  0=lite 快 / 1=full 准
  例：CAM_PROC_W=256 GEST_DETECT_EVERY=4 python3 desk_lamp_gui_linux.py

【依赖可选】cv2 / mediapipe / numpy / PIL 四件套改成 try-import：
  装了 → 手势模式正常；没装 → 自动禁用摄像头与手势（分段按钮置灰、日志提示原因），
  手动滑块调光、温湿度显示、图表、日志、串口协议全部照常工作。
  因此本文件在「系统 Python 3.8 + 只有 customtkinter/pyserial」的环境里也能直接跑。
  想强制走无摄像头模式（不加载 cv2/mediapipe，启动更快）：
      LAMP_NO_CAMERA=1 python3 desk_lamp_gui_linux.py

运行（板子）：export DISPLAY=:0 && python3 desk_lamp_gui_linux.py

【端口支持三种写法】原程序只会 serial.Serial(设备文件)，现已扩展为 _open_serial()：
  /dev/ttyUSB0              普通串口
  bt:AA:BB:CC:DD:EE:FF      蓝牙 RFCOMM 直连（要运行环境支持 AF_BLUETOOTH；
                            本板内核未编译 RFCOMM TTY，所以没有 /dev/rfcomm0）
  bt:auto                   从环境变量 LAMP_BT_MAC 取 MAC
  socket://127.0.0.1:8888   TCP 桥接（配 bt_bridge.py，可在不支持蓝牙 socket 的
                            conda Python 里间接使用蓝牙）
相关环境变量：
  LAMP_BT_MAC=AA:BB:CC:DD:EE:FF   蓝牙 MAC（板子上已配对 HC-05 即此值）
  LAMP_PORT=bt:auto               启动后自动连接该端口（无需手点「连接」）
  LAMP_BRIDGE=socket://127.0.0.1:8888   把桥接地址也列入下拉框
"""

import os
import sys
import json
import warnings
import contextlib

# 削减 MediaPipe/TFLite/protobuf 的启动日志（部分 absl 日志不受此控制，由下方 fd 重定向兜底）
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")   # TF Python 层 INFO/警告
os.environ.setdefault("GLOG_minloglevel", "3")       # glog 层输出
warnings.filterwarnings("ignore", message=".*SymbolDatabase.GetPrototype().*")

import socket
import select
import serial
import serial.tools.list_ports
import threading
import queue
import re
import time
import math
import datetime
import customtkinter as ctk
import tkinter as tk
from collections import deque

# ---- 摄像头/手势相关依赖：可选（缺失不影响程序运行，只是没有手势模式）----
# 装了 OpenCV + MediaPipe + Pillow → 手势模式可用；
# 没装（例如系统 Python 3.8 环境）→ 自动禁用摄像头与手势，只保留手动滑块调光。
CAMERA_OK = True
CAMERA_ERR = ""
cv2 = mp = np = None
Image = ImageDraw = ImageFont = ImageTk = None
if os.environ.get("LAMP_NO_CAMERA", "") == "1":
    # 显式要求无摄像头模式：即使依赖装齐也不加载，启动更快（手势按钮置灰）
    CAMERA_OK = False
    CAMERA_ERR = "已通过环境变量 LAMP_NO_CAMERA=1 强制关闭摄像头/手势"
else:
    try:
        import cv2
        import mediapipe as mp
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont, ImageTk
    except Exception as _e:             # ImportError / 二进制不兼容都归此类
        cv2 = mp = np = None
        Image = ImageDraw = ImageFont = ImageTk = None
        CAMERA_OK = False
        CAMERA_ERR = "%s: %s" % (type(_e).__name__, _e)

# ---- 音乐播放引擎（mpv IPC）：可选，缺失不影响其它功能 ----
# 依赖板上的 mpv 可执行文件（apt install mpv）+ music_player.py（纯 stdlib 实现）
MUSIC_OK = True
MUSIC_ERR = ""
MusicPlayer = None
try:
    from music_player import MusicPlayer
except Exception as _e:
    MusicPlayer = None
    MUSIC_OK = False
    MUSIC_ERR = "%s: %s" % (type(_e).__name__, _e)

# 音乐封面需要 PIL（与摄像头那套 try-import 独立：没装 cv2 也要能显示封面）
try:
    from PIL import Image as PILImage, ImageTk as PILImageTk, \
        ImageDraw as PILImageDraw
except Exception:
    PILImage = PILImageTk = PILImageDraw = None

# ---- 蓝牙 RFCOMM 直连能力检测 ----
# 本板内核未编译 CONFIG_BT_RFCOMM_TTY，所以 /dev/rfcomm0 不存在；
# 但 RFCOMM socket 层是有的，可直接连 MAC。注意：conda 版 Python 未编译
# socket.AF_BLUETOOTH（系统 /usr/bin/python3 3.8 有），因此要用桥接或系统解释器。
_HAS_BT_SOCKET = hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


@contextlib.contextmanager
def _suppress_native_stderr():
    """fd 级重定向 stderr 到 devnull，吞掉 MediaPipe/absl 的 C++ 层启动日志
    （XNNPACK delegate、inference_feedback_manager 等，环境变量无法屏蔽）。
    只应在模型初始化/预热这类已知会刷屏的瞬间短时使用。"""
    if sys.stderr is None:                          # 已无 stderr（打包/无控制台环境）
        yield
        return
    sys.stderr.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)

#------------------- 协议与调光参数 -------------------
FRAME_HEAD = 0xAA        # 调光帧头
FRAME_TAIL = 0x55        # 调光帧尾
DUTY_MIN, DUTY_MAX = 10, 90      # 亮度范围（与单片机死区一致）
DIST_MIN, DIST_MAX = 30, 250     # 手势：指尖像素距离映射范围
GEST_SEND_INTERVAL = 0.15         # 手势发送间隔下限（秒），防抖
GEST_SEND_DELTA = 1               # 手势亮度变化阈值（%），更跟手
SLIDER_SEND_INTERVAL = 0.15      # 滑块发送间隔下限（秒），节流
SLIDER_ECHO_GUARD = 0.4          # 滑块交互后 0.4s 内不接受回显同步（防"拽回"）
# --- 平台与性能参数（可用环境变量覆盖，方便在 Purple Pi OH 等 ARM 板子上调优）---
IS_LINUX = sys.platform.startswith("linux")
CAM_INDEX = int(os.environ.get("CAM_INDEX", "0"))      # 摄像头索引（多摄像头时改这里）
CAM_FPS = int(os.environ.get("CAM_FPS", "30"))         # 摄像头工作线程采集帧率
# 采集分辨率：RKISP(MIPI) 与 USB 摄像头都适用；640x480 在板子上最省算力
CAM_CAP_W = int(os.environ.get("CAM_CAP_W", "640"))
CAM_CAP_H = int(os.environ.get("CAM_CAP_H", "480"))
# 采集像素格式（fourcc）：留空=不指定（RKISP/MIPI 走 NV12 默认，推荐）
# USB 摄像头可设 CAM_FOURCC=MJPG 以获得更高帧率、更低 CPU
CAM_FOURCC = os.environ.get("CAM_FOURCC", "").strip()
# V4L2 缓冲区数量：★实测 RKISP 驱动必须 ≥3 才能跑满帧率★
#   1 → 5fps   2 → 10fps   3 及以上 → 15fps（sensor 全速）
# 设太小会把帧率按倍压掉，这是本板"卡顿"的主因之一
CAM_BUFSIZE = int(os.environ.get("CAM_BUFSIZE", "4"))
# 手势推理分辨率宽：关键点是归一化坐标，缩小推理图只省算力、不丢定位精度
# （RK3566 等 A55 板子默认 320，x86 桌面默认 640）
CAM_PROC_W = int(os.environ.get("CAM_PROC_W", "320" if IS_LINUX else "640"))
# 每 N 帧做一次手势推理，中间帧复用上次关键点（板子设 3 可省约 2/3 推理开销）
GEST_DETECT_EVERY = int(os.environ.get("GEST_DETECT_EVERY", "3" if IS_LINUX else "1"))
# 手势模型复杂度：0=lite（快） 1=full（准），板子默认 lite
GEST_MODEL_COMPLEXITY = int(os.environ.get("GEST_MODEL_COMPLEXITY", "0" if IS_LINUX else "1"))
MAX_LOG_LINES = 300              # 日志面板最大行数（超限自动裁剪，防变慢）
HISTORY_LEN = 24                 # 温度/湿度走势图保存点数

#------------------- 浅色磨砂玻璃主题配色 -------------------
# 背景是用户给的台灯照片，卡片颜色由 _apply_glass() 按背景图对应区域
# 的平均色 + 高比例白混色动态生成（Tk 没有真透明，这是玻璃质感的近似实现）。
# 下面的 CARD/CARD_IN 只是「取不到背景图时的兜底值」。
BG      = "#dfe9e4"      # 窗口兜底底色（正常被背景图盖住）
CARD    = "#f1f6f3"      # 卡片背景（磨砂白兜底）
CARD_IN = "#e3ede7"      # 卡片内嵌区域（日志框/摄像头区，比卡片再实一档）
TRACK   = "#d7e2dc"      # 滑槽/仪表轨道
BORDER  = "#ffffff"      # 玻璃高光边
ACCENT  = "#3d8bfd"      # 主题蓝
ACCENT_D = "#2f7bea"     # 主题蓝（hover 加深）
AMBER   = "#e8a020"      # 琥珀（灯泡/高亮）
GREEN   = "#0f9d6e"      # 在线绿
RED     = "#e5484d"      # 离线红
FG      = "#17222b"      # 主文字（深）
FG_DIM  = "#5f707d"      # 次要文字
HOVERBG = "#e2ede7"      # 悬停底色

GLASS_K    = 0.72        # 磨砂混白比例（卡片）
GLASS_K_IN = 0.58        # 内嵌区域混白比例（更深一档，做出层次）

CN_FONT  = "Noto Sans CJK SC" if IS_LINUX else "Microsoft YaHei UI"   # 中文字体（Linux 用 Noto CJK）

#------------------- 手部关键点编号（MediaPipe 21 点） -------------------
THUMB_TIP = 4            # 拇指尖
INDEX_TIP = 8            # 食指尖
INDEX_PIP = 6            # 食指近指关节
MIDDLE_TIP = 12          # 中指尖
MIDDLE_PIP = 10          # 中指近指关节
RING_TIP = 16            # 无名指尖
RING_PIP = 14            # 无名指近指关节
PINKY_TIP = 20           # 小指尖
PINKY_PIP = 18           # 小指近指关节
LOCK_MARGIN = 0.03       # 中指抬起判定余量（归一化坐标，防抖）

MODE_LABELS = {"manual": "手动滑块", "gesture": "手势调节"}


#=================== 中文文本绘制（cv2.putText 不支持非 ASCII） ===================

CN_FONT_CANDIDATES = ("C:/Windows/Fonts/msyh.ttc",     # 微软雅黑（Windows）
                      "C:/Windows/Fonts/simhei.ttf",   # 黑体（Windows 备用）
                      # --- 以下为 Linux（Purple Pi OH / Ubuntu）中文字体，需 apt install fonts-noto-cjk ---
                      "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                      "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                      "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",   # 文泉驿正黑
                      "/usr/share/fonts/truetype/arphic/uming.ttc")     # AR PL UMing
_font_cache = {}


def _get_cn_font(size):
    """取指定字号的中文字体对象（缓存，避免每帧重复加载字体文件）"""
    if size not in _font_cache:
        font = None
        for path in CN_FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        _font_cache[size] = font
    return _font_cache[size]


def _draw_cn_text(frame, text, xy, bgr, size=20):
    """在 BGR 帧上绘制中文（原地写回 frame）；字体不可用时退回 cv2.putText"""
    font = _get_cn_font(size)
    if font is None:
        cv2.putText(frame, text, xy, cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    bgr, 1, cv2.LINE_AA)
        return
    pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    ImageDraw.Draw(pil).text(xy, text, font=font, fill=(bgr[2], bgr[1], bgr[0]))
    frame[:] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


#=================== 摄像头打开（平台差异集中在此） ===================

def _open_camera():
    """打开摄像头（Linux 走 V4L2）
    - 采集分辨率用 CAM_CAP_W/CAM_CAP_H（板子上 640x480 最省算力）
    - CAM_FOURCC 留空则不指定像素格式：RKISP(MIPI) 摄像头走 NV12 默认即可；
      实测给 RKISP 设 MJPG 会失败。USB UVC 摄像头可设 CAM_FOURCC=MJPG
      （很多 USB 摄像头默认 YUYV，未压缩，USB 带宽与解码 CPU 都大）
    - BUFFERSIZE 用 CAM_BUFSIZE（默认 4）：★RKISP 驱动必须 ≥3 个缓冲区才跑满帧率★，
      设 1 会掉到 5fps、设 2 掉到 10fps
    - 以上设置失败不影响功能，静默忽略"""
    if IS_LINUX:
        cap = cv2.VideoCapture(CAM_INDEX, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(CAM_INDEX)
    if cap.isOpened() and IS_LINUX:
        try:
            if CAM_FOURCC:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAM_FOURCC[:4]))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_CAP_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_CAP_H)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, CAM_BUFSIZE)
        except Exception:
            pass
    return cap


#=================== 端口工厂：串口 / 蓝牙 RFCOMM / TCP 桥接 ===================

class RfcommSerial:
    """用内核 RFCOMM socket 伪装成 pyserial.Serial 的最小实现。

    背景：本板内核没编译 RFCOMM TTY（无 /dev/rfcomm0），只能直接用 socket 连蓝牙；
    只实现主程序用到的接口：in_waiting / read / write / close / is_open。
    要求运行环境支持 socket.AF_BLUETOOTH（系统 python3.8 有，conda 版没有）。"""

    def __init__(self, mac, channel=1, timeout=1.0):
        if not _HAS_BT_SOCKET:
            raise OSError("当前 Python 不支持蓝牙 socket（AF_BLUETOOTH 未编译）")
        self.mac = mac
        self.channel = int(channel)
        self.timeout = float(timeout)
        self._closed = False
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                   socket.BTPROTO_RFCOMM)
        try:
            self._sock.settimeout(self.timeout)
            self._sock.connect((mac, self.channel))
            self._sock.settimeout(self.timeout)
        except Exception:
            try:
                self._sock.close()
            except Exception:
                pass
            self._closed = True
            raise

    @property
    def in_waiting(self):
        """有无可读数据（0/1）。主循环只看是否为 0，够用。"""
        if self._closed:
            return 0
        try:
            r, _, _ = select.select([self._sock], [], [], 0)
        except Exception:
            return 0
        return 1 if r else 0

    def read(self, n=1):
        if self._closed:
            raise OSError("蓝牙连接已关闭")
        try:
            return self._sock.recv(n)
        except socket.timeout:
            return b""
        except OSError:
            raise

    def write(self, data):
        if self._closed:
            raise OSError("蓝牙连接已关闭")
        self._sock.sendall(bytes(data))
        return len(data)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._sock.close()
            except Exception:
                pass

    @property
    def is_open(self):
        return not self._closed


def _open_serial(port, baudrate=9600, timeout=1, write_timeout=1):
    """统一打开端口，支持三种写法：
      /dev/ttyUSB0            普通串口（pyserial）
      socket://127.0.0.1:8888 TCP 桥接（pyserial 内置 URL 支持；桥接进程负责转发到蓝牙）
      bt:AA:BB:CC:DD:EE:FF    蓝牙 RFCOMM 直连（可选 #通道号，默认 1）
    蓝牙 MAC 也可以从环境变量 LAMP_BT_MAC 取，直接写 bt:auto 即可。"""
    p = (port or "").strip()
    if p.startswith("bt:"):
        mac = p[3:].strip()
        if mac in ("", "auto"):
            mac = os.environ.get("LAMP_BT_MAC", "").strip()
        if not mac:
            raise OSError("未指定蓝牙 MAC（端口写 bt:MAC，或设置环境变量 LAMP_BT_MAC）")
        channel = 1
        if "#" in mac:                      # bt:MAC#2 → 指定 RFCOMM 通道
            mac, _, ch = mac.partition("#")
            channel = int(ch)
        # 蓝牙寻呼+认证常需数秒（HC-05 实测 3~6 秒），而串口的 1 秒超时在这里必然被误判为
        # 连接失败 → 于是程序反复重连却从未成功。蓝牙连接超时下限固定给 10 秒。
        return RfcommSerial(mac, channel, max(float(timeout), 10.0))
    if "://" in p:
        return serial.serial_for_url(p, timeout=timeout, write_timeout=write_timeout)
    return serial.Serial(p, baudrate, timeout=timeout, write_timeout=write_timeout)


#=================== Canvas 绘图助手 ===================

def round_rect(cv, x0, y0, x1, y1, r, **kw):
    """Canvas 圆角矩形（smooth 多边形近似，用于小尺寸图标底板）"""
    pts = [x0+r, y0, x1-r, y0, x1, y0, x1, y0+r, x1, y1-r, x1, y1,
           x1-r, y1, x0+r, y1, x0, y1, x0, y1-r, x0, y0+r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def icon_bulb(cv, cx, cy, s, color, filled=False, rays=False):
    """灯泡图标：玻璃泡 + 灯座两横线；filled=琥珀实心，rays=顶部光线"""
    cv.create_oval(cx-0.30*s, cy-0.44*s, cx+0.30*s, cy+0.16*s,
                   fill=color if filled else "", outline=color,
                   width=3 if s > 30 else 2)
    for dy in (0.28, 0.40):
        cv.create_line(cx-0.13*s, cy+dy*s, cx+0.13*s, cy+dy*s,
                       fill=color, width=3 if s > 30 else 2,
                       capstyle="round")
    if rays:
        # 光线绕灯泡顶部扇形分布（spread 为相对正上方的偏角）
        for spread in (-60, -30, 0, 30, 60):
            a = math.radians(spread)
            r1, r2 = 0.52 * s, 0.60 * s
            cv.create_line(cx + r1 * math.sin(a), cy - 0.14 * s - r1 * math.cos(a),
                           cx + r2 * math.sin(a), cy - 0.14 * s - r2 * math.cos(a),
                           fill=color, width=2, capstyle="round")


def icon_temp(cv, cx, cy, s, color):
    """温度计图标：竖杆 + 底泡 + 右侧两刻度"""
    cv.create_line(cx, cy-0.40*s, cx, cy+0.16*s, fill=color,
                   width=max(3, int(s*0.22)), capstyle="round")
    cv.create_oval(cx-0.17*s, cy+0.14*s, cx+0.17*s, cy+0.48*s,
                   fill=color, outline=color)
    for dy in (-0.18, -0.02):
        cv.create_line(cx+0.20*s, cy+dy*s, cx+0.32*s, cy+dy*s,
                       fill=color, width=2, capstyle="round")


def icon_drop(cv, cx, cy, s, color):
    """水滴图标（湿度）"""
    pts = [cx, cy-0.44*s, cx+0.30*s, cy-0.02*s, cx+0.22*s, cy+0.28*s,
           cx, cy+0.44*s, cx-0.22*s, cy+0.28*s, cx-0.30*s, cy-0.02*s]
    cv.create_polygon(pts, smooth=True, fill="", outline=color, width=2)


def icon_gauge(cv, cx, cy, s, color):
    """半圆仪表图标（亮度卡标题）：上缘弧 + 指针"""
    cv.create_arc(cx-0.42*s, cy-0.42*s, cx+0.42*s, cy+0.42*s,
                  start=180, extent=-180, style="arc", outline=color, width=2)
    cv.create_line(cx, cy, cx+0.26*s, cy-0.24*s, fill=color, width=2,
                   capstyle="round")
    cv.create_oval(cx-2, cy-2, cx+2, cy+2, fill=color, outline=color)


def icon_hand(cv, cx, cy, s, color):
    """手势图标：掌 + 四指 + 拇指"""
    round_rect(cv, cx-0.22*s, cy+0.00*s, cx+0.22*s, cy+0.46*s, 0.10*s,
               fill="", outline=color, width=2)
    fingers = ((-0.15, 0.34), (-0.05, 0.44), (0.05, 0.40), (0.15, 0.30))
    for dx, hh in fingers:
        cv.create_line(cx+dx*s, cy-0.02*s, cx+dx*s, cy-hh*s,
                       fill=color, width=2, capstyle="round")
    cv.create_line(cx-0.22*s, cy+0.16*s, cx-0.40*s, cy+0.04*s,
                   fill=color, width=2, capstyle="round")


def icon_sliders(cv, cx, cy, s, color):
    """调音台滑块图标（手动模式）"""
    cv.create_line(cx-0.40*s, cy-0.14*s, cx+0.40*s, cy-0.14*s,
                   fill=color, width=2, capstyle="round")
    cv.create_oval(cx+0.03*s, cy-0.23*s, cx+0.21*s, cy-0.05*s,
                   fill=color, outline=color)
    cv.create_line(cx-0.40*s, cy+0.20*s, cx+0.40*s, cy+0.20*s,
                   fill=color, width=2, capstyle="round")
    cv.create_oval(cx-0.21*s, cy+0.11*s, cx-0.03*s, cy+0.29*s,
                   fill=color, outline=color)


def icon_camera(cv, cx, cy, s, color):
    """摄像头图标"""
    round_rect(cv, cx-0.42*s, cy-0.26*s, cx+0.42*s, cy+0.30*s, 0.10*s,
               fill="", outline=color, width=2)
    cv.create_oval(cx-0.13*s, cy-0.11*s, cx+0.13*s, cy+0.15*s,
                   fill="", outline=color, width=2)
    cv.create_line(cx-0.14*s, cy-0.26*s, cx-0.14*s, cy-0.40*s,
                   cx+0.00*s, cy-0.40*s, cx+0.00*s, cy-0.26*s,
                   fill=color, width=2, capstyle="round")


def icon_lines(cv, cx, cy, s, color):
    """日志图标：三横线"""
    for i, w in enumerate((0.70, 0.50, 0.30)):
        cv.create_line(cx-0.35*s, cy-0.25*s+i*0.25*s,
                       cx-0.35*s+w*s, cy-0.25*s+i*0.25*s,
                       fill=color, width=2, capstyle="round")


#=================== 音乐播放图标 ===================

def icon_music(cv, cx, cy, s, color):
    """八分音符（音乐卡标题）"""
    cv.create_oval(cx-0.36*s, cy+0.12*s, cx-0.02*s, cy+0.46*s,
                   fill=color, outline=color)
    w = max(2, int(s*0.13))
    cv.create_line(cx-0.04*s, cy+0.30*s, cx-0.04*s, cy-0.42*s,
                   fill=color, width=w, capstyle="round")
    cv.create_line(cx-0.04*s, cy-0.42*s, cx+0.32*s, cy-0.26*s,
                   fill=color, width=w, capstyle="round")
    cv.create_oval(cx+0.08*s, cy+0.26*s, cx+0.42*s, cy+0.60*s,
                   fill=color, outline=color)
    cv.create_line(cx+0.32*s, cy+0.44*s, cx+0.32*s, cy-0.26*s,
                   fill=color, width=w, capstyle="round")


def icon_play(cv, cx, cy, s, color):
    """实心三角（播放）"""
    cv.create_polygon(cx-0.26*s, cy-0.38*s, cx+0.36*s, cy,
                      cx-0.26*s, cy+0.38*s, fill=color, outline=color)


def icon_pause(cv, cx, cy, s, color):
    """双竖条（暂停）"""
    for dx in (-0.26, 0.04):
        round_rect(cv, cx+dx*s, cy-0.36*s, cx+(dx+0.22)*s, cy+0.36*s,
                   0.07*s, fill=color, outline=color)


def icon_prev(cv, cx, cy, s, color):
    """上一曲：左三角 + 左竖条"""
    cv.create_polygon(cx+0.32*s, cy-0.34*s, cx-0.14*s, cy,
                      cx+0.32*s, cy+0.34*s, fill=color, outline=color)
    cv.create_line(cx-0.30*s, cy-0.34*s, cx-0.30*s, cy+0.34*s,
                   fill=color, width=max(2, int(s*0.13)), capstyle="round")


def icon_next(cv, cx, cy, s, color):
    """下一曲：右三角 + 右竖条"""
    cv.create_polygon(cx-0.32*s, cy-0.34*s, cx+0.14*s, cy,
                      cx-0.32*s, cy+0.34*s, fill=color, outline=color)
    cv.create_line(cx+0.30*s, cy-0.34*s, cx+0.30*s, cy+0.34*s,
                   fill=color, width=max(2, int(s*0.13)), capstyle="round")


def icon_volume(cv, cx, cy, s, color):
    """音量：喇叭 + 两道声波"""
    cv.create_polygon(cx-0.38*s, cy-0.12*s, cx-0.16*s, cy-0.12*s,
                      cx+0.06*s, cy-0.40*s, cx+0.06*s, cy+0.40*s,
                      cx-0.16*s, cy+0.12*s, cx-0.38*s, cy+0.12*s,
                      fill=color, outline=color)
    for r in (0.22, 0.38):
        cv.create_arc(cx-0.10*s-r*s, cy-r*s, cx-0.10*s+r*s, cy+r*s,
                      start=-55, extent=110, style="arc",
                      outline=color, width=2)


def icon_calendar(cv, cx, cy, s, color):
    """日历图标：圆角框 + 顶栏 + 两个挂环 + 四个日期点"""
    round_rect(cv, cx-0.40*s, cy-0.30*s, cx+0.40*s, cy+0.40*s, 0.09*s,
               fill="", outline=color, width=2)
    cv.create_line(cx-0.40*s, cy-0.06*s, cx+0.40*s, cy-0.06*s,
                   fill=color, width=2)
    for dx in (-0.19, 0.19):
        cv.create_line(cx+dx*s, cy-0.42*s, cx+dx*s, cy-0.22*s,
                       fill=color, width=2, capstyle="round")
    for gx in (-0.21, 0.03):
        for gy in (0.06, 0.22):
            cv.create_rectangle(cx+gx*s, cy+gy*s,
                                cx+(gx+0.13)*s, cy+(gy+0.11)*s,
                                fill=color, outline="")


#=================== 本周课表数据 ===================
# 默认课表（可被 assets/schedule.json 覆盖，见 _load_schedule）
# 每项：(星期 0=周一…4=周五, 起始节, 结束节, 课程, 地点)
DEFAULT_SCHEDULE = [
    (0, 1, 2, "大学英语（三级）", "河西A3-403"),
    (0, 3, 4, "单片机技术", "机房十二"),
    (0, 5, 6, "篮球", "西区篮球场"),
    (1, 1, 2, "Linux操作系统", "机房二"),
    (1, 3, 4, "Linux操作系统", "机房二"),
    (1, 5, 6, "线性代数", "河西B2-403"),
    (1, 7, 8, "大学物理（B）", "河西A3-503"),
    (2, 9, 10, "数字电子技术", "河西A1-202"),
    (3, 7, 8, "大学英语（三级）", "河西A3-301"),
    (4, 1, 2, "单片机技术", "机房十二"),
    (4, 3, 4, "单片机技术", "河西D2-304"),
    (4, 7, 8, "复变函数", "河西A3-503"),
    (4, 9, 10, "数字电子技术", "河西A1-202"),
]

WEEK_DAYS = ("周一", "周二", "周三", "周四", "周五")

# 课程色卡：(色块底, 主文字, 次文字) —— 浅色卡片上的低饱和配色
COURSE_COLORS = (
    ("#dbeafc", "#2f6bbf", "#8fa9cc"),   # 蓝
    ("#eae2fb", "#6a4dc0", "#a294cd"),   # 紫
    ("#d9f2e6", "#178a58", "#79b394"),   # 绿
    ("#fdf0d2", "#a8780a", "#c2a45c"),   # 琥珀
    ("#d8f0f2", "#188f9e", "#7ab3ba"),   # 青
)


def _frost_rgb(r, g, b, k=GLASS_K):
    """把背景色与白色按比例 k 混合 → 磨砂玻璃色"""
    f = lambda c: int(max(0, min(255, c + (255 - c) * k)))
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


def _load_schedule():
    """优先读 assets/schedule.json（用户可自行改），读不到就用内置默认表"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "schedule.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for it in data:
            out.append((int(it["day"]), int(it["start"]), int(it["end"]),
                        str(it["course"]), str(it.get("room", ""))))
        if out:
            return out
    except Exception as e:
        if os.path.exists(path):
            print("[课表] schedule.json 解析失败，改用内置表: %r" % (e,))
    return list(DEFAULT_SCHEDULE)


SCHEDULE = _load_schedule()
# 把出现过的 (起,止) 节次组合抽成表头行（本校都是 2 节一块，自动适配）
SCHEDULE_BLOCKS = sorted({(p0, p1) for _, p0, p1, _, _ in SCHEDULE})


def _course_style(name):
    """同一门课永远同一个颜色（名字哈希，稳定）"""
    h = 0
    for ch in name:
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    return COURSE_COLORS[h % len(COURSE_COLORS)]


def _text_w(s, size):
    """粗估一段中英混排文字在 size(磅) 下的像素宽度（Tk 拿不到真实度量时够用）"""
    n = 0.0
    for ch in s:
        n += size * 1.35 if ord(ch) > 0x2E80 else size * 0.62
    return n


#=================== 界面组件 ===================

class SideIconButton(tk.Canvas):
    """侧栏图标按钮：圆角底板 + 矢量图标，支持悬停/选中态"""

    def __init__(self, master, icon_fn, command):
        super().__init__(master, width=44, height=44, bg=CARD,
                         highlightthickness=0, bd=0)
        self.icon_fn = icon_fn
        self.command = command
        self.active = False
        self._hover = False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._render(True))
        self.bind("<Leave>", lambda e: self._render(False))
        self._render(False)

    def set_active(self, active):
        self.active = active
        self._render(self._hover)

    def _render(self, hover):
        self._hover = hover
        self.delete("all")
        if self.active:
            round_rect(self, 3, 3, 41, 41, 12, fill=ACCENT, outline="")
            color = "#ffffff"
        elif hover:
            round_rect(self, 3, 3, 41, 41, 12, fill=HOVERBG, outline="")
            color = FG
        else:
            color = FG_DIM
        self.icon_fn(self, 22, 22, 22, color)


class MetricCard(ctk.CTkFrame):
    """指标卡：图标+标题 / 图表区（折线、柱状或半圆仪表）/ 大数值 / 注释"""

    def __init__(self, master, title, icon_fn, accent, unit,
                 chart="line", chart_range=(0, 100), note=""):
        super().__init__(master, corner_radius=16, fg_color=CARD,
                         border_width=1, border_color=BORDER)
        self.accent = accent
        self.unit = unit
        self.kind = chart
        self.lo, self.hi = chart_range
        self.history = deque(maxlen=HISTORY_LEN)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 2))
        ic = tk.Canvas(head, width=20, height=20, bg=CARD, highlightthickness=0)
        ic.pack(side="left")
        icon_fn(ic, 10, 10, 16, accent)
        ctk.CTkLabel(head, text=title, font=("Microsoft YaHei UI", 12),
                     text_color=FG_DIM).pack(side="left", padx=(8, 0))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(anchor="w", padx=16)
        self.val = ctk.CTkLabel(row, text="--", font=("Segoe UI", 26, "bold"),
                                text_color=FG)
        self.val.pack(side="left")
        ctk.CTkLabel(row, text=" " + unit, font=("Microsoft YaHei UI", 11),
                     text_color=FG_DIM).pack(side="left", pady=(10, 0))

        self.chart = tk.Canvas(self, height=40, bg=CARD, highlightthickness=0)
        self.chart.pack(fill="x", padx=14, pady=(4, 0))
        self.chart.bind("<Configure>", lambda e: self._redraw())

        ctk.CTkLabel(self, text=note, font=("Microsoft YaHei UI", 10),
                     text_color=FG_DIM).pack(anchor="w", padx=16, pady=(2, 8))

    def set_value(self, v):
        self.val.configure(text=str(v))
        self.history.append(v)
        self._redraw()

    # ---------- 图表绘制 ----------
    def _redraw(self):
        c = self.chart
        c.delete("all")
        w, h = c.winfo_width(), 64
        if w < 30:
            return
        data = list(self.history)

        if self.kind == "gauge":
            # 半圆仪表：上缘轨道 + 进度弧（从左端 180° 负方向扫 = 经顶部向右）
            # 无数据也画轨道（否则启动时表盘空白）
            r = min(w/2 - 8, h - 10)
            cx, top = w/2, 6
            c.create_arc(cx-r, top, cx+r, top+2*r, start=180, extent=-180,
                         style="arc", outline=TRACK, width=8)
            if not data:
                return
            frac = (data[-1] - self.lo) / (self.hi - self.lo)
            frac = min(1.0, max(0.0, frac))
            c.create_arc(cx-r, top, cx+r, top+2*r, start=180,
                         extent=-180*frac, style="arc", outline=self.accent,
                         width=8)
            return

        if not data:
            return

        pad = 6
        if self.kind == "line":
            n = len(data)
            if n == 1:
                c.create_oval(pad-3, h/2-3, pad+3, h/2+3,
                              fill=self.accent, outline="")
            else:
                span = (w - 2*pad) / (n - 1)
                pts = []
                for i, v in enumerate(data):
                    x = pad + i*span
                    y = (h-10) - (v-self.lo)/(self.hi-self.lo)*(h-20)
                    pts += [x, y]
                c.create_line(pts, fill=self.accent, width=2, smooth=True)
                c.create_oval(pts[-2]-3, pts[-1]-3, pts[-2]+3, pts[-1]+3,
                              fill=self.accent, outline="")
            # 底部点状基线（装饰，参考图同款）
            for x in range(6, int(w)-4, 8):
                c.create_line(x, h-3, x+3, h-3, fill="#c3d2ca")
        else:  # bar
            slot = (w - 2*pad) / HISTORY_LEN
            bw = 5
            for i, v in enumerate(data):
                x = pad + i*slot + (slot-bw)/2
                bh = (v-self.lo)/(self.hi-self.lo)*(h-16)
                color = "#7fb0f5" if i == len(data)-1 else self.accent
                c.create_rectangle(x, h-8-bh, x+bw, h-8, fill=color, width=0)


#=================== 穿衣建议 ===================

def _clothing_advice(t, h):
    """温度分档 + 湿度修正 → (主建议, 补充提示)。DHT-11 量程 0~50℃"""
    if t >= 30:
        advice = "短袖短裤，注意防晒补水"
    elif t >= 26:
        advice = "短袖或薄衬衫即可"
    elif t >= 21:
        advice = "T恤，备一件薄外套"
    elif t >= 16:
        advice = "长袖衬衫或卫衣"
    elif t >= 10:
        advice = "夹克或风衣，配长裤"
    elif t >= 5:
        advice = "毛衣加厚外套"
    else:
        advice = "羽绒服或棉衣，注意保暖"

    note = ""
    if h >= 80 and t >= 28:
        note = "高温高湿较闷热，选速干透气面料"
    elif h >= 80 and t <= 10:
        note = "湿冷刺骨，加一层防风防水外套"
    elif h < 30:
        note = "空气干燥，多喝水注意保湿"
    return advice, note


def icon_shirt(cv, cx, cy, s, color):
    """T 恤线稿图标（穿衣建议卡标题）"""
    k = s / 2.0
    pts = [cx - 0.62 * k, cy - 0.50 * k, cx - 0.24 * k, cy - 0.76 * k,
           cx + 0.24 * k, cy - 0.76 * k, cx + 0.62 * k, cy - 0.50 * k,
           cx + 0.44 * k, cy - 0.10 * k, cx + 0.22 * k, cy - 0.26 * k,
           cx + 0.22 * k, cy + 0.74 * k, cx - 0.22 * k, cy + 0.74 * k,
           cx - 0.22 * k, cy - 0.26 * k, cx - 0.44 * k, cy - 0.10 * k]
    cv.create_polygon(pts, fill="", outline=color, width=2,
                      joinstyle="round", smooth=True)


class ClothingCard(ctk.CTkFrame):
    """穿衣建议卡：温度分档 + 湿度修正，随 DHT-11 上报刷新
    卡片被拉伸时内容保持垂直居中（大图标 + 建议），避免下方留白"""

    def __init__(self, master):
        super().__init__(master, corner_radius=16, fg_color=CARD,
                         border_width=1, border_color=BORDER)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(10, 0))
        ic = tk.Canvas(head, width=20, height=20, bg=CARD,
                       highlightthickness=0)
        ic.pack(side="left")
        icon_shirt(ic, 10, 10, 16, AMBER)
        ctk.CTkLabel(head, text="穿衣建议", font=(CN_FONT, 12),
                     text_color=FG_DIM).pack(side="left", padx=(8, 0))

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(expand=True, fill="both")
        big = tk.Canvas(mid, width=44, height=38, bg=CARD,
                        highlightthickness=0)
        big.pack(pady=(4, 0))
        icon_shirt(big, 22, 20, 32, AMBER)
        self.lbl_main = ctk.CTkLabel(mid, text="等待温湿度数据…",
                                     font=(CN_FONT, 14), text_color=FG,
                                     wraplength=180, justify="center")
        self.lbl_main.pack(padx=14)
        self.lbl_note = ctk.CTkLabel(mid, text="",
                                     font=(CN_FONT, 10), text_color=FG_DIM,
                                     wraplength=180, justify="center")
        self.lbl_note.pack(padx=14, pady=(4, 0))

        self.lbl_basis = ctk.CTkLabel(self, text="依据：-- ℃ · -- %RH",
                                      font=(CN_FONT, 10), text_color=FG_DIM)
        self.lbl_basis.pack(side="bottom", anchor="w", padx=16, pady=(0, 10))
        self._last = None

    def update_weather(self, t, h):
        """刷新建议（内容不变不重设，防 2s 上报触发重绘闪烁）"""
        advice, note = _clothing_advice(t, h)
        if (advice, note) == self._last:
            return
        self._last = (advice, note)
        self.lbl_main.configure(text=advice)
        self.lbl_note.configure(text=note)
        self.lbl_basis.configure(text=f"依据：{t} ℃ · {h} %RH")


#=================== 本周课表卡 ===================

class ScheduleCard(ctk.CTkFrame):
    """周课表卡：星期 × 节次块的紧凑网格（Canvas 绘制，随卡片尺寸自适应）

    · 行 = 课表里出现过的节次组合（本校都是 1-2 / 3-4 / 5-6 / 7-8 / 9-10 两节一块）
    · 今天那一列整列加底色 + 表头高亮
    · 同一门课颜色固定（名字哈希），一眼能认出哪几节是同一门
    """

    def __init__(self, master):
        super().__init__(master, corner_radius=16, fg_color=CARD,
                         border_width=1, border_color=BORDER)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 4))
        ic = tk.Canvas(head, width=20, height=20, bg=CARD,
                       highlightthickness=0)
        ic.pack(side="left")
        icon_calendar(ic, 10, 10, 16, ACCENT)
        ctk.CTkLabel(head, text="本周课表", font=(CN_FONT, 12),
                     text_color=FG_DIM).pack(side="left", padx=(8, 0))

        wd = datetime.date.today().weekday()
        self.today = wd if wd < 5 else -1          # 周末就不高亮任何一列
        self.lbl_today = ctk.CTkLabel(
            head, text="今天 · %s" % WEEK_DAYS[self.today] if self.today >= 0
            else "今天没课", font=(CN_FONT, 11), text_color=ACCENT)
        self.lbl_today.pack(side="right")

        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0, height=140)
        self.canvas.pack(fill="both", expand=True, padx=14, pady=(2, 12))
        self.canvas.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        """Canvas 绘制入口：包一层异常保护——画一半崩掉会整张卡变空白，
        宁可只丢一格也要把异常打到日志里（这个坑真踩过）"""
        try:
            self._redraw()
        except Exception as e:
            print("[课表] 绘制异常: %r" % (e,))

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 160 or h < 90 or not SCHEDULE_BLOCKS:
            return

        nr = len(SCHEDULE_BLOCKS)
        gutter = 42                                # 左侧节次栏
        gap = 3
        top = 20                                   # 星期表头高度
        cw = (w - gutter - gap * 5) / 5.0
        rh = (h - top - gap * (nr - 1)) / float(nr)
        fs = 10 if cw >= 118 else 9

        # ① 今天那一列先铺底（压在网格线下面）
        if self.today >= 0:
            x = gutter + gap + self.today * (cw + gap)
            c.create_rectangle(x, top, x + cw, h - 1,
                               fill="#ffffff", outline="")

        # ② 星期表头
        for d, name in enumerate(WEEK_DAYS):
            x = gutter + gap + d * (cw + gap) + cw / 2.0
            cur = (d == self.today)
            c.create_text(x, top / 2.0 + 1, text=name,
                          fill=ACCENT if cur else FG_DIM,
                          font=(CN_FONT, 11, "bold") if cur else (CN_FONT, 11))

        # ③ 节次栏
        c.create_text(gutter / 2.0 - 2, top / 2.0 + 1, text="节",
                      fill="#93a3ad", font=(CN_FONT, 9))
        for i, (p0, p1) in enumerate(SCHEDULE_BLOCKS):
            y = top + i * (rh + gap)
            c.create_text(gutter / 2.0 - 4, y + rh / 2.0,
                          text="%d-%d" % (p0, p1), fill="#93a3ad",
                          font=("Segoe UI", 8))

        # ④ 课程块
        row_of = {blk: i for i, blk in enumerate(SCHEDULE_BLOCKS)}
        used = set()
        for d, p0, p1, course, room in SCHEDULE:
            key = (d, p0, p1)
            if key in used or d > 4:
                continue
            used.add(key)
            i = row_of.get((p0, p1))
            if i is None:
                continue
            x = gutter + gap + d * (cw + gap)
            y = top + i * (rh + gap)
            bg, fg, dim = _course_style(course)
            round_rect(c, x, y, x + cw, y + rh, 6, fill=bg, outline="")
            c.create_line(x + 2.5, y + 6, x + 2.5, y + rh - 6,
                          fill=fg, width=2, capstyle="round")
            if rh >= 34:                       # 行高够 → 课程 / 地点 分两行
                c.create_text(x + cw / 2.0 + 2, y + rh * 0.35, text=course,
                              fill=fg, font=(CN_FONT, fs, "bold"), width=cw - 14)
                if room:
                    c.create_text(x + cw / 2.0 + 2, y + rh * 0.74, text=room,
                                  fill=dim, font=(CN_FONT, fs - 1), width=cw - 14)
            else:                              # 行高紧张 → 挤成一行，放不下就缩字/去前缀
                txt = ("%s · %s" % (course, room)) if room else course
                size = fs
                if _text_w(txt, size) > cw - 16 and room:
                    txt = "%s · %s" % (course, re.sub(
                        r"^(河西|河东|西区|东区|南区|北区)", "", room))
                if _text_w(txt, size) > cw - 16:
                    size = max(8, fs - 1)
                c.create_text(x + cw / 2.0 + 2, y + rh / 2.0, text=txt,
                              fill=fg, font=(CN_FONT, size, "bold"),
                              width=cw - 12)


#=================== 主应用 ===================

class DeskLampApp:
    """台灯上位机主应用"""

    def __init__(self, root):
        self.root = root
        self.root.title("ProTaiden · 智能台灯控制台")
        self.root.configure(fg_color=BG)
        # 铺满屏幕（板子屏 1920×1080）：底部留出任务栏高度，右侧不留空
        try:
            _sw = int(self.root.winfo_screenwidth())
            _sh = int(self.root.winfo_screenheight())
        except Exception:
            _sw, _sh = 1320, 1000
        self.root.geometry("%dx%d+0+0" % (max(1120, _sw), max(560, _sh - 42)))
        self.root.minsize(1120, 560)

        # ---------------- 触屏一体机（kiosk）模式 ----------------
        # 全屏盖住任务栏与标题栏，触屏无指针，左下角「退出」是唯一出口
        # 可用 LAMP_KIOSK=0 关闭（调试时保留标题栏）
        self._kiosk = IS_LINUX and os.environ.get("LAMP_KIOSK", "1") == "1"

        # ---------------- 运行状态 ----------------
        self.ser = None                  # 串口对象
        self._reconn_pending = False     # 自动重连是否已挂起（防重复排队）
        self._user_disconnected = False  # 用户主动断开过 → 不再自动重连
        self.rx_queue = queue.Queue()    # 串口接收线程 → 主线程 消息队列
        self.connecting = False          # 正在连接中标志（防止重复点击）
        self.attempt = 0                 # 连接尝试编号（丢弃过期结果）
        self.mode = "manual"             # 调光模式：manual / gesture
        self.last_sent = -1              # 上次已发送的亮度
        self.last_send_time = 0          # 上次发送时刻（节流用）
        self.slider_touch = 0.0          # 滑块最后一次交互时刻（回显防拽回）
        self._prog_set = False           # 程序正在设置滑块（不算用户交互）
        self.cam_on = False              # 摄像头工作线程开关
        self.cam_gen = 0                 # 摄像头线程代际号（防旧线程误停新线程）
        self._gest_seq = 0               # 手势推理帧计数（配合 GEST_DETECT_EVERY 隔帧检测）
        self._last_result = None         # 上次推理结果（中间帧复用，进入手势模式时重置）
        self.gesture_queue = queue.Queue(maxsize=1)  # 摄像头线程→主线程 帧队列
        self.photo = None                # 保持 PhotoImage 引用防止被 GC
        self.log_queue = queue.Queue()   # 日志队列（任意线程可写）
        self.last_log_light = None       # 上次记录的亮度回显（去重防刷屏）
        self._video_ratio = 0.75         # 摄像头画面高/宽比（默认 4:3，首帧后按实际更新）
        self._cam_body_h = 0             # 预览区当前高度（防抖：变了才重设）

        # ---------------- 音乐播放 ----------------
        self.music = None                # MusicPlayer（首次播放时懒启动 mpv）
        self.music_err = MUSIC_ERR if not MUSIC_OK else ""
        self.music_tracks = []
        self._music_cover_ref = None     # 封面 PhotoImage/CTkImage 防 GC
        self._music_cover_idx = -2       # 已画封面的曲目号（变了才重画）
        self._music_seek_touch = 0.0     # 用户正在拖进度条的时刻（防回拽）
        self._music_state = {"playing": False, "index": -1}
        self._music_rows = []            # 播放列表行的按钮引用

        # ---------------- MediaPipe（实例在工作线程内创建） ----------------
        self.mp_hands = mp.solutions.hands if CAMERA_OK else None
        self.mp_draw = mp.solutions.drawing_utils if CAMERA_OK else None

        # ---------------- 磨砂玻璃背景 ----------------
        self._bg_src = None          # 背景原图（PIL，cover 裁剪前的整图）
        self._bg_photo = None        # ImageTk（防 GC）
        self._bg_frost = None        # 已裁剪到窗口尺寸的图（供取色）
        self._bg_size = None
        self._glass_cards = []       # 参与玻璃着色的卡片
        self._glass_pending = None   # 防抖 after 句柄
        self._load_background()
        # 背景画布要先建（在所有卡片之下），用 place 平铺整窗
        self._bg_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0,
                                    bg=BG)
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.root.bind("<Configure>", self._on_root_configure)

        self._enable_dark_titlebar()
        self._init_music()                 # 扫描音乐目录（不启动 mpv）
        self._build_sidebar()
        self._build_main()
        self._update_brightness_ui(50)     # 亮度表盘初始就有 50% 进度（与滑块一致）
        self._poll_serial()
        self._poll_music()
        self._flush_slider()
        self._poll_camera()
        self._flush_log()
        self._tick_clock()
        if self._kiosk:
            # 全屏：盖住 LXDE 任务栏与标题栏（EWMH fullscreen 层在面板之上）
            self.root.attributes("-fullscreen", True)
        if IS_LINUX and os.environ.get("LAMP_HIDE_CURSOR", "1") == "1":
            self.root.after(300, self._hide_cursor)   # 触屏设备：隐藏鼠标指针
        # 玻璃着色要等布局算完才知道每张卡压在背景图的哪个位置
        self.root.after(250, self._glass_pass)
        self.root.after(1100, self._glass_pass)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 环境变量指定端口时，启动后自动连接（一键启动 / 无人值守场景）
        auto_port = os.environ.get("LAMP_PORT", "").strip()
        if auto_port:
            try:
                self.port_box.set(auto_port)
                self.root.after(1200, self._toggle_serial)
            except Exception:
                pass

    #=================== 界面搭建 ===================

    def _enable_dark_titlebar(self):
        """Windows 暗色标题栏（DWM，失败静默回退系统默认）；Linux 无此 API，直接跳过"""
        if IS_LINUX:
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr,
                        ctypes.byref(ctypes.c_int(1)), 4) == 0:
                    break
        except Exception:
            pass

    #=================== 触屏：隐藏鼠标指针 ===================

    def _hide_cursor(self):
        """触屏设备不需要鼠标指针。Tk 的 -cursor 直接支持 "none"（板端实测 OK，
        @file 位图方案反而不支持）；递归一遍把 CTk 控件自带的 hand2 也盖掉。"""
        try:
            self.root.configure(cursor="none")
            self._set_cursor_deep(self.root, "none")
            print("[触屏] 鼠标指针已隐藏")
        except Exception as e:
            print("[触屏] 隐藏鼠标失败: %r" % (e,))

    def _set_cursor_deep(self, w, cur):
        for ch in w.winfo_children():
            try:
                ch.configure(cursor=cur)
            except Exception:
                pass
            self._set_cursor_deep(ch, cur)

    #=================== 磨砂玻璃背景 ===================

    def _load_background(self):
        """读 assets/bg.jpg（cover 铺满窗口）；读不到就保持纯色兜底"""
        base = os.path.dirname(os.path.abspath(__file__))
        for name in ("bg.jpg", "bg.jpeg", "bg.png"):
            p = os.path.join(base, "assets", name)
            if os.path.exists(p):
                break
        else:
            print("[玻璃] 未找到 assets/bg.jpg，使用纯色背景")
            return
        try:
            if PILImage is None:
                print("[玻璃] 未装 PIL，背景图不可用")
                return
            self._bg_src = PILImage.open(p).convert("RGB")
            print("[玻璃] 背景图 %s %s（PILImageTk=%s）" % (
                os.path.basename(p), self._bg_src.size, PILImageTk is not None))
        except Exception as e:
            print("[玻璃] 背景图加载失败: %r" % (e,))

    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        if self._glass_pending:
            self.root.after_cancel(self._glass_pending)
        self._glass_pending = self.root.after(200, self._glass_pass)

    def _glass_pass(self):
        """重铺背景图 + 给每张卡按它压住的背景区域着磨砂色"""
        try:
            self._fit_screen()          # 屏幕分辨率变了（如 HDMI 重协商）就重设窗口
            self._render_background()
            self._apply_glass()
        except Exception as e:
            print("[玻璃] 着色异常: %r" % (e,))

    def _fit_screen(self):
        """窗口铺满当前屏幕；分辨率变化时自动跟随（1080p 掉回 800p 也能自救）
        kiosk 全屏模式下尺寸交给 WM 管理，不再手动设 geometry"""
        sw = int(self.root.winfo_screenwidth())
        sh = int(self.root.winfo_screenheight())
        if (sw, sh) == getattr(self, "_screen_key", None):
            return
        self._screen_key = (sw, sh)
        if self._kiosk:
            self.root.attributes("-fullscreen", True)   # 分辨率变了重申全屏
        else:
            self.root.geometry("%dx%d+0+0" % (max(1120, sw), max(560, sh - 42)))

    def _render_background(self):
        if self._bg_src is None or PILImage is None or PILImageTk is None:
            return
        w = max(self.root.winfo_width(), 1)
        h = max(self.root.winfo_height(), 1)
        if (w, h) == self._bg_size:
            return
        src = self._bg_src
        scale = max(w / float(src.width), h / float(src.height))
        nw, nh = int(src.width * scale) + 1, int(src.height * scale) + 1
        im = src.resize((nw, nh), PILImage.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        im = im.crop((left, top, left + w, top + h))
        self._bg_frost = im
        self._bg_photo = PILImageTk.PhotoImage(im)     # 防 GC
        self._bg_canvas.delete("all")
        self._bg_canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")
        self._bg_size = (w, h)

    def _apply_glass(self):
        """Tk 没有真透明：用『卡片压住的背景区域平均色 + 高比例白』近似磨砂玻璃。
        卡片底色变了之后，卡里用 CARD/CARD_IN 画底的 Canvas 也要跟着同步。"""
        if self._bg_frost is None:
            return
        self.root.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        for card in self._glass_cards:
            if not card.winfo_exists():
                continue
            x = card.winfo_rootx() - rx
            y = card.winfo_rooty() - ry
            w, h = card.winfo_width(), card.winfo_height()
            if w < 40 or h < 40:
                continue
            r, g, b = self._bg_frost.crop((x, y, x + w, y + h)) \
                                  .resize((1, 1), PILImage.LANCZOS) \
                                  .getpixel((0, 0))[:3]
            col = _frost_rgb(r, g, b, GLASS_K)
            card.configure(fg_color=col)
            self._recolor_children(card, col, r, g, b)

    def _recolor_children(self, widget, col, r, g, b):
        """递归同步卡片内的 Canvas/内嵌面：普通面跟卡片同色，标记了
        _glass_inset 的（视频区/封面/日志框）用更实一档的磨砂色"""
        inset = _frost_rgb(r, g, b, GLASS_K_IN)
        for ch in widget.winfo_children():
            marked = getattr(ch, "_glass_inset", False)
            if isinstance(ch, tk.Canvas):
                try:
                    ch.configure(bg=inset if marked else col)
                except Exception:
                    pass
            elif marked:
                try:
                    ch.configure(fg_color=inset)      # CTk 系列
                except Exception:
                    try:
                        ch.configure(bg=inset)        # tk 系列
                    except Exception:
                        pass
            self._recolor_children(ch, col, r, g, b)

    def _card(self, master):
        """统一卡片容器：圆角 16 + 玻璃高光边（底色由 _glass_pass 按背景图着色）"""
        f = ctk.CTkFrame(master, corner_radius=16, fg_color=CARD,
                         border_width=1, border_color=BORDER)
        self._glass_cards.append(f)
        return f

    def _card_head(self, master, icon_fn, title, accent=FG_DIM):
        """卡片标题行：小图标 + 灰标题"""
        head = ctk.CTkFrame(master, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 4))
        ic = tk.Canvas(head, width=20, height=20, bg=CARD, highlightthickness=0)
        ic.pack(side="left")
        icon_fn(ic, 10, 10, 16, accent)
        ctk.CTkLabel(head, text=title, font=(CN_FONT, 12),
                     text_color=FG_DIM).pack(side="left", padx=(8, 0))
        return head

    def _load_logo(self, height):
        """加载 assets/logo.png（等比缩放到指定高度）。
        注意：CTkImage 必须被引用住，否则被 GC 回收后图片会消失。"""
        try:
            from PIL import Image
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
            # 优先用圆形徽章版（完整 logo），没有时退回主体图形版
            p = os.path.join(base, "logo_circle.png")
            if not os.path.exists(p):
                p = os.path.join(base, "logo.png")
            if not os.path.exists(p):
                return None
            im = Image.open(p).convert("RGBA")
            w = max(1, int(im.width * height / float(im.height)))
            im = im.resize((w, height), Image.LANCZOS)
            img = ctk.CTkImage(light_image=im, dark_image=im, size=(w, height))
            self._logo_ref = img                      # 防 GC
            return img
        except Exception as e:
            print("[logo] 加载失败: %r" % (e,))
            return None

    def _build_sidebar(self):
        """左侧悬浮图标侧栏：logo / 模式切换 / 底部状态点"""
        side = ctk.CTkFrame(self.root, corner_radius=20, fg_color=CARD,
                            border_width=1, border_color=BORDER)
        side.grid(row=0, column=0, sticky="ns", padx=(14, 10), pady=14)
        self.side_frame = side
        self._glass_cards.append(side)

        _logo = self._load_logo(72)
        if _logo is not None:
            # 品牌 logo（assets/logo_circle.png，圆形透明 PNG）
            ctk.CTkLabel(side, image=_logo, text="").pack(pady=(18, 14))
        else:
            # 没有 logo 文件时回退到原来画的灯泡图标
            logo = tk.Canvas(side, width=44, height=44, bg=CARD,
                             highlightthickness=0)
            logo.pack(pady=(16, 10))
            icon_bulb(logo, 22, 26, 32, AMBER, filled=True, rays=True)

        self.nav_manual = SideIconButton(side, icon_sliders,
                                         lambda: self._set_mode("manual"))
        self.nav_manual.pack(pady=4)
        self.nav_gesture = SideIconButton(side, icon_hand,
                                          lambda: self._set_mode("gesture"))
        self.nav_gesture.pack(pady=4)
        self.nav_manual.set_active(True)

        tk.Frame(side, bg=BORDER, height=1, width=44).pack(pady=(14, 10))

        # ---- 退出按钮（侧栏最底部=整个界面左下角，点了回桌面）----
        ctk.CTkButton(side, text="退 出", width=76, height=34,
                      font=(CN_FONT, 11, "bold"),
                      fg_color="#fdecec", hover_color="#f9d7d7",
                      text_color=RED, corner_radius=10,
                      command=self._on_close).pack(side="bottom", pady=(0, 12))

        self.side_dot = tk.Canvas(side, width=12, height=12, bg=CARD,
                                  highlightthickness=0)
        self.side_dot.create_oval(2, 2, 10, 10, fill=RED, outline="")
        self.side_dot.pack(side="bottom", pady=(0, 10))

    def _build_main(self):
        """主区：顶栏 / 内容网格 / 底部日志"""
        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(0, 14), pady=14)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        self._build_header(main)
        self._build_log(main)                       # 先占底部，内容占中间
        self._build_body(main)
        if os.environ.get("LAMP_UI_DEBUG") == "1":   # LAMP_UI_DEBUG=1 打印各卡实际尺寸
            self.root.after(6000, self._dump_geometry)

    def _dump_geometry(self):
        """把主卡的请求尺寸/实际尺寸打到日志（调布局时用）"""
        print("=== 布局几何（req=自然尺寸, act=实际尺寸）===")
        for nm in ("card_hero", "card_temp", "card_humi", "card_light",
                   "card_music", "card_cam", "card_sched", "card_cloth",
                   "card_ctrl"):
            w = getattr(self, nm, None)
            if w is None:
                continue
            print("  %-11s req=%4dx%-4d act=%4dx%-4d" % (
                nm, w.winfo_reqwidth(), w.winfo_reqheight(),
                w.winfo_width(), w.winfo_height()))
        for nm, w in (("video_body", getattr(self, "video_body", None)),
                      ("hero_bulb", getattr(self, "hero_bulb", None))):
            if w is not None:
                print("  %-11s req=%4dx%-4d act=%4dx%-4d" % (
                    nm, w.winfo_reqwidth(), w.winfo_reqheight(),
                    w.winfo_width(), w.winfo_height()))
        print("  屏幕=%dx%d 窗口=%dx%d" % (
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            self.root.winfo_width(), self.root.winfo_height()))

    def _build_header(self, main):
        """顶栏：标题 / 串口下拉 / 刷新 / 连接 / 状态胶囊"""
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.pack(fill="x", pady=(0, 10))

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text="ProTaiden", font=("Segoe UI", 22, "bold"),
                     text_color=FG).pack(side="left")
        ctk.CTkLabel(left, text="  智能台灯 · 温湿度监控 · 手势调光",
                     font=(CN_FONT, 12), text_color=FG_DIM).pack(side="left",
                                                                 pady=(8, 0))

        # 状态胶囊（最右）
        pill = ctk.CTkFrame(head, corner_radius=999, fg_color="#e9f0ec",
                            border_width=1, border_color=BORDER)
        pill.pack(side="right", padx=(10, 0))
        self.pill_dot = ctk.CTkLabel(pill, text="●", font=(CN_FONT, 11),
                                     text_color=RED)
        self.pill_dot.pack(side="left", padx=(12, 2))
        self.pill_text = ctk.CTkLabel(pill, text="未连接",
                                      font=(CN_FONT, 11), text_color=FG_DIM)
        self.pill_text.pack(side="left", padx=(0, 12))

        self.btn_conn = ctk.CTkButton(head, text="连 接", width=88, height=32,
                                      font=(CN_FONT, 12, "bold"),
                                      fg_color=ACCENT, hover_color=ACCENT_D,
                                      text_color="#ffffff", corner_radius=10,
                                      command=self._toggle_serial)
        self.btn_conn.pack(side="right", padx=(0, 10))

        ctk.CTkButton(head, text="刷新", width=64, height=32,
                      font=(CN_FONT, 12), fg_color="#e9f0ec",
                      hover_color="#dfe9e4", text_color=FG_DIM,
                      corner_radius=10, border_width=1, border_color=BORDER,
                      command=self._refresh_ports).pack(side="right",
                                                        padx=(0, 8))

        self.port_box = ctk.CTkOptionMenu(head, width=132, height=32,
                                          font=(CN_FONT, 12),
                                          fg_color="#e9f0ec",
                                          button_color="#dfe9e4",
                                          button_hover_color="#d5e1db",
                                          text_color=FG, corner_radius=10,
                                          values=["无可用串口"])
        self.port_box.pack(side="right", padx=(0, 8))
        self._refresh_ports()

    def _build_body(self, main):
        """内容网格（4 行 4 列）：
            r0: Hero(跨 3 行) │ 温度 │ 湿度 │ 亮度
            r1: Hero          │ 音乐播放器（跨 2 列） │ 摄像头(跨 2 行)
            r2: Hero          │ 本周课表（跨 2 列）   │ ↕
            r3: 穿衣建议       │ 控制（跨 3 列，横向）
        摄像头跨 r1~r2 是刻意安排：4:3 画面只有给到接近方形的区域才不浪费；
        课表跨 2 列则是 5 天 × 5 个节次块展开的最小可用宽度。"""
        content = ctk.CTkFrame(main, fg_color="transparent")
        content.pack(fill="both", expand=True)
        for col in (1, 2, 3):
            content.grid_columnconfigure(col, weight=1, uniform="m")
        content.grid_columnconfigure(0, weight=0, minsize=300)
        content.grid_rowconfigure(0, weight=0)
        content.grid_rowconfigure(1, weight=1)
        content.grid_rowconfigure(2, weight=0)
        content.grid_rowconfigure(3, weight=0)

        # ----- Hero 大卡（左，跨前三行） -----
        hero = self._card(content)
        hero.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        self.card_hero = hero
        self._build_hero(hero)

        # ----- 三张指标卡（r0，等宽一行） -----
        self.card_temp = MetricCard(content, "温度", icon_temp, AMBER, "℃",
                                    chart="line", chart_range=(0, 50),
                                    note="每 2 秒刷新 · DHT-11")
        self.card_temp.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        self.card_humi = MetricCard(content, "湿度", icon_drop, ACCENT, "%RH",
                                    chart="bar", chart_range=(0, 100),
                                    note="相对湿度 · DHT-11")
        self.card_humi.grid(row=0, column=2, sticky="nsew", padx=(0, 10))
        self.card_light = MetricCard(content, "亮度", icon_gauge, GREEN, "%",
                                     chart="gauge", chart_range=(DUTY_MIN, DUTY_MAX),
                                     note="PWM 死区 10~90%")
        self.card_light.grid(row=0, column=3, sticky="nsew")

        # ----- r1：音乐播放器（跨两列）+ 摄像头（跨 r1~r2 两行） -----
        music = self._card(content)
        music.grid(row=1, column=1, columnspan=2, sticky="nsew",
                   padx=(0, 10), pady=(10, 0))
        self.card_music = music
        self._build_music(music)

        cam = self._card(content)
        cam.grid(row=1, column=3, rowspan=2, sticky="nsew", pady=(10, 0))
        self.card_cam = cam
        self._build_camera(cam)

        # ----- r2：本周课表（跨两列） -----
        self.card_sched = ScheduleCard(content)
        self.card_sched.grid(row=2, column=1, columnspan=2, sticky="nsew",
                             padx=(0, 10), pady=(10, 0))

        # ----- r3：穿衣建议（左窄）+ 控制（横向通栏） -----
        self.card_cloth = ClothingCard(content)
        self.card_cloth.grid(row=3, column=0, sticky="nsew", padx=(0, 10),
                             pady=(10, 0))

        ctrl = self._card(content)
        ctrl.grid(row=3, column=1, columnspan=3, sticky="nsew", pady=(10, 0))
        self.card_ctrl = ctrl
        self._build_control(ctrl)

    def _build_hero(self, hero):
        """亮度 Hero 大卡：灯泡 + 超大数值 + 模式 + 日期时间"""
        top = ctk.CTkFrame(hero, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(16, 0))
        ctk.CTkLabel(top, text="当前亮度", font=(CN_FONT, 12),
                     text_color=FG_DIM).pack(side="left")
        self.hero_mode_pill = ctk.CTkLabel(top, text="手动滑块",
                                           font=(CN_FONT, 11),
                                           fg_color="#e6f0ea",
                                           corner_radius=999,
                                           text_color=ACCENT)
        self.hero_mode_pill.pack(side="right")

        self.hero_bulb = tk.Canvas(hero, width=88, height=88, bg=CARD,
                                   highlightthickness=0)
        self.hero_bulb.pack(pady=(16, 0))

        row = ctk.CTkFrame(hero, fg_color="transparent")
        row.pack(pady=(6, 0))
        self.hero_value = ctk.CTkLabel(row, text="50",
                                       font=("Segoe UI Semilight", 58),
                                       text_color=FG)
        self.hero_value.pack(side="left")
        ctk.CTkLabel(row, text="%", font=("Segoe UI Light", 26),
                     text_color=FG_DIM).pack(side="left", pady=(18, 0),
                                             padx=(4, 0))

        self.hero_sub = ctk.CTkLabel(hero, text="当前亮度 · 手动滑块模式",
                                     font=(CN_FONT, 12), text_color=FG_DIM)
        self.hero_sub.pack(pady=(0, 10))

        tk.Frame(hero, bg=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        foot = ctk.CTkFrame(hero, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(4, 16))
        self.hero_date = ctk.CTkLabel(foot, text="--", font=(CN_FONT, 11),
                                      text_color=FG_DIM)
        self.hero_date.pack(anchor="w")
        self.hero_time = ctk.CTkLabel(foot, text="--:--:--",
                                      font=("Segoe UI", 16, "bold"),
                                      text_color=FG)
        self.hero_time.pack(anchor="w")
        self.hero_port = ctk.CTkLabel(foot, text="端口 未连接",
                                      font=(CN_FONT, 11),
                                      text_color=FG_DIM)
        self.hero_port.pack(anchor="w", pady=(6, 0))
        self._draw_hero_bulb(50)

        # ---- 设备状态（填住 Hero 下半部留白，显示的是本机真实参数）----
        st = ctk.CTkFrame(hero, fg_color=CARD_IN, corner_radius=12)
        st._glass_inset = True
        st.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        ctk.CTkLabel(st, text="设备状态", font=(CN_FONT, 10),
                     text_color=FG_DIM).pack(anchor="w", padx=12, pady=(8, 4))
        self.hero_stat = {}
        for key, val in self._device_rows():
            r = ctk.CTkFrame(st, fg_color="transparent")
            r.pack(fill="x", padx=12)
            ctk.CTkLabel(r, text=key, font=(CN_FONT, 10),
                         text_color=FG_DIM).pack(side="left")
            v = ctk.CTkLabel(r, text=val, font=(CN_FONT, 10, "bold"),
                             text_color=FG)
            v.pack(side="right")
            self.hero_stat[key] = v
        tk.Frame(st, bg=CARD_IN, height=6).pack()

    def _device_rows(self):
        """Hero 设备状态的 (名称, 值)：全部来自运行环境，不写死"""
        # 推理后端：NPU 版启动器在加载主程序前把 mp.solutions.hands.Hands
        # 换成了 hand_npu.Hands，看类的来路即可判断
        backend = "MediaPipe / CPU"
        try:
            if getattr(self.mp_hands, "Hands", None).__module__ == "hand_npu":
                backend = "RKNN / NPU"
        except Exception:
            pass
        # 摄像头来源
        if not CAMERA_OK:
            cam = "未启用"
        elif os.environ.get("CAM_INDEX", "") == "9" or os.path.exists("/dev/video9"):
            cam = "USB UVC · MJPG"
        else:
            cam = "MIPI / V4L2 · #%s" % os.environ.get("CAM_INDEX", "auto")
        # 音乐库
        if self.music_tracks:
            music = "%d 首 · %s" % (len(self.music_tracks),
                                    os.path.basename(self.music.music_dir))
        else:
            music = "无音乐文件"
        return (("推理后端", backend), ("摄像头", cam), ("音乐库", music))

    def _draw_hero_bulb(self, value):
        """Hero 灯泡：琥珀实心，亮度 ≥60% 显示光线"""
        c = self.hero_bulb
        c.delete("all")
        icon_bulb(c, 44, 44, 56, AMBER, filled=True, rays=value >= 60)

    def _build_camera(self, cam):
        """摄像头预览面板：标题 + LIVE 标记 + 视频区（高度按画面比例锁定）"""
        self._cam = cam
        head = self._card_head(cam, icon_camera, "摄像头预览", ACCENT)
        self.cam_live = ctk.CTkLabel(head, text="", font=("Segoe UI", 11,
                                                          "bold"),
                                     text_color=GREEN)
        self.cam_live.pack(side="right")

        self.video_body = tk.Frame(cam, bg=CARD_IN)
        # propagate 关闭：显式 height 才会生效（否则帧高度被子控件反向决定，
        # 首帧小图会把预览区带塌成指甲盖大小）
        self.video_body._glass_inset = True          # 内嵌面：磨砂更实一档
        self.video_body.pack_propagate(False)
        self.video_body.pack(fill="x", padx=12, pady=(0, 12))
        self.video = tk.Label(self.video_body,
                              text=("切到手势模式后\n自动开启摄像头" if CAMERA_OK else
                                    "未安装摄像头组件\n（OpenCV / MediaPipe 缺失）\n当前仅手动滑块模式"),
                              bg=CARD_IN, fg=FG_DIM,
                              font=(CN_FONT, 13), bd=0,
                              highlightthickness=0)
        self.video._glass_inset = True
        self.video.pack(fill="both", expand=True)
        cam.bind("<Configure>", lambda e: self._update_cam_body(e.width, e.height))

    def _update_cam_body(self, card_width, card_height=None):
        """预览区高度 = 卡宽 × 画面比例；行高不够时再压一档（画面等比内缩，不溢出卡片）"""
        body_w = card_width - 24               # 扣除 body 左右 padx
        if body_w < 60:
            return
        body_h = int(body_w * self._video_ratio)
        if card_height:                        # 扣掉标题行与上下 padding 的固定开销
            avail = int(card_height) - 42 - 22
            if avail >= 80:
                body_h = min(body_h, avail)
        if body_h >= 60 and body_h != self._cam_body_h:
            self._cam_body_h = body_h
            self.video_body.configure(height=body_h)

    def _build_control(self, ctrl):
        """控制卡（横向两栏，窄高比）：左=调光模式，右=亮度滑块 + 快捷档位"""
        self._card_head(ctrl, icon_sliders, "控制", ACCENT)

        body = ctk.CTkFrame(ctrl, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(2, 12))

        # ---- 左栏：调光模式 ----
        # 注意 height 必须显式给小值：CTkFrame 默认高 200，关掉 propagate 后会
        # 把整张控制卡撑到 260 高（实测踩过），把下面几行行高全挤掉
        left = ctk.CTkFrame(body, fg_color="transparent", width=360, height=1)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="调光模式", font=(CN_FONT, 11),
                     text_color=FG_DIM).pack(anchor="w")
        self.mode_seg = ctk.CTkSegmentedButton(
            left, values=[MODE_LABELS["manual"], MODE_LABELS["gesture"]],
            font=(CN_FONT, 12), height=32,
            selected_color=ACCENT, selected_hover_color=ACCENT_D,
            unselected_color="#e6eeea", unselected_hover_color="#dbe6e0",
            text_color=FG, command=self._on_segment)
        self.mode_seg.pack(fill="x", pady=(6, 0))
        self.mode_seg.set(MODE_LABELS["manual"])   # 5.2.2 构造后无默认选中项，需显式设置
        if not CAMERA_OK:
            # 没装 OpenCV/MediaPipe：手势模式整体不可用，直接置灰（避免点了报错）
            self.mode_seg.configure(values=[MODE_LABELS["manual"]], state="disabled")
        ctk.CTkLabel(left, text="手势模式下滑块锁定，由手势控制亮度 · 捏合调光 · "
                               "中指抬起锁定 · 张开手掌暂停调光",
                     font=(CN_FONT, 10), text_color=FG_DIM, justify="left",
                     wraplength=340).pack(anchor="w", pady=(8, 0))

        # ---- 右栏：亮度滑块 + 快捷档位 ----
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(28, 0))

        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="亮度调节", font=(CN_FONT, 11),
                     text_color=FG_DIM).pack(side="left")
        self.badge = ctk.CTkLabel(row, text=" 50% ", font=(CN_FONT, 12, "bold"),
                                  fg_color=ACCENT, corner_radius=8,
                                  text_color="#ffffff")
        self.badge.pack(side="right")

        self.slider = ctk.CTkSlider(
            right, from_=DUTY_MIN, to=DUTY_MAX,
            number_of_steps=DUTY_MAX - DUTY_MIN,
            command=self._on_slider, height=24,
            progress_color=ACCENT, button_color="#ffffff",
            button_hover_color="#f0f6f2", fg_color=TRACK, corner_radius=12)
        self.slider.set(50)
        self.slider.pack(fill="x", pady=(6, 0))

        # 快捷亮度：一键常用档位（手势模式下随滑块一起禁用）
        row_p = ctk.CTkFrame(right, fg_color="transparent")
        row_p.pack(fill="x", pady=(10, 0))
        self.preset_btns = []
        for v in (25, 50, 75, 90):
            b = ctk.CTkButton(row_p, text=str(v), height=26,
                              font=(CN_FONT, 11), corner_radius=8,
                              fg_color=CARD_IN, hover_color=HOVERBG,
                              text_color=FG_DIM,
                              command=lambda v=v: self._set_preset(v))
            b.pack(side="left", expand=True, fill="x", padx=2)
            self.preset_btns.append(b)

    #=================== 音乐播放卡 ===================

    def _init_music(self):
        """扫描音乐目录（只建对象、解析 ID3，不启动 mpv —— 首次播放才起进程）"""
        if not MUSIC_OK:
            return
        try:
            self.music = MusicPlayer()
            self.music_tracks = self.music.tracks
            if not self.music_tracks:
                self.music_err = self.music.error or "音乐目录为空"
        except Exception as e:
            self.music = None
            self.music_err = "%s: %s" % (type(e).__name__, e)

    def _mk_icon_btn(self, master, icon_fn, command, d=44, filled=False,
                     color=ACCENT, iscale=0.55):
        """Canvas 圆形图标按钮（不带文字），支持悬停反馈与动态换图标"""
        c = tk.Canvas(master, width=d, height=d, bg=CARD,
                      highlightthickness=0, bd=0)

        def render(hover=False):
            c.delete("all")
            if filled:                              # 主按钮：实心圆底
                bgc = ACCENT_D if hover else ACCENT
                c.create_oval(1, 1, d - 1, d - 1, fill=bgc, outline=bgc)
                fg = "#ffffff"
            else:                                   # 次按钮：悬停给个圆底
                if hover:
                    c.create_oval(1, 1, d - 1, d - 1, fill=HOVERBG, outline="")
                fg = FG if hover else color
            c.icon_fn(c, d / 2.0, d / 2.0, d * iscale, fg)

        c.icon_fn = icon_fn
        c.filled = filled
        c.render = render
        c.bind("<Button-1>", lambda e: command())
        c.bind("<Enter>", lambda e: render(True))
        c.bind("<Leave>", lambda e: render(False))
        render(False)
        return c

    def _set_btn_icon(self, btn, icon_fn):
        """切换按钮图标（播放 ↔ 暂停）"""
        if getattr(btn, "icon_fn", None) is icon_fn:
            return
        btn.icon_fn = icon_fn
        btn.render(False)

    def _build_music(self, card):
        """音乐播放卡：封面 + 曲目信息 + 进度条 + 三键控制 + 音量 + 播放列表
        （尺寸是压过的：这张卡的自然高度直接决定它所在行的高度，
          多出来 10px 就会把行撑高、把别的卡挤扁——见文件末尾的布局笔记）"""
        self._card_head(card, icon_music, "音乐播放", ACCENT)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # ---- 左：封面（120×120 圆角）----
        self.music_cover_box = tk.Frame(body, bg=CARD_IN, width=120, height=120)
        self.music_cover_box.pack(side="left", anchor="n")
        self.music_cover_box._glass_inset = True
        self.music_cover_box.pack_propagate(False)   # 关掉反向撑大
        self._draw_music_cover(None)

        # ---- 右：播放列表（先 pack right，避免被中间的 expand 挤掉）----
        # height 同样必须显式给小值：关掉 propagate 后 CTk 会用默认 200 高，
        # 把整张音乐卡撑到 256 高（实测踩过）
        pl = ctk.CTkFrame(body, fg_color=CARD_IN, corner_radius=12,
                          width=236, height=1)
        pl._glass_inset = True
        pl.pack(side="right", fill="y")
        pl.pack_propagate(False)
        ctk.CTkLabel(pl, text="播放列表 · %d 首" % len(self.music_tracks),
                     font=(CN_FONT, 10), text_color=FG_DIM
                     ).pack(anchor="w", padx=12, pady=(8, 2))
        self._music_rows = []
        if not self.music_tracks:
            ctk.CTkLabel(pl, text=(self.music_err or "未找到音乐文件"),
                         font=(CN_FONT, 9), text_color=FG_DIM,
                         wraplength=200, justify="left"
                         ).pack(anchor="w", padx=12)
        for i, tr in enumerate(self.music_tracks):
            b = ctk.CTkButton(pl, text="%d.  %s" % (i + 1, tr.title), anchor="w",
                              height=26, corner_radius=7, fg_color="transparent",
                              hover_color=HOVERBG, text_color=FG_DIM,
                              font=(CN_FONT, 10),
                              command=lambda i=i: self._music_play(i))
            b.pack(fill="x", padx=6)
            self._music_rows.append(b)

        # ---- 中：信息 / 进度 / 控制（最后 pack，吃掉剩余宽度）----
        mid = ctk.CTkFrame(body, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True, padx=(14, 14))

        self.music_title = ctk.CTkLabel(mid, text="未播放", anchor="w",
                                        font=(CN_FONT, 15, "bold"),
                                        text_color=FG)
        self.music_title.pack(fill="x")
        self.music_artist = ctk.CTkLabel(mid, text="点右侧列表选歌",
                                         anchor="w", font=(CN_FONT, 10),
                                         text_color=FG_DIM)
        self.music_artist.pack(fill="x", pady=(1, 0))

        self._music_prog_set = False
        self.music_prog = ctk.CTkSlider(
            mid, from_=0, to=1, number_of_steps=1000, height=16,
            command=self._on_music_seek, progress_color=ACCENT,
            button_color="#ffffff", button_hover_color="#f0f6f2",
            fg_color=TRACK, corner_radius=9)
        self.music_prog.set(0)
        self.music_prog.pack(fill="x", pady=(8, 0))

        trow = ctk.CTkFrame(mid, fg_color="transparent")
        trow.pack(fill="x", pady=(1, 0))
        self.music_time = ctk.CTkLabel(trow, text="00:00", anchor="w",
                                       font=("Consolas", 10), text_color=FG_DIM)
        self.music_time.pack(side="left")
        self.music_dur = ctk.CTkLabel(trow, text="00:00", anchor="e",
                                      font=("Consolas", 10), text_color=FG_DIM)
        self.music_dur.pack(side="right")

        ctl = ctk.CTkFrame(mid, fg_color="transparent")
        ctl.pack(fill="x", pady=(4, 0))
        self.music_btn_prev = self._mk_icon_btn(ctl, icon_prev,
                                                self._music_prev, 38)
        self.music_btn_prev.pack(side="left")
        self.music_btn_play = self._mk_icon_btn(ctl, icon_play,
                                                self._music_toggle, 46,
                                                filled=True)
        self.music_btn_play.pack(side="left", padx=8)
        self.music_btn_next = self._mk_icon_btn(ctl, icon_next,
                                                self._music_next, 38)
        self.music_btn_next.pack(side="left")

        vol = ctk.CTkFrame(ctl, fg_color="transparent")
        vol.pack(side="right")
        vic = tk.Canvas(vol, width=18, height=18, bg=CARD,
                        highlightthickness=0)
        vic.pack(side="left", padx=(0, 4))
        icon_volume(vic, 9, 9, 15, FG_DIM)
        self._music_vol_set = False
        self.music_vol = ctk.CTkSlider(
            vol, from_=0, to=100, number_of_steps=100, height=16, width=110,
            command=self._on_music_volume, progress_color=ACCENT,
            button_color="#ffffff", button_hover_color="#f0f6f2",
            fg_color=TRACK, corner_radius=9)
        self.music_vol.set(65)
        self.music_vol.pack(side="left")

    def _draw_music_cover(self, track):
        """封面：有内嵌图 → 圆角缩略图；无图/无 PIL → 深底音符占位"""
        box = self.music_cover_box
        for ch in box.winfo_children():
            ch.destroy()
        self._music_cover_ref = None

        img = None
        if track is not None:
            try:
                img = track.cover_image()
            except Exception:
                img = None
        if img is not None and PILImage is not None and PILImageTk is not None:
            try:
                S = 120
                im = img.convert("RGB").resize((S, S), PILImage.LANCZOS)
                im = im.convert("RGBA")
                mask = PILImage.new("L", (S, S), 0)
                PILImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, S - 1, S - 1), radius=14, fill=255)
                im.putalpha(mask)
                base = PILImage.new("RGBA", (S, S), CARD_IN)
                base.alpha_composite(im)
                photo = PILImageTk.PhotoImage(base.convert("RGB"))
                self._music_cover_ref = photo          # 防 GC
                tk.Label(box, image=photo, bg=CARD_IN, bd=0,
                         highlightthickness=0).pack(fill="both", expand=True)
                return
            except Exception as e:
                print("[music] 封面渲染失败: %r" % (e,))

        cv = tk.Canvas(box, width=120, height=120, bg=CARD_IN,
                       highlightthickness=0)
        cv.pack(fill="both", expand=True)
        icon_music(cv, 60, 60, 54, "#b7c7c0")          # 低对比占位音符

    # ---- 播放控制 ----
    def _music_start(self):
        """首次播放时启动 mpv（幂等）；失败写日志并返回 False"""
        if not MUSIC_OK or self.music is None:
            self._log("err", "音乐: %s" % (self.music_err or "播放器不可用"))
            return False
        if self.music.available:
            return True
        if not self.music.start():
            self.music_err = self.music.error
            self._log("err", "音乐: %s" % self.music.error)
            return False
        self._log("ok", "音乐: mpv 已启动 · %d 首" % len(self.music_tracks))
        return True

    def _music_play(self, i):
        if not (0 <= i < len(self.music_tracks)):
            return
        if not self._music_start():
            return
        if self.music.play_index(i):
            tr = self.music_tracks[i]
            self._log("info", "▶ 播放: %s - %s" % (tr.artist, tr.title))
            self._music_cover_idx = -2                 # 强制重画封面

    def _music_toggle(self):
        if not self._music_start():
            return
        self.music.toggle()

    def _music_next(self):
        if not self._music_start():
            return
        self.music.next()
        self._music_cover_idx = -2

    def _music_prev(self):
        if not self._music_start():
            return
        self.music.prev()
        self._music_cover_idx = -2

    def _on_music_seek(self, value):
        """拖动进度条 → 绝对定位（程序回填时跳过，防回拽）"""
        if self._music_prog_set or self.music is None or not self.music.available:
            return
        self._music_seek_touch = time.time()
        dur = self._music_state.get("dur", 0.0)
        if dur > 0:
            self.music.seek(float(value) * dur)

    def _on_music_volume(self, value):
        if self._music_vol_set or self.music is None:
            return
        self.music.set_volume(int(float(value)))

    def _poll_music(self):
        """主线程轮询播放状态（500ms）：刷新标题/封面/进度/按钮图标"""
        try:
            self._refresh_music()
        except Exception as e:
            print("[music] 刷新异常: %r" % (e,))
        self.root.after(500, self._poll_music)

    def _refresh_music(self):
        if self.music is None or not self.music.available:
            return

        # 自然播完 → 自动下一曲（循环播放）
        if self.music.eof_count() > 0:
            self._music_next()
            return

        st = self.music.status()

        # 标题 / 歌手
        if st["index"] >= 0 and st["title"]:
            if self.music_title.cget("text") != st["title"]:
                self.music_title.configure(text=st["title"])
            if self.music_artist.cget("text") != st["artist"]:
                self.music_artist.configure(text=st["artist"])
        elif st["index"] < 0:
            self.music_title.configure(text="未播放")
            self.music_artist.configure(text="点右侧列表选歌")

        # 封面（曲目变化才重画，避免每 500ms 重新解码）
        if self._music_cover_idx != st["index"]:
            self._music_cover_idx = st["index"]
            self._draw_music_cover(self.music.current())

        # 播放/暂停图标
        self._set_btn_icon(self.music_btn_play,
                           icon_pause if st["playing"] else icon_play)

        # 进度（用户拖拽后 1.5 秒内不回填，避免互相打架）
        dur, pos = st["dur"], st["pos"]
        self._music_state.update(st)
        if time.time() - self._music_seek_touch > 1.5:
            frac = (pos / dur) if dur > 0 else 0.0
            self._music_prog_set = True
            try:
                self.music_prog.set(max(0.0, min(1.0, frac)))
            finally:
                self._music_prog_set = False
        self.music_time.configure(text=self._fmt_time(pos))
        self.music_dur.configure(text=self._fmt_time(dur))

        # 音量回显（用户刚拖过就不覆盖）
        if abs(self.music_vol.get() - st["volume"]) > 1:
            self._music_vol_set = True
            try:
                self.music_vol.set(st["volume"])
            finally:
                self._music_vol_set = False

        # 列表高亮当前曲目
        for i, b in enumerate(self._music_rows):
            cur = (i == st["index"])
            col = ACCENT if cur else FG_DIM
            if b.cget("text_color") != col:
                b.configure(text_color=col, fg_color=(HOVERBG if cur
                                                      else "transparent"))

    @staticmethod
    def _fmt_time(sec):
        try:
            sec = int(sec)
        except Exception:
            return "00:00"
        if sec < 0:
            sec = 0
        return "%02d:%02d" % (sec // 60, sec % 60)

    def _build_log(self, main):
        """底部运行日志：可折叠 + 颜色分级 + 条数封顶"""
        panel = self._card(main)
        panel.pack(fill="x", side="bottom", pady=(10, 0))

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(10, 2))
        ic = tk.Canvas(head, width=20, height=20, bg=CARD,
                       highlightthickness=0)
        ic.pack(side="left")
        icon_lines(ic, 10, 10, 16, ACCENT)
        ctk.CTkLabel(head, text="运行日志", font=(CN_FONT, 12),
                     text_color=FG_DIM).pack(side="left", padx=(8, 0))

        self.btn_log_toggle = ctk.CTkButton(head, text="收起", width=56,
                                            height=22, font=(CN_FONT, 11),
                                            fg_color="#e6eeea",
                                            hover_color="#dbe6e0",
                                            text_color=FG_DIM,
                                            corner_radius=8,
                                            command=self._toggle_log)
        self.btn_log_toggle.pack(side="right")
        ctk.CTkButton(head, text="清空", width=56, height=22,
                      font=(CN_FONT, 11), fg_color="#e6eeea",
                      hover_color="#dbe6e0", text_color=FG_DIM,
                      corner_radius=8, command=self._clear_log
                      ).pack(side="right", padx=(0, 6))

        self.log_box = ctk.CTkTextbox(panel, height=92, fg_color=CARD_IN,
                                      text_color=FG, corner_radius=10,
                                      border_width=1, border_color=BORDER,
                                      font=("Consolas", 11), wrap="word")
        self.log_box._glass_inset = True
        self.log_box.pack(fill="x", padx=12, pady=(0, 12))
        self.log_box.tag_config("ok", foreground=GREEN)
        self.log_box.tag_config("err", foreground=RED)
        self.log_box.tag_config("info", foreground="#7d8c96")
        self.log_box.configure(state="disabled")

    def _toggle_log(self):
        """折叠/展开日志面板"""
        if self.log_box.winfo_manager():
            self.log_box.pack_forget()
            self.btn_log_toggle.configure(text="展开")
        else:
            self.log_box.pack(fill="x", padx=12, pady=(0, 12))
            self.btn_log_toggle.configure(text="收起")

    def _log(self, level, msg):
        """写入一条日志（线程安全：只入队，主线程批量刷新）"""
        try:
            self.log_queue.put((level, msg))
        except Exception:
            pass                            # 窗口关闭后忽略

    def _flush_log(self):
        """主线程：批量刷新日志队列 → 文本框，超限裁剪最旧行"""
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                tag = "ok" if level == "ok" else ("err" if level == "err"
                                                  else "info")
                self.log_box.configure(state="normal")
                self.log_box.insert("end",
                                    f"[{time.strftime('%H:%M:%S')}] {msg}\n",
                                    tag)
                lines = int(self.log_box.index("end-1c").split(".")[0])
                if lines > MAX_LOG_LINES:
                    self.log_box.delete("1.0",
                                        f"{lines - MAX_LOG_LINES + 1}.0")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._flush_log)

    def _clear_log(self):
        """清空日志面板"""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _tick_clock(self):
        """Hero 卡日期时间：每秒刷新"""
        now = datetime.datetime.now()
        wd = "周" + "一二三四五六日"[now.weekday()]   # weekday(): 周一=0 … 周日=6
        self.hero_time.configure(text=now.strftime("%H:%M:%S"))
        self.hero_date.configure(text=f"{now.strftime('%Y-%m-%d')} {wd}")
        self.root.after(1000, self._tick_clock)

    #=================== 界面状态更新 ===================

    def _set_status(self, text, color):
        """顶栏状态胶囊 + 侧栏圆点"""
        self.pill_dot.configure(text_color=color)
        self.pill_text.configure(text=text, text_color=FG_DIM
                                 if color == RED else color)
        self.side_dot.itemconfig(1, fill=color)

    def _set_conn_button(self, connected):
        """连接按钮外观：连接中(蓝) / 已连接(断开·灰) / 未连接(蓝)"""
        if connected:
            self.btn_conn.configure(text="断开", fg_color="#cfdcd5",
                                    hover_color="#c2d2c9")
        else:
            self.btn_conn.configure(text="连 接", fg_color=ACCENT,
                                    hover_color=ACCENT_D)

    def _update_brightness_ui(self, v):
        """亮度变化 → Hero 数值/灯泡、徽章、亮度仪表卡"""
        v = int(v)
        self.hero_value.configure(text=str(v))
        self._draw_hero_bulb(v)
        self.badge.configure(text=f" {v}% ")
        self.card_light.set_value(v)

    def _set_slider_silent(self, v):
        """程序设置滑块（不触发用户交互标记）"""
        self._prog_set = True
        try:
            self.slider.set(v)
        finally:
            self._prog_set = False

    def _slider_busy(self):
        """滑块处于用户交互后 0.4s 内（期间回显不同步，防拽回）"""
        return time.time() - self.slider_touch < SLIDER_ECHO_GUARD

    #=================== 串口通信 ===================

    def _refresh_ports(self):
        """刷新可用串口下拉框
        Linux 下保留 /dev/ttyUSB*、/dev/ttyACM*（/dev/rfcomm0 需内核编译 RFCOMM TTY，
        本板没有），并把板载调试口 /dev/ttyS* 从候选里剔除（若只有 ttyS 可用则仍保留）。
        另外会附加蓝牙相关选项：bt:<MAC>（RFCOMM 直连）与 socket:// 桥接地址。"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if IS_LINUX:
            useful = [p for p in ports
                      if not os.path.basename(p).startswith("ttyS")]
            if useful:
                ports = useful
        # 蓝牙选项（本板没有 /dev/rfcomm0，可以直接连 MAC，或走 TCP 桥接）
        bt_mac = os.environ.get("LAMP_BT_MAC", "").strip()
        if bt_mac and _HAS_BT_SOCKET:
            ports.append("bt:" + bt_mac)
        if os.environ.get("LAMP_BRIDGE", "").strip():
            ports.append(os.environ["LAMP_BRIDGE"].strip())
        env_port = os.environ.get("LAMP_PORT", "").strip()
        if env_port and env_port not in ports:
            ports.insert(0, env_port)       # 环境变量指定的端口置顶，方便自动连接
        if ports:
            self.port_box.configure(values=ports)
            if self.port_box.get() not in ports:
                self.port_box.set(ports[0])
        else:
            self.port_box.configure(values=["无可用串口"])
            self.port_box.set("无可用串口")

    def _toggle_serial(self):
        """连接 / 断开串口
        连接动作在后台线程执行：部分串口驱动（CH340 克隆、蓝牙虚拟串口）
        在 open 时会无限阻塞，若在主线程执行会直接冻结界面"""
        if self.connecting:
            return                          # 连接进行中，忽略重复点击
        if self.ser is None:
            port = self.port_box.get()
            if not port or port == "无可用串口":
                return
            self.connecting = True
            self.attempt += 1
            attempt = self.attempt          # 本次尝试编号
            self.btn_conn.configure(state="disabled", text="连接中...")
            self._set_status("正在连接...", ACCENT)
            self._log("info", f"正在连接 {port} ...")
            self._user_disconnected = False   # 用户重新连接 → 恢复自动重连能力

            def worker():
                """后台执行阻塞的 open 操作"""
                try:
                    ser = _open_serial(port)
                except Exception:
                    ser = None              # 端口被占用 / 不存在 / 蓝牙连不上等
                # 回到主线程更新界面（带尝试编号，过期结果会被丢弃）
                try:
                    self.root.after(0, lambda: self._connect_done(ser, port,
                                                                  attempt))
                except Exception:
                    if ser is not None:     # 窗口已关闭，直接释放串口
                        ser.close()

            threading.Thread(target=worker, daemon=True).start()
            # 蓝牙/TCP 桥接建链较慢（配对+寻呼可能要几秒），本地串口仍按 4 秒判定卡死
            wait_ms = 15000 if (port.startswith("bt:") or "://" in port) else 4000
            self.root.after(wait_ms, lambda: self._connect_timeout(attempt))
        else:
            self._disconnect()

    def _connect_done(self, ser, port, attempt):
        """后台连接完成回调：成功则启动接收线程，失败则提示"""
        if attempt != self.attempt:
            # 过期结果（已超时放弃或重新尝试）：迟到的成功连接直接关闭防泄漏
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            return
        self.connecting = False
        self.btn_conn.configure(state="normal")
        if ser is None:
            self._set_conn_button(False)
            self._set_status("连接失败（端口被占用？）", RED)
            self._log("err", "连接失败：端口被占用或不存在")
            self._schedule_reconnect()
        else:
            self.ser = ser
            threading.Thread(target=self._rx_thread, daemon=True).start()
            self._set_conn_button(True)
            self._set_status(f"已连接 {port}", GREEN)
            self.hero_port.configure(text=f"端口 {port}")
            self.last_sent = -1             # 重置节流记录，允许立即下发
            self._log("ok", f"已连接 {port}")

    def _connect_timeout(self, attempt):
        """连接超时处理：驱动卡死时恢复界面，提示用户处理"""
        if attempt != self.attempt or not self.connecting:
            return                          # 已完成或已放弃，无需处理
        self.attempt += 1                   # 使迟到的结果作废
        self.connecting = False
        self.btn_conn.configure(state="normal")
        self._set_conn_button(False)
        self._set_status("连接超时（请拔插USB或换端口）", RED)
        self._log("err", "连接超时：请拔插 USB 或更换端口")
        self._schedule_reconnect()

    def _disconnect(self):
        """断开串口：先置 None 停止接收线程，再关闭串口"""
        ser, self.ser = self.ser, None
        self._user_disconnected = True    # 用户主动断开 → 压制自动重连
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass                            # 线程可能正在读取，忽略关闭异常
        self._set_conn_button(False)
        self._set_status("未连接", RED)
        self.hero_port.configure(text="端口 未连接")
        self._log("info", "已断开连接")

    def _rx_thread(self):
        """串口接收线程：非阻塞轮询读取，按行解析送入消息队列
        串口被拔出时通知主线程更新界面，避免状态栏残留"已连接\""""
        ser = self.ser                      # 记录本线程服务的串口对象
        buf = b""                           # 行拼接缓冲
        while ser is not None and self.ser is ser:
            try:
                n = ser.in_waiting
                if n:
                    buf += ser.read(n)
                    # 按换行符切分出完整行
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        s = line.decode("ascii", errors="ignore").strip()
                        if s:
                            self.rx_queue.put(s)
            except (serial.SerialException, OSError):
                break                       # 串口被拔出或关闭
            time.sleep(0.02)                # 20ms 轮询间隔，不占 CPU
        # 异常退出（拔线）且未被用户主动断开 → 通知主线程
        if ser is not None and self.ser is ser:
            try:
                self.root.after(0, self._serial_lost)
            except RuntimeError:
                pass                        # 窗口已关闭，忽略

    def _serial_lost(self):
        """串口意外断开（拔线/驱动掉线/蓝牙链路丢失）：安全恢复界面状态"""
        if self.ser is None:
            return                          # 用户已主动断开，无需处理
        ser, self.ser = self.ser, None
        try:
            ser.close()
        except Exception:
            pass
        self.connecting = False
        self._set_conn_button(False)
        self._set_status("连接已断开", RED)
        self.hero_port.configure(text="端口 未连接")
        self._log("err", "串口连接断开（可能被拔出）")
        self._schedule_reconnect()

    #=================== 自动重连（一键启动场景） ===================

    def _auto_port(self):
        """一键启动时由 run_lamp.sh 注入的端口；为空表示手动模式（不自动重连）"""
        return os.environ.get("LAMP_PORT", "").strip()

    def _schedule_reconnect(self):
        """连接失败/断开后自动重试。仅在设置了 LAMP_PORT 时启用，
        避免手动模式下用户主动断开后又被自动连上。"""
        port = self._auto_port()
        if not port or self._reconn_pending or self._user_disconnected:
            return
        self._reconn_pending = True
        delay = int(os.environ.get("LAMP_RECONN_MS", "5000"))
        self._log("info", "将在 %.0f 秒后自动重连 %s ..." % (delay / 1000.0, port))
        try:
            self.root.after(delay, self._do_reconnect)
        except RuntimeError:
            self._reconn_pending = False    # 窗口已关闭

    def _do_reconnect(self):
        """执行自动重连"""
        self._reconn_pending = False
        if self.ser is not None or self._user_disconnected:
            return                          # 已连上 / 用户主动断开过，不打扰
        port = self._auto_port()
        if not port:
            return
        try:
            self.port_box.set(port)
        except Exception:
            pass
        self._log("info", "自动重连中 ...")
        self._toggle_serial()

    def _poll_serial(self):
        """主线程轮询：解析单片机上报的文本行"""
        try:
            while True:
                line = self.rx_queue.get_nowait()
                m = re.search(r"Humi:(\d+)%RH\s+Temp:(\d+)C", line)
                if m:                     # 温湿度上报
                    humi, temp = int(m.group(1)), int(m.group(2))
                    self.card_humi.set_value(humi)
                    self.card_temp.set_value(temp)
                    self.card_cloth.update_weather(temp, humi)
                    self._log("ok", f"温湿度上报: {humi}%RH  {temp}C")
                m = re.search(r"Light:(\d+)", line)
                if m:                     # 亮度回显（单片机实际生效值）
                    val = int(m.group(1))
                    self.last_sent = val              # 记录，避免回环重发
                    if not self._slider_busy():       # 拖动中不同步（防拽回）
                        self._set_slider_silent(val)
                        self._update_brightness_ui(val)
                        if val != self.last_log_light:  # 去重防刷屏
                            self.last_log_light = val
                            self._log("info", f"亮度回显: {val}%")
                if "Error" in line or "error" in line:
                    self._log("err", line)            # 单片机上报的错误
        except queue.Empty:
            pass
        self.root.after(100, self._poll_serial)

    def _send_frame(self, value):
        """下发调光帧 0xAA [亮度] 0x55（带节流）"""
        now = time.time()
        if (self.ser is None or value == self.last_sent
                or now - self.last_send_time < SLIDER_SEND_INTERVAL):
            return
        try:
            self.ser.write(bytes([FRAME_HEAD, value, FRAME_TAIL]))
        except serial.SerialException:
            return
        self.last_sent = value
        self.last_send_time = now
        self._log("info", f"发送调光帧: {value}%")

    #=================== 调光控制 ===================

    def _on_slider(self, value):
        """滑块回调：记录交互时刻（防回显拽回），手动模式下实时刷新界面"""
        if not self._prog_set:
            self.slider_touch = time.time()
        v = int(float(value))
        if self.mode == "manual":
            self._update_brightness_ui(v)

    def _set_preset(self, v):
        """快捷亮度：同步滑块/界面并立即下发一帧"""
        self._set_slider_silent(v)
        self._update_brightness_ui(v)
        self._send_frame(v)
        self._log("info", f"快捷亮度: {v}%")

    def _on_segment(self, label):
        """分段按钮回调 → 模式切换"""
        mode = "manual" if label == MODE_LABELS["manual"] else "gesture"
        self._set_mode(mode)

    def _set_mode(self, mode):
        """模式切换：分段按钮/侧栏高亮 + 滑块锁定 + 摄像头启停"""
        if mode == "gesture" and not CAMERA_OK:
            # 无摄像头组件时拒绝切手势（侧栏按钮/自动切换都走这里兜底）
            self._log("err", "手势模式不可用：未安装 OpenCV/MediaPipe/Pillow"
                             + ("（" + CAMERA_ERR + "）" if CAMERA_ERR else ""))
            try:
                self.mode_seg.set(MODE_LABELS["manual"])
            except Exception:
                pass
            return
        if mode == self.mode:
            return
        self.mode = mode
        label = MODE_LABELS[mode]
        if self.mode_seg.get() != label:
            self.mode_seg.set(label)        # 会触发 _on_segment，同值守卫兜底
        self.nav_manual.set_active(mode == "manual")
        self.nav_gesture.set_active(mode == "gesture")
        self.hero_mode_pill.configure(
            text=label, text_color=ACCENT if mode == "gesture" else FG_DIM)
        self.hero_sub.configure(text="当前亮度 · " +
                                ("手势调节模式" if mode == "gesture"
                                 else "手动滑块模式"))
        self._log("info", "切换到" + label + "模式")
        self._on_mode_change()

    def _flush_slider(self):
        """周期任务：手动模式下把滑块当前值节流下发"""
        if self.mode == "manual":
            self._send_frame(int(self.slider.get()))
        self.root.after(100, self._flush_slider)

    def _on_mode_change(self):
        """模式切换：手势模式启动摄像头工作线程并锁定滑块，手动模式反之"""
        if self.mode == "gesture":
            self.slider.configure(state="disabled")
            for b in self.preset_btns:
                b.configure(state="disabled")
            self.cam_live.configure(text="● LIVE")
            if not self.cam_on:
                self.cam_gen += 1                   # 代际号：防旧线程误停新线程
                gen = self.cam_gen
                self._last_result = None            # 新手势会话：丢弃上轮残留的推理结果
                self._gest_seq = 0
                self.cam_on = True
                threading.Thread(target=self._camera_worker,
                                 args=(gen,), daemon=True).start()
        else:
            self.slider.configure(state="normal")
            for b in self.preset_btns:
                b.configure(state="normal")
            self.cam_on = False             # 通知工作线程退出（自行释放摄像头）
            self.cam_live.configure(text="")
            # 清除残留画面 + 占位提示（否则画面冻结在最后一帧）
            self.video.config(image="", text="手动滑块模式\n摄像头已关闭")

    #=================== 摄像头与手势 ===================

    def _camera_worker(self, gen):
        """摄像头工作线程：采集 + 手势识别 + 画面绘制
        独立于 Tk 主线程运行；识别结果只通过队列交给主线程做 UI 更新与下发"""
        if not CAMERA_OK:                  # 无摄像头组件，绝不起线程
            if self.cam_gen == gen:
                self.cam_on = False
            return
        cap = _open_camera()
        if not cap.isOpened():
            # 快速切换模式时旧线程可能尚未释放设备，稍候重试一次再判定失败
            cap.release()
            time.sleep(0.25)
            cap = _open_camera()
        if not cap.isOpened():
            cap.release()
            if self.cam_gen == gen:
                self.cam_on = False
            self._log("err", "摄像头打开失败（可能被其他程序占用）")
            try:
                self.root.after(0, self._camera_open_fail, gen)
            except RuntimeError:
                pass
            return
        # ---- 推理与采集解耦 ----
        # 实测：MediaPipe 单次推理约 200ms，且**与输入图分辨率无关**（内部会缩到固定尺寸），
        # 所以"缩小画质"救不了卡顿；若在采集线程里同步推理，预览会被拖到 5~10fps。
        # 这里把推理放到独立线程，采集线程只做取帧/绘制/推送：
        #   → 预览可跑到 20~30fps，手势检测仍保持约 5 次/秒（对调光完全够用）
        self._gest_lock = threading.Lock()
        self._gest_frame = None                        # 待推理的最新帧（只留一帧）
        self._gest_out = (None, False, None, False)    # (value, frozen, landmarks, palm)
        self._hint_ts = 0.0                            # 中文提示限频用
        infer_thread = threading.Thread(target=self._infer_worker, args=(gen,), daemon=True)
        infer_thread.start()
        target_dt = 1.0 / max(1, CAM_FPS)              # 采集节流目标周期
        try:
            while self.cam_on:
                t_loop = time.time()
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.05)
                    continue
                frame = cv2.flip(frame, 1)             # 镜像，符合直觉

                # 1) 把最新帧丢给推理线程（不等待 → 绝不会被 200ms 推理阻塞）
                with self._gest_lock:
                    if self._gest_frame is None:
                        self._gest_frame = frame.copy()

                # 2) 取最近一次推理结果，用于本帧绘制与下发
                with self._gest_lock:
                    value, frozen, landmarks, _ = self._gest_out

                # 3) 绘制叠加层（纯 cv2 画线，开销 1~2ms 级）
                if landmarks is not None:
                    self._draw_overlay(frame, landmarks, value)

                # 4) 只保留最新一帧交给主线程（队列满则丢弃旧帧，防延迟堆积）
                try:
                    self.gesture_queue.put_nowait((frame, value, frozen))
                except queue.Full:
                    try:
                        self.gesture_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.gesture_queue.put_nowait((frame, value, frozen))
                    except queue.Full:
                        pass

                # 5) 只补足到目标周期（不额外多睡，避免白白丢帧）
                rest = target_dt - (time.time() - t_loop)
                if rest > 0:
                    time.sleep(rest)
        finally:
            cap.release()                      # 线程内释放摄像头
            if self.cam_gen == gen:            # 仅最新代际有权关闭标志
                self.cam_on = False

    def _camera_open_fail(self, gen):
        """摄像头打开失败（主线程回调）：切回手动模式恢复滑块，并给出提示"""
        if self.cam_gen != gen:
            return                          # 用户已重开新手势会话，过期不作数
        if self.mode == "gesture":
            self._set_mode("manual")
        self.video.config(image="", text="摄像头打开失败\n已切回手动滑块模式")

    # ---------------- 手势推理线程（与采集解耦） ----------------

    def _infer_worker(self, gen):
        """独立推理线程：从 self._gest_frame 取最新帧跑 MediaPipe。
        实测单次推理约 200ms 且与输入分辨率无关（内部会缩到固定尺寸），
        放在这里跑就不会拖慢采集/预览；只做纯计算并写入 self._gest_out，
        绝不触碰任何 Tk 组件。"""
        hands = None
        try:
            # 模型初始化 + 预热推理放在 stderr 屏蔽窗口内，吞掉首次加载日志
            with _suppress_native_stderr():
                hands = self.mp_hands.Hands(max_num_hands=1,
                                            model_complexity=GEST_MODEL_COMPLEXITY)
                hands.process(np.zeros((240, 320, 3), dtype=np.uint8))
            while self.cam_on and self.cam_gen == gen:
                with self._gest_lock:
                    fr = self._gest_frame
                    self._gest_frame = None
                if fr is None:
                    time.sleep(0.005)              # 还没新帧，稍等
                    continue
                fh, fw = fr.shape[:2]
                if 0 < CAM_PROC_W < fw:
                    small = cv2.resize(fr, (CAM_PROC_W, max(1, int(fh * CAM_PROC_W / fw))),
                                       interpolation=cv2.INTER_AREA)
                else:
                    small = fr
                try:
                    result = hands.process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
                except Exception:
                    continue
                value, frozen, lm, palm = self._parse_hands(result, fw, fh)
                with self._gest_lock:
                    self._gest_out = (value, frozen, lm, palm)
        finally:
            if hands is not None:
                hands.close()                  # 释放 MediaPipe 实例

    def _parse_hands(self, result, fw, fh):
        """解析推理结果 → (value, frozen, landmarks, palm_detected)
        landmarks 为归一化坐标（None=未检测到手），绘制交给采集线程。
        手势语义：
          - 拇指-食指距离   → 调节亮度（10~90%），按原帧像素距离换算
          - 中指抬起(12>10) → 锁定当前亮度
          - 手掌全开        → 暂停调光（frozen），不再触发退出——
                              退出手势模式一律走界面的「手动滑块」按钮"""
        if not result.multi_hand_landmarks:
            return None, False, None, False
        lm = result.multi_hand_landmarks[0].landmark
        # 中指抬起检测：中指尖(12) 明显高于中指近指关节(10) → 锁定
        locked = lm[MIDDLE_TIP].y < lm[MIDDLE_PIP].y - LOCK_MARGIN
        # 手掌全开检测：食指/中指/无名指/小指指尖均高于各自 PIP
        palm = (lm[INDEX_TIP].y < lm[INDEX_PIP].y
                and lm[MIDDLE_TIP].y < lm[MIDDLE_PIP].y
                and lm[RING_TIP].y < lm[RING_PIP].y
                and lm[PINKY_TIP].y < lm[PINKY_PIP].y)
        # 归一化坐标 → 原帧像素距离（与旧实现完全一致）
        dx = (lm[INDEX_TIP].x - lm[THUMB_TIP].x) * fw
        dy = (lm[INDEX_TIP].y - lm[THUMB_TIP].y) * fh
        dist = math.hypot(dx, dy)
        value = int(np.interp(dist, [DIST_MIN, DIST_MAX], [DUTY_MIN, DUTY_MAX]))
        return value, (locked or palm), lm, palm

    def _draw_overlay(self, frame, lm, value):
        """在预览帧上绘制手势叠加层（采集线程内执行，纯 cv2，开销 1~2ms）
        归一化坐标按当前帧尺寸换算，与推理时用的分辨率无关。"""
        h, w = frame.shape[:2]
        pts = [(int(p.x * w), int(p.y * h)) for p in lm]
        for a, b in self.mp_hands.HAND_CONNECTIONS:     # 骨架连线
            cv2.line(frame, pts[a], pts[b], (0, 180, 0), 1)
        for p in pts:
            cv2.circle(frame, p, 2, (0, 255, 255), -1)
        x1, y1 = pts[THUMB_TIP]
        x2, y2 = pts[INDEX_TIP]
        cv2.line(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.circle(frame, ((x1 + x2) // 2, (y1 + y2) // 2), 5, (0, 0, 255), -1)

        locked = lm[MIDDLE_TIP].y < lm[MIDDLE_PIP].y - LOCK_MARGIN
        # 中文提示开销较大（PIL 往返），限频到约 0.5s 一次
        now = time.time()
        if now - self._hint_ts > 0.5:
            self._hint_ts = now
            _draw_cn_text(frame, "捏合调光  中指=锁定  张开=暂停", (10, 10),
                          (255, 255, 255), 20)
        if locked:
            cv2.putText(frame, "LOCKED  %d%%" % value, (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3, cv2.LINE_AA)
            cv2.rectangle(frame, (10, 90), (220, 100), (0, 0, 255), -1)
        else:
            cv2.putText(frame, "Light: %d%%" % value, (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            bar_h = int(np.interp(value, [DUTY_MIN, DUTY_MAX], [0, 200]))
            cv2.rectangle(frame, (10, 100), (40, 300), (255, 255, 255), 1)
            cv2.rectangle(frame, (10, 300 - bar_h), (40, 300), (0, 255, 0), -1)

    def _poll_camera(self):
        """主线程：取工作线程最新帧 → 节流下发 + 界面刷新（只做轻量工作）"""
        if self.cam_on:
            try:
                frame, value, frozen = self.gesture_queue.get_nowait()
            except queue.Empty:
                pass
            else:
                if not frozen and value is not None:
                    self._gesture_update(value)
                # 显示最新帧（图片转换与 Tk 操作只能在主线程做）
                fh, fw = frame.shape[:2]
                # 画面比例变化超 1% → 更新比例并重排预览区（16:9/4:3 摄像头都适配）
                ratio = fh / fw
                if abs(ratio - self._video_ratio) > 0.01:
                    self._video_ratio = ratio
                    self._update_cam_body(self._cam.winfo_width(),
                                          self._cam.winfo_height())
                # 按预览区实际尺寸精确 fit 缩放（同比例、不变形、不留大黑边）
                bw = max(self.video_body.winfo_width(), 10)
                bh = max(self.video_body.winfo_height(), 10)
                s = min(bw / fw, bh / fh)
                tw, th = max(1, int(fw * s)), max(1, int(fh * s))
                # 先用 cv2（NEON 优化）缩到预览尺寸，再交给 PIL——
                # 比让 PIL 在大图上 resize 快数倍（ARM 板上尤其明显）
                if tw < fw or th < fh:
                    frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                self.photo = ImageTk.PhotoImage(img)
                self.video.config(image=self.photo, text="")
        self.root.after(30, self._poll_camera)

    def _gesture_update(self, value):
        """手势亮度（主线程执行）：变化 ≥1% 且距上次 ≥0.15s 才发，界面同步"""
        now = time.time()
        if (abs(value - self.last_sent) >= GEST_SEND_DELTA
                and now - self.last_send_time >= GEST_SEND_INTERVAL):
            if self.ser is not None:
                try:
                    self.ser.write(bytes([FRAME_HEAD, value, FRAME_TAIL]))
                except serial.SerialException:
                    return
            self.last_sent = value
            self.last_send_time = now
            self._log("info", f"手势调光: {value}%")
        self._set_slider_silent(value)
        self._update_brightness_ui(value)

    #=================== 退出清理 ===================

    def _on_close(self):
        """关窗清理：停止摄像头工作线程、关闭串口、结束 mpv"""
        self.cam_on = False                 # 工作线程循环退出并自行释放摄像头
        if self.music is not None:
            try:
                self.music.close()
            except Exception:
                pass
        if self.ser is not None:
            self.ser.close()
        self.root.destroy()


if __name__ == "__main__":
    ctk.set_appearance_mode("light")   # 浅色磨砂玻璃主题（背景是浅色照片）
    root = ctk.CTk()
    DeskLampApp(root)
    root.mainloop()
