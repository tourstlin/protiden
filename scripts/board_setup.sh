#!/bin/bash
# ============================================================
# Purple Pi OH（RK3566 / Ubuntu 20.04 aarch64）台灯上位机 · 板端初始化脚本
#
# 做什么：
#   1) 装系统包：python3-pip / python3-venv / python3-tk / lrzsz / v4l-utils
#   2) 装 Miniforge（aarch64），建 Python 3.10 环境 —— 系统自带 3.8 装不了 MediaPipe
#   3) 装 Python 依赖：customtkinter / pyserial / opencv-python / mediapipe / pillow
#   4) 时区设为 Asia/Shanghai、CPU 调频设为 performance
#   5) 依赖自检并打印结果
#
# 用法（sudo 密码从标准输入读一行，不写进日志）：
#   printf '密码\n' | bash ~/board_setup.sh 2>&1 | tee ~/setup.log
# 实时查看（另开一个终端/另一个 Xshell 页签）：
#   tail -f ~/setup.log
#
# 说明：本脚本可重复执行（幂等）；已存在的 Miniforge 与 conda 环境不会重装。
# ============================================================

set -u

MF_DIR="$HOME/miniforge3"
ENV_NAME="lamp"
PY_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
MF_URL_CN="https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Linux-aarch64.sh"
MF_URL_GH="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh"

read -r SUDO_PW || SUDO_PW=""
sudo_do() { echo "$SUDO_PW" | sudo -S -p '' "$@"; }
step() { echo; echo "########## $* ##########"; }

step "0/7 环境信息"
echo "user=$(whoami)  home=$HOME  arch=$(uname -m)  kernel=$(uname -r)"
echo "系统自带 python3: $(python3 -V 2>&1)"
echo "根分区可用: $(df -h / | tail -1 | awk '{print $4}')"

step "1/7 安装系统包"
sudo_do apt-get update -qq
sudo_do apt-get install -y python3-pip python3-venv python3-tk lrzsz v4l-utils
dpkg -l 2>/dev/null | grep -E "^ii  (python3-tk|python3-venv|python3-pip|lrzsz|v4l-utils)" | awk '{print "  [已装]", $2, $3}'

step "2/7 下载 Miniforge (aarch64)"
cd "$HOME" || exit 1
if [ ! -f Miniforge3-Linux-aarch64.sh ]; then
    echo "→ 尝试清华镜像 ..."
    curl -fL --retry 2 -m 600 -o Miniforge3-Linux-aarch64.sh "$MF_URL_CN" \
        || { echo "→ 镜像失败，改用 GitHub ..."; curl -fL --retry 2 -m 600 -o Miniforge3-Linux-aarch64.sh "$MF_URL_GH"; }
fi
ls -lh Miniforge3-Linux-aarch64.sh 2>/dev/null || echo "!! 脚本下载失败，请检查网络"

step "3/7 安装 Miniforge 到 $MF_DIR"
if [ ! -x "$MF_DIR/bin/conda" ]; then
    bash Miniforge3-Linux-aarch64.sh -b -p "$MF_DIR" >/dev/null
fi
"$MF_DIR/bin/conda" --version 2>&1 | head -1

step "4/7 创建 Python 3.10 环境 [$ENV_NAME]"
if ! "$MF_DIR/bin/conda" env list 2>/dev/null | grep -qE "^$ENV_NAME[[:space:]]"; then
    "$MF_DIR/bin/conda" create -y -n "$ENV_NAME" python=3.10
fi
PY="$MF_DIR/envs/$ENV_NAME/bin/python"
echo "环境解释器: $PY"
"$PY" -V

step "5/7 安装 Python 依赖（清华源）"
"$PY" -m pip install --upgrade pip -i "$PY_MIRROR"
"$PY" -m pip install customtkinter pyserial opencv-python "mediapipe==0.10.14" pillow -i "$PY_MIRROR" \
    || { echo "→ 带 GUI 的 opencv 装不上，退回 headless 版（本项目不需要 cv2 窗口，够用）"; \
         "$PY" -m pip install customtkinter pyserial opencv-python-headless "mediapipe==0.10.14" pillow -i "$PY_MIRROR"; }
# conda 的 python 默认可能没带 tkinter，缺则补上
"$PY" -c "import tkinter" 2>/dev/null || "$MF_DIR/bin/conda" install -y -n "$ENV_NAME" -c conda-forge tk

step "6/7 时区与 CPU 调频"
sudo_do timedatectl set-timezone Asia/Shanghai
sudo_do sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$f" 2>/dev/null; done'
echo "时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "调频: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"

step "7/7 依赖自检"
"$PY" - <<'PYEOF'
import sys
print("Python:", sys.version.split()[0])
ok = True
for m in ["tkinter", "cv2", "mediapipe", "customtkinter", "serial", "PIL"]:
    try:
        mod = __import__(m)
        print("  OK    %-12s %s" % (m, getattr(mod, "__version__", "")))
    except Exception as e:
        ok = False
        print("  FAIL  %-12s %s" % (m, e))
print("自检结果:", "全部通过" if ok else "有缺失（见上面 FAIL 行）")
PYEOF

echo
echo "########## 脚本执行结束 ##########"
echo "启动程序命令："
echo "  DISPLAY=:0 $PY ~/desk_lamp_gui_linux.py"
