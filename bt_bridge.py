#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蓝牙 RFCOMM → 本地 TCP 桥接（跑在系统 python3 上）

为什么需要它：
  Purple Pi OH 的内核没有编译 CONFIG_BT_RFCOMM_TTY，所以没有 /dev/rfcomm0；
  只能直接用内核 RFCOMM socket 连蓝牙。而 conda 版 Python 3.10 编译时没带
  socket.AF_BLUETOOTH，**系统自带的 /usr/bin/python3(3.8) 才支持**。
  于是用系统 python 起这个桥：监听 127.0.0.1:8888，把字节原样转发到 HC-05。
  台灯 GUI 里选 socket://127.0.0.1:8888 就能间接使用蓝牙。

断线自愈（v2）：
  - 蓝牙链路掉线（RFCOMM 断开 / 超时 / EOF）→ **保持 TCP 会话不断**，自动重连 HC-05
  - 客户端（GUI）断开 → 等下一个客户端接入
  这样 GUI 端完全无感知，不需要用户重新点「连接」。

用法：
  /usr/bin/python3 bt_bridge.py [MAC] [监听端口]
  MAC 也可来自环境变量 LAMP_BT_MAC 或文件 ~/.lamp_bt_mac
日志：stdout（run_lamp.sh 会重定向到 ~/bt_bridge.log）
"""
import os
import socket
import select
import sys
import time

CHANNEL = 1


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def close_quiet(sock):
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


def read_mac():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    env = os.environ.get("LAMP_BT_MAC", "").strip()
    if env:
        return env
    try:
        with open(os.path.expanduser("~/.lamp_bt_mac"), "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def bt_connect(mac, channel=CHANNEL, timeout=20):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                      socket.BTPROTO_RFCOMM)
    s.settimeout(timeout)
    s.connect((mac, channel))
    s.settimeout(None)          # 阻塞模式，靠 select 驱动
    return s


def client_alive(cli):
    """客户端是否还连着（用 peek 判断 EOF，不消费数据）"""
    try:
        return bool(cli.recv(1, socket.MSG_PEEK))
    except BlockingIOError:
        return True
    except Exception:
        return False


def serve_client(cli, addr, mac):
    """服务一个 GUI 连接；蓝牙掉线时保持会话并自动重连"""
    log("客户端接入 %s" % (addr,))
    bt = None
    try:
        while True:
            # ---- 确保蓝牙链路 ----
            if bt is None:
                try:
                    bt = bt_connect(mac)
                    log("蓝牙已连接 ✓")
                except Exception as e:
                    log("蓝牙连接失败：%s: %s （3 秒后重试）"
                        % (type(e).__name__, e))
                    r, _, _ = select.select([cli], [], [], 3.0)
                    if r and not client_alive(cli):
                        log("客户端已断开")
                        return
                    continue

            # ---- 双向转发 ----
            try:
                r, _, _ = select.select([cli, bt], [], [], 1.0)
            except Exception as e:
                log("select 异常：%s" % e)
                return

            if cli in r:
                try:
                    data = cli.recv(4096)
                except Exception:
                    data = b""
                if not data:
                    log("客户端断开")
                    return
                try:
                    bt.sendall(data)
                except Exception as e:
                    log("写蓝牙失败：%s: %s → 重连蓝牙" % (type(e).__name__, e))
                    close_quiet(bt)
                    bt = None

            if bt is not None and bt in r:
                try:
                    data = bt.recv(4096)
                except Exception as e:
                    log("读蓝牙失败：%s: %s → 重连蓝牙" % (type(e).__name__, e))
                    close_quiet(bt)
                    bt = None
                    continue
                if not data:
                    log("蓝牙链路断开（EOF）→ 重连蓝牙")
                    close_quiet(bt)
                    bt = None
                    continue
                try:
                    cli.sendall(data)
                except Exception as e:
                    log("回传客户端失败：%s" % e)
                    return
    finally:
        close_quiet(bt)


def serve_forever(mac, host="127.0.0.1", port=8888):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log("桥接启动：监听 %s:%d  →  蓝牙 %s 通道 %d" % (host, port, mac, CHANNEL))
    while True:
        cli, addr = srv.accept()
        try:
            serve_client(cli, addr, mac)
        except Exception as e:
            log("会话异常：%s: %s" % (type(e).__name__, e))
        finally:
            close_quiet(cli)
        log("等待下一个客户端 ...")


def main():
    if not (hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")):
        log("!! 当前 Python 不支持蓝牙 socket（AF_BLUETOOTH）")
        log("   请用系统解释器运行：/usr/bin/python3 bt_bridge.py")
        return 1
    mac = read_mac()
    if not mac:
        log("!! 没有拿到蓝牙 MAC：请 ./run_lamp.sh 自动传入，或设置 LAMP_BT_MAC，")
        log("   或写入 ~/.lamp_bt_mac（一行 MAC）")
        return 1
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8888
    while True:
        try:
            serve_forever(mac, port=port)
        except KeyboardInterrupt:
            log("收到中断，退出")
            return 0
        except Exception as e:
            log("桥接异常（5 秒后重启）：%s: %s" % (type(e).__name__, e))
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
