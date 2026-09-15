# -*- coding: utf-8 -*-
"""
run_npu.py —— 手势推理后端注入器

设计目标：**主程序 desk_lamp_gui_linux.py 一个字都不用改**。

理由：主程序里手势后端的入口只有 3 处，全都走同一个对象
    self.mp_hands = mp.solutions.hands          (第 714 行)
    self.mp_hands.Hands(...)                    (第 1607 行)
    self.mp_hands.HAND_CONNECTIONS              (第 1663 行)
所以只要在 import 主程序之前，把 mp.solutions.hands.Hands 换成 NPU 实现，
UI / 蓝牙 / 采集 / 手势规则 / 解耦线程 全部原样复用，也便于和 MediaPipe 后端做 A/B 对照。

回退：环境变量 LAMP_INFER=mp 即可回到 MediaPipe（用于 A/B 对比）。

用法：
    LAMP_INFER=npu python3 run_npu.py
"""

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)                      # ~/bthost
MAIN = os.path.join(PARENT, "desk_lamp_gui_linux.py")

for p in (HERE, PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

BACKEND = os.environ.get("LAMP_INFER", "npu").strip().lower()


def _inject_npu():
    import mediapipe as mp                          # 主程序还要用它的常量
    from hand_npu import Hands as NpuHands
    from hand_npu import HAND_CONNECTIONS as NpuConn

    mp.solutions.hands.Hands = NpuHands             # ★ 只替换这一个类
    mp.solutions.hands.HAND_CONNECTIONS = NpuConn   # 骨架定义统一（两边一致）
    print("[ProTaiden] 手势推理后端 = RKNN / NPU  "
          "(每 %s 帧重检)" % os.environ.get("LAMP_DETECT_EVERY", "3"), flush=True)


if BACKEND == "npu":
    try:
        _inject_npu()
    except Exception as exc:                        # 注入失败不让程序挂掉
        print("[ProTaiden] NPU 后端加载失败，回退 MediaPipe: %r" % (exc,), flush=True)
else:
    print("[ProTaiden] LAMP_INFER=%s，使用 MediaPipe 后端" % BACKEND, flush=True)

if not os.path.exists(MAIN):
    sys.exit("找不到主程序: %s" % MAIN)

sys.argv = [MAIN] + sys.argv[1:]
runpy.run_path(MAIN, run_name="__main__")
