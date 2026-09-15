# -*- coding: utf-8 -*-
"""从徽章图中提取 logo 主体（去掉外圈蓝环与环绕文字），输出透明背景 PNG。

思路：
  1) 按比例裁出中间区域（避开外环和"追逐梦想/物联网工程"环绕文字）
  2) 用 flood fill 从四角清除白底 —— 只清与外部连通的白，
     logo 内部的白色（字母 e、椭圆高光）会保留
  3) 按 alpha 通道再收一次边界，去掉多余留白
"""
import os

import numpy as np
import cv2
from PIL import Image

import sys
# 用法：python3 make_logo.py <徽章图片路径> [输出目录]
SRC = sys.argv[1] if len(sys.argv) > 1 else "badge.jpg"
OUT_DIR = r"D:\新建文件夹 (3)\2026-09-08-08-36-36\bthost\assets"

img = Image.open(SRC).convert("RGB")
W, H = img.size
print("原图: %dx%d" % (W, H))

# 1) 按比例裁出主体（起点下移以避开左上角残留的装饰元素）
l, r = int(W * 0.267), int(W * 0.716)
t, b = int(H * 0.288), int(H * 0.792)
crop = np.array(img)[t:b, l:r].copy()
print("裁剪区: x[%d,%d] y[%d,%d] -> %dx%d" % (l, r, t, b, crop.shape[1], crop.shape[0]))

# 2) 去白底（只清除与四角连通的部分）
h, w = crop.shape[:2]
gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
white = (gray > 238).astype(np.uint8)
mask = np.zeros((h + 2, w + 2), np.uint8)
ff = white.copy()
for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
    cv2.floodFill(ff, mask, seed, 2)
bg = (ff == 2)

alpha = np.where(bg, 0, 255).astype(np.uint8)
out = np.dstack([crop, alpha])

# 3) 收紧边界
ys, xs = np.where(~bg)
y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
out = out[y0:y1 + 1, x0:x1 + 1]
print("主体 bbox -> %dx%d" % (out.shape[1], out.shape[0]))

os.makedirs(OUT_DIR, exist_ok=True)
p = os.path.join(OUT_DIR, "logo.png")
Image.fromarray(out, "RGBA").save(p)
print("已保存:", p, os.path.getsize(p), "bytes")

# ======================================================================
# 第二版：完整圆形徽章（裁成圆形 + 三片白底全部透明）
#   徽章结构：外蓝环 → 白圈 → 细蓝环 → 内白圆(含图形)
#   需要变透明的白有三片：圆外背景、白圈、内白圆
#   而 logo 内部的白色（字母 e 等小连通域）必须保留
# ======================================================================
full = np.array(img)
fgray = cv2.cvtColor(full, cv2.COLOR_RGB2GRAY)
# 用"每行/每列的非白像素数量"定边界：单点噪点不会影响判断，
# 否则原图边缘的 JPEG 伪影会把 bbox 拉宽，导致圆被裁成椭圆
fmask = (fgray < 220).astype(np.uint8)
# 取最大连通域 = 徽章本体。图片边缘零散的伪影/描边是独立的小连通域，会被自动排除
# （之前用"每列非白数 > 8"的判据，被图片最边缘那 10 个杂散像素骗了，导致 bbox 撑满全宽）
_n, _lab, _stats, _ = cv2.connectedComponentsWithStats(fmask, 8)
_bi = 1 + int(np.argmax(_stats[1:, 4]))
bx0, by0, _bw, _bh = (int(v) for v in _stats[_bi][:4])
bx1, by1 = bx0 + _bw - 1, by0 + _bh - 1
print("最大连通域面积 = %d（占比 %.1f%%）"
      % (_stats[_bi][4], 100.0 * _stats[_bi][4] / fmask.size))
badge = full[by0:by1 + 1, bx0:bx1 + 1].copy()
bh, bw = badge.shape[:2]
print("徽章外接框: %dx%d" % (bw, bh))

# 白色连通域标记
wg = cv2.cvtColor(badge, cv2.COLOR_RGB2GRAY)
white = (wg > 238).astype(np.uint8)
n_lab, lab = cv2.connectedComponents(white, 8)
corner = int(lab[0, 0])                        # 与四角连通的 = 圆外背景
areas = sorted(((i, int((lab == i).sum())) for i in range(1, n_lab)),
               key=lambda t: -t[1])
inners = [i for i, _ in areas if i != corner][:2]   # 最大的两片内部白：白圈 + 内白圆
kill = set([corner] + inners)
transparent = np.isin(lab, list(kill))
print("透明化白域 label: %s（圆外=%d, 内部=%s）" % (sorted(kill), corner, inners))

# 圆形蒙版（顺带把圆外的残余切掉，半径略收防止白边）
ccx, ccy = bw // 2, bh // 2
rad = int(min(bw, bh) / 2 * 0.997)
Y, X = np.ogrid[:bh, :bw]
inside = (X - ccx) ** 2 + (Y - ccy) ** 2 <= rad * rad

alpha = np.where(inside & (~transparent), 255, 0).astype(np.uint8)
badge_rgba = np.dstack([badge, alpha])
p2 = os.path.join(OUT_DIR, "logo_circle.png")
Image.fromarray(badge_rgba, "RGBA").save(p2)
print("已保存:", p2, os.path.getsize(p2), "bytes")
