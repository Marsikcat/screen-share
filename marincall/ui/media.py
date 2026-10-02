"""Pictures and videos: animated GIFs in the chat, the inline video player and the media viewer
(full window, zoom and pan, ← → through every picture and video of the channel)."""

import weakref
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImageReader, QMovie, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import audio, video
from . import icons
from .theme import T
from .widgets import IconButton, human_size

CHAT_BOX = (420, 320)


def is_image(meta):
    return meta.get("type", "").startswith("image/")


def is_media(meta):
    return is_image(meta) or video.is_video(meta)


_animated = {}


def is_animated(fid, path):
    """A GIF / WebP with more than one frame (asked once per file)."""
    if fid not in _animated:
        reader = QImageReader(str(path))
        _animated[fid] = bool(reader.supportsAnimation() and reader.imageCount() != 1
                              and QMovie(str(path)).frameCount() != 1)
    return _animated[fid]


def fit(size, box):
    w, h = size
    if w <= 0 or h <= 0:
        return box
    k = min(box[0] / w, box[1] / h, 1.0)
    return max(1, int(w * k)), max(1, int(h * k))


# ── one sound at a time: a video pauses the voice message, and the other way round ────────
class _Now(QObject):
    def __init__(self):
        super().__init__()
        self.surface = None

    def take(self, surface):
        from .player import lib
        lib().pause()
        old = self.surface
        if old is not None and old is not surface:
            try:
                old.pause()
            except RuntimeError:
                pass
        self.surface = surface

    def pause(self):
        if self.surface is not None:
            try:
                self.surface.pause()
            except RuntimeError:
                pass


now = _Now()


# ── animated pictures in the chat ───────────────────────────────────
_gifs = weakref.WeakSet()
_gif_timer = None


def _check_gifs():
    """Play the GIFs you can see while the window is active; the rest stand still."""
    for w in list(_gifs):
        try:
            win = w.window()
            on = (w.isVisible() and not w.visibleRegion().isEmpty() and win.isActiveWindow()
                  and w.core.s.get("animate_gifs", True))
            w.set_running(on)
        except RuntimeError:
            pass


