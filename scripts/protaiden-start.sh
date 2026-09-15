#!/usr/bin/env bash
# ============================================================
# ProTaiden 启动脚本（板端 / 桌面图标 / 开机自启用这一个）
#
#   ./scripts/protaiden-start.sh                  启动（默认 NPU 后端）
#   LAMP_INFER=mp ./scripts/protaiden-start.sh    回退 MediaPipe(CPU) 后端
#   ./scripts/protaiden-start.sh --stop           停掉所有相关进程
#   LAMP_KIOSK=0  ./scripts/protaiden-start.sh    调试：保留标题栏
#   LAMP_HIDE_CURSOR=0 ./scripts/protaiden-start.sh  调试：显示鼠标指针
#
# 前置：模型与 librknnrt 用 npu/fetch_models.sh 下载；依赖见 README
# ============================================================
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LAMP_LOG_DIR:-$HOME}"
MAIN_LOG="$LOG_DIR/protaiden.log"
BRIDGE_LOG="$LOG_DIR/bt_bridge.log"
BRIDGE_PORT="${LAMP_BRIDGE_PORT:-8888}"
MAC_FILE="${LAMP_BT_MAC_FILE:-$HOME/.lamp_bt_mac}"
# 主程序跑在装了 mediapipe/opencv/customtkinter 的解释器里（多为 conda 环境）
PY="${LAMP_PY:-python3}"
# 蓝牙桥要用原生 socket.AF_BLUETOOTH —— conda 版 Python 通常没编，走系统解释器
BRIDGE_PY="${LAMP_BRIDGE_PY:-/usr/bin/python3}"

export DISPLAY="${DISPLAY:-:0}"

# conda 的 Tk 没编 Xft → 中文会变位图马赛克；借系统 Tk（有就用）
if [ -e /usr/lib/aarch64-linux-gnu/libtk8.6.so ]; then
    export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libtk8.6.so:/usr/lib/aarch64-linux-gnu/libtcl8.6.so"
fi

# ---- 手势推理后端 ----
export LAMP_INFER="${LAMP_INFER:-npu}"                # npu = RKNN 加速 / mp = MediaPipe(CPU)
export LAMP_DETECT_EVERY="${LAMP_DETECT_EVERY:-3}"    # 每 N 帧重新全图找手，中间帧只跑关键点
export LAMP_HIDE_CURSOR="${LAMP_HIDE_CURSOR:-1}"      # 触屏设备：隐藏鼠标指针
export LAMP_KIOSK="${LAMP_KIOSK:-1}"                  # 全屏，盖住任务栏与标题栏

# ---- 摄像头自动适配：有 USB UVC 就用它，否则用 MIPI ----
if [ -e /dev/video9 ]; then
    export CAM_INDEX=9 CAM_FOURCC=MJPG CAM_BUFSIZE=2
    CAM_DESC="USB UVC /dev/video9 · MJPG"
else
    export CAM_INDEX=0 CAM_FOURCC= CAM_BUFSIZE=4
    CAM_DESC="MIPI /dev/video0 · NV12"
fi
unset LAMP_NO_CAMERA

say() { echo "$@"; }

if [ "${1:-}" = "--stop" ]; then
    pkill -f "[r]un_npu.py"
    pkill -f "[d]esk_lamp_gui_linux.py"
    pkill -f "[b]t_bridge.py"
    sleep 1
    say "已停止 ProTaiden"
    exit 0
fi

say "=========================================="
say " ProTaiden 智能台灯启动中"
say "   摄像头  : $CAM_DESC"
say "   推理后端: $LAMP_INFER（每 $LAMP_DETECT_EVERY 帧重检）"
say "   仓库目录: $ROOT"
say "=========================================="

# 1) 停掉旧实例（避免两个 mpv / 两套摄像头抢占）
pkill -f "[r]un_npu.py" 2>/dev/null
pkill -f "[d]esk_lamp_gui_linux.py" 2>/dev/null
pkill -f "[b]t_bridge.py" 2>/dev/null
sleep 1

# 2) MIPI sensor 开机 probe 太早会失败，需要 unbind/bind 重绑一次。
#    这一步要 root：给这两条命令配 NOPASSWD 免密，或直接用 USB 摄像头（不需要 root）。
if [ "$CAM_INDEX" = "0" ]; then
    if sudo -n sh -c 'echo 4-0036 > /sys/bus/i2c/drivers/ov5648/unbind 2>/dev/null; sleep 0.2;
                      echo 4-0036 > /sys/bus/i2c/drivers/ov5648/bind 2>/dev/null' 2>/dev/null; then
        say "  MIPI sensor 已重绑"
    else
        say "  ⚠ 跳过 MIPI sensor 重绑（需 sudo 免密，见 README）
             MIPI 出不来画面时，先手动执行一次这条命令再启动。"
    fi
fi

# 3) 蓝牙桥：HC-05 ⟷ 本地 TCP（主程序用 socket://127.0.0.1:8888 连它）
if [ -s "$MAC_FILE" ]; then
    MAC="$(tr -d ' \r\n' < "$MAC_FILE")"
    setsid nohup "$BRIDGE_PY" -u "$ROOT/bt_bridge.py" "$MAC" "$BRIDGE_PORT" \
        > "$BRIDGE_LOG" 2>&1 < /dev/null &
    export LAMP_PORT="socket://127.0.0.1:$BRIDGE_PORT"
    say "  蓝牙桥接: $MAC → 127.0.0.1:$BRIDGE_PORT"
else
    say "  未配置蓝牙（$MAC_FILE 不存在，跳过；界面里可手动选串口/端口）"
fi

# 4) 启动主程序：npu/run_npu.py 会按 LAMP_INFER 把手势后端注入进去
setsid nohup "$PY" -u "$ROOT/npu/run_npu.py" > "$MAIN_LOG" 2>&1 < /dev/null &

sleep 10
if pgrep -f "[r]un_npu.py" > /dev/null; then
    say "  ✔ 已启动（$CAM_DESC）"
    say "    日志: $MAIN_LOG"
    grep -m1 "手势推理后端" "$MAIN_LOG" 2>/dev/null
else
    say "  ✘ 启动失败，末尾日志："
    tail -15 "$MAIN_LOG" 2>/dev/null
fi
