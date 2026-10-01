"""Search through the history (Ctrl+F) and the message cards it shares with the pins popup."""

import html
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)

from . import icons
from .message import readable, when
from .theme import T, mix
from .widgets import Avatar, IconButton, label


def _snippet(text, words, width=150):
    """A piece of the text around the first match, the matches marked."""
    plain = " ".join(re.sub(r"```[\w+-]*|[*_~`>|]", " ", text).split())   # code stays searchable
    low = plain.casefold()
    first = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, first - width // 3)
    piece = plain[start:start + width]
    piece = ("…" if start else "") + piece + ("…" if start + width < len(plain) else "")
    out = html.escape(piece)
    if words:
        mark = f"background-color:{mix(T.c['main'], T.c['yellow'], 0.4)}; color:{T.c['header']}"
        pattern = re.compile("|".join(re.escape(html.escape(w)) for w in words), re.IGNORECASE)
        out = pattern.sub(lambda m: f"<span style='{mark}'>{m.group(0)}</span>", out)
    return out


class MessageCard(QFrame):
    """One message, compact: who, where, when and a piece of the text. Click → go there."""

    clicked = Signal(str, str)          # channel id, message id

    def __init__(self, core, msg, words=(), show_channel=True, action=None):
        super().__init__()
        self.cid, self.mid = msg["ch"], msg["id"]
        c = T.c
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("MessageCard")
        self.setStyleSheet(f"#MessageCard {{ background: {c['main']}; border-radius: 8px; }}"
                           f"#MessageCard:hover {{ background: {c['hover']}; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        member = core.member(msg["author"])
        top.addWidget(Avatar.of(member, 20))
        where = ""
        if show_channel:
            ch = core.channel(msg["ch"]) or {"name": "", "kind": "text"}
            where = (f" · @{html.escape(ch['name'])}" if ch["kind"] == "dm"
                     else f" · #{html.escape(ch['name'])}")
        head = QLabel(f"<span style='color:{readable(member['color'])}; font-weight:600'>"
                      f"{html.escape(member['name'])}</span>"
                      f"<span style='color:{c['muted']}; font-size:{T.px(8)}pt'>{where} · "
                      f"{when(msg['ts'])}</span>")
        top.addWidget(head, 1)
        if action:
            name, tip, fn = action
            b = IconButton(name, tip, 16, 26)
            b.clicked.connect(fn)
            top.addWidget(b)
        v.addLayout(top)
        store = core.store_for(msg["ch"])
        text = store.text_of(msg)
        if text:
            body = QLabel(_snippet(text, list(words)))
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            body.setStyleSheet(f"color: {c['text']};")
            v.addWidget(body)
        for f in msg["files"][:3]:
            v.addWidget(label(f"📎 {f['name']}", "hint"))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.cid, self.mid)


class SearchPanel(QWidget):
    """The right column while searching: a query, where to look, the results."""

    open_message = Signal(str, str)
    closed = Signal()

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.cid = None                  # the channel you are in: for «В этом канале»
        self.setObjectName("Members")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(T.px(360))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 0)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Поиск по сообщениям")
        self.input.addAction(icons.icon("search", T.c["muted"], 18), QLineEdit.LeadingPosition)
        self.input.setClearButtonEnabled(True)
        close = IconButton("x", "Закрыть поиск (Esc)", 18, 30)
        close.clicked.connect(self.closed.emit)
        top.addWidget(self.input, 1)
        top.addWidget(close)
        lay.addLayout(top)

        scope = QHBoxLayout()
        scope.setSpacing(4)
        self.group = QButtonGroup(self)
        for i, text in enumerate(("Везде", "В этом канале")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(f"QToolButton {{ border: none; padding: 4px 10px; border-radius: 4px;"
                            f"color: {T.c['muted']}; font-weight: 600; }}"
                            f"QToolButton:checked {{ background: {T.c['active']}; color: {T.c['header']}; }}"
                            f"QToolButton:hover {{ color: {T.c['header']}; }}")
            self.group.addButton(b, i)
            scope.addWidget(b)
        scope.addStretch(1)
        self.count = label("", "hint")
        scope.addWidget(self.count)
        lay.addLayout(scope)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.scroll, 1)
        self._timer = QTimer(self, singleShot=True, interval=250, timeout=self.run)
        self.input.textChanged.connect(lambda _: self._timer.start())
        self.input.returnPressed.connect(self.run)
        self.group.idClicked.connect(lambda _: self.run())
        self.run()

    def set_context(self, cid):
        self.cid = cid
        if self.group.checkedId() == 1:
            self.run()

    def focus(self):
        self.input.setFocus()
        self.input.selectAll()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(e)

    def run(self):
        query = self.input.text().strip()
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 4, 12)
        col.setSpacing(6)
        here = self.group.checkedId() == 1 and self.cid
        if not query:
            self.count.setText("")
            col.addWidget(label("Введите слово или часть фразы — найдутся сообщения из всех каналов "
                                "и личных переписок, которые хранятся у вас.", "hint", wrap=True))
        else:
            results = self.core.search(query, self.cid if here else None, limit=100)
            self.count.setText(f"Найдено: {len(results)}" + ("+" if len(results) == 100 else ""))
            if not results:
                col.addWidget(label("Ничего не нашлось. Попробуйте другое слово"
                                    + (" или ищите везде." if here else "."), "hint", wrap=True))
            words = query.casefold().split()
            for m in results:
                card = MessageCard(self.core, m, words, show_channel=not here)
                card.clicked.connect(self.open_message.emit)
                col.addWidget(card)
        col.addStretch(1)
        self.scroll.setWidget(body)