class AnimatedImage(QWidget):
    clicked = Signal()

    def __init__(self, core, path, box=CHAT_BOX):
        super().__init__()
        global _gif_timer
        self.core = core
        self.movie = QMovie(str(path))
        self.movie.setCacheMode(QMovie.CacheNone)
        self.movie.jumpToFrame(0)
        size = self.movie.currentImage().size()
        w, h = fit((size.width(), size.height()), box)
        self.movie.setScaledSize(QSize(w, h))
        self.movie.jumpToFrame(0)
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self.movie.frameChanged.connect(self.update)
        _gifs.add(self)
        if _gif_timer is None:
            _gif_timer = QTimer(QApplication.instance(), interval=400, timeout=_check_gifs)
            _gif_timer.start()
        QTimer.singleShot(0, _check_gifs)

    def set_running(self, on):
        state = self.movie.state()
        if on and state == QMovie.NotRunning:
            self.movie.start()
        elif on and state == QMovie.Paused:
            self.movie.setPaused(False)
        elif not on and state == QMovie.Running:
            self.movie.setPaused(True)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 8, 8)
        p.setClipPath(path)
        p.drawPixmap(self.rect(), self.movie.currentPixmap())
        if self.movie.state() != QMovie.Running:          # «GIF»: it moves when you look
            p.setClipping(False)
            r = QRectF(8, 8, 38, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(r, 4, 4)
            p.setPen(QColor("white"))
            f = p.font()
            f.setBold(True)
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.drawText(r, Qt.AlignCenter, "GIF")
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


# ── videos ──────────────────────────────────────────────────────────
class _Probes(QObject):
    """First frame and length of each video, found off the UI thread."""

    done = Signal(str)

    def __init__(self):
        super().__init__()
        self.info = {}
        self._busy = set()
        self._pool = ThreadPoolExecutor(max_workers=1)

    def get(self, fid, path):
        if fid in self.info:
            return self.info[fid]
        if fid not in self._busy:
            self._busy.add(fid)

            def work():
                try:
                    info = video.probe(path)
                except Exception:
                    info = None
                self.info[fid] = info
                self._busy.discard(fid)
                self.done.emit(fid)
            self._pool.submit(work)
        return "loading"


_probes = None


def probes():
    global _probes
    if _probes is None:
        _probes = _Probes()
    return _probes


class SeekBar(QWidget):
    seek = Signal(float)

    def __init__(self):
        super().__init__()
        self.value = 0.0
        self.setFixedHeight(16)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        y = self.height() / 2 - 2
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawRoundedRect(QRectF(0, y, self.width(), 4), 2, 2)
        p.setBrush(QColor(T.c["accent"]))
        p.drawRoundedRect(QRectF(0, y, self.width() * self.value, 4), 2, 2)
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(self.width() * self.value, self.height() / 2), 5, 5)
        p.end()

    def _emit(self, e):
        self.seek.emit(min(1.0, max(0.0, e.position().x() / max(1, self.width()))))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._emit(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._emit(e)


class VideoSurface(QWidget):
    """A video: the first frame with a play button, then the picture with controls below."""

    expand = Signal()                       # «на весь экран»: open it in the viewer

    def __init__(self, core, meta, path, info, compact=True, radius=8):
        super().__init__()
        self.core, self.meta, self.path, self.info = core, meta, path, info
        self.compact, self.radius = compact, radius
        self.playback = None
        self._loading = False
        self._resume_at = 0.0               # where it stopped when it was scrolled away / hidden
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.bar = QWidget(self)
        self.bar.setObjectName("VideoBar")
        self.bar.setStyleSheet("#VideoBar { background: rgba(0, 0, 0, 150); border-radius: 6px; }"
                               "QLabel { color: white; background: transparent; }")
        h = QHBoxLayout(self.bar)
        h.setContentsMargins(6, 2, 8, 2)
        h.setSpacing(8)
        self.b_play = light_button("play", "Пауза / продолжить (пробел)", 16, 26, self.toggle)
        h.addWidget(self.b_play)
        self.time = QLabel("0:00")
        h.addWidget(self.time)
        self.seekbar = SeekBar()
        self.seekbar.seek.connect(self._seek)
        h.addWidget(self.seekbar, 1)
        if compact:
            h.addWidget(light_button("maximize", "Открыть в просмотре", 16, 26, self.expand))
        self.bar.hide()
        self._tick = QTimer(self, interval=200, timeout=self._update)

    # ── control ─────────────────────────────────────────────────────
    def toggle(self):
        if self.playback is None:
            self.start(self._resume_at)
        elif self.playback.paused:
            now.take(self)
            self.playback.resume()
            self._tick.start()
        else:
            self.pause()
        self._update()

    def start(self, at=0.0):
        now.take(self)
        pb = self.playback = video.Playback(self.core.voice, self.path, self.info.get("duration") or 0.0)
        pb.target = self._target()
        pb.frame_ready.connect(self.update)
        pb.loading.connect(self._set_loading)
        pb.finished.connect(self._update)
        pb.failed.connect(self._failed)
        pb.play(at)
        self.bar.show()
        self._tick.start()

    def pause(self):
        if self.playback is not None and not self.playback.paused:
            self.playback.pause()
        self._tick.stop()
        self._update()

    def stop(self):
        if self.playback is not None:
            self.playback.stop()
            self.playback = None
        self._tick.stop()

    def position(self):
        return self.playback.position() if self.playback is not None else 0.0

    def playing(self):
        return self.playback is not None and not self.playback.paused

    def _seek(self, frac):
        total = self.info.get("duration") or 0.0
        if self.playback is None:
            self.start(frac * total)
        else:
            self.playback.seek(frac * total)
        self._update()

    def _set_loading(self, on):
        self._loading = on
        self.update()

    def _failed(self, text):
        self.stop()
        self.core.toast.emit("Не получилось показать это видео — откроется в проигрывателе", "error")
        from .message import open_file
        open_file(self.core, self.meta)

    def _target(self):
        dpr = self.devicePixelRatioF()
        return max(64, int(self.width() * dpr)), max(64, int(self.height() * dpr))

    def _update(self):
        total = self.info.get("duration") or 0.0
        pos = self.position()
        playing = self.playing()
        self.b_play.set_icon("pause" if playing else "play")
        self.time.setText(f"{audio.clock(pos)} / {audio.clock(total)}")
        self.seekbar.value = min(1.0, pos / total) if total else 0.0
        self.seekbar.update()
        self.update()

    # ── look ────────────────────────────────────────────────────────
    def resizeEvent(self, e):
        bh = 30
        self.bar.setGeometry(8, self.height() - bh - 8, self.width() - 16, bh)
        if self.playback is not None:
            self.playback.target = self._target()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.radius:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
            p.setClipPath(path)
        if self.compact:
            p.fillRect(self.rect(), QColor("#000000"))
        img = self.playback.take_frame() if self.playback is not None else None
        img = img if img is not None else self.info.get("thumb")
        if img is not None and not img.isNull():
            s = img.size().scaled(self.size(), Qt.KeepAspectRatio)
            r = QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2, s.width(), s.height())
            p.drawImage(r, img)
        if self.playback is None or self._loading:          # the big round play button
            c = QPointF(self.width() / 2, self.height() / 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawEllipse(c, 28, 28)
            if self._loading:
                p.setPen(QColor("white"))
                p.drawText(QRectF(c.x() - 28, c.y() - 28, 56, 56), Qt.AlignCenter, "…")
            else:
                p.drawPixmap(int(c.x() - 11), int(c.y() - 12), icons.pixmap("play", "#ffffff", 24))
            if self.compact and self.playback is None:
                badge = f"{audio.clock(self.info.get('duration') or 0)}"
                f = p.font()
                f.setBold(True)
                p.setFont(f)
                r = QRectF(self.width() - 60, self.height() - 30, 52, 22)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 160))
                p.drawRoundedRect(r, 4, 4)
                p.setPen(QColor("white"))
                p.drawText(r, Qt.AlignCenter, badge)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.toggle()

    def mouseDoubleClickEvent(self, e):
        if self.compact:
            self.pause()
            self.expand.emit()

    def enterEvent(self, e):
        if self.playback is not None:
            self.bar.show()

    def leaveEvent(self, e):
        if self.playback is not None and self.playing() and self.compact:
            self.bar.hide()

    def hideEvent(self, e):
        # the chat was rebuilt or the channel switched: let the decoder go, remember the spot
        # (not when the whole window is minimised — the sound keeps playing then)
        if self.playback is not None and not e.spontaneous():
            self._resume_at = self.position()
            self.stop()
            self.bar.hide()
            self.update()


