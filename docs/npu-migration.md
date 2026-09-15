# 把 MediaPipe 手部推理迁到 RK3566 NPU

面向"想把 MediaPipe Hands 搬到瑞芯微 NPU 上跑"的完整路线，含实测数据与踩坑记录。
本项目 `npu/` 目录是这条路线跑通后的成品。

## 为什么值得迁

| 指标 | MediaPipe（CPU/XNNPACK） | RKNN（NPU） | 变化 |
|---|---|---|---|
| 单帧推理 | 293 ms | **74.4 ms**（检测 43.6 + 关键点 30.9） | 3.9× |
| 开检测节流（每 3 帧全图找手） | — | **47.6 ms/帧**（≈21 fps） | 6.2× |
| 程序 CPU 占用 | 1.01 核 | **0.5 核** | 省半个 A55 |
| 手势响应延迟 | ~300 ms（明显迟滞） | ~60 ms（基本无感） | — |

关键结论：**MediaPipe 的耗时与输入分辨率无关**（640 宽 209ms vs 160 宽 206ms），
因为内部会把图缩到模型固定输入尺寸。所以"缩画质换流畅"救不了它，只能换硬件。

## 路线（2 个模型，不需要手势分类器）

MediaPipe 的手势判定其实由 21 个关键点的规则完成，所以只要两个模型：

```
手掌检测 palm detection (192×192 → 2016 个锚框 + 分数/框)
        ↓ 取最高分框，外扩 2.6 倍 → 仿射变换到 224×224
关键点回归 hand landmark (224×224 → 21×3 关键点 [+ world] [+ presence/handedness])
        ↓ 逆仿射回原图 → 复用原有的捏合/锁定/暂停规则
```

## 步骤

### 1. 拿模型（两条路，建议都试）

- **现成 `.rknn`**：社区有转好的（本项目用 [w13364/rknn-mediapipe](https://github.com/w13364/rknn-mediapipe)）。
  省掉整个转换环境，直接跳到第 3 步。
  ⚠️ 该仓库**未声明许可证**，商业使用请先联系作者；本仓库因此不分发模型文件。
- **自己转**：从 `mediapipe` pip 包里取出 `palm_detection_lite.tflite` /
  `hand_landmark_lite.tflite`（`find $(python3 -c "import mediapipe,os;print(os.path.dirname(mediapipe.__file__))") -name "*.tflite"`），
  用 rknn-toolkit2 转：

```python
from rknn.api import RKNN
rknn = RKNN()
rknn.config(target_platform='rk3566', quantized_dtype='w8a8',
            mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]])
rknn.load_tflite(model='hand_landmark_lite.tflite')
rknn.build(do_quantization=True, dataset='calib.txt')   # INT8 需要 200~300 张真实手部图
rknn.export_rknn('hand_landmark.rknn')
```

> `rknn-toolkit2` **只有 Linux x86_64 wheel**（转换必须在 PC 上做，板子只能跑推理）。
> 想先验证流程通不通，可以先 `do_quantization=False` 出一版 FP16 模型跑通再说。

### 2. 板端环境（不需要 pip 装 rknn 包）

板子只需要 `librknnrt.so`——本项目的 `npu/rknn_lite_ctypes.py` 用 ctypes 直接调 C API：

```bash
./npu/fetch_models.sh        # 下载 librknnrt.so 与模型
# 或指定自己的：export RKNN_RT_LIB=/path/to/librknnrt.so
```

> 官方 `rknn-toolkit-lite2` 只有 GitHub Release 的 aarch64 wheel，网络不通时很难拿到。
> 实测 **NPU 驱动 v0.8.2 能直接加载 toolkit2 2.3.2 转的模型**（4 组运行时×模型组合
> 全部 `rknn_init → 0`），所以旧驱动不用急着升级。

### 3. 板端集成（主程序零改动）

`npu/run_npu.py` 在加载主程序**之前**替换掉手势后端：

```python
import mediapipe as mp
from hand_npu import Hands, HAND_CONNECTIONS      # 接口与 mp.solutions.hands 一致
mp.solutions.hands.Hands = Hands
mp.solutions.hands.HAND_CONNECTIONS = HAND_CONNECTIONS
runpy.run_path("desk_lamp_gui_linux.py", run_name="__main__")
```

主程序里手势入口只有 3 处（`self.mp_hands.Hands(...)`、`HAND_CONNECTIONS`），
所以 UI / 蓝牙 / 采集线程 / 手势规则全部原样复用，回退只要 `LAMP_INFER=mp`。

## 七个坑（按踩到的顺序）

1. **输出顺序与官方不同** —— 转出来是 `landmarks / presence / handedness / world`，
   官方是 `landmarks / handedness / presence / world`。按官方取第 2 个单值 → 拿到手性
   `0.0018` → 每帧判定"没检测到手"。**別假设顺序，打印出来看**；取法用
   `score = max(单值输出)` 更稳。
2. **关键点要不要 ×224** —— 有的版本输出 `[0,1]` 归一化，有的是 `[0,224]` 像素。
   别猜：打印值域，或做自适应（`<=1.5` 视为归一化）。
3. **检测节流的 ROI 必须与检测帧同构** —— 用"上一帧关键点包围盒"当 ROI，和"手掌框×2.6"
   构图不一致，两套结果来回切会让 21 个点**极差 326px**。检测/非检测帧复用**同一个框**。
   排查手段：**同一张图连续推理 10 次**（推理本该确定性，有波动就是输入不一致）。
4. **旧驱动兼容新模型** —— 不必为了模型去刷固件，先试着加载。
5. **官方 wheel 拿不到** —— 用 `rknn_lite_ctypes.py`，或想办法搞到 `librknnrt.so`。
6. **解释器不对** —— mediapipe/numpy 装在 conda 环境，系统 python3 里没有。
7. **性能大头不在 NPU** —— 剩下的 30~40ms 是 Python 侧预处理（letterbox/仿射）、
   2016×18 锚框解码、NMS、逆变换。已验证：320 与 640 输入耗时几乎一样，
   说明瓶颈是固定开销而非图像大小。想再压只能把后处理挪到 C。

## 精度验证方法

用 MediaPipe 当**参照真值**，同一帧、同一份 RGB 各跑一次，比 21 点像素误差：

```
>>> 与 MediaPipe 像素误差  平均 11.8  中位 10.8  最大 24.9   （640×480）
>>> 同一帧连续 10 次：命中 10/10
```

11.8px 的偏差远小于捏合手势的标定范围（30~250px），足够可用。
