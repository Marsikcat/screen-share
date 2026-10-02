"""
The screen-share encoder, in a process of its own: `MarinCall.exe --stream-worker <json>`.

FFmpeg's own libraries do the work through PyAV — no ffmpeg.exe ships any more:
DXGI desktop duplication (ddagrab) hands D3D11 frames straight to NVENC on the GPU;
windows, x264 and scaled shares go through system memory. Sound is either everything
the PC plays except MarinCall (loopback.py) or a DirectShow device. The MPEG-TS goes to
a loopback TCP socket the app reads and relays to the viewers.

The same worker sends a webcam (`camera`): DirectShow video, small and steady.

A separate process keeps the encoder away from the app's voice and UI threads, and the
app ties it to its own lifetime (a job object), so it never outlives MarinCall.
"""

import ctypes
import json
import socket
import sys
import threading
import time
from fractions import Fraction

SR = 48000


def _say(text):
    """A line for the app (stderr) — works in the windowed exe too, which has no sys.stderr."""
    data = (text.replace("\n", " ") + "\n").encode("utf-8", "replace")
    try:
        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        handle = k32.GetStdHandle(-12)
        written = ctypes.c_ulong()
        k32.WriteFile(ctypes.c_void_p(handle), data, len(data), ctypes.byref(written), None)
    except Exception:
        pass


class _Sink:
    """PyAV writes the muxed stream here; a broken socket means the app is gone — stop."""

    def __init__(self, sock):
        self.sock = sock

    def write(self, data):
        self.sock.sendall(data)
        return len(data)


