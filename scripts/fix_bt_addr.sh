#!/bin/bash
# ============================================================
# 修复 Purple Pi OH「蓝牙重启后连不上」问题
#
# 【根因】/usr/sbin/bt-setup 启动蓝牙时用了 --bd_addr_rand，
#   让蓝牙适配器地址**每次开机随机生成**；而 BlueZ 是按
#   /var/lib/bluetooth/<适配器地址>/<设备地址>/ 存放配对信息的，
#   适配器地址一变，之前配对好的 HC-05 就"查无此设备"，
#   连接时报 Host is down（Errno 112）。
#   这正好解释：重启前好好的，重启后"没连接上蓝牙"。
#
# 【怎么做】把 --bd_addr_rand 换成固定地址，重启后地址不再变，
#   配对信息就稳定了。脚本会自动备份原文件。
#
# 【用法】（在板子上执行）
#   sudo bash ~/fix_bt_addr.sh
#   然后 reboot（必须重启，patchram 才会用新参数重新加载）
#
# 验证：
#   hciconfig -a hci0 | head -2     # 地址应显示为固定值，且每次开机都一样
# ============================================================

FIXMAC="${1:-13:75:71:4D:ED:0A}"     # 用今天成功配对时用过的地址，也可自己改
SETUP=/usr/sbin/bt-setup

if [ "$(id -u)" != "0" ]; then
    echo "请用 sudo 运行：sudo bash $0"
    exit 1
fi
if [ ! -f "$SETUP" ]; then
    echo "找不到 $SETUP"
    exit 1
fi

echo "=== 0) 当前状态（请拍照/记录，方便对比）==="
hciconfig -a hci0 2>/dev/null | head -2
echo "--- BlueZ 存储目录 ---"
ls -l /var/lib/bluetooth/ 2>/dev/null
echo "--- 存储树 ---"
find /var/lib/bluetooth -maxdepth 2 2>/dev/null | head -15

echo
echo "=== 1) 备份 $SETUP ==="
BAK="$SETUP.bak.$(date +%Y%m%d%H%M%S)"
cp -n "$SETUP" "$BAK" && echo "已备份到：$BAK"

echo
echo "=== 2) 原始关键行 ==="
grep -n "patchram\|bd_addr" "$SETUP" | head -5

if grep -q -- "--bd_addr_rand" "$SETUP"; then
    sed -i "s/--bd_addr_rand/--bd_addr $FIXMAC/" "$SETUP"
    echo
    echo "=== 3) 已替换 --bd_addr_rand → --bd_addr $FIXMAC ==="
else
    echo
    echo "=== 3) 未发现 --bd_addr_rand（可能已改过），跳过替换 ==="
fi
echo "--- 修改后关键行 ---"
grep -n "patchram\|bd_addr" "$SETUP" | head -5

echo
echo "=== 4) 重启蓝牙服务 ==="
systemctl restart bluetooth 2>/dev/null || true
echo "完成。"

cat <<'TIP'

============================================================
下一步（按顺序做）：

1) 重启板子，让新参数生效：
     sudo reboot

2) 重启后检查适配器地址是否已固定：
     hciconfig -a hci0 | head -2

3) 若 HC-05 仍连不上（说明配对信息确实丢了），在板子上重新配对一次：
     nmcli dev disconnect wlan0            # 关 WiFi（共存会干扰扫描）
     bluetoothctl
       power on
       agent on
       default-agent
       scan on                             # 等出现 HC-05，记下 MAC
       pair AA:BB:CC:DD:EE:FF              # PIN 输入 1234
       trust AA:BB:CC:DD:EE:FF
       quit
     nmcli con up id "<你的WiFi名称>"      # 恢复 WiFi（名字按实际）

4) 启动台灯程序：
     bash ~/bthost/run_lamp.sh
     然后看日志：tail -f ~/bt_bridge.log   应出现「蓝牙已连接 ✓」

5) 以后再重启板子，配对和地址都不会再丢，直接跑第 4 步即可。
============================================================
TIP
