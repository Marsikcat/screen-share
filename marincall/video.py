"""
Video files in the chat: a first-frame preview and playback.

Decoding runs in a thread (PyAV). The sound is decoded up front and played through the voice
engine's clip channel — so the echo canceller hears it, as with voice messages — and it is
the clock: a picture is shown when the sound reaches its time. Without sound, the wall clock.
"""

import threading
import time

import av
from av.video.reformatter import VideoReformatter
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QTransform

from . import audio
from .voice import SR

CLIP = "video-file"
VIDEO_TYPES = ("video/mp4", "video/webm", "video/quicktime", "video/x-matroska", "video/x-msvideo",
               "video/mpeg", "video/ogg", "video/3gpp")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg", ".ogv", ".3gp")


def is_video(meta):
    return meta.get("type", "").startswith("video/") or meta.get("name", "").lower().endswith(VIDEO_EXT)


def to_qimage(frame, box, reformatter=None, upscale=1.0):
    """A decoded frame scaled to fit `box` (device pixels), upright."""
    w, h = frame.width, frame.height
    rot = int(getattr(frame, "rotation", 0) or 0) % 360
    bw, bh = box if rot in (0, 180) else (box[1], box[0])          # fit after turning
    k = min(bw / w, bh / h, upscale)
    dw, dh = max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)
    rgb = (reformatter or VideoReformatter()).reformat(frame, width=dw, height=dh, format="bgra",
                                                       interpolation="BILINEAR")
    plane = rgb.planes[0]
    img = QImage(bytes(plane), dw, dh, plane.line_size, QImage.Format_RGB32).copy()
    if rot:
        img = img.transformed(QTransform().rotate(-rot))           # the frame's rotation is CCW
    return img


def probe(path, box=(480, 360)):
    """{"duration", "width", "height", "audio", "thumb": QImage} — the first picture and facts."""
    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        duration = c.duration / 1_000_000 if c.duration else (
            float(vs.duration * vs.time_base) if vs.duration else 0.0)
        thumb, size = None, (vs.codec_context.width, vs.codec_context.height)
        for frame in c.decode(vs):
            thumb = to_qimage(frame, box)
            rot = int(getattr(frame, "rotation", 0) or 0) % 360
            size = (frame.width, frame.height) if rot in (0, 180) else (frame.height, frame.width)
            break
        return {"duration": duration, "width": size[0], "height": size[1], "audio": bool(c.streams.audio),
                "thumb": thumb}


class Playback(QObject):
    """One video file playing: take_frame() after frame_ready."""

    frame_ready = Signal()
    loading = Signal(bool)       # True while the sound is being prepared
    finished = Signal()
    failed = Signal(str)

    def __init__(self, voice, path, duration=0.0):
        super().__init__()
        self.voice, self.path, self.duration = voice, path, duration
        self.target = (960, 540)
        self.paused = False
        self._gen = 0
        self._pcm = None
        self._audio_loaded = False
        self._pause_pos = 0.0
        self._anchor = (time.monotonic(), 0.0)
        self._frame = None
        self._lock = threading.Lock()
        self.gain = 1.0

    # ── control (UI thread) ─────────────────────────────────────────
    def play(self, at=0.0):
        self._gen += 1
        self.paused = False
        self._pause_pos = at
        threading.Thread(target=self._run, args=(self._gen, at), daemon=True, name="video-file").start()

    def pause(self):
        if not self.paused:
            self._pause_pos = self.position()
            self.paused = True
            self.voice.stop_clip(CLIP)

    def resume(self):
        if self.paused:
            if self._pause_pos >= self.duration - 0.05 > 0:
                self.play(0.0)                     # at the end: from the start again
                return
            self._start_clock(self._pause_pos)
            self.paused = False

    def seek(self, at):
        at = max(0.0, min(at, max(0.0, self.duration - 0.05)))
        was_paused = self.paused
        self._gen += 1
        self.voice.stop_clip(CLIP)
        self._pause_pos = at
        gen = self._gen
        threading.Thread(target=self._run, args=(gen, at, was_paused), daemon=True,
                         name="video-file").start()
        self.paused = was_paused

    def stop(self):
        self._gen += 1
        self.voice.stop_clip(CLIP)

    def position(self):
        if self.paused:
            return self._pause_pos
        if self._pcm is not None:
            p = self.voice.clip_pos(CLIP)
            if p is not None:
                self._anchor = (time.monotonic(), p / SR)
                return p / SR
        t0, p0 = self._anchor
        return p0 + (time.monotonic() - t0)

    def take_frame(self):
        with self._lock:
            return self._frame

    # ── decoding (its own thread) ───────────────────────────────────
    def _start_clock(self, at):
        self._anchor = (time.monotonic(), at)
        if self._pcm is not None and int(at * SR) < len(self._pcm):
            self.voice.play_clip(CLIP, self._pcm, gain=self.gain, start=int(at * SR))

    def _run(self, gen, at, paused=False):
        try:
            if not self._audio_loaded:
                self.loading.emit(True)
                try:
                    self._pcm = audio.decode(self.path, audio.PLAYABLE_SECONDS)
                    if not len(self._pcm):
                        self._pcm = None
                except Exception:
                    self._pcm = None          # no sound track
                self._audio_loaded = True
                self.loading.emit(False)
            if gen != self._gen:
                return
            if not paused:
                self._start_clock(at)
            reformatter = VideoReformatter()
            with av.open(str(self.path)) as c:
                vs = c.streams.video[0]
                vs.thread_type = "AUTO"
                if at > 0.05:
                    c.seek(int(at / vs.time_base), stream=vs, backward=True)
                shown = False
                for frame in c.decode(vs):
                    if gen != self._gen:
                        return
                    t = frame.time
                    if t is None or (t < at - 0.02 and not shown and t < at):
                        continue                  # after a seek: decode up to the wanted spot
                    while gen == self._gen:
                        wait = t - self.position()
                        if wait <= 0.004 or not shown:
                            break
                        time.sleep(min(0.02, wait))
                    if gen != self._gen:
                        return
                    if shown and self.position() - t > 0.12:
                        continue                  # late: skip the picture, keep the sound
                    img = to_qimage(frame, self.target, reformatter, upscale=2.0)
                    with self._lock:
                        self._frame = img
                    shown = True
                    self.frame_ready.emit()
            while gen == self._gen and (self.paused or self.position() < self.duration - 0.05):
                time.sleep(0.05)                  # the last picture stays until the sound ends
            if gen == self._gen:
                self.paused = True
                self._pause_pos = self.duration
                self.finished.emit()
        except Exception as e:
            if gen == self._gen:
                self.failed.emit(str(e))
