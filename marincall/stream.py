"""
Screen share inside voice channels.

Same idea as ScreenShare: FFmpeg captures (DXGI Desktop Duplication for
monitors, GDI for windows), encodes with NVENC when available, and emits
MPEG-TS to a local relay. The relay feeds one QUIC stream per viewer (iroh:
lost packets are retransmitted, it works through NAT, and if a viewer's link
can't keep up the oldest data is dropped rather than letting delay pile up),
so viewers can come and go without restarting FFmpeg. On the viewer's side
the stream is handed to ffplay over loopback UDP.
"""

import ctypes
import ctypes.wintypes as wt
import socket
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from .config import FFMPEG_BIN, FFPLAY_BIN, STREAM_PORT, STREAM_RELAY_PORT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TS_CHUNK = 1316                 # 7 MPEG-TS packets: what FFmpeg emits per datagram

QUALITY = {
    "720p30": {"h": 720, "fps": 30, "kbps": 3500, "label": "720p · 30 FPS"},
    "720p60": {"h": 720, "fps": 60, "kbps": 5000, "label": "720p · 60 FPS"},
    "1080p30": {"h": 1080, "fps": 30, "kbps": 6000, "label": "1080p · 30 FPS"},
    "1080p60": {"h": 1080, "fps": 60, "kbps": 9000, "label": "1080p · 60 FPS"},
    "1440p60": {"h": 1440, "fps": 60, "kbps": 14000, "label": "1440p · 60 FPS"},
    "source": {"h": None, "fps": 60, "kbps": 16000, "label": "Исходное · 60 FPS"},
}

_nvenc = None


def has_nvenc():
    global _nvenc
    if _nvenc is None:
        try:
            r = subprocess.run(
                [str(FFMPEG_BIN), "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=640x480:d=0.1",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, timeout=10, creationflags=NO_WINDOW)
            _nvenc = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _nvenc = False
    return _nvenc


# ── capture sources ─────────────────────────────────────────────────
class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", wt.WCHAR * 32), ("DesktopCoordinates", wt.RECT),
                ("AttachedToDesktop", wt.BOOL), ("Rotation", wt.UINT), ("Monitor", wt.HMONITOR)]


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]


def _vcall(obj, index, proto, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtbl[index])(obj, *args)


def dxgi_outputs():
    """Desktop rects of the default adapter's outputs, in ddagrab's output_idx order."""
    HR = ctypes.c_long
    enum_p = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
    desc_p = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.POINTER(_DXGI_OUTPUT_DESC))
    release_p = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    out = []
    try:
        iid = _GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
        factory = ctypes.c_void_p()
        if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return out
        adapter = ctypes.c_void_p()
        if _vcall(factory, 7, enum_p, 0, ctypes.byref(adapter)) == 0:     # EnumAdapters(0)
            i = 0
            while True:
                output = ctypes.c_void_p()
                if _vcall(adapter, 7, enum_p, i, ctypes.byref(output)) != 0:  # EnumOutputs(i)
                    break
                d = _DXGI_OUTPUT_DESC()
                _vcall(output, 7, desc_p, ctypes.byref(d))                    # GetDesc
                _vcall(output, 2, release_p)
                r = d.DesktopCoordinates
                out.append((i, r.left, r.top, r.right - r.left, r.bottom - r.top))
                i += 1
            _vcall(adapter, 2, release_p)
        _vcall(factory, 2, release_p)
    except (OSError, AttributeError, ValueError):
        pass
    return out


def list_monitors():
    try:
        import mss
        with mss.MSS() as sct:
            mons = sct.monitors[1:]
    except Exception:
        return []
    outputs = dxgi_outputs()
    res = []
    for i, m in enumerate(mons, 1):
        dxgi = next((o[0] for o in outputs
                     if (o[1], o[2], o[3], o[4]) == (m["left"], m["top"], m["width"], m["height"])), None)
        res.append({"kind": "monitor", "index": i, "left": m["left"], "top": m["top"],
                    "width": m["width"], "height": m["height"], "dxgi": dxgi,
                    "label": f"Экран {i}  ·  {m['width']}×{m['height']}"})
    return res


def list_windows(exclude_titles=()):
    user32 = ctypes.windll.user32
    wins = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if title and title not in exclude_titles and title != "Program Manager":
                    wins.append({"kind": "window", "title": title, "label": title})
        return True

    user32.EnumWindows(proto(cb), 0)
    return sorted(wins, key=lambda w: w["title"].lower())


