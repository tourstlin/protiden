# -*- coding: utf-8 -*-
"""
ProTaiden 音乐播放引擎（板端）
==============================
用 mpv 的 JSON IPC 做后端（板子上 mpv / ffplay / gst-play 都预装了，mpv 最强）：
  · 不需要 pip 装任何东西（纯 stdlib：socket / json / subprocess / struct）
  · 播放/暂停/上下曲/拖动进度/音量，全部通过一个 unix socket 发 JSON 命令
  · 歌名/歌手/专辑/封面：自己解析 ID3v2（不依赖 mutagen）

对外接口（供 desk_lamp_gui_linux.py 调用）：
    p = MusicPlayer(music_dir)      # 建对象，不启动
    p.start()                       # 起 mpv 进程（幂等）
    p.play_index(i) / p.toggle() / p.next() / p.prev()
    p.seek(seconds) / p.set_volume(v)
    p.status()                      # -> dict（title/artist/playing/pos/dur/vol...）
    p.tracks                        # -> [Track, ...]
    p.close()                       # 退出时清理

环境变量：
    LAMP_MUSIC_DIR    音乐目录（默认 ~/Music）
    LAMP_MPV_DEVICE   mpv 音频设备（默认 auto；耳机口可设 alsa/plughw:1,0）
    LAMP_MPV_BIN      mpv 可执行文件（默认 mpv）
"""

import os
import io
import json
import time
import socket
import struct
import subprocess
import threading

HOME = os.path.expanduser("~")
DEFAULT_DIR = os.path.join(HOME, "Music")
SOCK_PATH = "/tmp/lamp-mpv.sock"

AUDIO_EXT = (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".ape")


# --------------------------------------------------------------------------
# ID3v2 标签解析（TIT2/TPE1/TALB/APIC），失败返回空 dict
# --------------------------------------------------------------------------
def _syncsafe(b):
    """ID3v2.4 的 syncsafe 整数：每字节只用低 7 位"""
    n = 0
    for x in b:
        n = (n << 7) | (x & 0x7F)
    return n


def _decode_text(raw, enc):
    if not raw:
        return ""
    try:
        if enc == 0:
            return raw.split(b"\x00")[0].decode("latin-1", "replace").strip()
        if enc == 1:
            return raw.split(b"\x00\x00")[0].decode("utf-16", "replace").strip("\x00").strip()
        if enc == 2:
            return raw.split(b"\x00\x00")[0].decode("utf-16-be", "replace").strip("\x00").strip()
        return raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
    except Exception:
        return ""


