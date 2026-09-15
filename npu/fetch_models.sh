#!/usr/bin/env bash
# ============================================================
# 下载 NPU 模型与运行时（不随仓库分发，各自有独立的许可）
#
#   ./npu/fetch_models.sh              # 下载到 npu/models 与 npu/runtime
#   GITHUB_PROXY=https://ghproxy.net/https://github.com ./npu/fetch_models.sh
#
# 说明：
#   · 模型来自社区工程 w13364/rknn-mediapipe（作者未声明开源许可，
#     因此本仓库不分发这些文件，只提供下载指引；商业使用请先联系原作者）
#   · 原始模型是 Google MediaPipe 的 palm_detection / hand_landmark（Apache-2.0）
#   · librknnrt.so 来自 Rockchip 官方 airockchip/rknn-toolkit2
# ============================================================
set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="$HERE/models"
RUNTIME="$HERE/runtime"
PROXY="${GITHUB_PROXY:-}"
GH="${PROXY}https://github.com"

mkdir -p "$MODELS" "$RUNTIME"

dl() {  # dl <url> <输出路径>
    echo "→ $(basename "$2")"
    curl -fL --connect-timeout 15 --retry 2 -o "$2" "$1" \
        || { echo "   ✘ 下载失败：$1"; echo "   可尝试设置 GITHUB_PROXY 走镜像"; return 1; }
    echo "   ✔ $(du -h "$2" | cut -f1)"
}

echo "=== 1/2 手部模型（palm detection + 21 点 landmark）==="
BASE="$GH/w13364/rknn-mediapipe/raw/master/models"
dl "$BASE/hand_detector.rknn"           "$MODELS/hand_detector.rknn"           || true
dl "$BASE/hand_landmarks_detector.rknn" "$MODELS/hand_landmarks_detector.rknn" || true
# 原始 tflite（想自己用 rknn-toolkit2 重新量化成 INT8 时用得上）
dl "$BASE/hand_detector.tflite"           "$MODELS/hand_detector.tflite"           || true
dl "$BASE/hand_landmarks_detector.tflite" "$MODELS/hand_landmarks_detector.tflite" || true

echo ""
echo "=== 2/2 RKNN 运行时 librknnrt.so（Rockchip 官方）==="
# 板端只用 .so；x86 上要转模型的话装 rknn-toolkit2（只有 x86_64 wheel）
dl "$GH/airockchip/rknn-toolkit2/raw/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so" \
   "$RUNTIME/librknnrt.so" || true

echo ""
echo "=== 结果 ==="
ls -la "$MODELS" "$RUNTIME" 2>/dev/null
cat <<'TIP'

下一步：
  · 板端把手势后端切到 NPU：  LAMP_INFER=npu ./scripts/protaiden-start.sh
  · 检查模型是否被正确加载：  LAMP_NPU_DEBUG=1 ./scripts/protaiden-start.sh
  · 若想自己转 INT8 模型（更快）：见 docs/npu-migration.md
TIP
