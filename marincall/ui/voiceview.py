"""Voice channel view (participant tiles, the stream you watch, controls) and the source picker."""

import math

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMenu, QSlider, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from .. import stream
from . import icons, theme
from .theme import T, mix
from .widgets import Avatar, button, label

SMALL = (200, 112)              # participant tiles under the stream you watch
BADGE = "background: rgba(0,0,0,0.55); color: white; border-radius: 4px; padding: 3px 8px; font-weight: 600;"


def _clear(layout):
    while layout.count():
        it = layout.takeAt(0)
        if it.widget():
            it.widget().hide()              # gone at once, not on the next event loop pass
            it.widget().deleteLater()
        elif it.layout():
            _clear(it.layout())
            it.layout().deleteLater()


def _live_badge(size=8):
    live = QLabel("В ЭФИРЕ")
    live.setStyleSheet(f"background: {T.c['red']}; color: white; border-radius: 4px;"
                       f"font-size: {T.px(size)}pt; font-weight: 800; padding: 2px 6px;")
    return live


def _fit(img_w, img_h, rect):
    """The largest rect with the picture's aspect ratio inside `rect`, centred."""
    if img_w <= 0 or img_h <= 0:
        return rect
    k = min(rect.width() / img_w, rect.height() / img_h)
    w, h = img_w * k, img_h * k
    return QRectF(rect.x() + (rect.width() - w) / 2, rect.y() + (rect.height() - h) / 2, w, h)


def _draw_frame(p, img, rect):
    """A decoded picture, letterboxed; it usually arrives at exactly the size it is shown."""
    dpr = p.device().devicePixelRatioF() if hasattr(p.device(), "devicePixelRatioF") else 1.0
    target = _fit(img.width() / dpr, img.height() / dpr, rect)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawImage(target, img)


