"""Left column: room header, channels with voice participants, voice panel, user panel."""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QSlider,
                               QVBoxLayout, QWidget, QWidgetAction)

from .. import hotkeys
from . import icons
from .theme import T
from .widgets import Avatar, IconButton


def elide(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


class ChannelItem(QFrame):
    clicked = Signal(str)
    context = Signal(str, QPoint)

    def __init__(self, ch):
        super().__init__()
        self.cid, self.kind = ch["id"], ch["kind"]
        self.selected = self.unread = False
        self.mentions = 0
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(T.px(34))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)
        self.icon = QLabel()
        self.name = QLabel(elide(ch["name"], 26))
        self.badge = QLabel()
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setStyleSheet(f"background: {T.c['red']}; color: white; border-radius: 8px;"
                                 f"font-size: {T.px(8)}pt; font-weight: 700; padding: 0 5px;")
        self.badge.setFixedHeight(16)
        self.badge.hide()
        lay.addWidget(self.icon)
        lay.addWidget(self.name, 1)
        lay.addWidget(self.badge)
        self._paint()

    def set_state(self, selected=None, unread=None, mentions=None):
        if selected is not None:
            self.selected = selected
        if unread is not None:
            self.unread = unread
        if mentions is not None:
            self.mentions = mentions
        self._paint()

    def _paint(self):
        c = T.c
        bright = self.selected or self.unread or self._hover
        fg = c["header"] if (self.selected or self.unread) else (c["text"] if self._hover else c["muted"])
        bg = c["active"] if self.selected else (c["hover"] if self._hover else "transparent")
        self.setStyleSheet(f"ChannelItem {{ background: {bg}; border-radius: 4px; }}")
        self.name.setStyleSheet(f"color: {fg}; font-weight: {600 if bright else 500};")
        self.icon.setPixmap(icons.pixmap("hash" if self.kind == "text" else "volume",
                                         c["muted"] if not self.selected else c["text"], 20))
        self.badge.setText(str(self.mentions))
        self.badge.setVisible(self.mentions > 0)

    def enterEvent(self, e):
        self._hover = True
        self._paint()

    def leaveEvent(self, e):
        self._hover = False
        self._paint()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.cid)

    def contextMenuEvent(self, e):
        self.context.emit(self.cid, e.globalPos())


class VoiceMemberRow(QFrame):
    def __init__(self, sidebar, uid):
        super().__init__()
        self.sb, self.core, self.uid = sidebar, sidebar.core, uid
        m = self.core.member(uid)
        st = m["state"]
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"VoiceMemberRow {{ border-radius: 4px; }}"
                           f"VoiceMemberRow:hover {{ background: {T.c['hover']}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(34, 3, 8, 3)
        lay.setSpacing(6)
        self.avatar = Avatar(m["name"], m["color"], 22)
        self.avatar.set_speaking(self.core.is_speaking(uid) and not st.get("muted"))
        lay.addWidget(self.avatar)
        name = QLabel(elide(m["name"], 18))
        name.setStyleSheet(f"color: {T.c['header'] if self.core.is_speaking(uid) else T.c['muted']};")
        self.name = name
        lay.addWidget(name, 1)
        if st.get("streaming"):
            live = QLabel("В ЭФИРЕ")
            live.setStyleSheet(f"background: {T.c['red']}; color: white; border-radius: 4px;"
                               f"font-size: {T.px(7)}pt; font-weight: 800; padding: 1px 5px;")
            lay.addWidget(live)
        for flag, icon_name in (("muted", "mic_off"), ("deafened", "headphones_off")):
            if st.get(flag):
                ic = QLabel()
                ic.setPixmap(icons.pixmap(icon_name, T.c["icon"], 15))
                lay.addWidget(ic)

    def set_speaking(self, on):
        self.avatar.set_speaking(on)
        self.name.setStyleSheet(f"color: {T.c['header'] if on else T.c['muted']};")

    def mouseDoubleClickEvent(self, e):
        if self.core.states.get(self.uid, {}).get("streaming"):
            self.core.watch(self.uid)

    def contextMenuEvent(self, e):
        self.sb.member_menu(self.uid, e.globalPos())


