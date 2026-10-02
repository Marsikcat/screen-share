"""In-chat sound: the voice-message / audio-file player and the composer's recording bar."""

import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from .. import audio
from ..voice import SR
from . import icons
from .theme import T, mix
from .widgets import IconButton, human_size

BARS = 40
CLIP = "voice-message"            # the voice engine's clip key: one message plays at a time


class Library(QObject):
    """Decoded audio shared by every card, and which one is playing."""

    loaded = Signal(str)          # fid: its waveform and length are known (or it failed)
    tick = Signal()               # the playing position moved, or playback started / stopped

    def __init__(self):
        super().__init__()
        self.voice = None
        self.info = {}            # fid -> (seconds, peaks) | "long" | "error"
        self.pcm = OrderedDict()  # fid -> samples of the last few files
        self.current = None       # fid playing now
        self.paused_at = {}       # fid -> sample position to go on from
        self._busy = set()
        self._results = {}
        self._play_when_loaded = None
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._lock = threading.Lock()
        self.loaded.connect(self._take, Qt.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)

    def load(self, fid, path, keep=False):
        if (fid in self.info and not keep) or fid in self._busy or (keep and fid in self.pcm):
            return
        self._busy.add(fid)

        def work():
            pcm = None
            try:
                secs = audio.duration(path)
                if secs and secs > audio.PLAYABLE_SECONDS:
                    result = "long"
                else:
                    pcm = audio.decode(path, audio.PLAYABLE_SECONDS)
                    result = (len(pcm) / SR, audio.peaks(pcm, BARS)) if len(pcm) else "error"
            except Exception:
                result = "error"
            with self._lock:
                self._results[fid] = (result, pcm)
            self.loaded.emit(fid)
        self._pool.submit(work)

    def _take(self, fid):
        with self._lock:
            got = self._results.pop(fid, None)
        if got is None:
            return
        self._busy.discard(fid)
        result, pcm = got
        self.info[fid] = result
        if pcm is not None and len(pcm):
            self.pcm[fid] = pcm
            self.pcm.move_to_end(fid)
            while len(self.pcm) > 3:            # keep a few: a five-minute message is ~55 MB
                old = next((k for k in self.pcm if k not in (fid, self.current)), None)
                if old is None:
                    break
                self.pcm.pop(old)
        if self._play_when_loaded == fid:
            self._play_when_loaded = None
            self._start(fid)

    # ── playback ────────────────────────────────────────────────────
    def toggle(self, fid, path):
        if self.current == fid:
            self.pause()
        else:
            self.play(fid, path)

    def play(self, fid, path, at=None):
        if self.current and self.current != fid:
            self.pause()
        if at is not None:
            self.paused_at[fid] = at
        if fid in self.pcm:
            self._start(fid)
        else:
            self._play_when_loaded = fid
            self.load(fid, path, keep=True)

    def _start(self, fid):
        pcm = self.pcm[fid]
        start = self.paused_at.get(fid, 0)
        if start >= len(pcm) - SR // 10:
            start = 0
        self.voice.play_clip(CLIP, pcm, start=start)
        self.current = fid
        self.timer.start()
        self.tick.emit()

    def pause(self):
        if self.current:
            pos = self.voice.clip_pos(CLIP)
            if pos is not None:
                self.paused_at[self.current] = pos
            self.voice.stop_clip(CLIP)
            self.current = None
            self.timer.stop()
            self.tick.emit()

    def stop(self):
        self.voice and self.voice.stop_clip(CLIP)
        self.current = None
        self.timer.stop()

    def position(self, fid):
        """Seconds into this file."""
        if fid == self.current:
            pos = self.voice.clip_pos(CLIP)
            if pos is not None:
                return pos / SR
        return self.paused_at.get(fid, 0) / SR

    def _poll(self):
        if self.current and self.voice.clip_pos(CLIP) is None:       # played to the end
            self.paused_at.pop(self.current, None)
            self.current = None
            self.timer.stop()
        self.tick.emit()


_library = None


def lib():
    """The one Library (made on first use, once Qt is running)."""
    global _library
    if _library is None:
        _library = Library()
    return _library


class Waveform(QWidget):
    """Bars of loudness; the played part in the accent colour. Click to jump there."""

    seek = Signal(float)

    def __init__(self, bars=BARS):
        super().__init__()
        self.peaks = [0.12] * bars
        self.progress = 0.0
        self.setFixedHeight(30)
        self.setMinimumWidth(bars * 4)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self):
        return QSize(len(self.peaks) * 5, 30)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        n = len(self.peaks)
        step = self.width() / max(1, n)
        bar = max(2.0, step * 0.55)
        played, rest = QColor(T.c["accent"]), QColor(mix(T.c["muted"], T.c["side"], 0.35))
        h = self.height()
        for i, v in enumerate(self.peaks):
            bh = max(3.0, v * (h - 4))
            p.setBrush(played if (i + 0.5) / n <= self.progress else rest)
            p.drawRoundedRect(QRectF(i * step + (step - bar) / 2, (h - bh) / 2, bar, bh), bar / 2, bar / 2)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.seek.emit(min(1.0, max(0.0, e.position().x() / max(1, self.width()))))


