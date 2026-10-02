"""
Screen share inside voice channels.

A worker process (streamworker.py — FFmpeg's libraries through PyAV, no ffmpeg.exe)
captures (DXGI Desktop Duplication for monitors, GDI for windows), encodes with NVENC
when available and sends MPEG-TS to the app over loopback TCP. The relay feeds one
QUIC stream per viewer (iroh: lost packets are retransmitted, it works through NAT,
and if a viewer's link can't keep up the oldest data is dropped rather than letting
delay pile up), so viewers can come and go without restarting the encoder. On the viewer's side
the stream is decoded right in the app (PyAV) and shown in the voice channel;
its sound goes into the same output as the voices, so echo cancellation
knows about it.

Sound of a share: by default "everything the PC plays except MarinCall" (see
loopback.py) — the people watching hear the game, not their own voices.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import threading
import time
from fractions import Fraction

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from . import loopback
from .config import FROZEN, ROOT

SYSTEM_AUDIO = "system"         # stream_audio value: process loopback without our own sounds
CAMERA = {"h": 360, "fps": 30, "kbps": 800}      # a webcam in a voice channel
TEST_CAMERA = "__test__"        # camera_device for tools/screens.py: a test pattern, no hardware

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

QUALITY = {
    "720p30": {"h": 720, "fps": 30, "kbps": 3500, "label": "720p · 30 FPS"},
    "720p60": {"h": 720, "fps": 60, "kbps": 5000, "label": "720p · 60 FPS"},
    "1080p30": {"h": 1080, "fps": 30, "kbps": 6000, "label": "1080p · 30 FPS"},
    "1080p60": {"h": 1080, "fps": 60, "kbps": 9000, "label": "1080p · 60 FPS"},
    "1440p60": {"h": 1440, "fps": 60, "kbps": 14000, "label": "1440p · 60 FPS"},
    "source": {"h": None, "fps": 60, "kbps": 16000, "label": "Исходное · 60 FPS"},
}

_nvenc = None


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMITS), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_job = None


def _tie_to_us(proc):
    """If MarinCall dies (a crash, Task Manager) Windows ends the encoder too, instead of leaving
    it capturing the screen and loading the GPU until the next reboot."""
    global _job
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        if _job is None:
            job = k32.CreateJobObjectW(None, None)
            limits = _EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits))
            _job = job
        k32.AssignProcessToJobObject(_job, int(proc._handle))
    except (OSError, AttributeError):
        pass


def has_nvenc():
    """Is there an NVIDIA encoder? (opening it fails without a suitable GPU and driver)"""
    global _nvenc
    if _nvenc is None:
        try:
            import av
            ctx = av.CodecContext.create("h264_nvenc", "w")
            ctx.width, ctx.height, ctx.pix_fmt = 640, 480, "yuv420p"
            ctx.time_base = Fraction(1, 30)
            ctx.open()
            _nvenc = True
        except Exception:
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
        self.paused = False         # nobody is looking: don't grab the screen at all

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
                if self.paused:
                    self._stop.wait(0.25)
                    continue
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


def audio_choices():
    """(label, value) for the "sound of the share" lists, the recommended one first."""
    out = []
    if loopback.available():
        out.append(("Звук компьютера — без голосового чата", SYSTEM_AUDIO))
    out.append(("Без звука", ""))
    out += [(f"Устройство: {name}", name) for name in list_audio_devices()]
    return out


def list_audio_devices():
    """DirectShow audio inputs, loopback-style devices first ("Stereo Mix", "CABLE"…)."""
    names = _dshow_devices("audio")
    loop_kw = ("stereo mix", "стерео микшер", "cable", "loopback", "what u hear", "virtual")
    return sorted(dict.fromkeys(names), key=lambda n: not any(k in n.lower() for k in loop_kw))


def list_cameras():
    return list(dict.fromkeys(_dshow_devices("video")))


def _dshow_devices(kind):
    try:
        import av
        import av.logging
    except ImportError:
        return []
    names = []
    old = av.logging.get_level()
    try:
        av.logging.set_level(av.logging.INFO)          # FFmpeg prints the list as log lines
        with av.logging.Capture() as logs:
            try:
                av.open("dummy", format="dshow", options={"list_devices": "true"})
            except Exception:
                pass
        for _level, _name, line in logs:
            if f"({kind})" in line and '"' in line:
                names.append(line.split('"')[1])
    finally:
        av.logging.set_level(old)
    return names


# ── sender ──────────────────────────────────────────────────────────
def _worker_command(cfg):
    arg = json.dumps(cfg, ensure_ascii=False)
    if FROZEN:
        return [sys.executable, "--stream-worker", arg]
    return [sys.executable, str(ROOT / "app.py"), "--stream-worker", arg]


class StreamSender(QObject):
    stopped = Signal(str)       # "" when stopped on purpose, otherwise the error
    warning = Signal(str)       # started, but not quite as asked (e.g. without sound)

    def __init__(self, mesh, header=b""):
        super().__init__()
        self.mesh = mesh
        self.header = header        # a camera stream says so in its first bytes
        self.tap = None             # callback(bytes): our own copy (the camera self-view)
        self.proc = None
        self.outs = {}              # viewer uid -> net.OutStream
        self.running = False
        self.encoder_label = ""
        self._conn = None
        self._err_tail = []
        self._bytes = 0             # for the live bitrate shown to the streamer
        self._mark = (0.0, 0)
        self._rate = 0.0

    def start(self, source, quality, encoder="auto", audio=""):
        self.stop()
        q = quality if isinstance(quality, dict) else QUALITY.get(quality, QUALITY["1080p60"])
        nvenc = encoder == "nvenc" or (encoder == "auto" and has_nvenc())
        if audio == SYSTEM_AUDIO and not loopback.available():
            self.warning.emit("Звук компьютера без голосового чата есть только в Windows 10 2004 и новее "
                              "— трансляция без звука")
            audio = ""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(20)
        cfg = {"source": source, "fps": q["fps"], "height": q["h"], "kbps": q["kbps"],
               "encoder": "nvenc" if nvenc else "x264", "audio": audio,
               "port": listener.getsockname()[1], "parent_pid": os.getpid()}
        try:
            self.proc = subprocess.Popen(_worker_command(cfg), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         creationflags=NO_WINDOW)
        except OSError as e:
            listener.close()
            self.stopped.emit(f"Не удалось запустить трансляцию: {e}")
            return False
        _tie_to_us(self.proc)
        self.running = True
        self._err_tail = []
        self._bytes, self._mark, self._rate = 0, (0.0, 0), 0.0
        threading.Thread(target=self._relay_loop, args=(listener,), daemon=True, name="relay").start()
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
            self.outs[uid] = self.mesh.open_stream(uid, header=self.header)

    def _relay_loop(self, listener):
        try:
            conn, _ = listener.accept()
        except OSError:
            return                              # the worker never came: _watch_proc says why
        finally:
            listener.close()
        self._conn = conn
        pending = b""
        while self.running:
            try:
                data = conn.recv(256 * 1024)
            except OSError:
                break
            if not data:
                break
            self._bytes += len(data)
            pending += data
            n = len(pending) // 188 * 188       # whole TS packets: a dropped chunk never splits one
            if n:
                chunk, pending = pending[:n], pending[n:]
                for out in list(self.outs.values()):
                    out.push(chunk)
                if self.tap is not None:
                    self.tap(chunk)
        conn.close()

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
            if line.startswith("WARN "):
                self.warning.emit(line[5:] + " — трансляция без звука")
            elif line.startswith("INFO NVENC"):
                self.encoder_label = "CPU (x264)"
            elif line and not line.startswith("OK "):
                self._err_tail = (self._err_tail + [line])[-3:]
        proc.wait()
        if self.proc is proc and self.running:
            self.running = False
            self.stopped.emit("Трансляция прервалась: " + (" | ".join(self._err_tail) or f"код {proc.returncode}"))

    def stop(self):
        was = self.running
        self.running = False
        conn, self._conn = self._conn, None
        if conn:
            try:
                conn.close()                    # the worker sees the socket close and exits
            except OSError:
                pass
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.set_viewers(())
        return was


# ── viewer ──────────────────────────────────────────────────────────
class _ByteQueue:
    """Bytes from the network thread → a file-like object for PyAV (blocks until data arrives)."""

    LIMIT = 24 * 1024 * 1024

    def __init__(self):
        self._buf = bytearray()
        self._cv = threading.Condition()
        self.closed = False

    def put(self, data):
        with self._cv:
            if len(self._buf) > self.LIMIT:       # hopelessly behind: start over from fresh data
                self._buf.clear()
            self._buf += data
            self._cv.notify()

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def pending(self):
        return len(self._buf)

    def read(self, n=-1):
        with self._cv:
            while not self._buf and not self.closed:
                self._cv.wait(0.5)
            if not self._buf:
                return b""
            n = len(self._buf) if n is None or n < 0 else min(n, len(self._buf))
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out


class VideoDecoder(QObject):
    """One incoming MPEG-TS stream → the newest picture (and its sound, if `voice` is given)."""

    frame_ready = Signal(object)    # self: a new picture, take it with take_frame()
    ended = Signal(object)          # self: the stream stopped by itself (not via stop())

    BEHIND = 3 * 1024 * 1024        # this much undecoded data waiting: skip pictures to catch up

    def __init__(self, voice=None, uid=None):
        super().__init__()
        self.voice, self.uid = voice, uid
        self.target = (1280, 720)   # device pixels the picture is shown at (set by the UI)
        self.fill = False           # True: cover the target (cropped), False: fit inside it
        self.show_video = True      # False while nobody looks: only the sound is decoded
        self.source_size = (0, 0)
        self.fps = 0.0
        self._queue = _ByteQueue()
        self._stopped = False
        self._lock = threading.Lock()
        self._frame = None
        self._waiting = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="video-decoder").start()

    def feed(self, chunk):
        if not self._stopped:
            self._queue.put(chunk)

    def stop(self):
        self._stopped = True
        self._queue.close()
        if self.voice is not None:
            self.voice.clear_stream_audio()

    def take_frame(self):
        with self._lock:
            self._waiting = False
            return self._frame

    def _run(self):
        import av
        q = self._queue
        try:
            container = av.open(q, mode="r", format="mpegts",
                                options={"probesize": "300000", "analyzeduration": "500000",
                                         "fflags": "nobuffer", "flags": "low_delay"})
        except Exception:
            if not self._stopped:
                self.ended.emit(self)
            return
        video = container.streams.video[0] if container.streams.video else None
        audio = container.streams.audio[0] if container.streams.audio and self.voice else None
        if video is not None:
            video.thread_type = "SLICE"             # frame threads would add a frame of delay each
        resampler = av.AudioResampler(format="flt", layout="stereo", rate=48000) if audio else None
        from av.video.reformatter import VideoReformatter
        self._reformatter = VideoReformatter()      # reused: keeps its scaler, ~10× cheaper per frame
        vindex = video.index if video is not None else -1       # packet.stream is a new object each time
        aindex = audio.index if audio is not None else -1
        keyframe = False            # until the first one the picture would be grey mush
        shown, t_mark = 0, time.monotonic()
        try:
            for packet in container.demux([st for st in (video, audio) if st is not None]):
                if self._stopped:
                    break
                if packet.size == 0:
                    continue
                index = packet.stream.index
                if index == vindex and not keyframe:
                    if not packet.is_keyframe:
                        continue
                    keyframe = True
                try:
                    frames = packet.decode()
                except av.error.FFmpegError:
                    continue                        # a lost piece: carry on from the next one
                for frame in frames:
                    if index == vindex:
                        # skip the picture if we are behind or the UI has not shown the last one yet
                        if self.show_video and q.pending() < self.BEHIND and not self._waiting:
                            self._publish(frame)
                            shown += 1
                    elif index == aindex and resampler is not None:
                        for out in resampler.resample(frame):
                            self.voice.push_stream_audio(out.to_ndarray().reshape(-1, 2))
                now = time.monotonic()
                if now - t_mark >= 1.0:
                    self.fps, shown, t_mark = shown / (now - t_mark), 0, now
        except Exception:
            import traceback
            traceback.print_exc()                   # into app.log: a picture that froze should say why
        finally:
            container.close()
        if not self._stopped:
            self.ended.emit(self)

    def _publish(self, frame):
        w, h = frame.width, frame.height
        self.source_size = (w, h)
        tw, th = self.target
        k = max(tw / w, th / h) if self.fill else min(tw / w, th / h)
        k = min(k, 2.0)
        dw, dh = max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)
        rgb = self._reformatter.reformat(frame, width=dw, height=dh, format="bgra",
                                         interpolation="BILINEAR", threads=2)
        # straight from FFmpeg's buffer: rows can be padded for alignment (line_size > dw * 4)
        plane = rgb.planes[0]
        img = QImage(bytes(plane), dw, dh, plane.line_size, QImage.Format_RGB32).copy()
        with self._lock:
            self._frame = img
            notify = not self._waiting
            self._waiting = True
        if notify:
            self.frame_ready.emit(self)


class StreamViewer(QObject):
    """Watches one peer's screen share: decodes it and hands out the newest picture."""

    closed = Signal(str)        # uid of the streamer — the stream could not be shown
    frame_ready = Signal()      # a new picture: take it with take_frame()

    def __init__(self, mesh, voice):
        super().__init__()
        mesh.on_stream = self._on_stream
        self.voice = voice
        self.uid = None
        self._dec = None
        self._target = (1280, 720)
        self._show = True

    # the decoder's knobs, kept across watches
    @property
    def target(self):
        return self._target

    @target.setter
    def target(self, value):
        self._target = value
        if self._dec is not None:
            self._dec.target = value

    @property
    def show_video(self):
        return self._show

    @show_video.setter
    def show_video(self, value):
        self._show = value
        if self._dec is not None:
            self._dec.show_video = value

    @property
    def source_size(self):
        return self._dec.source_size if self._dec else (0, 0)

    @property
    def fps(self):
        return self._dec.fps if self._dec else 0.0

    def _on_stream(self, uid, chunk):
        """Bytes of a screen share arriving over QUIC (network thread)."""
        dec = self._dec
        if uid == self.uid and dec is not None and chunk is not None:
            dec.feed(chunk)

    def watch(self, uid, title=""):
        self.stop()
        self.uid = uid
        dec = VideoDecoder(self.voice, uid)
        dec.target, dec.show_video = self._target, self._show
        dec.frame_ready.connect(self._on_frame)
        dec.ended.connect(self._on_ended)
        self._dec = dec
        dec.start()
        return True

    def _on_frame(self, dec):
        if dec is self._dec:
            self.frame_ready.emit()

    def _on_ended(self, dec):
        if dec is self._dec:
            self.closed.emit(dec.uid)

    def stop(self):
        dec, self._dec = self._dec, None
        self.uid = None
        if dec is not None:
            dec.stop()
        else:
            self.voice.clear_stream_audio()

    def take_frame(self):
        return self._dec.take_frame() if self._dec else None


