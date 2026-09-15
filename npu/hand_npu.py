# -*- coding: utf-8 -*-
"""
hand_npu.py — RK3566 NPU(RKNN) 手部推理层

设计目标：**对外接口与 mediapipe.solutions.hands 完全兼容**，这样主程序
(desk_lamp_gui_linux.py) 只需要替换实例化那一行：

    hands = mp.solutions.hands.Hands(max_num_hands=1, model_complexity=1)
    ↓
    from hand_npu import Hands
    hands = Hands(max_num_hands=1)

其余代码（_parse_hands / _draw_overlay / 解耦线程 / 蓝牙 / UI）一律不用动。

    res = hands.process(rgb_ndarray)   # 输入必须是 RGB（uint8）
    res.multi_hand_landmarks           # None 或 [HandLandmarks]
    hlm.landmark[i].x / .y / .z        # 归一化到 [0,1]（相对传入帧）
    hands.close()

模型（放在本文件同级的 models/ 目录，或用环境变量指定）：
    hand_detector.rknn            192x192, 2016 anchors, 输出 reg(2016,18) + cls(2016,)
    hand_landmarks_detector.rknn  224x224, 输出 landmarks(63) + 手性 + 置信度

------------------------------------------------------------------------------
相对参考实现 (w13364/rknn-mediapipe 的 rknn.py) 修正的问题
------------------------------------------------------------------------------
1. 双重 BGR→RGB：参考实现内部又做了一次 cvtColor，但调用方传入的已是 RGB。
   本实现直接按 RGB 处理，不再转换。
2. 关键点漏乘 224：参考实现把模型输出的 [0,1] 归一化坐标当成像素坐标直接做
   逆仿射变换，坐标会整体缩小 224 倍。本实现先 ×224 再逆变换。
3. 丢弃置信度：参考实现把 score 硬编码为 1.0。本实现从模型输出中取回真值，
   低于阈值的检测直接判为"未检测到手"。
4. 每帧 print + cv2.imwrite：参考实现每次推理都打印十几行并写 jpg 文件，
   在板子上是灾难。本实现默认静默（DEBUG=True 时才输出）。
5. 多余的阈值遍历：参考实现对 5 个阈值各遍历一遍 2016 个 anchor 只为打印统计。
   本实现只做一次向量化筛选。
6. 锚框/解码/打分全部改为 numpy 向量化（原来是 2016 次 Python 循环 + dict）。
7. WNMS 被简化成"只取最高分"。本实现做标准 NMS（按 IoU 抑制），
   并在 max_num_hands 内返回多个手。
"""

import os
import math

import cv2
import numpy as np

try:
    from rknnlite.api import RKNNLite          # 官方 rknn-toolkit-lite2（如果装了）
except ImportError:
    try:
        from rknn_lite_ctypes import RKNNLite  # 本目录自带的 ctypes 实现（板子上用的是这个）
    except ImportError:
        RKNNLite = None


# --------------------------------------------------------------------------
# 参数（与 MediaPipe 官方 hand_detector / hand_landmark 保持一致）
# --------------------------------------------------------------------------
DET_SIZE = 192
LM_SIZE = 224
NUM_ANCHORS = 2016
NUM_COORDS = 18
NUM_KEYPOINTS = 7
SCORE_CLIPPING_THRESH = 100.0

DET_SCORE_THRESH = 0.75       # 手掌检测分数阈值（MediaPipe 默认）
NMS_IOU_THRESH = 0.3          # 抑制阈值（MediaPipe min_suppression_threshold）
ROI_SCALE = 2.6               # 关键点阶段的 ROI 放大系数（MediaPipe 默认 2.6）
LM_PRESENCE_THRESH = 0.5      # 关键点置信度下限

DEBUG = os.environ.get("LAMP_NPU_DEBUG", "0") == "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("LAMP_NPU_MODEL_DIR", os.path.join(_HERE, "models"))
DET_MODEL_PATH = os.environ.get("LAMP_DET_MODEL", os.path.join(MODEL_DIR, "hand_detector.rknn"))
LM_MODEL_PATH = os.environ.get("LAMP_LM_MODEL", os.path.join(MODEL_DIR, "hand_landmarks_detector.rknn"))

# 21 点骨架连线（与 mediapipe HAND_CONNECTIONS 一致）
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),           # 拇指
    (0, 5), (5, 6), (6, 7), (7, 8),           # 食指
    (5, 9), (9, 10), (10, 11), (11, 12),      # 中指
    (9, 13), (13, 14), (14, 15), (15, 16),    # 无名指
    (13, 17), (17, 18), (18, 19), (19, 20),   # 小指
    (0, 17),                                  # 掌根
]


