#!/bin/bash
# ============================================================
# ProTaiden 智能台灯控制台 · 启动脚本（Purple Pi OH）
#
# 用法：
#   ./run_lamp.sh          日常模式：系统 Python 3.8 + 原生蓝牙
#                          （Tk 带 Xft → 中文字体清晰；不需要桥接进程）
#   ./run_lamp.sh cam      手势模式：conda Python（MediaPipe）+ 蓝牙桥接 + 摄像头
#                          （conda 的 Tk 无 Xft，字体会偏糊，属已知限制）
#   ./run_lamp.sh --stop   停止全部相关进程
#
# 为什么分两个解释器：
#   - conda Python 3.10 的 Tk 8.6.13 未编译 Xft，所有字体退化成位图字体（马赛克感）
#   - 系统 /usr/bin/python3 (3.8) 的 Tk 8.6.10 带 Xft，字体锐利；且支持 socket.AF_BLUETOOTH
#     可以直连 HC-05（本板内核没编译 RFCOMM TTY，没有 /dev/rfcomm0）
#   - 手势模式必须要 MediaPipe，只能跑 conda Python，于是用 bt_bridge.py 转发蓝牙
#
# 蓝牙 MAC 存在 ~/.lamp_bt_mac（一行），当前：AA:BB:CC:DD:EE:FF
# ============================================================

H="$HOME"
BTHOST="$H/bthost"
SYS_PY="/usr/bin/python3"
CONDA_PY="$H/miniforge3/envs/lamp/bin/python"
APP="$BTHOST/desk_lamp_gui_linux.py"
BRIDGE="$BTHOST/bt_bridge.py"
MAC_FILE="$H/.lamp_bt_mac"
BRIDGE_PORT=8888
LOG="$H/lamp.log"

is_running() { pgrep -f "$1" > /dev/null; }

# ---------------- 停止 ----------------
if [ "${1:-}" = "--stop" ]; then
    pkill -f "[d]esk_lamp_gui_linux.py" && echo "已停止台灯程序" || echo "台灯程序未运行"
    pkill -f "[b]t_bridge.py" && echo "已停止蓝牙桥接" || echo "蓝牙桥接未运行"
    exit 0
fi

BT_MAC=$(tr -d ' \r\n' < "$MAC_FILE" 2>/dev/null)
MODE="${1:-nocam}"
export DISPLAY=:0

if [ "$MODE" = "cam" ]; then
    # ============ 手势模式：conda Python + 桥接 ============
    [ -x "$CONDA_PY" ] || { echo "!! 找不到 conda 解释器 $CONDA_PY"; exit 1; }
    if [ -n "$BT_MAC" ] && [ -f "$BRIDGE" ]; then
        if is_running "[b]t_bridge.py"; then
            echo "蓝牙桥接已在运行（$BT_MAC）"
        else
            echo "启动蓝牙桥接：$BT_MAC → 127.0.0.1:$BRIDGE_PORT"
            setsid nohup "$SYS_PY" -u "$BRIDGE" "$BT_MAC" "$BRIDGE_PORT" \
                > "$H/bt_bridge.log" 2>&1 < /dev/null &
            sleep 4
            is_running "[b]t_bridge.py" && echo "  ✔ 桥接就绪" || { echo "  !! 桥接失败："; tail -8 "$H/bt_bridge.log"; }
        fi
        export LAMP_BT_MAC="$BT_MAC"
        export LAMP_BRIDGE="socket://127.0.0.1:$BRIDGE_PORT"
        export LAMP_PORT="socket://127.0.0.1:$BRIDGE_PORT"
    fi
    unset LAMP_NO_CAMERA
    PY="$CONDA_PY"
    # ★ 关键：把系统 Tcl/Tk（带 Xft 抗锯齿）预载进来，覆盖 conda 里那个没编 Xft 的版本。
    #   不加这行 conda Python 下所有字体都会退化成位图字体（发糊/马赛克）。
    export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libtk8.6.so:/usr/lib/aarch64-linux-gnu/libtcl8.6.so"
    # 摄像头同一时刻只能被一个进程占用：先关掉 gst 预览
    pkill -f "[g]st-launch-1.0" > /dev/null 2>&1
    # MIPI(RKISP mainpath) = /dev/video0 = 索引 0（默认即可）
    # USB 摄像头插上后索引通常是 3 开始，可用 CAM_INDEX=3 ./run_lamp.sh cam
    echo "模式：手势（conda Python + MediaPipe，已预载系统 Tk 字体）"
    echo "摄像头索引 CAM_INDEX=${CAM_INDEX:-0}（MIPI=/dev/video0=0；USB 通常从 3 起）"
else
    # ============ 日常模式：系统 Python + 原生蓝牙 ============
    pkill -f "[b]t_bridge.py" > /dev/null 2>&1    # 不需要桥接，顺手清掉
    export LAMP_NO_CAMERA=1
    if [ -n "$BT_MAC" ]; then
        export LAMP_BT_MAC="$BT_MAC"
        export LAMP_PORT="bt:$BT_MAC"            # 原生 RFCOMM 直连
        echo "模式：无摄像头（系统 Python，字体清晰）→ 蓝牙直连 $BT_MAC"
    else
        echo "模式：无摄像头（未配置 ~/.lamp_bt_mac，请在界面里手动选串口）"
    fi
    PY="$SYS_PY"
fi

# ---------------- 启动 GUI ----------------
pkill -f "[d]esk_lamp_gui_linux.py" > /dev/null 2>&1
sleep 1
setsid nohup "$PY" -u "$APP" > "$LOG" 2>&1 < /dev/null &
sleep 6
if is_running "[d]esk_lamp_gui_linux.py"; then
    echo "GUI 已启动（解释器 $PY）"
    echo "日志：$LOG    实时面板：bash $BTHOST/watch_logs.sh"
else
    echo "!! GUI 启动失败，日志："
    tail -20 "$LOG"
fi