class VideoCard(QWidget):
    """A video attachment in the chat."""

    def __init__(self, core, msg, meta, path):
        super().__init__()
        self.core, self.msg, self.meta, self.path = core, msg, meta, path
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 4, 0, 4)
        self.surface = None
        info = probes().get(meta["id"], path)
        if info == "loading":
            self._placeholder("Готовлю превью видео…")
            probes().done.connect(self._probed)
        else:
            self._build(info)

    def _placeholder(self, text):
        ph = QLabel(f"🎬  {text}")
        ph.setFixedSize(320, 180)
        ph.setAlignment(Qt.AlignCenter)
        ph.setStyleSheet(f"background: {T.c['side']}; color: {T.c['muted']}; border-radius: 8px;")
        self.ph = ph
        self.lay.addWidget(ph)

    def _probed(self, fid):
        if fid != self.meta["id"]:
            return
        info = probes().info.get(fid)
        self.ph.hide()
        self.ph.deleteLater()
        self._build(info)

    def _build(self, info):
        if not info or not info.get("thumb"):
            from .message import open_file
            card = QLabel(f"🎬  {self.meta['name']} · {human_size(self.meta['size'])} — открыть")
            card.setTextFormat(Qt.PlainText)
            card.setCursor(Qt.PointingHandCursor)
            card.setStyleSheet(f"background: {T.c['side']}; color: {T.c['link']}; border-radius: 8px;"
                               f"padding: 12px;")
            card.mousePressEvent = lambda e: open_file(self.core, self.meta)
            self.lay.addWidget(card, 0, Qt.AlignLeft)
            return
        w, h = info["width"], info["height"]
        k = min(CHAT_BOX[0] / max(1, w), CHAT_BOX[1] / max(1, h), 1.0)
        size = (max(240, int(w * k)), max(135, int(h * k)))
        self.surface = VideoSurface(self.core, self.meta, self.path, info, compact=True)
        self.surface.setFixedSize(*size)
        self.surface.expand.connect(self._expand)
        self.lay.addWidget(self.surface, 0, Qt.AlignLeft)

    def _expand(self):
        at = self.surface.position() if self.surface else 0.0
        play = self.surface is not None and self.surface.playback is not None
        if self.surface:
            self.surface.pause()
        open_viewer(self.window(), self.core, self.msg, self.meta, start_at=at, autoplay=play)