def _log(*a):
    if DEBUG:
        print("[hand_npu]", *a)


# --------------------------------------------------------------------------
# 与 mediapipe 对齐的结果容器
# --------------------------------------------------------------------------
class _Landmark(object):
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class _HandLandmarks(object):
    __slots__ = ("landmark", "score")

    def __init__(self, pts, score=1.0):
        self.landmark = [_Landmark(x, y, z) for (x, y, z) in pts]
        self.score = float(score)


class _Result(object):
    __slots__ = ("multi_hand_landmarks",)

    def __init__(self, hands):
        self.multi_hand_landmarks = hands or None


# --------------------------------------------------------------------------
# 锚框：按 MediaPipe SSD 配置生成（顺序必须与模型输出严格对应）
# --------------------------------------------------------------------------
def _build_anchors():
    cfg = {
        "min_scale": 0.1484375,
        "max_scale": 0.75,
        "input_size": DET_SIZE,
        "anchor_offset_x": 0.5,
        "anchor_offset_y": 0.5,
        "strides": [8, 16, 16, 16],
        "aspect_ratios": [1.0],
        "interpolated_scale_aspect_ratio": 1.0,
        "fixed_anchor_size": True,
    }
    strides = cfg["strides"]
    n_layers = len(strides)

    def calc_scale(idx):
        if n_layers == 1:
            return (cfg["max_scale"] + cfg["min_scale"]) * 0.5
        return cfg["min_scale"] + (cfg["max_scale"] - cfg["min_scale"]) * idx / (n_layers - 1.0)

    xs, ys, ws, hs = [], [], [], []
    layer_id = 0
    while layer_id < n_layers:
        scales, ars = [], []
        last = layer_id
        while last < n_layers and strides[last] == strides[layer_id]:
            scale = calc_scale(last)
            for ar in cfg["aspect_ratios"]:
                ars.append(ar)
                scales.append(scale)
            if cfg["interpolated_scale_aspect_ratio"] > 0.0:
                scale_next = 1.0 if last == n_layers - 1 else calc_scale(last + 1)
                scales.append(math.sqrt(scale * scale_next))
                ars.append(cfg["interpolated_scale_aspect_ratio"])
            last += 1

        a_h, a_w = [], []
        for i in range(len(ars)):
            r = math.sqrt(ars[i])
            a_h.append(scales[i] / r)
            a_w.append(scales[i] * r)

        stride = strides[layer_id]
        fm = int(math.ceil(cfg["input_size"] / stride))
        for y in range(fm):
            for x in range(fm):
                for i in range(len(a_h)):
                    xs.append((x + cfg["anchor_offset_x"]) / fm)
                    ys.append((y + cfg["anchor_offset_y"]) / fm)
                    ws.append(1.0 if cfg["fixed_anchor_size"] else a_w[i])
                    hs.append(1.0 if cfg["fixed_anchor_size"] else a_h[i])
        layer_id = last

    return (np.asarray(xs, np.float32), np.asarray(ys, np.float32),
            np.asarray(ws, np.float32), np.asarray(hs, np.float32))