def round_button(icon_name, tip, bg, size=36):
    b = QToolButton()
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(size, size)
    b.setIconSize(QSize(size // 2 - 2, size // 2 - 2))
    b.setIcon(icons.icon(icon_name, "#ffffff", size // 2 - 2))
    b.setStyleSheet(f"QToolButton {{ background: {bg}; border: none; border-radius: {size // 2}px; }}"
                    f"QToolButton:hover {{ background: {mix(bg, '#000000', 0.15)}; }}")
    return b


class AudioCard(QFrame):
    """A voice message, or any audio file: play / pause, waveform, time."""

    def __init__(self, core, meta, path, open_file):
        super().__init__()
        self.core, self.meta, self.path, self.fid = core, meta, path, meta["id"]
        self._open = open_file
        lib().voice = core.voice
        c = T.c
        voice = audio.is_voice(meta)
        self.setObjectName("AudioCard")
        self.setStyleSheet(f"QFrame#AudioCard {{ background: {c['side']}; border: 1px solid {c['border']};"
                           f"border-radius: {22 if voice else 12}px; }} QLabel {{ background: transparent; border: none; }}")
        self.setFixedWidth(340 if voice else 400)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 6, 14, 6)
        h.setSpacing(10)
        self.btn = round_button("play", "Слушать", c["accent"])
        self.btn.clicked.connect(self._toggle)
        h.addWidget(self.btn)
        col = QVBoxLayout()
        col.setSpacing(2)
        if not voice:
            name = QLabel(f"<span style='color:{c['link']}'>{audio_name(meta)}</span>"
                          f"<span style='color:{c['muted']}'> · {human_size(meta['size'])}</span>")
            col.addWidget(name)
        self.wave = Waveform()
        self.wave.seek.connect(self._seek)
        col.addWidget(self.wave)
        h.addLayout(col, 1)
        self.time = QLabel("…")
        self.time.setStyleSheet(f"color: {c['muted']}; font-size: {T.px(8.5)}pt;")
        self.time.setMinimumWidth(T.px(34))
        self.time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.time)
        if not voice:
            save = IconButton("download", "Сохранить как…", 18, 30)
            save.clicked.connect(lambda: open_file(core, meta, True))
            h.addWidget(save)
        lib().loaded.connect(self._loaded)
        lib().tick.connect(self._update)
        lib().load(self.fid, path)
        self._update()

    def _loaded(self, fid):
        if fid == self.fid:
            QTimer.singleShot(0, self._update)          # after the library took the result

    def _toggle(self):
        info = lib().info.get(self.fid)
        if info in ("long", "error"):                   # the system player can do it
            self._open(self.core, self.meta)
            return
        lib().toggle(self.fid, self.path)

    def _seek(self, frac):
        info = lib().info.get(self.fid)
        if isinstance(info, tuple):
            lib().play(self.fid, self.path, at=int(frac * info[0] * SR))

    def _update(self):
        info = lib().info.get(self.fid)
        playing = lib().current == self.fid
        self.btn.setIcon(icons.icon("pause" if playing else "play", "#ffffff", 16))
        self.btn.setToolTip("Пауза" if playing else "Слушать")
        if isinstance(info, tuple):
            total, peaks = info
            pos = lib().position(self.fid)
            self.wave.peaks = peaks
            self.wave.progress = pos / total if total else 0.0
            started = playing or self.fid in lib().paused_at
            self.time.setText(audio.clock(pos) if started else audio.clock(total))
        elif info == "long":
            self.time.setText("")
            self.btn.setToolTip("Открыть в проигрывателе")
        elif info == "error":
            self.time.setText("")
            self.btn.setToolTip("Не получилось прочитать звук — открыть в проигрывателе")
        self.wave.update()


def audio_name(meta):
    import html
    return html.escape(meta["name"])


class Recording(QObject):
    """A voice message being recorded: the mic into memory, then an .ogg to send."""

    encoded = Signal(object)        # path of the .ogg, or an error text

    def __init__(self, settings):
        super().__init__()
        self.rec = audio.Recorder(settings)

    def start(self):
        return self.rec.start()

    def finish(self):
        pcm = self.rec.stop()
        if len(pcm) < SR // 2:
            return False
        folder = Path(tempfile.mkdtemp(prefix="marincall-voice-"))
        path = folder / audio.VOICE_NAME

        def work():
            try:
                audio.encode_ogg(pcm, path)
                self.encoded.emit(path)
            except Exception as e:
                self.encoded.emit(f"Не получилось сохранить запись: {e}")
        threading.Thread(target=work, daemon=True).start()
        return True

    def cancel(self):
        self.rec.stop()


class RecordBar(QWidget):
    """Shown in place of the message box while you record: cancel · ● 0:07 · waveform · send."""

    send = Signal()
    cancel = Signal()

    def __init__(self):
        super().__init__()
        c = T.c
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 6, 0, 6)
        h.setSpacing(10)
        trash = IconButton("trash", "Отменить (Esc)", 20, 36)
        trash.hover_bg = "transparent"
        trash.clicked.connect(self.cancel)
        h.addWidget(trash)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {c['red']}; font-size: {T.px(11)}pt;")
        h.addWidget(self.dot)
        self.time = QLabel("0:00")
        self.time.setStyleSheet(f"color: {c['header']}; font-weight: 600;")
        self.time.setMinimumWidth(T.px(60))
        h.addWidget(self.time)
        self.wave = Waveform(48)
        self.wave.setCursor(Qt.ArrowCursor)
        self.wave.progress = 1.0
        h.addWidget(self.wave, 1)
        go = round_button("send", "Отправить (Enter)", c["accent"], 34)
        go.clicked.connect(self.send)
        h.addWidget(go)
        self._blink = 0
        self.setFocusPolicy(Qt.StrongFocus)          # Enter sends, Esc cancels (the composer)

    def show_state(self, rec):
        self._blink += 1
        self.dot.setVisible(rec.full or self._blink % 10 < 6)
        text = audio.clock(rec.seconds)
        self.time.setText(f"{text} · максимум" if rec.full else text)
        levels = rec.levels[-len(self.wave.peaks):]
        levels = [0.0] * (len(self.wave.peaks) - len(levels)) + levels
        self.wave.peaks = [min(1.0, v ** 0.5 * 1.2) for v in levels]
        self.wave.update()