# ── the viewer ──────────────────────────────────────────────────────
class ImageCanvas(QWidget):
    """A picture (or animated GIF) you can zoom with the wheel and drag around."""

    backdrop = Signal()                     # a click beside the picture: close the viewer
    zoomed = Signal(float)

    def __init__(self, path, animated):
        super().__init__()
        self.movie = None
        self.pixmap = None
        if animated:
            self.movie = QMovie(str(path))
            self.movie.frameChanged.connect(self.update)
            self.movie.start()
            self.src = self.movie.currentPixmap().size()
        else:
            self.pixmap = QPixmap(str(path))
            self.src = self.pixmap.size()
        self.scale = None                   # None: fit the window
        self.offset = QPointF(0, 0)         # of the picture's centre from the view's centre
        self._drag = None
        self.setMouseTracking(True)

    def valid(self):
        return self.src.width() > 0 and self.src.height() > 0

    def fit_scale(self):
        if not self.valid():
            return 1.0
        w, h = max(40, self.width() - 40), max(40, self.height() - 40)
        return min(w / self.src.width(), h / self.src.height(), 1.0)

    def current_scale(self):
        return self.fit_scale() if self.scale is None else self.scale

    def _rect(self):
        k = self.current_scale()
        w, h = self.src.width() * k, self.src.height() * k
        c = QPointF(self.width() / 2, self.height() / 2) + self.offset
        return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)

    def zoom_at(self, factor, at=None):
        old = self.current_scale()
        new = max(min(self.fit_scale(), 1.0) * 0.5, min(8.0, old * factor))
        at = at if at is not None else QPointF(self.width() / 2, self.height() / 2)
        centre = QPointF(self.width() / 2, self.height() / 2) + self.offset
        self.offset += (centre - at) * (new / old - 1)     # the point under the cursor stays put
        self.scale = new
        self._clamp()
        self.update()
        self.zoomed.emit(new)

    def reset(self):
        self.scale, self.offset = None, QPointF(0, 0)
        self.update()
        self.zoomed.emit(self.current_scale())

    def _clamp(self):
        r = self._rect()
        dx = max(0.0, (r.width() - self.width()) / 2 + 20)
        dy = max(0.0, (r.height() - self.height()) / 2 + 20)
        self.offset = QPointF(max(-dx, min(dx, self.offset.x())), max(-dy, min(dy, self.offset.y())))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, self.current_scale() < 2)
        pm = self.movie.currentPixmap() if self.movie is not None else self.pixmap
        if pm is not None and not pm.isNull():
            p.drawPixmap(self._rect(), pm, QRectF(pm.rect()))
        p.end()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120
        if steps:
            self.zoom_at(1.2 ** steps, e.position())

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if not self._rect().contains(e.position()):
            self.backdrop.emit()
        elif self.current_scale() > self.fit_scale() + 1e-6:
            self._drag = (e.position(), QPointF(self.offset))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            start, off = self._drag
            self.offset = off + (e.position() - start)
            self._clamp()
            self.update()
        elif self._rect().contains(e.position()) and self.current_scale() > self.fit_scale() + 1e-6:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        if self.scale is None or abs(self.current_scale() - self.fit_scale()) < 1e-6:
            target = 1.0 if self.fit_scale() < 0.999 else 2.0
            self.zoom_at(target / self.current_scale(), e.position())
        else:
            self.reset()

    def resizeEvent(self, e):
        self._clamp()
        self.zoomed.emit(self.current_scale())