# --------------------------------------------------------------------------
# 手掌检测
# --------------------------------------------------------------------------
class _PalmDetector(object):
    def __init__(self):
        self.rknn = None
        self._ax, self._ay, self._aw, self._ah = _build_anchors()
        assert self._ax.size == NUM_ANCHORS, "锚框数量异常: %d" % self._ax.size

    def load(self):
        if RKNNLite is None:
            raise RuntimeError("未找到 rknnlite（板端需 pip3 install rknn-toolkit-lite2）")
        if not os.path.exists(DET_MODEL_PATH):
            raise RuntimeError("模型不存在: %s" % DET_MODEL_PATH)
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(DET_MODEL_PATH)
        if ret != 0:
            raise RuntimeError("load_rknn 失败: %s (%d)" % (DET_MODEL_PATH, ret))
        ret = self.rknn.init_runtime()
        if ret != 0:
            raise RuntimeError("init_runtime 失败 (%d)：请确认 NPU 驱动与 librknnrt 版本" % ret)
        _log("手掌检测模型就绪")

    @staticmethod
    def _letterbox(rgb):
        h, w = rgb.shape[:2]
        scale = min(DET_SIZE / float(w), DET_SIZE / float(h))
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((DET_SIZE, DET_SIZE, 3), np.uint8)
        pad_l, pad_t = (DET_SIZE - nw) // 2, (DET_SIZE - nh) // 2
        canvas[pad_t:pad_t + nh, pad_l:pad_l + nw] = resized
        return canvas, scale, pad_l, pad_t

    def detect(self, rgb):
        """返回 [(xmin, ymin, xmax, ymax, score, [(kx,ky)*7]), ...]（原图坐标）"""
        canvas, scale, pad_l, pad_t = self._letterbox(rgb)
        inp = np.expand_dims(canvas.astype(np.float32) / 255.0, 0)   # RGB, NHWC
        outs = self.rknn.inference(inputs=[inp])
        if not outs or len(outs) < 2:
            return []

        # 输出顺序自适应：reg 是 (2016,18)，cls 是 (2016,)
        a, b = np.asarray(outs[0]).reshape(-1), np.asarray(outs[1]).reshape(-1)
        if a.size == NUM_ANCHORS * NUM_COORDS:
            reg, cls = np.asarray(outs[0]).reshape(NUM_ANCHORS, NUM_COORDS), a
            if cls.size != NUM_ANCHORS:
                cls = b
        elif b.size == NUM_ANCHORS * NUM_COORDS:
            reg, cls = np.asarray(outs[1]).reshape(NUM_ANCHORS, NUM_COORDS), a
        else:
            _log("输出形状异常: %s / %s" % (np.asarray(outs[0]).shape, np.asarray(outs[1]).shape))
            return []
        if cls.size != NUM_ANCHORS:
            return []

        # 分数：sigmoid
        score = 1.0 / (1.0 + np.exp(-np.clip(cls.astype(np.float32),
                                             -SCORE_CLIPPING_THRESH, SCORE_CLIPPING_THRESH)))
        keep = np.where(score >= DET_SCORE_THRESH)[0]
        if keep.size == 0:
            return []

        # 解码（向量化）
        r = reg[keep]
        cx = r[:, 0] / DET_SIZE * self._aw[keep] + self._ax[keep]
        cy = r[:, 1] / DET_SIZE * self._ah[keep] + self._ay[keep]
        bw = r[:, 2] / DET_SIZE * self._aw[keep]
        bh = r[:, 3] / DET_SIZE * self._ah[keep]
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)  # 归一化 0~1
        sc = score[keep]

        # 按分数排序 + 标准 NMS
        order = np.argsort(-sc)
        picked = []
        for i in order:
            if len(picked) >= 4:
                break
            bi = boxes[i]
            if all(self._iou(bi, boxes[j]) <= NMS_IOU_THRESH for j in picked):
                picked.append(i)

        h, w = rgb.shape[:2]
        out = []
        for i in picked:
            x1 = (boxes[i][0] * DET_SIZE - pad_l) / scale
            y1 = (boxes[i][1] * DET_SIZE - pad_t) / scale
            x2 = (boxes[i][2] * DET_SIZE - pad_l) / scale
            y2 = (boxes[i][3] * DET_SIZE - pad_t) / scale
            x1, x2 = max(0.0, min(x1, w)), max(0.0, min(x2, w))
            y1, y2 = max(0.0, min(y1, h)), max(0.0, min(y2, h))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            kps = []
            for k in range(NUM_KEYPOINTS):
                kx = (r[i][4 + k * 2] / DET_SIZE * self._aw[keep][i] + self._ax[keep][i]) * DET_SIZE
                ky = (r[i][5 + k * 2] / DET_SIZE * self._ah[keep][i] + self._ay[keep][i]) * DET_SIZE
                kps.append(((kx - pad_l) / scale, (ky - pad_t) / scale))
            out.append((x1, y1, x2, y2, float(sc[i]), kps))
        return out

    @staticmethod
    def _iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / ua if ua > 0 else 0.0

    def release(self):
        if self.rknn is not None:
            try:
                self.rknn.release()
            except Exception:
                pass
            self.rknn = None


