"""Right column: who is in the room, online first."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QVBoxLayout, QWidget

from . import icons
from .message import readable
from .sidebar import elide
from .theme import T
from .widgets import Avatar


class MemberRow(QFrame):
    def __init__(self, core, m, on_dm):
        super().__init__()
        self.core, self.uid, self.on_dm = core, m["uid"], on_dm
        if self.uid != core.me:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip("Двойной щелчок — написать лично")
        self.setStyleSheet(f"MemberRow {{ border-radius: 4px; }}"
                           f"MemberRow:hover {{ background: {T.c['hover']}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(10)
        av = Avatar.of(m, 32)
        av.status = m["status"]
        lay.addWidget(av)
        col = QVBoxLayout()
        col.setSpacing(0)
        name = QLabel(elide(m["name"], 20) + (" (вы)" if m["uid"] == core.me else ""))
        name.setStyleSheet(f"color: {readable(m['color']) if m['online'] else T.c['muted']};"
                           f"font-weight: 600;")
        col.addWidget(name)
        st = m["state"]
        sub = ""
        if st.get("streaming"):
            sub = "🔴 Ведёт трансляцию"
        elif st.get("voice"):
            ch = core.store.channel(st["voice"])
            sub = f"🔊 {ch['name']}" if ch else ""
        if sub and m["online"]:
            s = QLabel(sub)
            s.setProperty("role", "hint")
            col.addWidget(s)
        lay.addLayout(col, 1)
        if not m["online"]:
            name.setStyleSheet(f"color: {T.c['muted']}; font-weight: 500;")

    def mouseDoubleClickEvent(self, e):
        if self.uid != self.core.me:
            self.on_dm(self.uid)

    def contextMenuEvent(self, e):
        if self.uid != self.core.me:
            m = QMenu(self)
            m.addAction(icons.icon("at", T.c["text"], 16), "Написать лично", lambda: self.on_dm(self.uid))
            m.exec(e.globalPos())


class MemberList(QWidget):
    dm_requested = Signal(str)

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.setObjectName("Members")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(T.px(240))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.col = QVBoxLayout(self.body)
        self.col.setContentsMargins(8, 12, 8, 12)
        self.col.setSpacing(1)
        scroll.setWidget(self.body)
        lay.addWidget(scroll)
        core.members_changed.connect(self.rebuild)
        core.voice_changed.connect(self.rebuild)
        self.rebuild()

    def rebuild(self):
        while self.col.count():
            it = self.col.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        members = self.core.members()
        online = [m for m in members if m["online"]]
        offline = [m for m in members if not m["online"]]
        for title, group in ((f"В СЕТИ — {len(online)}", online), (f"НЕ В СЕТИ — {len(offline)}", offline)):
            if not group:
                continue
            cap = QLabel(title)
            cap.setProperty("role", "caption")
            cap.setContentsMargins(8, 12, 0, 4)
            self.col.addWidget(cap)
            for m in group:
                self.col.addWidget(MemberRow(self.core, m, self.dm_requested.emit))
        self.col.addStretch(1)