class Tile(QFrame):
    """A participant. mode: "normal" | "small" (under a watched stream) | "watch" (the stream)."""

    def __init__(self, view, uid, mode="normal"):
        super().__init__()
        self.view, self.core, self.uid, self.mode = view, view.core, uid, mode
        m = self.core.member(uid)
        self.m = m
        self.preview = None          # live thumbnail of our own screen share
        self.frame = None            # the picture of the stream we watch (mode "watch")
        self.stats = None
        self.speaking = self.core.is_speaking(uid) and not m["state"].get("muted")
        st = m["state"]
        self.setCursor(Qt.PointingHandCursor if st.get("streaming") and uid != self.core.me
                       else Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        small = mode == "small"
        lay.setContentsMargins(8, 6, 8, 6) if small else lay.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        if st.get("streaming"):
            top.addWidget(_live_badge(7 if small else 8))
        top.addStretch(1)
        if mode == "watch":
            self.stats = QLabel()
            self.stats.setStyleSheet(BADGE + "font-weight: 500;")
            top.addWidget(self.stats)
            for name, tip, fn in (("screen", "Во весь экран (F, двойной щелчок)", view.open_fullscreen),
                                  ("x", "Прекратить просмотр", self.core.unwatch)):
                b = QToolButton()                   # white on the picture, whatever the theme
                b.setIcon(icons.icon(name, "white", 18))
                b.setIconSize(QSize(18, 18))
                b.setFixedSize(30, 30)
                b.setToolTip(tip)
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet("QToolButton { background: rgba(0,0,0,0.55); border: none; border-radius: 4px; }"
                                "QToolButton:hover { background: rgba(0,0,0,0.8); }")
                b.clicked.connect(fn)
                top.addWidget(b)
        lay.addLayout(top)
        lay.addStretch(1)
        self.avatar = Avatar.of(m, 44 if small else 76)
        if mode != "watch":
            center = QHBoxLayout()
            center.addStretch(1)
            center.addWidget(self.avatar)
            center.addStretch(1)
            lay.addLayout(center)
        else:
            self.avatar.hide()
        if st.get("streaming") and mode == "normal":
            lay.addSpacing(8)
            if uid == self.core.me:
                self.avatar.hide()          # the preview itself shows who is sharing what
                self.stats = QLabel()
                self.stats.setAlignment(Qt.AlignCenter)
                self.stats.setStyleSheet(BADGE)
                lay.addWidget(self.stats, 0, Qt.AlignHCenter)
                lay.addSpacing(6)
            row = QHBoxLayout()
            row.addStretch(1)
            if uid == self.core.me:
                b = button("Остановить трансляцию", "danger", self.core.stop_stream)
            else:
                b = button("Смотреть трансляцию", None, lambda: self.core.watch(uid))
            row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
        elif st.get("streaming") and small and uid == self.core.me:
            self.avatar.hide()
        lay.addStretch(1)
        bottom = QHBoxLayout()
        name = QLabel(m["name"] + (" (вы)" if uid == self.core.me else ""))
        name.setStyleSheet(BADGE + (f"font-size: {T.px(8)}pt; padding: 2px 6px;" if small else ""))
        bottom.addWidget(name)
        for flag, icon_name in (("muted", "mic_off"), ("deafened", "headphones_off")):
            if st.get(flag):
                ic = QLabel()
                ic.setPixmap(icons.pixmap(icon_name, "white", 14 if small else 16))
                ic.setStyleSheet("background: rgba(0,0,0,0.55); border-radius: 4px; padding: 3px;")
                bottom.addWidget(ic)
        bottom.addStretch(1)
        lay.addLayout(bottom)
        self.avatar.set_speaking(self.speaking)
        self.update_stats()

    def set_speaking(self, on):
        self.speaking = on
        self.avatar.set_speaking(on)
        self.update()

    def set_preview(self, pixmap):
        self.preview = pixmap
        self.update()

    def set_frame(self, img):
        self.frame = img
        self.update()

    def update_stats(self):
        if self.stats is None:
            return
        if self.mode == "watch":
            w, h = self.core.viewer.source_size
            fps = self.core.viewer.fps
            self.stats.setText(f"{w}×{h} · {fps:.0f} к/с" if w else "Подключаемся…")
        else:
            rate = self.core.sender.bitrate()
            watchers = len(self.core.watchers)
            who = "никто не смотрит" if not watchers else f"смотрят: {watchers}"
            self.stats.setText(f"{who}   ·   {rate:.1f} Мбит/с".replace(".", ","))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        watching = self.mode == "watch"
        p.setBrush(QColor("#000000" if watching else mix(self.m["color"], T.c["rail"], 0.55)))
        p.setPen(QPen(QColor(T.c["green"]), 3) if self.speaking and not watching else Qt.NoPen)
        p.drawRoundedRect(r, 10, 10)
        picture = self.frame if watching else self.preview
        if picture is not None and not picture.isNull():
            path = QPainterPath()
            path.addRoundedRect(r, 10, 10)
            p.setClipPath(path)
            if watching:
                _draw_frame(p, picture, r)
            else:
                scaled = picture.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                p.drawPixmap(int((self.width() - scaled.width()) / 2),
                             int((self.height() - scaled.height()) / 2), scaled)
            p.setClipping(False)
            if self.speaking and not watching:
                p.setPen(QPen(QColor(T.c["green"]), 3))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(r, 10, 10)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.mode == "watch":
            self.view.update_target()

    def mouseDoubleClickEvent(self, e):
        if self.mode == "watch":
            self.view.open_fullscreen()
        elif self.uid != self.core.me and self.m["state"].get("streaming"):
            self.core.watch(self.uid)

    def contextMenuEvent(self, e):
        if self.mode == "watch":
            self.view.stream_menu(e.globalPos())


class StreamWindow(QWidget):
    """The stream you watch, full screen. Esc, F or a double click returns to the app."""

    def __init__(self, view, title):
        super().__init__(None, Qt.Window)
        self.view = view
        self.frame = None
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.BlankCursor)
        self._cursor_timer = QTimer(self, singleShot=True, interval=2000,
                                    timeout=lambda: self.setCursor(Qt.BlankCursor))
        self.setMouseTracking(True)

    def set_frame(self, img):
        self.frame = img
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.frame is not None and not self.frame.isNull():
            _draw_frame(p, self.frame, QRectF(self.rect()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.view.update_target()

    def mouseMoveEvent(self, e):
        self.setCursor(Qt.ArrowCursor)          # show the cursor for a moment when it moves
        self._cursor_timer.start()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_F, 0x410):     # 0x410: «А» — F on the Russian layout
            self.close()
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.close()

    def contextMenuEvent(self, e):
        self.view.stream_menu(e.globalPos())

    def closeEvent(self, e):
        self.view.fullscreen_closed(self)
        super().closeEvent(e)


class VoiceView(QWidget):
    stream_requested = Signal()

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.cid = None
        self.tiles = {}
        self.preview = None                 # last frame of our own screen share
        self.frame = None                   # last picture of the stream we watch
        self.big = None                     # the tile showing it
        self.full = None                    # …or the full-screen window
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QFrame()
        header.setObjectName("ChatHeader")
        header.setFixedHeight(48)
        h = QHBoxLayout(header)
        h.setContentsMargins(16, 0, 16, 0)
        ic = QLabel()
        ic.setPixmap(icons.pixmap("volume", T.c["muted"], 22))
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {T.c['header']}; font-weight: 700; font-size: {T.px(11)}pt;")
        h.addWidget(ic)
        h.addWidget(self.title)
        h.addStretch(1)
        lay.addWidget(header)

        stage_bg = T.c["rail"] if not T.light else T.c["side"]
        self.stage = QWidget()
        self.stage.setObjectName("Stage")
        self.stage.setAttribute(Qt.WA_StyledBackground, True)
        self.stage.setStyleSheet(f"#Stage {{ background: {stage_bg}; }}")
        self.grid = QVBoxLayout(self.stage)          # rows of tiles, each row centred
        self.grid.setContentsMargins(24, 24, 24, 24)
        self.grid.setSpacing(12)
        lay.addWidget(self.stage, 1)

        self.controls = QFrame()
        self.controls.setObjectName("VoiceControls")
        self.controls.setStyleSheet(f"#VoiceControls {{ background: {stage_bg}; }}")
        c = QHBoxLayout(self.controls)
        c.setContentsMargins(0, 8, 0, 20)
        c.setSpacing(14)
        c.addStretch(1)
        self.b_mic = self._round("mic", "Микрофон", core.toggle_mute)
        self.b_deaf = self._round("headphones", "Звук", core.toggle_deafen)
        self.b_share = self._round("screen", "Демонстрация экрана", self._share)
        self.b_leave = self._round("phone_off", "Отключиться", core.leave_voice, danger=True)
        for b in (self.b_mic, self.b_deaf, self.b_share, self.b_leave):
            c.addWidget(b)
        c.addStretch(1)
        lay.addWidget(self.controls)

        core.voice_changed.connect(self.rebuild)
        core.stream_changed.connect(self.rebuild)
        core.members_changed.connect(self.rebuild)
        core.speaking_changed.connect(self._speaking)
        core.preview.frame.connect(self._on_preview)
        core.viewer.frame_ready.connect(self._on_frame)
        self._stats_timer = QTimer(self, interval=1000, timeout=self._update_stats)
        self._stats_timer.start()

    def _round(self, name, tip, fn, danger=False):
        b = QToolButton()
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(56, 56)
        b.setIconSize(QSize(24, 24))
        b.clicked.connect(fn)
        b._name, b._danger = name, danger
        return b

    def _paint_round(self, b, name, active):
        bg = T.c["red"] if (b._danger or active) else T.c["active"]
        b.setIcon(icons.icon(name, "white" if (b._danger or active) else T.c["header"], 24))
        b.setStyleSheet(f"QToolButton {{ background: {bg}; border: none; border-radius: 28px; }}"
                        f"QToolButton:hover {{ background: {mix(bg, '#ffffff', 0.12)}; }}")

    def _share(self):
        if self.core.sender.running:
            self.core.stop_stream()
        else:
            self.stream_requested.emit()

    def set_channel(self, cid):
        self.cid = cid
        self.rebuild()

    def rebuild(self):
        if not self.cid:
            return
        ch = self.core.store.channel(self.cid)
        if not ch:
            return
        self.title.setText(ch["name"])
        _clear(self.grid)
        self.tiles, self.big = {}, None
        joined = self.core.my_voice == self.cid
        uids = self.core.voice_members(self.cid)
        watched = self.core.viewer.uid if joined and self.core.viewer.uid in uids else None
        if watched is None:
            self.frame = None
            if self.full:
                self.full.close()
        if not joined:
            self.grid.addStretch(1)
            self.grid.addWidget(self._invite(ch, uids), 0, Qt.AlignHCenter)
            self.grid.addStretch(1)
        elif watched:
            # the stream you watch fills the stage, everyone else in a row underneath
            self.big = Tile(self, watched, "watch")
            self.big.set_frame(self.frame)
            self.tiles[watched] = self.big
            self.grid.addWidget(self.big, 1, Qt.AlignHCenter)
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addStretch(1)
            for uid in uids:
                if uid != watched:
                    t = Tile(self, uid, "small")
                    if uid == self.core.me and self.core.sender.running:
                        t.set_preview(self.preview)
                    t.setFixedSize(*SMALL)
                    self.tiles[uid] = t
                    row.addWidget(t)
            row.addStretch(1)
            self.grid.addLayout(row)
            self._size_tiles()
        else:
            self.grid.addStretch(1)
            cols = max(1, min(4, math.ceil(math.sqrt(len(uids)))))
            for start in range(0, len(uids), cols):
                row = QHBoxLayout()
                row.setSpacing(12)
                row.addStretch(1)
                for uid in uids[start:start + cols]:
                    t = Tile(self, uid)
                    if uid == self.core.me and self.core.sender.running:
                        t.set_preview(self.preview)
                    self.tiles[uid] = t
                    row.addWidget(t)
                row.addStretch(1)
                self.grid.addLayout(row)
            self._cols = cols
            self._size_tiles()
            self.grid.addStretch(1)
        self.controls.setVisible(joined)
        s = self.core.s
        self._paint_round(self.b_mic, "mic_off" if s["muted"] or s["deafened"] else "mic",
                          s["muted"] or s["deafened"])
        self._paint_round(self.b_deaf, "headphones_off" if s["deafened"] else "headphones", s["deafened"])
        self._paint_round(self.b_share, "screen_off" if self.core.sender.running else "screen",
                          self.core.sender.running)
        self._paint_round(self.b_leave, "phone_off", False)
        self.update_target()

    _cols = 1

    def _size_tiles(self):
        if not self.tiles:
            return
        if self.big is not None:
            others = len(self.tiles) - 1
            w_avail = self.stage.width() - 48
            h_avail = self.stage.height() - 48 - ((SMALL[1] + 12) if others else 0)
            w = int(max(320, min(w_avail, h_avail * 16 / 9)))
            self.big.setFixedSize(w, w * 9 // 16)
            return
        cols = self._cols
        rows = math.ceil(len(self.tiles) / cols)
        w_avail = (self.stage.width() - 48 - 12 * (cols - 1)) / cols
        h_avail = (self.stage.height() - 48 - 12 * (rows - 1)) / rows
        w = int(max(240, min(720, w_avail, h_avail * 16 / 9)))
        for t in self.tiles.values():
            t.setFixedSize(w, w * 9 // 16)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._size_tiles()

    # ── the stream you watch ────────────────────────────────────────
    def update_target(self):
        """Tell the decoder how big the picture is shown, and whether anyone sees it."""
        viewer = self.core.viewer
        shown = self.full if self.full is not None else (self.big if self.isVisible() else None)
        viewer.show_video = shown is not None
        if shown is not None:
            dpr = shown.devicePixelRatioF()
            viewer.target = (max(160, int(shown.width() * dpr)), max(90, int(shown.height() * dpr)))

    def _on_frame(self):
        img = self.core.viewer.take_frame()
        if img is None:
            return
        self.frame = img
        if self.full is not None:
            self.full.set_frame(img)
        elif self.big is not None:
            self.big.set_frame(img)

    def open_fullscreen(self):
        uid = self.core.viewer.uid
        if not uid:
            return
        if self.full is None:
            self.full = StreamWindow(self, f"Трансляция — {self.core.name_of(uid)}")
            self.full.set_frame(self.frame)
            screen = self.window().screen()
            if screen:
                self.full.setGeometry(screen.geometry())
            self.full.showFullScreen()
        self.full.raise_()
        self.full.activateWindow()
        self.update_target()

    def fullscreen_closed(self, win):
        if self.full is win:
            self.full = None
            if self.big is not None and self.frame is not None:
                self.big.set_frame(self.frame)
            self.update_target()

    def stream_menu(self, pos):
        s = self.core.s
        m = QMenu(self)
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 6, 10, 6)
        cap = label(f"Громкость трансляции · {s['stream_volume']}%", "caption")
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 200)
        slider.setValue(s["stream_volume"])
        slider.setMinimumWidth(200)

        def changed(val):
            s["stream_volume"] = val
            cap.setText(f"Громкость трансляции · {val}%")
        slider.valueChanged.connect(changed)
        slider.sliderReleased.connect(s.save)
        v.addWidget(cap)
        v.addWidget(slider)
        act = QWidgetAction(m)
        act.setDefaultWidget(box)
        m.addAction(act)
        m.addSeparator()
        if self.full is None:
            m.addAction("Во весь экран", self.open_fullscreen)
        else:
            m.addAction("Выйти из полноэкранного режима", self.full.close)
        m.addAction("Прекратить просмотр", self.core.unwatch)
        m.exec(pos)

    def showEvent(self, e):
        super().showEvent(e)
        self.update_target()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.update_target()          # in another channel: keep the sound, skip the pictures

    def _invite(self, ch, uids):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        v.addWidget(label(ch["name"], "h1"), 0, Qt.AlignHCenter)
        who = ", ".join(self.core.name_of(u) for u in uids) if uids else "Пока никого нет"
        v.addWidget(label(who, "muted"), 0, Qt.AlignHCenter)
        row = QHBoxLayout()
        row.addStretch(1)
        for u in uids[:8]:
            row.addWidget(Avatar.of(self.core.member(u), 40))
        row.addStretch(1)
        v.addLayout(row)
        b = button("Присоединиться к голосовому каналу", "success",
                   lambda: self.core.join_voice(self.cid))
        b.setMinimumHeight(40)
        v.addWidget(b, 0, Qt.AlignHCenter)
        return w

    def _speaking(self, uid, on):
        t = self.tiles.get(uid)
        if t:
            t.set_speaking(on and not self.core.member(uid)["state"].get("muted"))

    def _on_preview(self, image):
        self.preview = QPixmap.fromImage(image)
        tile = self.tiles.get(self.core.me)
        if tile:
            tile.set_preview(self.preview)

    def _update_stats(self):
        for tile in self.tiles.values():
            if tile.stats is not None:
                tile.update_stats()


def _thumbnail(mon):
    try:
        import mss
        with mss.MSS() as sct:
            shot = sct.grab({"left": mon["left"], "top": mon["top"],
                             "width": mon["width"], "height": mon["height"]})
        img = QImage(shot.rgb, shot.width, shot.height, shot.width * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(img.scaled(256, 144, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    except Exception:
        return icons.pixmap("screen", T.c["muted"], 96, 1.2)


class SourcePicker(QDialog):
    """Pick a monitor or a window, quality and audio, then start streaming."""

    def __init__(self, core, parent):
        super().__init__(parent)
        self.core = core
        self.source = None
        self.setWindowTitle("Демонстрация экрана")
        self.setMinimumSize(640, 560)
        self.setStyleSheet(f"QDialog {{ background: {T.c['main']}; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(12)
        v.addWidget(label("Демонстрация экрана", "h2"))
        v.addWidget(label("Смотреть смогут все, кто в вашем голосовом канале. Видео идёт напрямую "
                          "каждому зрителю через FFmpeg" + (" с аппаратным NVENC." if stream.has_nvenc()
                                                           else " (кодирование на процессоре)."),
                          "muted", wrap=True))
        tabs = QHBoxLayout()
        self.tab_group = QButtonGroup(self)
        self.pages = QStackedWidget()
        for i, name in enumerate(("Экраны", "Окна")):
            b = QToolButton()
            b.setText(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"QToolButton {{ border: none; padding: 6px 14px; border-radius: 4px;"
                            f"color: {T.c['muted']}; font-weight: 600; }}"
                            f"QToolButton:checked {{ background: {T.c['active']}; color: {T.c['header']}; }}"
                            f"QToolButton:hover {{ color: {T.c['header']}; }}")
            self.tab_group.addButton(b, i)
            tabs.addWidget(b)
        tabs.addStretch(1)
        self.tab_group.idClicked.connect(self.pages.setCurrentIndex)
        v.addLayout(tabs)

        screens = QWidget()
        grid = QGridLayout(screens)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setSpacing(12)
        self.mon_group = QButtonGroup(self)
        self.monitors = stream.list_monitors()
        for i, mon in enumerate(self.monitors):
            b = QToolButton()
            b.setCheckable(True)
            b.setIcon(QIcon(_thumbnail(mon)))
            b.setIconSize(QSize(256, 144))
            b.setText(mon["label"])
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"QToolButton {{ background: {T.c['side']}; border: 2px solid transparent;"
                            f"border-radius: 8px; padding: 8px; color: {T.c['text']}; }}"
                            f"QToolButton:checked {{ border: 2px solid {T.c['accent']}; }}"
                            f"QToolButton:hover {{ background: {T.c['hover']}; }}")
            self.mon_group.addButton(b, i)
            grid.addWidget(b, i // 2, i % 2)
            if i == 0:
                b.setChecked(True)
        grid.setRowStretch(math.ceil(len(self.monitors) / 2), 1)    # thumbnails stay at the top
        self.pages.addWidget(screens)
        self.win_list = QListWidget()
        for w in stream.list_windows(exclude_titles=(parent.windowTitle(),)):
            item = QListWidgetItem(icons.icon("screen", T.c["muted"], 18), w["title"])
            item.setData(Qt.UserRole, w)
            self.win_list.addItem(item)
        self.pages.addWidget(self.win_list)
        v.addWidget(self.pages, 1)

        opts = QGridLayout()
        opts.setHorizontalSpacing(12)
        self.quality = QComboBox()
        for key, q in stream.QUALITY.items():
            self.quality.addItem(q["label"], key)
        self.quality.setCurrentIndex(max(0, self.quality.findData(core.s["stream_quality"])))
        self.audio = QComboBox()
        for text, value in stream.audio_choices():
            self.audio.addItem(text, value)
        self.audio.setCurrentIndex(max(0, self.audio.findData(core.s["stream_audio"])))
        opts.addWidget(label("КАЧЕСТВО", "caption"), 0, 0)
        opts.addWidget(label("ЗВУК ТРАНСЛЯЦИИ", "caption"), 0, 1)
        opts.addWidget(self.quality, 1, 0)
        opts.addWidget(self.audio, 1, 1)
        v.addLayout(opts)
        hint = label("«Звук компьютера» — всё, что играет на этом ПК, кроме самого MarinCall: зрители слышат игру или видео, но не голоса из канала и не себя. Нужна Windows 10 2004 или новее.", "hint", wrap=True)
        v.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Отмена", "link", self.reject))
        go = button("Начать трансляцию", None, self._accept)
        go.setMinimumHeight(38)
        row.addWidget(go)
        v.addLayout(row)

    def showEvent(self, e):
        super().showEvent(e)
        theme.style_window(self)

    def _accept(self):
        if self.pages.currentIndex() == 0:
            i = self.mon_group.checkedId()
            self.source = self.monitors[i] if 0 <= i < len(self.monitors) else None
        else:
            item = self.win_list.currentItem()
            self.source = item.data(Qt.UserRole) if item else None
        if not self.source:
            return
        self.core.s["stream_quality"] = self.quality.currentData()
        self.core.s["stream_audio"] = self.audio.currentData()
        self.core.s.save()
        self.accept()