# --------------------------------------------------------------------------
# 21 点关键点
# --------------------------------------------------------------------------
class _LandmarkDetector(object):
    def __init__(self):
        self.rknn = None

    def load(self):
        if RKNNLite is None:
            raise RuntimeError("未找到 rknnlite（板端需 pip install rknn-toolkit-lite2）")
        if not os.path.exists(LM_MODEL_PATH):
            raise RuntimeError("模型不存在: %s" % LM_MODEL_PATH)
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(LM_MODEL_PATH)
        if ret != 0:
            raise RuntimeError("load_rknn 失败: %s (%d)" % (LM_MODEL_PATH, ret))
        ret = self.rknn.init_runtime()
        if ret != 0:
            raise RuntimeError("init_runtime 失败 (%d)：请确认 NPU 驱动与 librknnrt 版本" % ret)
        _log("关键点模型就绪")

    def predict(self, rgb, box):
        """按 ROI 裁出正方形 → 推理 → 返回 (21 点原图像素坐标, 置信度) 或 (None, 0)"""
        h, w = rgb.shape[:2]
        x1, y1, x2, y2 = box[:4]
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        side = max(x2 - x1, y2 - y1) * ROI_SCALE

        # 仿射：把 ROI 正方形映射到 224x224
        src = np.asarray([[cx - side / 2, cy - side / 2],
                          [cx + side / 2, cy - side / 2],
                          [cx - side / 2, cy + side / 2]], np.float32)
        dst = np.asarray([[0, 0], [LM_SIZE, 0], [0, LM_SIZE]], np.float32)
        m = cv2.getAffineTransform(src, dst)
        roi = cv2.warpAffine(rgb, m, (LM_SIZE, LM_SIZE))

        inp = np.expand_dims(roi.astype(np.float32) / 255.0, 0)
        outs = self.rknn.inference(inputs=[inp])
        if not outs:
            return None, 0.0

        lm63 = None
        singles = []
        for o in outs:
            v = np.squeeze(np.asarray(o)).astype(np.float32).reshape(-1)
            if v.size == 63 and lm63 is None:
                lm63 = v
            elif v.size == 1:
                singles.append(float(v[0]))
        if lm63 is None:
            return None, 0.0

        # 板端实测（真实手部图 hand_test.jpg）的输出顺序：
        #   out0 = 21 点(63)   out1 = **置信度 0.9971**   out2 = 手性 0.0018   out3 = 世界坐标(63)
        # 注意：这个转换版本的顺序和 MediaPipe 官方（landmarks/handedness/presence/world）不同，
        # 而且"手性"在右手时≈0.002 —— 若把它当置信度，会被判成"没检测到手"，手势直接失灵。
        # 两个单值输出取较大者：有手时置信度必然高，且不依赖两者的先后顺序。
        if singles:
            score = max(singles)
        else:
            score = 1.0

        # ★ 修正点 2：关键点坐标还原
        # MediaPipe 官方 hand_landmark 输出的是相对 ROI 的 [0,1] 归一化坐标，需 ×224；
        # 但个别转换版本直接给 [0,224] 像素坐标。按值域自适应判断，两种情况都对，
        # 免得因为模型版本差异导致 21 个点整体缩放 224 倍。
        pts_roi = lm63.reshape(21, 3).copy()
        span = float(np.max(np.abs(pts_roi[:, :2])))
        k = LM_SIZE if span <= 2.0 else 1.0
        _log("关键点值域 max=%.3f -> 缩放系数 %.1f" % (span, k))
        pts_roi[:, 0] *= k
        pts_roi[:, 1] *= k

        # 逆仿射映射回原图，再归一化到 [0,1]
        mi = cv2.invertAffineTransform(m)
        out = []
        for px, py, pz in pts_roi:
            ox = mi[0, 0] * px + mi[0, 1] * py + mi[0, 2]
            oy = mi[1, 0] * px + mi[1, 1] * py + mi[1, 2]
            out.append((max(0.0, min(ox / w, 1.0)), max(0.0, min(oy / h, 1.0)), float(pz) / LM_SIZE))
        return out, score

    def release(self):
        if self.rknn is not None:
            try:
                self.rknn.release()
            except Exception:
                pass
            self.rknn = None