class Sidebar(QWidget):
    channel_selected = Signal(str)
    open_settings = Signal(str)
    stream_requested = Signal()

    def __init__(self, core, window):
        super().__init__()
        self.core, self.win = core, window
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(T.px(248))
        self.selected = None
        self.items = {}
        self.voice_rows = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.header = QFrame()
        self.header.setObjectName("SideHeader")
        self.header.setFixedHeight(48)
        self.header.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(16, 0, 12, 0)
        self.room = QLabel()
        self.room.setStyleSheet(f"color: {T.c['header']}; font-weight: 700; font-size: {T.px(11)}pt;")
        chev = QLabel()
        chev.setPixmap(icons.pixmap("chevron_down", T.c["header"], 18))
        h.addWidget(self.room, 1)
        h.addWidget(chev)
        self.header.mousePressEvent = lambda e: self.room_menu()
        lay.addWidget(self.header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list = QWidget()
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(8, 8, 8, 8)
        self.list_lay.setSpacing(1)
        scroll.setWidget(self.list)
        lay.addWidget(scroll, 1)

        self.voice_panel = self._build_voice_panel()
        lay.addWidget(self.voice_panel)
        lay.addWidget(self._build_user_panel())

        core.channels_changed.connect(self.rebuild)
        core.voice_changed.connect(self.rebuild)
        core.members_changed.connect(self.rebuild)
        core.room_changed.connect(self.update_room)
        core.stream_changed.connect(self.update_voice_panel)
        core.speaking_changed.connect(self.update_speaking)
        core.message_added.connect(self._on_message)
        core.read_changed.connect(self.update_unread)
        self.update_room()
        self.rebuild()

    # ── channel list ────────────────────────────────────────────────
    def _category(self, title, kind):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 14, 2, 4)
        cap = QLabel(title)
        cap.setProperty("role", "caption")
        add = IconButton("plus", "Создать канал", 16, 22)
        add.hover_bg = "transparent"
        add.clicked.connect(lambda: self.win.create_channel_dialog(kind))
        h.addWidget(cap, 1)
        h.addWidget(add)
        return w

    def rebuild(self):
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.items, self.voice_rows = {}, {}
        store = self.core.store
        self.list_lay.addWidget(self._category("ТЕКСТОВЫЕ КАНАЛЫ", "text"))
        for ch in store.channel_list("text"):
            self._add_item(ch)
        self.list_lay.addWidget(self._category("ГОЛОСОВЫЕ КАНАЛЫ", "voice"))
        for ch in store.channel_list("voice"):
            self._add_item(ch)
            for uid in self.core.voice_members(ch["id"]):
                row = VoiceMemberRow(self, uid)
                self.voice_rows[uid] = row
                self.list_lay.addWidget(row)
        self.list_lay.addStretch(1)
        self.update_unread()
        self.update_voice_panel()

    def _add_item(self, ch):
        item = ChannelItem(ch)
        item.clicked.connect(self._clicked)
        item.context.connect(self.channel_menu)
        item.set_state(selected=ch["id"] == self.selected)
        self.items[ch["id"]] = item
        self.list_lay.addWidget(item)

    def _clicked(self, cid):
        ch = self.core.store.channel(cid)
        if ch and ch["kind"] == "voice" and self.core.my_voice != cid:
            self.core.join_voice(cid)
        self.channel_selected.emit(cid)

    def select(self, cid):
        self.selected = cid
        for k, item in self.items.items():
            item.set_state(selected=k == cid)
        self.update_unread()

    def update_unread(self):
        for cid, item in self.items.items():
            if item.kind == "text":
                n, mentions = self.core.unread(cid) if cid != self.selected or not self.win.isActiveWindow() \
                    else (0, 0)
                item.set_state(unread=n > 0, mentions=mentions)
        self.win.update_badge()

    def _on_message(self, cid, msg):
        self.update_unread()

    def update_speaking(self, uid, on):
        row = self.voice_rows.get(uid)
        if row:
            row.set_speaking(on and not self.core.member(uid)["state"].get("muted"))

    def update_room(self):
        self.room.setText(elide(self.core.room_name(), 24))

    # ── menus ───────────────────────────────────────────────────────
    def room_menu(self):
        m = QMenu(self)
        m.addAction(icons.icon("hash", T.c["text"], 16), "Создать текстовый канал",
                    lambda: self.win.create_channel_dialog("text"))
        m.addAction(icons.icon("volume", T.c["text"], 16), "Создать голосовой канал",
                    lambda: self.win.create_channel_dialog("voice"))
        m.addSeparator()
        m.addAction(icons.icon("edit", T.c["text"], 16), "Переименовать комнату", self.win.rename_room_dialog)
        m.addAction(icons.icon("globe", T.c["text"], 16), "Сеть и участники",
                    lambda: self.open_settings.emit("network"))
        m.exec(self.header.mapToGlobal(QPoint(8, self.header.height())))

    def channel_menu(self, cid, pos):
        ch = self.core.store.channel(cid)
        if not ch:
            return
        m = QMenu(self)
        if ch["kind"] == "text":
            m.addAction("Отметить как прочитанное", lambda: (self.core.mark_read(cid), self.update_unread()))
        else:
            if self.core.my_voice == cid:
                m.addAction("Отключиться", self.core.leave_voice)
            else:
                m.addAction("Присоединиться", lambda: self._clicked(cid))
        m.addSeparator()
        m.addAction("Переименовать канал", lambda: self.win.rename_channel_dialog(cid))
        m.addAction("Удалить канал", lambda: self.win.delete_channel_dialog(cid))
        m.exec(pos)

    def member_menu(self, uid, pos):
        core = self.core
        m = QMenu(self)
        if uid != core.me:
            st = core.states.get(uid, {})
            if st.get("streaming"):
                if core.viewer.uid == uid:
                    m.addAction("Прекратить просмотр", core.unwatch)
                else:
                    m.addAction("Смотреть трансляцию", lambda: core.watch(uid))
                m.addSeparator()
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(10, 6, 10, 6)
            vol = core.s["user_volumes"].get(uid, 100)
            cap = QLabel(f"Громкость пользователя · {vol}%")
            cap.setProperty("role", "caption")
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 200)
            slider.setValue(vol)
            slider.setMinimumWidth(200)

            def changed(val):
                core.s["user_volumes"][uid] = val
                cap.setText(f"Громкость пользователя · {val}%")
            slider.valueChanged.connect(changed)
            slider.sliderReleased.connect(core.s.save)
            v.addWidget(cap)
            v.addWidget(slider)
            act = QWidgetAction(m)
            act.setDefaultWidget(box)
            m.addAction(act)
            muted = uid in core.s["local_mutes"]

            def toggle_local():
                lst = core.s["local_mutes"]
                lst.remove(uid) if uid in lst else lst.append(uid)
                core.s.save()
            m.addAction("Включить звук" if muted else "Заглушить для себя", toggle_local)
        else:
            m.addAction("Настройки голоса", lambda: self.open_settings.emit("voice"))
        m.exec(pos)

    # ── bottom panels ───────────────────────────────────────────────
    def _build_voice_panel(self):
        panel = QFrame()
        panel.setObjectName("VoicePanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(10, 8, 8, 8)
        v.setSpacing(6)
        top = QHBoxLayout()
        info = QVBoxLayout()
        info.setSpacing(0)
        self.vp_status = QLabel("Голосовая связь подключена")
        self.vp_status.setStyleSheet(f"color: {T.c['green']}; font-weight: 700; font-size: {T.px(9)}pt;")
        self.vp_channel = QLabel()
        self.vp_channel.setProperty("role", "hint")
        self.vp_channel.setCursor(Qt.PointingHandCursor)
        self.vp_channel.mousePressEvent = lambda e: self.core.my_voice and \
            self.channel_selected.emit(self.core.my_voice)
        info.addWidget(self.vp_status)
        info.addWidget(self.vp_channel)
        top.addLayout(info, 1)
        hang = IconButton("phone_off", "Отключиться", 20, 32)
        hang.clicked.connect(self.core.leave_voice)
        top.addWidget(hang)
        v.addLayout(top)
        self.vp_stream = QLabel()
        self.vp_stream.setProperty("role", "hint")
        self.vp_stream.setWordWrap(True)
        v.addWidget(self.vp_stream)
        self.vp_share = QFrame()
        self.vp_share.setCursor(Qt.PointingHandCursor)
        sh = QHBoxLayout(self.vp_share)
        sh.setContentsMargins(0, 6, 0, 6)
        self.vp_share_icon = QLabel()
        self.vp_share_text = QLabel()
        sh.addStretch(1)
        sh.addWidget(self.vp_share_icon)
        sh.addWidget(self.vp_share_text)
        sh.addStretch(1)
        self.vp_share.mousePressEvent = lambda e: self._toggle_stream()
        v.addWidget(self.vp_share)
        panel.hide()
        return panel

    def _toggle_stream(self):
        if self.core.sender.running:
            self.core.stop_stream()
        else:
            self.stream_requested.emit()

    def update_voice_panel(self):
        cid = self.core.my_voice
        self.voice_panel.setVisible(bool(cid))
        if not cid:
            return
        ch = self.core.store.channel(cid) or {"name": ""}
        self.vp_channel.setText(f"{ch['name']} / {elide(self.core.room_name(), 18)}")
        streaming = self.core.sender.running
        c = T.c
        self.vp_share.setStyleSheet(f"QFrame {{ background: {c['red'] if streaming else c['active']};"
                                    f"border-radius: 4px; }}")
        self.vp_share_icon.setPixmap(icons.pixmap("screen_off" if streaming else "screen",
                                                  "white" if streaming else c["header"], 18))
        self.vp_share_text.setText("Остановить трансляцию" if streaming else "Демонстрация экрана")
        self.vp_share_text.setStyleSheet(f"color: {'white' if streaming else c['header']}; font-weight: 600;")
        if streaming:
            n = len(self.core.watchers)
            self.vp_stream.setText(f"В эфире: {self.core.stream_info}\nСмотрят: {n}")
        elif self.core.viewer.uid:
            self.vp_stream.setText(f"Вы смотрите трансляцию {self.core.name_of(self.core.viewer.uid)}")
        else:
            self.vp_stream.setText("")
        self.vp_stream.setVisible(bool(self.vp_stream.text()))

    def _build_user_panel(self):
        panel = QFrame()
        panel.setObjectName("UserPanel")
        panel.setFixedHeight(56)
        h = QHBoxLayout(panel)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(2)
        self.me_avatar = Avatar(self.core.s["name"], self.core.s["color"], 32)
        self.me_avatar.status = "online"
        self.me_avatar.ring_bg = T.c["rail"] if not T.light else "#ebedef"
        h.addWidget(self.me_avatar)
        info = QVBoxLayout()
        info.setSpacing(0)
        self.me_name = QLabel()
        self.me_name.setStyleSheet(f"color: {T.c['header']}; font-weight: 600;")
        self.me_sub = QLabel("В сети")
        self.me_sub.setProperty("role", "hint")
        info.addWidget(self.me_name)
        info.addWidget(self.me_sub)
        h.addLayout(info, 1)
        self.mic_btn = IconButton("mic", "Выкл. микрофон", 20, 32, checkable=True, danger_when_checked=True)
        self.mic_btn.clicked.connect(self.core.toggle_mute)
        self.deaf_btn = IconButton("headphones", "Выкл. звук", 20, 32, checkable=True, danger_when_checked=True)
        self.deaf_btn.clicked.connect(self.core.toggle_deafen)
        gear = IconButton("settings", "Настройки", 20, 32)
        gear.clicked.connect(lambda: self.open_settings.emit("profile"))
        for b in (self.mic_btn, self.deaf_btn, gear):
            h.addWidget(b)
        self.core.voice_changed.connect(self.update_user)
        self.core.members_changed.connect(self.update_user)
        self.update_user()
        return panel

    def update_user(self):
        s = self.core.s
        self.me_avatar.set(s["name"], s["color"])
        self.me_name.setText(elide(s["name"] or "Без имени", 16))
        n = len(self.core.mesh.peers())
        self.me_sub.setText(f"В сети · рядом {n}" if n else "В сети · вы одни")
        muted = s["muted"] or s["deafened"]
        self.mic_btn.setChecked(muted)
        self.mic_btn.set_icon("mic_off" if muted else "mic")
        self.mic_btn.setToolTip(self._with_key("Вкл. микрофон" if muted else "Выкл. микрофон", "toggle_mute"))
        self.deaf_btn.setChecked(s["deafened"])
        self.deaf_btn.set_icon("headphones_off" if s["deafened"] else "headphones")
        self.deaf_btn.setToolTip(self._with_key("Вкл. звук" if s["deafened"] else "Выкл. звук", "toggle_deafen"))

    def _with_key(self, text, action):
        b = self.core.s["hotkeys"].get(action)
        return f"{text}  ({hotkeys.describe(b)})" if b else text