class MediaViewer(QWidget):
    """Over the whole window: one picture or video of the channel, ← → for the others."""

    def __init__(self, win, core, items, index, start_at=0.0, autoplay=False):
        super().__init__(win)
        self.win, self.core, self.items, self.index = win, core, items, index
        self.content = None
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("MediaViewer { background: rgba(0, 0, 0, 215); }"
                           "QLabel { color: white; background: transparent; }")
        self.setFocusPolicy(Qt.StrongFocus)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        top = QWidget()
        th = QHBoxLayout(top)
        th.setContentsMargins(18, 10, 12, 6)
        self.who = QLabel()
        self.who.setStyleSheet(f"font-weight: 600; font-size: {T.px(10)}pt;")
        self.what = QLabel()
        self.what.setStyleSheet("color: rgba(255,255,255,170);")
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(self.who)
        col.addWidget(self.what)
        th.addLayout(col, 1)
        self.zoom_label = QLabel()
        self.zoom_label.setStyleSheet("color: rgba(255,255,255,170);")
        th.addWidget(self.zoom_label)
        for name, tip, fn in (("eye", "Открыть в программе Windows", self._open),
                              ("download", "Сохранить как…", self._save),
                              ("x", "Закрыть (Esc)", self.close)):
            th.addWidget(light_button(name, tip, 20, 36, fn))
        v.addWidget(top)
        mid = QHBoxLayout()
        mid.setContentsMargins(8, 0, 8, 0)
        self.b_prev = self._arrow("chevron_left", "Предыдущее (←)", lambda: self.step(-1))
        self.b_next = self._arrow("chevron_right", "Следующее (→)", lambda: self.step(1))
        self.stage = QVBoxLayout()
        mid.addWidget(self.b_prev)
        mid.addLayout(self.stage, 1)
        mid.addWidget(self.b_next)
        v.addLayout(mid, 1)
        self.counter = QLabel()
        self.counter.setAlignment(Qt.AlignCenter)
        self.counter.setStyleSheet("color: rgba(255,255,255,170); padding: 6px 0 12px 0;")
        v.addWidget(self.counter)
        win.installEventFilter(self)
        core.file_ready.connect(self._file_ready)
        self.setGeometry(win.rect())
        self._show(start_at, autoplay)
        self.show()
        self.raise_()
        self.setFocus()

    def _arrow(self, name, tip, fn):
        return light_button(name, tip, 28, 48, fn)

    def eventFilter(self, obj, e):
        if obj is self.win and e.type() == e.Type.Resize:
            self.setGeometry(self.win.rect())
        return False

    # ── items ───────────────────────────────────────────────────────
    def _item(self):
        return self.items[self.index]

    def _show(self, start_at=0.0, autoplay=False):
        if self.content is not None:
            if isinstance(self.content, VideoSurface):
                self.content.stop()
            self.content.hide()
            self.content.deleteLater()
            self.content = None
        msg, meta = self._item()
        member = self.core.member(msg["author"])
        from .message import when
        self.who.setText(member["name"])
        self.what.setText(f"{when(msg['ts'])} · {meta['name']} · {human_size(meta['size'])}")
        for lb in (self.who, self.what):
            lb.setTextFormat(Qt.PlainText)            # a file name is just text
        self.counter.setText(f"{self.index + 1} из {len(self.items)}" if len(self.items) > 1 else "")
        self.b_prev.setVisible(self.index > 0)
        self.b_next.setVisible(self.index < len(self.items) - 1)
        self.zoom_label.setText("")
        path = self.core.file_path(meta["id"])
        if not path:
            self.core.request_file(meta["id"], prefer=msg["author"], only=self.core.dm_peer.get(msg["ch"]))
            w = QLabel("Файл загружается у участников…")
            w.setAlignment(Qt.AlignCenter)
        elif video.is_video(meta):
            info = probes().get(meta["id"], path)
            if info == "loading":
                w = QLabel("Готовлю видео…")
                w.setAlignment(Qt.AlignCenter)
                probes().done.connect(self._probed)
            elif not info:
                w = QLabel("Это видео здесь не показать — кнопка с глазом откроет его в проигрывателе")
                w.setAlignment(Qt.AlignCenter)
            else:
                w = VideoSurface(self.core, meta, path, info, compact=False, radius=0)
                w.bar.show()
                if autoplay or start_at:
                    QTimer.singleShot(0, lambda s=w, a=start_at, go=autoplay: self._start_video(s, a, go))
        else:
            w = ImageCanvas(path, is_animated(meta["id"], path))
            w.backdrop.connect(self.close)
            w.zoomed.connect(lambda k: self.zoom_label.setText(f"{round(k * 100)}%"))
            if not w.valid():
                w = QLabel("Не получилось открыть эту картинку")
                w.setAlignment(Qt.AlignCenter)
        # Expanding, or the fixed-height arrows beside it would cap the row's height
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.content = w
        self.stage.addWidget(w)
        self.setFocus()

    def _start_video(self, surface, at, go):
        try:
            surface.start(at)
            if not go:
                surface.pause()
        except RuntimeError:
            pass

    def _probed(self, fid):
        if fid == self._item()[1]["id"] and isinstance(self.content, QLabel):
            self._show()

    def _file_ready(self, fid):
        if fid == self._item()[1]["id"] and isinstance(self.content, QLabel):
            self._show()

    def step(self, d):
        i = self.index + d
        if 0 <= i < len(self.items):
            self.index = i
            self._show()

    # ── actions ─────────────────────────────────────────────────────
    def _open(self):
        from .message import open_file
        if isinstance(self.content, VideoSurface):
            self.content.pause()
        open_file(self.core, self._item()[1])

    def _save(self):
        from .message import open_file
        open_file(self.core, self._item()[1], save=True)

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.close()
        elif k == Qt.Key_Left:
            self.step(-1)
        elif k == Qt.Key_Right:
            self.step(1)
        elif k == Qt.Key_Space and isinstance(self.content, VideoSurface):
            self.content.toggle()
        elif isinstance(self.content, ImageCanvas) and k in (Qt.Key_Plus, Qt.Key_Equal):
            self.content.zoom_at(1.25)
        elif isinstance(self.content, ImageCanvas) and k == Qt.Key_Minus:
            self.content.zoom_at(0.8)
        elif isinstance(self.content, ImageCanvas) and k == Qt.Key_0:
            self.content.reset()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not isinstance(self.content, (ImageCanvas, VideoSurface)):
            self.close()

    def closeEvent(self, e):
        if isinstance(self.content, VideoSurface):
            self.content.stop()
        self.win.removeEventFilter(self)
        global _viewer
        _viewer = None
        self.deleteLater()
        super().closeEvent(e)


_viewer = None


def viewer_open():
    return _viewer is not None


def open_viewer(win, core, msg, meta, start_at=0.0, autoplay=False):
    """Every picture and video of the message's channel, starting at this one."""
    global _viewer
    if _viewer is not None:
        try:
            _viewer.close()
        except RuntimeError:
            pass
    store = core.store_for(msg["ch"])
    items = [(m, f) for m in store.visible_messages(msg["ch"]) for f in m["files"] if is_media(f)]
    index = next((i for i, (m, f) in enumerate(items) if m["id"] == msg["id"] and f["id"] == meta["id"]), None)
    if index is None:
        items, index = [(msg, meta)], 0
    _viewer = MediaViewer(win, core, items, index, start_at, autoplay)
    return _viewer


def light_button(name, tip, size, box, fn):
    """A white icon button for the dark viewer and the video controls."""
    b = IconButton(name, tip, size, box)
    b.hover_bg = "rgba(255,255,255,40)"
    b.fixed_color = "#ffffff"
    b._refresh()
    b.clicked.connect(fn)
    return b