def window_rect(title):
    """Where the shared window is right now (it can be moved or resized while streaming)."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return None
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if r.right - r.left < 8 or r.bottom - r.top < 8:
        return None
    return {"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top}


class SourcePreview(QObject):
    """A few frames per second of what we are sharing, so the streamer sees it is really live."""

    frame = Signal(QImage)

    def __init__(self):
        super().__init__()
        self._thread = None
        self._stop = threading.Event()

    def start(self, source, fps=3, width=520):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(source, fps, width), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None

    @staticmethod
    def _region(source):
        if source["kind"] == "monitor":
            return {k: source[k] for k in ("left", "top", "width", "height")}
        return window_rect(source["title"])

    def _loop(self, source, fps, width):
        try:
            import mss
        except ImportError:
            return
        with mss.MSS() as sct:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    region = self._region(source)
                    if region:
                        shot = sct.grab(region)
                        img = QImage(shot.rgb, shot.width, shot.height, shot.width * 3,
                                     QImage.Format_RGB888)
                        self.frame.emit(img.scaledToWidth(width, Qt.SmoothTransformation).copy())
                except Exception:
                    pass
                self._stop.wait(max(0.05, 1 / fps - (time.perf_counter() - t0)))


def list_audio_devices():
    """DirectShow audio inputs, loopback-style devices first ("Stereo Mix", "CABLE"…)."""
    if not FFMPEG_BIN.exists():
        return []
    try:
        r = subprocess.run([str(FFMPEG_BIN), "-hide_banner", "-list_devices", "true", "-f", "dshow",
                            "-i", "dummy"], capture_output=True, timeout=8, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in r.stderr.decode("utf-8", errors="replace").splitlines():
        if "(audio)" in line and '"' in line:
            names.append(line.split('"')[1])
    loop_kw = ("stereo mix", "стерео микшер", "cable", "loopback", "what u hear", "virtual")
    return sorted(dict.fromkeys(names), key=lambda n: not any(k in n.lower() for k in loop_kw))


# ── sender ──────────────────────────────────────────────────────────
class StreamSender(QObject):
    stopped = Signal(str)       # "" when stopped on purpose, otherwise the error

    def __init__(self, mesh):
        super().__init__()
        self.mesh = mesh
        self.proc = None
        self.outs = {}              # viewer uid -> net.OutStream
        self.running = False
        self._relay = None
        self._err_tail = []
        self._bytes = 0             # for the live bitrate shown to the streamer
        self._mark = (0.0, 0)
        self._rate = 0.0

    def build_command(self, source, quality, encoder, audio_device):
        q = QUALITY.get(quality, QUALITY["1080p60"])
        fps, kbps = q["fps"], q["kbps"]
        nvenc = encoder == "nvenc" or (encoder == "auto" and has_nvenc())
        src_h = source.get("height") or 0
        scale = q["h"] if q["h"] and src_h and src_h > q["h"] else None

        cmd = [str(FFMPEG_BIN), "-hide_banner", "-loglevel", "warning", "-stats_period", "2"]
        vf = []
        gpu_direct = False
        if source["kind"] == "monitor" and source.get("dxgi") is not None:
            cmd += ["-f", "lavfi", "-i", f"ddagrab=output_idx={source['dxgi']}:framerate={fps}:draw_mouse=1"]
            gpu_direct = nvenc and not scale  # NVENC takes the D3D11 frames as they are
            if not gpu_direct:
                vf += ["hwdownload", "format=bgra"]
        elif source["kind"] == "monitor":
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                    "-offset_x", str(source["left"]), "-offset_y", str(source["top"]),
                    "-video_size", f"{source['width']}x{source['height']}", "-i", "desktop"]
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                    "-i", f"title={source['title']}"]
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # windows can have odd sizes
        if scale:
            vf.append(f"scale=-2:{scale}:flags=bicubic")
        if not gpu_direct:
            vf.append("format=yuv420p")
        if audio_device:
            cmd += ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={audio_device}"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-map", "0:v"] + (["-map", "1:a"] if audio_device else [])

        if nvenc:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr",
                    "-zerolatency", "1", "-bf", "0"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "superfast" if fps >= 60 else "veryfast",
                    "-tune", "zerolatency"]
        cmd += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k",
                "-g", str(fps * 2)]
        if audio_device:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
        cmd += ["-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                f"udp://127.0.0.1:{STREAM_RELAY_PORT}?pkt_size=1316"]
        return cmd, nvenc

    def start(self, source, quality, encoder="auto", audio_device=""):
        self.stop()
        if not FFMPEG_BIN.exists():
            self.stopped.emit("FFmpeg не найден — запустите setup.bat")
            return False
        cmd, nvenc = self.build_command(source, quality, encoder, audio_device)
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            relay.bind(("127.0.0.1", STREAM_RELAY_PORT))
        except OSError as e:
            self.stopped.emit(f"Порт {STREAM_RELAY_PORT} занят: {e.strerror or e}")
            return False
        relay.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        relay.settimeout(0.5)
        self._relay = relay
        self.running = True
        self._err_tail = []
        self._bytes, self._mark, self._rate = 0, (0.0, 0), 0.0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        threading.Thread(target=self._relay_loop, args=(relay,), daemon=True).start()
        threading.Thread(target=self._watch_proc, args=(self.proc,), daemon=True).start()
        self.encoder_label = "NVENC" if nvenc else "CPU (x264)"
        return True

    def set_viewers(self, uids):
        """uids of everyone watching right now: open / close their streams."""
        uids = set(uids) if self.running else set()
        for uid in list(self.outs):
            if uid not in uids:
                self.outs.pop(uid).close()
        for uid in uids - set(self.outs):
            self.outs[uid] = self.mesh.open_stream(uid)

    def _relay_loop(self, relay):
        while self.running:
            try:
                data = relay.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            self._bytes += len(data)
            for out in list(self.outs.values()):
                out.push(data)

    def bitrate(self):
        """Mbit/s actually leaving the encoder right now."""
        now = time.monotonic()
        t0, b0 = self._mark
        if now - t0 >= 1.0:
            if t0:
                self._rate = (self._bytes - b0) * 8 / (now - t0) / 1e6
            self._mark = (now, self._bytes)
        return self._rate

    def _watch_proc(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line and "frame=" not in line:
                self._err_tail = (self._err_tail + [line])[-4:]
        proc.wait()
        if self.proc is proc and self.running:
            self.running = False
            self.stopped.emit("FFmpeg завершился: " + (" | ".join(self._err_tail) or f"код {proc.returncode}"))

    def stop(self):
        was = self.running
        self.running = False
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        if self._relay:
            self._relay.close()
            self._relay = None
        self.set_viewers(())
        return was


# ── viewer ──────────────────────────────────────────────────────────
class StreamViewer(QObject):
    closed = Signal(str)        # uid of the streamer whose window closed

    def __init__(self, mesh):
        super().__init__()
        mesh.on_stream = self._on_stream
        self.proc = None
        self.uid = None
        self._buf = b""
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _on_stream(self, uid, chunk):
        """Bytes of a screen share arriving over QUIC (network thread) -> ffplay on loopback."""
        if uid != self.uid or chunk is None:
            return
        self._buf += chunk
        n = len(self._buf) // TS_CHUNK * TS_CHUNK
        for i in range(0, n, TS_CHUNK):
            try:
                self._out.sendto(self._buf[i:i + TS_CHUNK], ("127.0.0.1", STREAM_PORT))
            except OSError:
                break
        self._buf = self._buf[n:]

    def watch(self, uid, title):
        self.stop()
        if not FFPLAY_BIN.exists():
            return False
        self.uid = uid
        self._buf = b""
        self.proc = subprocess.Popen(
            [str(FFPLAY_BIN), "-hide_banner", "-loglevel", "error", "-window_title", title,
             "-x", "1280", "-y", "720", "-fflags", "nobuffer", "-flags", "low_delay",
             "-framedrop", "-max_delay", "200000",
             "-i", f"udp://127.0.0.1:{STREAM_PORT}?localaddr=127.0.0.1&overrun_nonfatal=1"
                   f"&fifo_size=500000&buffer_size=8388608"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW)
        threading.Thread(target=self._wait, args=(self.proc, uid), daemon=True).start()
        return True

    def _wait(self, proc, uid):
        proc.wait()
        if self.proc is proc:
            self.proc, self.uid = None, None
            self.closed.emit(uid)

    def stop(self):
        proc, self.proc = self.proc, None
        self.uid = None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
