"""Voice channel view (participant tiles, controls) and the screen-share source picker."""

import math

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import stream
from . import icons
from .theme import T, mix
from .widgets import Avatar, button, label


class Tile(QFrame):
    def __init__(self, view, uid):
        super().__init__()
        self.view, self.core, self.uid = view, view.core, uid
        m = self.core.member(uid)
        self.m = m
        self.preview = None          # live thumbnail of our own screen share
        self.stats = None
        self.speaking = self.core.is_speaking(uid) and not m["state"].get("muted")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        st = m["state"]
        if st.get("streaming"):
            live = QLabel("В ЭФИРЕ")
            live.setStyleSheet(f"background: {T.c['red']}; color: white; border-radius: 4px;"
                               f"font-size: {T.px(8)}pt; font-weight: 800; padding: 2px 6px;")
            top.addWidget(live)
        top.addStretch(1)
        lay.addLayout(top)
        lay.addStretch(1)
        center = QHBoxLayout()
        self.avatar = Avatar(m["name"], m["color"], 76)
        center.addStretch(1)
        center.addWidget(self.avatar)
        center.addStretch(1)
        lay.addLayout(center)
        if st.get("streaming"):
            lay.addSpacing(8)
            if uid == self.core.me:
                self.avatar.hide()          # the preview itself shows who is sharing what
                self.stats = QLabel()
                self.stats.setAlignment(Qt.AlignCenter)
                self.stats.setStyleSheet("background: rgba(0,0,0,0.45); color: white;"
                                         "border-radius: 4px; padding: 3px 8px; font-weight: 600;")
                lay.addWidget(self.stats, 0, Qt.AlignHCenter)
                lay.addSpacing(6)
                self.update_stats()
            row = QHBoxLayout()
            row.addStretch(1)
            if uid == self.core.me:
                b = button("Остановить трансляцию", "danger", self.core.stop_stream)
            elif self.core.viewer.uid == uid:
                b = button("Прекратить просмотр", "secondary", self.core.unwatch)
            else:
                b = button("Смотреть трансляцию", None, lambda: self.core.watch(uid))
            row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)
        bottom = QHBoxLayout()
        name = QLabel(m["name"] + ("  (вы)" if uid == self.core.me else ""))
        name.setStyleSheet("background: rgba(0,0,0,0.45); color: white; border-radius: 4px;"
                           "padding: 3px 8px; font-weight: 600;")
        bottom.addWidget(name)
        for flag, icon_name in (("muted", "mic_off"), ("deafened", "headphones_off")):
            if st.get(flag):
                ic = QLabel()
                ic.setPixmap(icons.pixmap(icon_name, "white", 16))
                ic.setStyleSheet("background: rgba(0,0,0,0.45); border-radius: 4px; padding: 3px;")
                bottom.addWidget(ic)
        bottom.addStretch(1)
        lay.addLayout(bottom)
        self.avatar.set_speaking(self.speaking)

    def set_speaking(self, on):
        self.speaking = on
        self.avatar.set_speaking(on)
        self.update()

    def set_preview(self, pixmap):
        self.preview = pixmap
        self.update()

    def update_stats(self):
        if self.stats is not None:
            rate = self.core.sender.bitrate()
            watchers = len(self.core.watchers)
            who = "никто не смотрит" if not watchers else f"смотрят: {watchers}"
            self.stats.setText(f"{who}   ·   {rate:.1f} Мбит/с".replace(".", ","))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setBrush(QColor(mix(self.m["color"], T.c["rail"], 0.55)))
        p.setPen(QPen(QColor(T.c["green"]), 3) if self.speaking else Qt.NoPen)
        p.drawRoundedRect(r, 10, 10)
        if self.preview and not self.preview.isNull():
            path = QPainterPath()
            path.addRoundedRect(r, 10, 10)
            p.setClipPath(path)
            scaled = self.preview.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            p.drawPixmap(int((self.width() - scaled.width()) / 2),
                         int((self.height() - scaled.height()) / 2), scaled)
            p.setClipping(False)
            if self.speaking:
                p.setPen(QPen(QColor(T.c["green"]), 3))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(r, 10, 10)


class VoiceView(QWidget):
    stream_requested = Signal()

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.cid = None
        self.tiles = {}
        self.preview = None                 # last frame of our own screen share
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

        self.stage = QWidget()
        self.stage.setStyleSheet(f"background: {T.c['rail'] if not T.light else T.c['side']};")
        self.grid = QGridLayout(self.stage)
        self.grid.setContentsMargins(24, 24, 24, 24)
        self.grid.setSpacing(12)
        lay.addWidget(self.stage, 1)

        self.controls = QFrame()
        self.controls.setStyleSheet(f"background: {T.c['rail'] if not T.light else T.c['side']};")
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
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.tiles = {}
        joined = self.core.my_voice == self.cid
        uids = self.core.voice_members(self.cid)
        if not joined:
            self.grid.addWidget(self._invite(ch, uids), 0, 0, Qt.AlignCenter)
        else:
            cols = max(1, min(4, math.ceil(math.sqrt(len(uids)))))
            for i, uid in enumerate(uids):
                t = Tile(self, uid)
                if uid == self.core.me and self.core.sender.running:
                    t.set_preview(self.preview)
                self.tiles[uid] = t
                self.grid.addWidget(t, i // cols, i % cols, Qt.AlignCenter)
            self._cols = cols
            self._size_tiles()
        self.controls.setVisible(joined)
        s = self.core.s
        self._paint_round(self.b_mic, "mic_off" if s["muted"] or s["deafened"] else "mic",
                          s["muted"] or s["deafened"])
        self._paint_round(self.b_deaf, "headphones_off" if s["deafened"] else "headphones", s["deafened"])
        self._paint_round(self.b_share, "screen_off" if self.core.sender.running else "screen",
                          self.core.sender.running)
        self._paint_round(self.b_leave, "phone_off", False)

    _cols = 1

    def _size_tiles(self):
        if not self.tiles:
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
            m = self.core.member(u)
            row.addWidget(Avatar(m["name"], m["color"], 40))
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
        if self.core.sender.running:
            tile = self.tiles.get(self.core.me)
            if tile:
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
        self.audio.addItem("Без звука", "")
        for name in stream.list_audio_devices():
            self.audio.addItem(name, name)
        self.audio.setCurrentIndex(max(0, self.audio.findData(core.s["stream_audio"])))
        opts.addWidget(label("КАЧЕСТВО", "caption"), 0, 0)
        opts.addWidget(label("ЗВУК ТРАНСЛЯЦИИ", "caption"), 0, 1)
        opts.addWidget(self.quality, 1, 0)
        opts.addWidget(self.audio, 1, 1)
        v.addLayout(opts)
        hint = label("Звук компьютера передаётся через устройство «Стерео микшер» (или VB-Cable). "
                     "Если его нет в списке — включите его в настройках звука Windows.", "hint", wrap=True)
        v.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Отмена", "link", self.reject))
        go = button("Начать трансляцию", None, self._accept)
        go.setMinimumHeight(38)
        row.addWidget(go)
        v.addLayout(row)

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
