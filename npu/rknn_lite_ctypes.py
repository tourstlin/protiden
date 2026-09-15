# -*- coding: utf-8 -*-
"""
rknn_lite_ctypes.py — 纯 ctypes 实现的 RKNNLite（替代 rknn-toolkit-lite2 的 Python 包）

为什么需要它：
    板子（Ubuntu 20.04 / aarch64 / Python 3.8）拿不到 rknn_toolkit_lite2 的 wheel
    （官方只发 GitHub Release，而 GitHub 在本机和板子都不可达）。
    但 librknnrt.so 本身是好的（已实测 rknn_init 返回 0），
    所以直接按 rknn_api.h 用 ctypes 调 C API，完全绕开 pip 包。

接口与官方 rknnlite.api.RKNNLite 保持一致：
    r = RKNNLite()
    r.load_rknn("model.rknn")
    r.init_runtime()
    outs = r.inference(inputs=[arr])     # arr: float32, NHWC
    r.release()

结构体与常量取自 rknn-toolkit2 v2.3.2 的 rknpu2/runtime/Linux/librknn_api/include/rknn_api.h
"""

import ctypes
import glob
import os

import numpy as np

# ---- 常量（rknn_api.h v2.3.2）----
RKNN_QUERY_IN_OUT_NUM = 0
RKNN_QUERY_SDK_VERSION = 5
RKNN_TENSOR_FLOAT32 = 0
RKNN_TENSOR_NCHW = 0
RKNN_TENSOR_NHWC = 1

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_CANDIDATES = [
    os.environ.get("RKNN_RT_LIB"),
    "/usr/lib/librknnrt.so",
    "/usr/local/lib/librknnrt.so",
    os.path.join(_HERE, "runtime", "librknnrt_v2.3.2.so"),
    os.path.join(_HERE, "runtime", "librknnrt.so"),
]


def _find_lib():
    for p in _LIB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for pat in ("/usr/lib/**/librknnrt.so", "/usr/local/lib/**/librknnrt.so"):
        hit = glob.glob(pat, recursive=True)
        if hit:
            return hit[0]
    raise OSError("找不到 librknnrt.so：请放到 runtime/ 下，或设 RKNN_RT_LIB 指向它")


# ---- C 结构体 ----
class _IO_NUM(ctypes.Structure):
    _fields_ = [("n_input", ctypes.c_uint32), ("n_output", ctypes.c_uint32)]


class _SDKVer(ctypes.Structure):
    _fields_ = [("api_version", ctypes.c_char * 256), ("drv_version", ctypes.c_char * 256)]


class _Input(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("type", ctypes.c_int),
        ("fmt", ctypes.c_int),
    ]


class _Output(ctypes.Structure):
    _fields_ = [
        ("want_float", ctypes.c_uint8),
        ("is_prealloc", ctypes.c_uint8),
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
    ]