class CameraHub(QObject):
    """The cameras of everyone in our voice channel (and our own self-view): one decoder each."""

    frame_ready = Signal(str)       # uid with a new picture

    def __init__(self, mesh):
        super().__init__()
        mesh.on_camera = self._on_camera
        self.decoders = {}          # uid -> VideoDecoder
        self.show_video = True
        self._paused = set()        # uids whose pictures are not wanted now (our own self-view)

    def set_sources(self, uids):
        uids = set(uids)
        for uid in [u for u in self.decoders if u not in uids]:
            self.decoders.pop(uid).stop()
        for uid in uids - set(self.decoders):
            dec = VideoDecoder(None, uid)
            dec.fill, dec.target = True, (640, 360)
            dec.show_video = self.show_video and uid not in self._paused
            dec.frame_ready.connect(self._on_frame)
            self.decoders[uid] = dec
            dec.start()

    def _on_camera(self, uid, chunk):
        """Bytes of someone's camera (network thread)."""
        dec = self.decoders.get(uid)
        if dec is not None and chunk is not None:
            dec.feed(chunk)

    feed_local = _on_camera         # our own encoded camera, for the self-view

    def _on_frame(self, dec):
        if self.decoders.get(dec.uid) is dec:
            self.frame_ready.emit(dec.uid)

    def take_frame(self, uid):
        dec = self.decoders.get(uid)
        return dec.take_frame() if dec else None

    def has(self, uid):
        return uid in self.decoders

    def set_target(self, uid, size):
        dec = self.decoders.get(uid)
        if dec is not None:
            dec.target = size

    def set_visible(self, on):
        self.show_video = on
        for uid, dec in self.decoders.items():
            dec.show_video = on and uid not in self._paused

    def set_paused(self, uid, paused):
        (self._paused.add if paused else self._paused.discard)(uid)
        dec = self.decoders.get(uid)
        if dec is not None:
            dec.show_video = self.show_video and not paused

    def paused(self, uid):
        return uid in self._paused

    def stop(self):
        self.set_sources(())
