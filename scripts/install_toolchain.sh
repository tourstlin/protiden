#!/bin/bash
# 安装 V3.0 编译工具链：build-essential + cmake（结果实时进 agent_console.log）
LOG=$HOME/agent_console.log
# 需要 root 的操作走 sudo -n（免密），详见 README
exec > >(tee -a "$LOG") 2>&1

echo ""
echo "########## [$(date '+%H:%M:%S')] 安装 V3.0 编译工具链 ##########"

echo "--- 1) 网络连通性 ---"
ping -c 2 -W 3 mirrors.tuna.tsinghua.edu.cn >/dev/null 2>&1 && echo "  外网 OK ✓" || echo "  ⚠ 外网不通（apt 可能失败）"

echo "--- 2) 换清华源（arm64 ports，加速）---"
sudo -n cp -n /etc/apt/sources.list /etc/apt/sources.list.bak 2>/dev/null
sudo -n sed -i 's|http://ports.ubuntu.com|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' /etc/apt/sources.list
grep -ac tuna /etc/apt/sources.list

echo "--- 3) apt update ---"
sudo -n apt-get update 2>&1 | tail -3

echo "--- 4) 安装 build-essential + cmake ---"
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential cmake 2>&1 | tail -4

echo "--- 5) 验证 ---"
echo "  gcc:    $(gcc --version 2>/dev/null | head -1 || echo '未安装')"
echo "  g++:    $(g++ --version 2>/dev/null | head -1 || echo '未安装')"
echo "  cmake:  $(cmake --version 2>/dev/null | head -1 || echo '未安装')"
echo "  make:   $(make --version 2>/dev/null | head -1 || echo '未安装')"
echo "--- 6) 内核头文件（DRM 后端要用）---"
ls /usr/include/drm/drm.h 2>/dev/null && echo "  drm.h ✓" || echo "  drm.h 缺（可装 libdrm-dev，暂不阻塞）"
echo "########## 完成 [$(date '+%H:%M:%S')] ##########"