class RKNNLite(object):
    """与 rknnlite.api.RKNNLite 兼容的最小子集"""

    def __init__(self):
        self._lib = None
        self._ctx = ctypes.c_uint64(0)
        self._model = None
        self._n_in = 1
        self._n_out = 1
        self._ready = False

    # ------------------------------------------------------------------
    def _bind(self):
        lib = ctypes.CDLL(_find_lib())
        lib.rknn_init.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        lib.rknn_init.restype = ctypes.c_int
        lib.rknn_query.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.rknn_query.restype = ctypes.c_int
        lib.rknn_inputs_set.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.POINTER(_Input)]
        lib.rknn_inputs_set.restype = ctypes.c_int
        lib.rknn_run.argtypes = [ctypes.c_uint64, ctypes.c_void_p]
        lib.rknn_run.restype = ctypes.c_int
        lib.rknn_outputs_get.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                                         ctypes.POINTER(_Output), ctypes.c_void_p]
        lib.rknn_outputs_get.restype = ctypes.c_int
        lib.rknn_outputs_release.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                                             ctypes.POINTER(_Output)]
        lib.rknn_outputs_release.restype = ctypes.c_int
        lib.rknn_destroy.argtypes = [ctypes.c_uint64]
        lib.rknn_destroy.restype = ctypes.c_int
        self._lib = lib

    # ------------------------------------------------------------------
    def load_rknn(self, path, **_kw):
        """读入 .rknn 文件（保持在内存里，rknn_init 需要 buffer 存活）"""
        if self._lib is None:
            self._bind()
        with open(path, "rb") as f:
            data = f.read()
        self._model = ctypes.create_string_buffer(data, len(data))
        return 0

    def init_runtime(self, target=None, **_kw):
        if self._lib is None:
            self._bind()
        if self._model is None:
            raise RuntimeError("请先调用 load_rknn()")
        ret = self._lib.rknn_init(ctypes.byref(self._ctx), self._model,
                                  len(self._model.raw), 0, None)
        if ret != 0:
            return ret
        io = _IO_NUM()
        if self._lib.rknn_query(self._ctx, RKNN_QUERY_IN_OUT_NUM,
                                ctypes.byref(io), ctypes.sizeof(io)) == 0:
            self._n_in, self._n_out = int(io.n_input), int(io.n_output)
        self._ready = True
        return 0

    def inference(self, inputs, data_format=None):
        """inputs: [np.ndarray(float32, NHWC)]  →  返回 [np.ndarray(float32)]"""
        if not self._ready:
            raise RuntimeError("请先调用 init_runtime()")
        n = len(inputs)
        arr = (_Input * n)()
        holders = []                     # 必须持有引用，防止 GC 掉输入缓冲
        for i, x in enumerate(inputs):
            a = np.ascontiguousarray(x, dtype=np.float32)
            holders.append(a)
            arr[i].index = i
            arr[i].buf = a.ctypes.data_as(ctypes.c_void_p)
            arr[i].size = a.nbytes
            arr[i].pass_through = 0
            arr[i].type = RKNN_TENSOR_FLOAT32
            arr[i].fmt = RKNN_TENSOR_NHWC

        ret = self._lib.rknn_inputs_set(self._ctx, n, arr)
        if ret != 0:
            raise RuntimeError("rknn_inputs_set 失败 ret=%d" % ret)
        ret = self._lib.rknn_run(self._ctx, None)
        if ret != 0:
            raise RuntimeError("rknn_run 失败 ret=%d" % ret)

        outs = (_Output * self._n_out)()
        for i in range(self._n_out):
            outs[i].want_float = 1       # 让 runtime 统一输出 float32
            outs[i].index = i
        ret = self._lib.rknn_outputs_get(self._ctx, self._n_out, outs, None)
        if ret != 0:
            raise RuntimeError("rknn_outputs_get 失败 ret=%d" % ret)

        result = []
        for i in range(self._n_out):
            cnt = int(outs[i].size) // 4
            ptr = ctypes.cast(outs[i].buf, ctypes.POINTER(ctypes.c_float))
            result.append(np.ctypeslib.as_array(ptr, shape=(cnt,)).copy())
        self._lib.rknn_outputs_release(self._ctx, self._n_out, outs)
        return result

    def release(self):
        if self._lib is not None and self._ctx.value:
            try:
                self._lib.rknn_destroy(self._ctx)
            except Exception:
                pass
            self._ctx = ctypes.c_uint64(0)
        self._ready = False

    # 诊断用
    def sdk_version(self):
        v = _SDKVer()
        if self._lib is not None and self._ctx.value and self._lib.rknn_query(
                self._ctx, RKNN_QUERY_SDK_VERSION, ctypes.byref(v), ctypes.sizeof(v)) == 0:
            return (v.api_version.decode(errors="replace"),
                    v.drv_version.decode(errors="replace"))
        return (None, None)

    def n_outputs(self):
        return self._n_out