def _even(n):
    return max(2, int(n) // 2 * 2)


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()         # one muxer, two threads
        self.stopping = threading.Event()
        self.out = self.video = self.audio = None
        self.t0 = 0.0

    # ── inputs ──────────────────────────────────────────────────────
    def _open_video(self):
        import av
        src, fps, nvenc = self.cfg["source"], self.cfg["fps"], self.cfg["encoder"] == "nvenc"
        target = self.cfg.get("height")
        if src["kind"] == "test":                       # tools/screens.py: no real screen involved
            return av.open(f"testsrc2=s=1280x720:r={fps}", format="lavfi")
        if src["kind"] == "camera_test":                # …and no real webcam
            return av.open(f"testsrc=s=640x360:r={fps}", format="lavfi")
        if src["kind"] == "camera":
            # webcams offer a handful of modes: try the usual good ones, then whatever it gives
            last = None
            for mode in ({"video_size": "1280x720", "framerate": str(fps)},
                         {"video_size": "640x480", "framerate": str(fps)}, {}):
                try:
                    return av.open(f"video={src['device']}", format="dshow",
                                   options={**mode, "rtbufsize": "64M"})
                except av.error.FFmpegError as e:
                    last = e
            raise last
        if src["kind"] == "monitor" and src.get("dxgi") is not None:
            graph = f"ddagrab=output_idx={src['dxgi']}:framerate={fps}:draw_mouse=1"
            scale = target if target and src.get("height", 0) > target else None
            if scale or not nvenc:                       # otherwise the frames never leave the GPU
                graph += ",hwdownload,format=bgra"
                if scale:
                    graph += f",scale=-2:{scale}:flags=bicubic"
            return av.open(graph, format="lavfi")
        opts = {"framerate": str(fps), "draw_mouse": "1"}
        if src["kind"] == "monitor":
            opts.update(offset_x=str(src["left"]), offset_y=str(src["top"]),
                        video_size=f"{src['width']}x{src['height']}")
            return av.open("desktop", format="gdigrab", options=opts)
        return av.open(f"title={src['title']}", format="gdigrab", options=opts)

    def _add_video(self, frame):
        cfg, fps = self.cfg, self.cfg["fps"]
        nvenc = cfg["encoder"] == "nvenc"
        w, h = frame.width, frame.height
        target = cfg.get("height")
        if frame.format.name != "d3d11" and target and h > target:
            w, h = w * target / h, target
        self.size = (_even(w), _even(h))
        vs = self.out.add_stream("h264_nvenc" if nvenc else "libx264", rate=fps)
        vs.width, vs.height = self.size
        kbps = cfg["kbps"]
        common = {"g": str(fps), "maxrate": f"{kbps}k", "bufsize": f"{kbps // 2}k"}
        if nvenc:
            # NVENC converts BGRA (and takes D3D11 surfaces) itself
            vs.pix_fmt = frame.format.name if frame.format.name in ("d3d11", "bgra", "bgr0") else "yuv420p"
            vs.options = {"preset": "p4", "tune": "ll", "rc": "cbr", "zerolatency": "1", "bf": "0", **common}
        else:
            vs.pix_fmt = "yuv420p"
            vs.options = {"preset": "superfast" if fps >= 60 else "veryfast", "tune": "zerolatency",
                          **common}
        vs.bit_rate = kbps * 1000
        self.video = vs

    def _add_audio(self):
        a = self.out.add_stream("aac", rate=SR)
        a.layout = "stereo"
        a.bit_rate = 160_000
        self.audio = a

    # ── muxing ──────────────────────────────────────────────────────
    def _mux(self, packets):
        with self.lock:
            for p in packets:
                self.out.mux(p)

    def _video_loop(self, vin, first):
        from av.video.reformatter import VideoReformatter
        vs, fps = self.video, self.cfg["fps"]
        reformat = VideoReformatter()
        convert = vs.pix_fmt != first.format.name or (first.width, first.height) != self.size
        last = -1
        frames = [first]
        stream = vin.decode(video=0)
        paced = self.cfg["source"]["kind"] in ("test", "camera_test")    # generators, not live
        while not self.stopping.is_set():
            frame = frames.pop() if frames else next(stream)
            if paced:
                time.sleep(max(0.0, self.t0 + (last + 1) / fps - time.perf_counter()))
            # wall-clock timestamps: the sound runs on the same clock
            pts = max(last + 1, round((time.perf_counter() - self.t0) * fps))
            if convert:
                frame = reformat.reformat(frame, width=self.size[0], height=self.size[1],
                                          format=vs.pix_fmt, interpolation="BICUBIC", threads=2)
            frame.pts, last = pts, pts
            frame.time_base = Fraction(1, fps)
            frame.pict_type = 0                       # let the encoder decide
            self._mux(vs.encode(frame))

    def _audio_loop(self):
        import av
        import numpy as np
        audio = self.cfg["audio"]
        sent = 0

        def encode(samples_s16):
            nonlocal sent
            frame = av.AudioFrame.from_ndarray(samples_s16.reshape(1, -1), format="s16", layout="stereo")
            frame.sample_rate = SR
            frame.pts = sent
            frame.time_base = Fraction(1, SR)
            sent += samples_s16.size // 2
            self._mux(self.audio.encode(frame))

        if audio in ("system", "app"):
            from .loopback import LoopbackCapture
            if audio == "app":                  # the shared window's program, and nothing else
                cap = LoopbackCapture(self.cfg["source"]["pid"], include=True)
            else:                               # MarinCall's whole process tree is left out
                cap = LoopbackCapture(self.cfg["parent_pid"])
            try:
                cap.open()
            except OSError as e:
                _say(f"WARN звук компьютера недоступен ({e})")
                return self._silence(encode)
            cap.restart_clock()
            try:
                while not self.stopping.is_set():
                    data = cap.read()
                    if data:
                        encode(np.frombuffer(data, np.int16))
            finally:
                cap.close()
        elif audio == "test":
            t = np.arange(SR // 50) / SR
            tone = (np.sin(2 * np.pi * 330 * t) * 3000).astype(np.int16)
            pcm = np.repeat(tone, 2)
            while not self.stopping.is_set():
                due = int((time.perf_counter() - self.t0) * SR) - sent
                if due >= len(tone):
                    encode(pcm)
                else:
                    time.sleep(0.005)
        else:                                              # a DirectShow device
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=SR)
            try:
                dev = av.open(f"audio={audio}", format="dshow", options={"audio_buffer_size": "50"})
            except av.error.FFmpegError as e:
                _say(f"WARN устройство «{audio}» недоступно ({e})")
                return self._silence(encode)
            for frame in dev.decode(audio=0):
                if self.stopping.is_set():
                    break
                for out in resampler.resample(frame):
                    encode(out.to_ndarray().reshape(-1))

    def _silence(self, encode):
        """No sound source after all: keep the audio track going so the muxer never stalls."""
        import numpy as np
        chunk = np.zeros(SR // 50 * 2, np.int16)
        sent = 0
        while not self.stopping.is_set():
            due = int((time.perf_counter() - self.t0) * SR)
            while sent + len(chunk) // 2 <= due:
                encode(chunk)
                sent += len(chunk) // 2
            time.sleep(0.01)

    @staticmethod
    def _nvenc_free():
        """Can we get an NVENC session right now? (consumer GPUs allow only a few at once)"""
        import av
        try:
            ctx = av.CodecContext.create("h264_nvenc", "w")
            ctx.width, ctx.height, ctx.pix_fmt = 256, 256, "yuv420p"
            ctx.time_base = Fraction(1, 30)
            ctx.open()
            return True
        except Exception:
            return False

    # ── run ─────────────────────────────────────────────────────────
    def run(self):
        import av
        if self.cfg["encoder"] == "nvenc" and not self._nvenc_free():
            self.cfg["encoder"] = "x264"           # the video card is busy: the processor encodes
            _say("INFO NVENC занят — кодирует процессор")
        sock = socket.create_connection(("127.0.0.1", self.cfg["port"]), timeout=10)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        vin = self._open_video()
        first = next(vin.decode(video=0))
        self.out = av.open(_Sink(sock), mode="w", format="mpegts",
                           options={"max_delay": "0", "flush_packets": "1"})
        self._add_video(first)
        if self.cfg["audio"]:
            self._add_audio()
        _say(f"OK {self.size[0]}x{self.size[1]} {self.video.codec_context.name}")
        self.t0 = time.perf_counter()
        errors = []
        if self.audio is not None:
            def audio_thread():
                try:
                    self._audio_loop()
                except Exception as e:
                    errors.append(e)
                    self.stopping.set()
            threading.Thread(target=audio_thread, daemon=True, name="audio").start()
        try:
            self._video_loop(vin, first)
        finally:
            self.stopping.set()
        if errors:
            raise errors[0]


def main(arg):
    try:
        Worker(json.loads(arg)).run()
    except (ConnectionError, OSError) as e:
        if isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return 0                                       # the app stopped the share
        _say(f"{type(e).__name__}: {e}")
        return 1
    except StopIteration:
        _say("захват экрана прекратился")
        return 1
    except Exception as e:
        _say(f"{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
