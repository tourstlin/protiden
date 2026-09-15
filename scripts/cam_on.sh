#!/bin/bash
# 打开 MIPI 摄像头（OV5648）实时预览 —— 在板子屏幕上显示
# 用法: bash scripts/cam_on.sh [宽] [高]     默认 1280x720
export DISPLAY=:0
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/aarch64-linux-gnu/gstreamer-1.0
W=${1:-1280}
H=${2:-720}
# 需要 root 的操作走 sudo -n（免密），详见 README
echo "=== 打开 MIPI 摄像头预览 ${W}x${H} ==="

# 1) 确保 sensor 驱动已绑定（开机 probe 偶尔会因上电时序失败）
if [ ! -e /sys/bus/i2c/devices/4-0036/driver ]; then
    echo "  sensor 未绑定，重新绑定中..."
    sudo -n sh -c 'echo 4-0036 > /sys/bus/i2c/drivers/ov5648/bind'
    sleep 4
fi
echo "  sensor: $(basename $(readlink -f /sys/bus/i2c/devices/4-0036/driver 2>/dev/null) 2>/dev/null || echo 未绑定)"

# 2) 关掉旧的预览
pkill -f "gst-launch-1.0" 2>/dev/null
sleep 2

# 3) 启动预览（必须带 videoconvert，xvimagesink 直接吃不下 NV12）
setsid nohup gst-launch-1.0 v4l2src device=/dev/video0 \
    ! video/x-raw,format=NV12,width=$W,height=$H \
    ! videoconvert ! ximagesink \
    > /tmp/cam_preview.log 2>&1 < /dev/null &

sleep 8
if pgrep -f "gst-launch-1.0" >/dev/null; then
    echo "  ✔ 预览已在板子屏幕上打开"
else
    echo "  ✘ 启动失败："
    tail -5 /tmp/cam_preview.log
fi