def read_id3(path):
    """解析 ID3v2 标签 -> {'title','artist','album','cover'(bytes|None)}"""
    out = {"title": "", "artist": "", "album": "", "cover": None}
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return out
            major = head[3]
            size = _syncsafe(head[6:10])
            body = f.read(size)
    except Exception:
        return out
    if not body:
        return out

    pos, end = 0, len(body)
    while pos + 6 <= end:
        if major == 2:                       # ID3v2.2：3 字节 ID + 3 字节长度
            fid = body[pos:pos + 3]
            if not fid.strip(b"\x00"):
                break
            fsz = int.from_bytes(body[pos + 3:pos + 6], "big")
            hdr = 6
        else:                                # v2.3 / v2.4
            fid = body[pos:pos + 4]
            if not fid.strip(b"\x00"):
                break
            raw_sz = body[pos + 4:pos + 8]
            fsz = _syncsafe(raw_sz) if major >= 4 else int.from_bytes(raw_sz, "big")
            hdr = 10
        if fsz <= 0 or pos + hdr + fsz > end:
            break
        data = body[pos + hdr: pos + hdr + fsz]
        pos += hdr + fsz

        try:
            if fid in (b"TIT2", b"TT2"):
                out["title"] = _decode_text(data[1:], data[0])
            elif fid in (b"TPE1", b"TP1"):
                out["artist"] = _decode_text(data[1:], data[0])
            elif fid in (b"TALB", b"TAL"):
                out["album"] = _decode_text(data[1:], data[0])
            elif fid in (b"APIC", b"PIC"):
                enc = data[0]
                rest = data[1:]
                if fid == b"PIC":            # v2.2：3 字节图片格式（如 JPG）
                    rest = rest[3:]
                else:                        # v2.3+：MIME 以 \x00 结尾
                    z = rest.find(b"\x00")
                    rest = rest[z + 1:] if z >= 0 else rest
                rest = rest[1:]              # 跳过图片类型字节
                sep = b"\x00\x00" if enc in (1, 2) else b"\x00"
                z = rest.find(sep)
                rest = rest[z + len(sep):] if z >= 0 else rest
                if rest[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                    out["cover"] = rest
        except Exception:
            continue
    return out


def _split_name(path):
    """文件名兜底：'宋冬野 - 董小姐.mp3' -> ('宋冬野', '董小姐')"""
    n = os.path.splitext(os.path.basename(path))[0]
    for sep in (" - ", "-", "–"):
        if sep in n:
            a, _, b = n.partition(sep)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return "", n


class Track(object):
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        tag = read_id3(path)
        if tag.get("title") or tag.get("artist"):
            self.title = tag.get("title") or _split_name(path)[1]
            self.artist = tag.get("artist") or "未知歌手"
        else:
            self.artist, self.title = _split_name(path)
            self.artist = self.artist or "未知歌手"
        self.album = tag.get("album", "")
        self.cover = tag.get("cover")            # bytes | None
        self._img = None                         # PIL 图像缓存

    def cover_image(self):
        """封面 -> PIL.Image（无封面返回 None）"""
        if self._img is not None:
            return self._img
        if not self.cover:
            return None
        try:
            from PIL import Image
            self._img = Image.open(io.BytesIO(self.cover)).convert("RGB")
        except Exception:
            self._img = None
        return self._img


def scan(music_dir):
    """扫描目录（递归）下所有音频文件 -> [Track, ...]（按文件名排序）"""
    out = []
    if not music_dir or not os.path.isdir(music_dir):
        return out
    for root, dirs, files in os.walk(music_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                p = os.path.join(root, fn)
                try:
                    out.append(Track(p))
                except Exception:
                    pass
    return out


# --------------------------------------------------------------------------
# mpv JSON IPC 客户端
# --------------------------------------------------------------------------
class MusicPlayer(object):
    def __init__(self, music_dir=None, mpv_bin=None, device=None,
                 volume=65, sock=None):
        self.music_dir = music_dir or os.environ.get("LAMP_MUSIC_DIR") or DEFAULT_DIR
        self.mpv_bin = mpv_bin or os.environ.get("LAMP_MPV_BIN", "mpv")
        self.device = device or os.environ.get("LAMP_MPV_DEVICE", "auto")
        self.sock_path = sock or SOCK_PATH
        self.volume = int(volume)

        self.tracks = scan(self.music_dir)
        self.index = -1
        self.available = False
        self.error = ""

        self._proc = None
        self._sock = None
        self._rid = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._events = []
        self._reader = None
        self._alive = True

    # ---------------- 生命周期 ----------------
    def _try_attach(self):
        """试着接管已经存在的 mpv（上一次运行残留但还活着）——避免重复起进程"""
        if not os.path.exists(self.sock_path):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect(self.sock_path)
            s.settimeout(None)
        except Exception:
            return False
        self._sock = s
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 问一句版本号，确认对面真的是 mpv（不是别的进程占着这个路径）
        if self._cmd("get_property", "mpv-version"):
            return True
        try:
            s.close()
        except Exception:
            pass
        self._sock = None
        return False

    def _kill_stale(self):
        """清掉上次遗留的 mpv（socket 文件可能已被 unlink，只能按命令行匹配）"""
        try:
            subprocess.run(["pkill", "-f", "input-ipc-server=%s" % self.sock_path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except Exception:
                pass

    def start(self):
        """启动 mpv（幂等）。返回 True/False"""
        if self.available:
            return True
        if not self.tracks:
            self.error = "音乐目录为空: %s" % self.music_dir
            return False
        if not self._which(self.mpv_bin):
            self.error = "未找到 mpv（apt install mpv）"
            return False

        # ★ 先尝试接管上一个进程遗留的 mpv：既省一次启动，也不会出现两个 mpv 同时出声
        if self._try_attach():
            self.available = True
            self.error = ""
            return True

        # 没有可复用的 → 把残留 socket 和孤儿进程清干净再起新的
        self._kill_stale()

        cmd = [self.mpv_bin,
               "--idle=yes",                 # 播完不退出，等着下一条命令
               "--no-video",
               "--no-terminal",
               "--really-quiet",
               "--input-ipc-server=%s" % self.sock_path,
               "--volume=%d" % self.volume,
               "--audio-display=no",
               "--gapless-audio=yes"]
        if self.device and self.device != "auto":
            cmd.append("--audio-device=%s" % self.device)
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except Exception as e:
            self.error = "启动 mpv 失败: %r" % (e,)
            return False

        # 等 socket 出现（最多 5 秒）
        t0 = time.time()
        while time.time() - t0 < 5.0:
            if os.path.exists(self.sock_path):
                try:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.settimeout(2.0)
                    self._sock.connect(self.sock_path)
                    self._sock.settimeout(None)
                    break
                except Exception:
                    self._sock = None
            if self._proc.poll() is not None:          # mpv 已退出
                self.error = "mpv 启动后立即退出（exit=%s）" % self._proc.returncode
                return False
            time.sleep(0.1)
        if self._sock is None:
            self.error = "连接 mpv IPC 超时"
            return False

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.available = True
        self.error = ""
        return True

    @staticmethod
    def _which(binary):
        if os.path.sep in binary:
            return os.path.exists(binary)
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, binary)
            if os.path.exists(p) and os.access(p, os.X_OK):
                return True
        return False

    def close(self):
        """退干净：关掉 IPC 并收掉 mpv（不管是本进程起的还是接管来的）"""
        self._alive = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
            else:
                # 接管来的进程没有 Popen 句柄，按命令行里的 socket 路径匹配收掉
                # （这个 socket 路径只有本程序会用，不会误伤别人的 mpv）
                self._kill_stale()
        except Exception:
            pass
        self._proc = None
        self.available = False

    # ---------------- IPC 底层 ----------------
    def _read_loop(self):
        buf = b""
        while self._alive and self._sock:
            try:
                chunk = self._sock.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if "request_id" in msg:
                    with self._lock:
                        self._pending[msg["request_id"]] = msg
                elif "event" in msg:
                    # 存整个事件体：end-file 带 reason（eof=自然播完，可据此自动下一曲）
                    with self._lock:
                        self._events.append(msg)
                        if len(self._events) > 64:
                            self._events = self._events[-64:]
        self.available = False

    def _cmd(self, *args, **kw):
        """发一条命令；wait=True 时等回复，返回 data（失败返回 None）"""
        wait = kw.get("wait", True)
        if not self._sock:
            return None
        with self._lock:
            self._rid += 1
            rid = self._rid
        payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
        try:
            self._sock.sendall(payload.encode("utf-8"))
        except Exception:
            return None
        if not wait:
            return True
        t0 = time.time()
        while time.time() - t0 < 1.5:
            with self._lock:
                msg = self._pending.pop(rid, None)
            if msg is not None:
                if msg.get("error") not in ("success", None):
                    return None
                return msg.get("data")
            time.sleep(0.005)
        return None

    def _get(self, prop):
        return self._cmd("get_property", prop)

    def take_event(self, name):
        """取出一个事件（有则返回 True 并移除）"""
        with self._lock:
            for i, ev in enumerate(self._events):
                if ev.get("event") == name:
                    self._events.pop(i)
                    return True
        return False

    def pop_events(self):
        """取出并清空事件列表（元素是 mpv 的完整事件字典）"""
        with self._lock:
            ev, self._events = self._events, []
        return ev

    def eof_count(self):
        """弹出一批事件里「自然播完」的次数（用于自动下一曲）"""
        n = 0
        for ev in self.pop_events():
            if ev.get("event") == "end-file" and ev.get("reason") == "eof":
                n += 1
        return n

    # ---------------- 播放控制 ----------------
    def play_index(self, i):
        if not self.tracks:
            return False
        i = i % len(self.tracks)
        if not self.available and not self.start():
            return False
        self.index = i
        self._cmd("loadfile", self.tracks[i].path, "replace")
        self._cmd("set_property", "pause", False)
        return True

    def toggle(self):
        if not self.available:
            return self.play_index(0 if self.index < 0 else self.index)
        if self._get("idle-active") is True:
            return self.play_index(0 if self.index < 0 else self.index)
        p = self._get("pause")
        self._cmd("set_property", "pause", not bool(p))
        return True

    def stop(self):
        self._cmd("stop")
        self.index = -1

    def next(self):
        return self.play_index(0 if self.index < 0 else self.index + 1)

    def prev(self):
        if self.index < 0:
            return self.play_index(0)
        return self.play_index(self.index - 1)

    def seek(self, seconds):
        if self.available:
            self._cmd("seek", float(seconds), "absolute")

    def set_volume(self, v):
        self.volume = max(0, min(100, int(v)))
        if self.available:
            self._cmd("set_property", "volume", self.volume)

    def current(self):
        if 0 <= self.index < len(self.tracks):
            return self.tracks[self.index]
        return None

    # ---------------- 状态快照 ----------------
    def status(self):
        st = {"available": self.available, "error": self.error,
              "index": self.index, "title": "", "artist": "",
              "playing": False, "pos": 0.0, "dur": 0.0,
              "volume": self.volume, "idle": True, "count": len(self.tracks)}
        if not self.available:
            return st
        tr = self.current()
        if tr is not None:
            st["title"] = tr.title
            st["artist"] = tr.artist
        dur = self._get("duration")
        pos = self._get("time-pos")
        idle = self._get("idle-active")
        pause = self._get("pause")
        vol = self._get("volume")
        st["dur"] = float(dur) if isinstance(dur, (int, float)) else 0.0
        st["pos"] = float(pos) if isinstance(pos, (int, float)) else 0.0
        st["idle"] = bool(idle)
        st["playing"] = (not idle) and (not pause)
        if isinstance(vol, (int, float)):
            st["volume"] = int(vol)
        return st


if __name__ == "__main__":
    import sys
    mp = MusicPlayer(sys.argv[1] if len(sys.argv) > 1 else None)
    print("目录:", mp.music_dir)
    for i, t in enumerate(mp.tracks):
        print("  %d. %s - %s  [%s]  封面:%s" % (
            i + 1, t.artist, t.title, os.path.basename(t.path),
            "有" if t.cover else "无"))
    if not mp.start():
        print("启动失败:", mp.error)
        sys.exit(1)
    print("播放第 1 首…")
    mp.play_index(0)
    for _ in range(6):
        time.sleep(1)
        s = mp.status()
        print("   %s %s  %.1f/%.1fs  vol=%d  播放中=%s" % (
            s["artist"], s["title"], s["pos"], s["dur"], s["volume"], s["playing"]))
    mp.close()
    print("已停止")