# --------------------------------------------------------------------------
# 对外接口：与 mp.solutions.hands.Hands 对齐
# --------------------------------------------------------------------------
class Hands(object):
    """用法与 mediapipe 一致：

        hands = Hands(max_num_hands=1)
        res = hands.process(rgb)          # rgb: uint8, 形状 (H,W,3)
        if res.multi_hand_landmarks:
            for p in res.multi_hand_landmarks[0].landmark:
                ...  p.x, p.y, p.z   # 归一化 0~1
        hands.close()
    """

    def __init__(self, max_num_hands=1, model_complexity=1, detect_every=None, **_kw):
        self.max_num_hands = max(1, int(max_num_hands))
        self._det = _PalmDetector()
        self._lm = _LandmarkDetector()
        self._ready = False
        self.last_score = 0.0

        # ---- 检测节流 ----
        # 实测：手掌检测 43.6ms / 关键点 30.9ms，检测是主要开销。
        # 而手在连续帧里移动很小 → 每 N 帧才重新全图找手，
        # 中间帧直接用上一帧的 21 点反推 ROI（跟随手部移动，比复用旧框更准）。
        # 预估：43.6/3 + 30.9 ≈ 45ms → 约 22fps，可跑满采集链路的 15fps 上限。
        self._detect_every = max(1, int(detect_every if detect_every is not None
                                        else os.environ.get("LAMP_DETECT_EVERY", "3")))
        # 输出平滑（指数移动平均系数，1.0 = 不平滑）：抑制 21 点逐帧抖动
        self._smooth = float(os.environ.get("LAMP_SMOOTH", "0.6"))
        self._ema = None
        self._frame_i = 0
        self._last_box = None        # 上一帧的检测框
        self._last_pts = None        # 上一帧的 21 点（归一化）
        self._miss = 0               # 连续未检出计数
        self.detect_count = 0        # 统计：真正跑了几次全图检测

    def _ensure(self):
        if not self._ready:
            self._det.load()
            self._lm.load()
            self._ready = True

    def process(self, rgb):
        if rgb is None:
            return _Result(None)
        if rgb.ndim == 2:
            rgb = cv2.cvtColor(rgb, cv2.COLOR_GRAY2RGB)
        try:
            self._ensure()
            self._frame_i += 1
            hands = []
            for d in self._candidate_boxes(rgb)[:self.max_num_hands]:
                pts, score = self._lm.predict(rgb, d)
                if pts is None or score < LM_PRESENCE_THRESH:
                    continue
                hands.append(_HandLandmarks(pts, score))

            if hands:
                # 指数移动平均平滑：压制逐帧抖动（LAMP_SMOOTH=1.0 可关闭）
                if 0.0 < self._smooth < 1.0:
                    cur = [(p.x, p.y, p.z) for p in hands[0].landmark]
                    if self._ema is None:
                        self._ema = cur
                    else:
                        a = self._smooth
                        self._ema = [(a * c[0] + (1 - a) * e[0],
                                      a * c[1] + (1 - a) * e[1],
                                      a * c[2] + (1 - a) * e[2])
                                     for c, e in zip(cur, self._ema)]
                    hands[0] = _HandLandmarks(self._ema, hands[0].score)
                self._last_pts = hands[0].landmark
                self._miss = 0
            else:
                self._ema = None
                self._miss += 1
                if self._miss >= 3:        # 连续丢手 → 清缓存，下帧强制全图重检
                    self._last_box = None
                    self._last_pts = None
            self.last_score = hands[0].score if hands else 0.0
            return _Result(hands)
        except Exception as e:
            if DEBUG:
                import traceback
                traceback.print_exc()
            else:
                print("[hand_npu] 推理异常: %s" % e, file=__import__("sys").stderr)
            return _Result(None)

    # ---------------- 检测节流 ----------------
    def _candidate_boxes(self, rgb):
        """返回本帧要送进关键点模型的候选框列表"""
        need_detect = (self._detect_every <= 1
                       or self._last_box is None
                       or (self._frame_i % self._detect_every) == 0)
        if need_detect:
            self.detect_count += 1
            dets = self._det.detect(rgb)
            if dets:
                self._last_box = dets[0]
                return dets
            # 这一帧偶发漏检：先留用上次的框继续跟（不该因此丢一帧），
            # 真正的"手已离开"由 _miss 连续计数兜底清空。
            return [self._last_box] if self._last_box else []

        # 非检测帧：直接复用上次的检测框（位置最多滞后 detect_every 帧，换来的是构图稳定）
        # ⚠️ 踩坑记录：原先用"上一帧 21 点的包围盒"当 ROI，但那个尺度与检测框不一致
        # （landmark 模型期望的是"手掌框 ×2.6 倍"那种构图）。结果同一张图连续推理时，
        # 检测帧与非检测帧的结果来回跳变——实测逐点极差 326 像素，肉眼就是"关键点乱跳"。
        return [self._last_box] if self._last_box else []

    def close(self):
        self._lm.release()
        self._det.release()
        self._ready = False
