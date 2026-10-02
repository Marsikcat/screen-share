"""Text channel view: header, message list, composer."""

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from ..updater import parse_version
from . import icons
from .message import DateDivider, MessageWidget, day_title
from .player import RecordBar, Recording
from .richtext import QUICK_REACTIONS, EmojiPicker
from .search import MessageCard
from .theme import T
from .widgets import Avatar, IconButton, label

PAGE = 80
GROUP_GAP_MS = 7 * 60 * 1000
UNREAD_REACH = 400          # how far back a channel opens to show its first unread message


class NewDivider(QWidget):
    """The red line before the first message you have not read yet."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 2)
        lay.setSpacing(0)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {T.c['red']};")
        lay.addWidget(line, 1)
        tag = QLabel("НОВЫЕ")
        tag.setStyleSheet(f"background: {T.c['red']}; color: white; font-size: {T.px(7)}pt; font-weight: 800;"
                          f"padding: 1px 6px; border-radius: 3px;")
        lay.addWidget(tag)


class HoverToolbar(QFrame):
    """Floating actions for the message under the cursor."""

    def __init__(self, parent, view):
        super().__init__(parent)
        self.view = view
        self.target = None
        self.setStyleSheet(f"HoverToolbar {{ background: {T.c['main']}; border: 1px solid {T.c['border']};"
                           f"border-radius: 6px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 2, 3, 2)
        lay.setSpacing(0)
        for emoji in QUICK_REACTIONS[:3]:
            b = QToolButton()
            b.setText(emoji)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(32, 30)
            b.setStyleSheet(f"QToolButton {{ border:none; border-radius:4px; font-size:13pt; }}"
                            f"QToolButton:hover {{ background:{T.c['hover']}; }}")
            b.clicked.connect(lambda _=False, e=emoji: self._react(e))
            lay.addWidget(b)
        self.more = IconButton("smile_plus", "Добавить реакцию", 18, 30)
        self.more.clicked.connect(self._pick)
        self.reply = IconButton("reply", "Ответить", 18, 30)
        self.reply.clicked.connect(lambda: self.target and view.reply_to(self.target.msg))
        self.pin = IconButton("pin", "Закрепить", 18, 30)
        self.pin.clicked.connect(lambda: self.target and view.core.toggle_pin(self.target.msg["id"]))
        self.edit = IconButton("edit", "Изменить", 18, 30)
        self.edit.clicked.connect(lambda: self.target and self.target.start_edit())
        self.delete = IconButton("trash", "Удалить", 18, 30)
        self.delete.clicked.connect(lambda: self.target and view.core.delete_message(self.target.msg["id"]))
        for b in (self.more, self.reply, self.pin, self.edit, self.delete):
            b.hover_bg = T.c["hover"]
            lay.addWidget(b)
        self.hide()

    def attach(self, w):
        self.target = w
        mine = w.msg["author"] == self.view.core.me
        self.edit.setVisible(mine)
        self.delete.setVisible(mine)
        pinned = w.store.is_pinned(w.msg["id"])
        self.pin.setToolTip("Открепить" if pinned else "Закрепить — сообщение будет в списке 📌 канала")
        self.adjustSize()
        pos = w.mapTo(self.parent(), QPoint(w.width() - self.width() - 20, -14))
        self.move(pos.x(), max(0, pos.y()))
        self.raise_()
        self.show()

    def _react(self, emoji):
        if self.target:
            self.view.core.toggle_reaction(self.target.msg["id"], emoji)

    def _pick(self):
        target = self.target
        picker = EmojiPicker(self.window(), self.view.core.s)
        picker.picked.connect(lambda e: target and self.view.core.toggle_reaction(target.msg["id"], e))
        picker.popup_at(self.more.mapToGlobal(QPoint(self.more.width(), 0)))


class MessageList(QScrollArea):
    def __init__(self, view):
        super().__init__()
        self.view, self.core = view, view.core
        self.cid = None
        self.shown = []            # [(msg, widget)] in display order
        self.start = 0             # index into the channel's message list
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.col = QVBoxLayout(self.content)
        self.col.setContentsMargins(0, 0, 0, 16)
        self.col.setSpacing(0)
        self.col.addStretch(1)
        self.setWidget(self.content)
        self.toolbar = HoverToolbar(self.content, view)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # at the bottom stays at the bottom whatever makes the chat taller: pictures, reactions,
        # a narrower window (the search panel), lines re-wrapping
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._on_range)
        self._loading = False
        self.unread_since = None   # messages after this moment get the «НОВЫЕ» line
        self._divider = None
        self._missed = 0           # arrived while you were reading older messages
        self.jump = QToolButton(self.viewport())
        self.jump.setCursor(Qt.PointingHandCursor)
        self.jump.setStyleSheet(f"QToolButton {{ background: {T.c['accent']}; color: white; border: none;"
                                f"border-radius: 14px; padding: 5px 14px; font-weight: 600; }}"
                                f"QToolButton:hover {{ background: {T.c['accent_hover']}; }}")
        self.jump.clicked.connect(self.to_present)
        self.jump.hide()

    def at_bottom(self):
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - 40

    def scroll_bottom(self):
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
        QTimer.singleShot(60, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def _clear(self):
        self.toolbar.hide()
        self.toolbar.target = None
        while self.col.count() > 1:
            item = self.col.takeAt(1)
            if item.widget():
                item.widget().hide()        # no flash of the old channel before deletion
                item.widget().deleteLater()
        self.shown = []

    def _first_unread(self, msgs):
        if self.unread_since is None:
            return None
        return next((i for i, m in enumerate(msgs)
                     if m["ts"] > self.unread_since and m["author"] != self.core.me), None)

    def set_channel(self, cid, keep_scroll=False, fresh=False):
        """fresh: the channel was just opened — show where the unread messages begin."""
        old = self.verticalScrollBar().value()
        if keep_scroll and cid == self.cid and self.at_bottom() and not self._loading:
            keep_scroll = False                 # rebuilt while at the bottom: stay at the bottom
        self.cid = cid
        msgs = self.core.store_for(cid).visible_messages(cid)
        first_unread = self._first_unread(msgs)
        if not keep_scroll:
            self.start = max(0, len(msgs) - PAGE)
            if fresh and first_unread is not None:
                self.start = min(self.start, max(first_unread - 5, len(msgs) - UNREAD_REACH, 0))
        self.start = min(self.start, max(0, len(msgs) - 1)) if msgs else 0
        self._clear()
        self._divider = None
        if self.start == 0:
            self.col.addWidget(self._welcome())
        prev = None
        for i, m in enumerate(msgs[self.start:], self.start):
            self._add(m, prev, first_unread=i == first_unread)
            prev = m
        self._missed = 0
        self._receipt = None
        self.update_receipt()
        if keep_scroll:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(old))
        elif fresh and self._divider is not None:
            QTimer.singleShot(60, self._show_divider)
        else:
            self.scroll_bottom()
        QTimer.singleShot(100, self._update_jump)

    def update_receipt(self):
        """Conversations: under your newest message — read, delivered, or still on its way."""
        if getattr(self, "_receipt", None) is not None:
            self._receipt.hide()
            self._receipt.deleteLater()
            self._receipt = None
        if not (self.cid or "").startswith("dm:") or not self.shown:
            return
        last, w = self.shown[-1]
        if last["author"] != self.core.me:
            return                                # they replied: obviously got it
        state = self.core.dm_delivery(self.cid, last)
        peer = self.core.dm_peer.get(self.cid)
        online = peer in self.core.mesh.peers_map
        if state == "read":
            text = "✓ Прочитано"
        elif state == "delivered":
            text = "✓ Доставлено"
        elif state == "pending" and not online:
            name = self.core.name_of(peer)
            relays = [u for u in self.core.mesh.peers_map if u != peer and self.core._mail_peer(u)]
            text = (f"🕓 Ждёт доставки: {name} не в сети. Участники комнаты передадут сообщение "
                    f"в зашифрованном виде — прочитать его они не могут" if relays else
                    f"🕓 Ждёт доставки: {name} не в сети. Уйдёт, когда в сети будет {name} "
                    f"или кто-то из комнаты")
        else:
            return
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet(f"color: {T.c['muted']}; font-size: {T.px(8)}pt; padding: 0 0 2px 72px;")
        self.col.insertWidget(self.col.indexOf(w) + 1, lb)
        self._receipt = lb

    def _show_divider(self):
        """Open at the first unread message — unless all of them fit on the screen anyway."""
        div = self._divider
        if div is None:
            return
        y = div.mapTo(self.content, div.rect().topLeft()).y()
        if self.content.height() - y > self.viewport().height():
            self.verticalScrollBar().setValue(max(0, y - 40))
            self._stick = False
        else:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self._update_jump()

    def to_present(self):
        msgs = self.core.store_for(self.cid).visible_messages(self.cid)
        if self.start < len(msgs) - 2 * PAGE:      # far back in history: load just the latest again
            self.set_channel(self.cid)
        self._stick = True
        self.scroll_bottom()
        self._missed = 0
        self.jump.hide()

    def _update_jump(self):
        sb = self.verticalScrollBar()
        far = sb.maximum() - sb.value() > self.viewport().height() // 2
        if far or self._missed:
            self.jump.setText(f"Новых сообщений: {self._missed}  ↓" if self._missed else "К последним сообщениям  ↓")
            self.jump.adjustSize()
            self.jump.move((self.viewport().width() - self.jump.width()) // 2,
                           self.viewport().height() - self.jump.height() - 12)
            self.jump.show()
            self.jump.raise_()
        else:
            self.jump.hide()
            if sb.value() >= sb.maximum() - 40:
                self._missed = 0

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_jump()

    def _welcome(self):
        ch = self.core.channel(self.cid) or {"name": "", "kind": "text"}
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 24, 16, 8)
        if ch["kind"] == "dm":
            lay.addWidget(Avatar.of(self.core.member(ch["peer"]), 72))
            lay.addWidget(label(ch["name"], "h1"))
            lay.addWidget(label(f"Начало личной переписки. Её видите только вы и {ch['name']}: сообщения "
                                f"идут напрямую и хранятся только на ваших компьютерах.", "muted", wrap=True))
            return w
        badge = QLabel()
        badge.setFixedSize(68, 68)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(icons.pixmap("hash", T.c["header"], 40, 2.2))
        badge.setStyleSheet(f"background: {T.c['active']}; border-radius: 34px;")
        lay.addWidget(badge)
        lay.addWidget(label(f"Добро пожаловать в #{ch['name']}!", "h1"))
        lay.addWidget(label(f"Это начало канала #{ch['name']}. История хранится у каждого участника "
                            f"и сама досинхронизируется, когда вы снова в сети.", "muted", wrap=True))
        return w

    def _group_with(self, m, prev):
        return (prev is not None and prev["author"] == m["author"] and not m.get("reply")
                and m["ts"] - prev["ts"] < GROUP_GAP_MS and day_title(m["ts"]) == day_title(prev["ts"]))

    def _add(self, m, prev, index=None, first_unread=False):
        widgets = []
        if prev is None or day_title(prev["ts"]) != day_title(m["ts"]):
            widgets.append(DateDivider(m["ts"]))
        if first_unread:                         # under the date, above the message
            self._divider = NewDivider()
            widgets.append(self._divider)
        # the first new message starts its own group, with the author shown
        w = MessageWidget(self.core, m, first=first_unread or not self._group_with(m, prev))
        w.hovered.connect(self.toolbar.attach)
        w.reply_clicked.connect(self.reveal)
        widgets.append(w)
        for x in widgets:
            if index is None:
                self.col.addWidget(x)
            else:
                self.col.insertWidget(index, x)
                index += 1
        self.shown.append((m, w))
        return w

    def append(self, msg):
        stick = self.at_bottom() or msg["author"] == self.core.me
        if not stick:
            self._missed += 1                   # shown on the «Новых сообщений: N ↓» button
        msgs = self.core.store_for(self.cid).visible_messages(self.cid)
        if msgs and msgs[-1]["id"] == msg["id"]:
            prev = self.shown[-1][0] if self.shown else None
            self._add(msg, prev)
        else:  # arrived out of order (sync) — rebuild in place
            self.set_channel(self.cid, keep_scroll=not stick)
        if stick:
            self.scroll_bottom()
        self.update_receipt()
        self._update_jump()

    def widget_for(self, mid):
        return next((w for m, w in self.shown if m["id"] == mid), None)

    def refresh_message(self, mid):
        w = self.widget_for(mid)
        if w and not w.editing:
            stick = self.at_bottom()
            w.refresh()
            if stick:                       # a reaction or an edit made the message taller
                self.scroll_bottom()

    def refresh_files(self, fid):
        stick = self.at_bottom()
        store = self.core.store_for(self.cid) if self.cid else None
        for m, w in self.shown:
            if any(f["id"] == fid for f in m["files"]) or \
                    (store and any(e["image"] == fid for e in store.embeds_of(m["id"]))):
                w.refresh()
        if stick:                           # a picture arrived and pushed the chat up
            self.scroll_bottom()

    def refresh_all_files(self):
        from .message import _thumbs
        _thumbs.clear()
        store = self.core.store_for(self.cid) if self.cid else None
        for m, w in self.shown:
            if m["files"] or (store and store.embeds_of(m["id"])):
                w.refresh()

    def reveal(self, mid):
        """Scroll to a message (loading older history if needed) and light it up."""
        msgs = self.core.store_for(self.cid).visible_messages(self.cid)
        idx = next((i for i, m in enumerate(msgs) if m["id"] == mid), None)
        if idx is None:
            return False
        self._loading = True                    # no paging or bottom-sticking while we jump
        if idx < self.start:
            self.start = max(0, idx - 15)
            self.set_channel(self.cid, keep_scroll=True)
        w = self.widget_for(mid)

        def go():
            if w is not None:
                self.ensureWidgetVisible(w, 0, max(60, self.viewport().height() // 3))
                w.flash()
            QTimer.singleShot(300, self._done_jumping)
        QTimer.singleShot(80, go)
        return True

    def edit_last_own(self):
        for m, w in reversed(self.shown):
            if m["author"] == self.core.me:
                w.start_edit()
                self.ensureWidgetVisible(w)
                return

    def _done_jumping(self):
        self._loading = False
        self._stick = self.at_bottom()        # reading old messages now: don't pull back down

    def _on_range(self, _lo, hi):
        if self._stick and not self._loading:
            self.verticalScrollBar().setValue(hi)

    def _on_scroll(self, value):
        if not self._loading:
            self._stick = value >= self.verticalScrollBar().maximum() - 40
        self._update_jump()
        if value == 0 and self.start > 0 and not self._loading:
            self._loading = True
            sb = self.verticalScrollBar()
            before = sb.maximum()
            self.start = max(0, self.start - PAGE)
            self.set_channel(self.cid, keep_scroll=True)

            def restore():
                sb.setValue(sb.maximum() - before)
                self._loading = False
            QTimer.singleShot(30, restore)


class PinsPopup(QFrame):
    """The 📌 list of a channel: click a message to go to it, × to unpin."""

    def __init__(self, view):
        super().__init__(view.window(), Qt.Popup | Qt.FramelessWindowHint)
        self.view = view
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("PinsPopup")
        self.setStyleSheet(f"#PinsPopup {{ background: {T.c['side']}; border: 1px solid {T.c['border']};"
                           f"border-radius: 8px; }}")
        self.setFixedWidth(T.px(420))
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("pin", T.c["header"], 18))
        head.addWidget(ic)
        head.addWidget(label("Закреплённые сообщения", "h2"), 1)
        v.addLayout(head)
        core, cid = view.core, view.cid
        pinned = core.store_for(cid).pinned(cid)
        if not pinned:
            v.addWidget(label("Здесь пока ничего не закреплено. Наведите на сообщение и нажмите 📌 — "
                              "оно появится в этом списке у всех.", "hint", wrap=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 4, 0)
        col.setSpacing(6)
        for m in pinned[:50]:
            card = MessageCard(core, m, show_channel=False,
                               action=("x", "Открепить", lambda _=False, mid=m["id"]: self._unpin(mid)))
            card.clicked.connect(self._open)
            col.addWidget(card)
        col.addStretch(1)
        scroll.setWidget(body)
        scroll.setVisible(bool(pinned))
        scroll.setFixedHeight(min(460, 90 * len(pinned) + 10))
        v.addWidget(scroll)

    def _open(self, cid, mid):
        self.close()
        self.view.list.reveal(mid)

    def _unpin(self, mid):
        self.view.core.toggle_pin(mid)
        self.close()


class InputBox(QPlainTextEdit):
    submit = Signal()
    files_pasted = Signal(list)
    edit_last = Signal()
    escape = Signal()
    page = Signal(int)          # PgUp/PgDn scroll the messages while typing

    def __init__(self):
        super().__init__()
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self):
        lines = max(1, int(self.document().size().height()))
        want = self.fontMetrics().lineSpacing() * lines + 22
        self.setFixedHeight(max(44, min(220, want)))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if want > 220 else Qt.ScrollBarAlwaysOff)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            self.submit.emit()
        elif e.key() == Qt.Key_Up and not self.toPlainText():
            self.edit_last.emit()
        elif e.key() == Qt.Key_Escape:
            self.escape.emit()
        elif e.key() in (Qt.Key_PageUp, Qt.Key_PageDown):
            self.page.emit(-1 if e.key() == Qt.Key_PageUp else 1)
        else:
            super().keyPressEvent(e)

    def canInsertFromMimeData(self, src):
        return src.hasImage() or src.hasUrls() or super().canInsertFromMimeData(src)

    def insertFromMimeData(self, src):
        if src.hasImage():
            img = src.imageData()
            path = Path(tempfile.gettempdir()) / f"Снимок {time.strftime('%Y-%m-%d %H-%M-%S')}.png"
            img.save(str(path), "PNG")
            self.files_pasted.emit([str(path)])
        elif src.hasUrls() and all(u.isLocalFile() for u in src.urls()):
            self.files_pasted.emit([u.toLocalFile() for u in src.urls()])
        else:
            self.insertPlainText(src.text())


class Composer(QWidget):
    def __init__(self, view):
        super().__init__()
        self.view, self.core = view, view.core
        self.setObjectName("Composer")
        self.files = []
        self.reply = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(0)

        self.reply_bar = QFrame()
        self.reply_bar.setStyleSheet(f"background: {T.c['side']}; border-top-left-radius: 8px;"
                                     f"border-top-right-radius: 8px;")
        rb = QHBoxLayout(self.reply_bar)
        rb.setContentsMargins(14, 6, 8, 6)
        self.reply_label = QLabel()
        self.reply_label.setStyleSheet(f"color: {T.c['muted']};")
        rb.addWidget(self.reply_label, 1)
        close = IconButton("x", "Отменить ответ", 16, 24)
        close.clicked.connect(self.clear_reply)
        rb.addWidget(close)
        self.reply_bar.hide()
        lay.addWidget(self.reply_bar)

        self.files_bar = QWidget()
        self.files_lay = QHBoxLayout(self.files_bar)
        self.files_lay.setContentsMargins(0, 0, 0, 8)
        self.files_bar.hide()
        lay.addWidget(self.files_bar)

        box = QFrame()
        box.setObjectName("ComposerBox")
        h = QHBoxLayout(box)
        h.setContentsMargins(10, 0, 8, 0)
        h.setSpacing(4)
        attach = IconButton("plus_circle", "Прикрепить файл (Ctrl+Shift+U)", 22, 36)
        attach.hover_bg = "transparent"
        attach.clicked.connect(self.pick_files)
        self.input = InputBox()
        self.input.submit.connect(self.send)
        self.input.files_pasted.connect(self.add_files)
        self.input.edit_last.connect(lambda: view.list.edit_last_own())
        self.input.escape.connect(self._escape)
        self.input.page.connect(self._page)
        self.input.textChanged.connect(self._typing)
        emoji = IconButton("smile", "Эмодзи (Ctrl+E)", 22, 36)
        emoji.hover_bg = "transparent"
        emoji.clicked.connect(self.open_emoji)
        self.emoji_btn = emoji
        mic = IconButton("mic", "Записать голосовое сообщение", 21, 36)
        mic.hover_bg = "transparent"
        mic.clicked.connect(self.start_recording)
        self.record_bar = RecordBar()
        self.record_bar.send.connect(self.finish_recording)
        self.record_bar.cancel.connect(self.cancel_recording)
        self.record_bar.hide()
        self._rec = None
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(100)
        self._rec_timer.timeout.connect(self._rec_tick)
        self._box_widgets = (attach, self.input, mic, emoji)
        h.addWidget(attach, 0, Qt.AlignBottom)
        h.addWidget(self.input, 1)
        h.addWidget(self.record_bar, 1)
        h.addWidget(mic, 0, Qt.AlignBottom)
        h.addWidget(emoji, 0, Qt.AlignBottom)
        lay.addWidget(box)

        self.typing = QLabel(" ")
        self.typing.setFixedHeight(T.px(22))
        self.typing.setStyleSheet(f"color: {T.c['text']}; font-size: {T.px(8)}pt; padding-left: 4px;")
        lay.addWidget(self.typing)

    def set_placeholder(self, where, offline=False):
        text = f"Написать {where}" if where.startswith("@") else f"Написать в {where}"
        if offline:                 # direct messages travel sealed through the room
            text += " — не в сети, сообщение доставят через комнату"
        self.input.setPlaceholderText(text)

    _restoring = False

    def _typing(self):
        if self.input.toPlainText().strip() and self.view.cid and not self._restoring:
            self.core.send_typing(self.view.cid)

    def swap_draft(self, old, new):
        """Keep what you were writing in `old`, bring back what you left in `new`."""
        drafts = self.core.s.setdefault("drafts", {})
        text = self.input.toPlainText()
        if old:
            if text.strip():
                drafts[old] = text[:4000]
            else:
                drafts.pop(old, None)
        self._restoring = True
        self.input.setPlainText(drafts.get(new, "") if new else "")
        self._restoring = False
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)
        self.core.s.save()

    # ── voice messages ──────────────────────────────────────────────
    def start_recording(self):
        if self._rec or not self.view.cid:
            return
        rec = Recording(self.core.s)
        err = rec.start()
        if err:
            self.window().toast(f"Микрофон недоступен: {err}", "error")
            return
        self._rec, self._rec_cid = rec, self.view.cid
        rec.encoded.connect(self._recorded)
        for w in self._box_widgets:
            w.hide()
        self.record_bar.show()
        self.record_bar.setFocus()
        self._rec_timer.start()

    def _rec_tick(self):
        if self._rec:
            self.record_bar.show_state(self._rec.rec)
            if self._rec.rec.full and self._rec.rec.stream is not None:
                self._rec.rec.stream.stop()          # five minutes: wait for send / cancel

    def _end_recording_ui(self):
        self._rec_timer.stop()
        self.record_bar.hide()
        for w in self._box_widgets:
            w.show()
        self.input.setFocus()

    def finish_recording(self):
        rec, self._rec = self._rec, None
        self._end_recording_ui()
        if rec and not rec.finish():
            self.window().toast("Слишком короткая запись", "error")
        self._pending_rec = rec                      # keep it alive until it is encoded

    def cancel_recording(self):
        rec, self._rec = self._rec, None
        if rec:
            rec.cancel()
        self._end_recording_ui()

    def _recorded(self, result):
        self._pending_rec = None
        if isinstance(result, str):
            self.window().toast(result, "error")
            return
        self.core.send_message(self._rec_cid, "", [str(result)], self.reply)
        self.clear_reply()
        import shutil
        shutil.rmtree(result.parent, ignore_errors=True)

    def keyPressEvent(self, e):
        if self._rec and e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_recording()
        elif self._rec and e.key() == Qt.Key_Escape:
            self.cancel_recording()
        else:
            super().keyPressEvent(e)

    def _escape(self):
        if self.reply:
            self.clear_reply()
        else:
            self.view.list.scroll_bottom()
            self.core.mark_read(self.view.cid)

    def _page(self, direction):
        sb = self.view.list.verticalScrollBar()
        sb.setValue(sb.value() + direction * sb.pageStep())

    def open_emoji(self):
        anchor = self.emoji_btn
        picker = EmojiPicker(self.window(), self.core.s)
        picker.picked.connect(lambda e: (self.input.insertPlainText(e), self.input.setFocus()))
        picker.popup_at(anchor.mapToGlobal(QPoint(anchor.width(), 0)))

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Прикрепить файлы")
        self.add_files(paths)

    def add_files(self, paths):
        for p in paths:
            if p and p not in self.files and Path(p).is_file():
                self.files.append(p)
        self._render_files()
        self.input.setFocus()

    def _render_files(self):
        while self.files_lay.count():
            it = self.files_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for p in self.files:
            chip = QFrame()
            chip.setStyleSheet(f"background: {T.c['side']}; border-radius: 6px;")
            h = QHBoxLayout(chip)
            h.setContentsMargins(10, 6, 4, 6)
            ic = QLabel()
            ic.setPixmap(icons.pixmap("file", T.c["accent"], 18))
            h.addWidget(ic)
            chip_name = QLabel(Path(p).name[:40])
            chip_name.setTextFormat(Qt.PlainText)
            h.addWidget(chip_name)
            rm = IconButton("x", "Убрать", 14, 22)
            rm.clicked.connect(lambda _=False, x=p: (self.files.remove(x), self._render_files()))
            h.addWidget(rm)
            self.files_lay.addWidget(chip)
        self.files_lay.addStretch(1)
        self.files_bar.setVisible(bool(self.files))

    def reply_to(self, msg):
        self.reply = msg["id"]
        self.reply_label.setText(f"Ответ пользователю <b style='color:{T.c['header']}'>"
                                 f"{self.core.name_of(msg['author'])}</b>")
        self.reply_bar.show()
        self.input.setFocus()

    def clear_reply(self):
        self.reply = None
        self.reply_bar.hide()

    def send(self):
        text = self.input.toPlainText()
        if not text.strip() and not self.files:
            return
        self.core.send_message(self.view.cid, text, list(self.files), self.reply)
        self.core.s.get("drafts", {}).pop(self.view.cid, None)
        self.input.clear()
        self.files = []
        self._render_files()
        self.clear_reply()

    def show_typing(self, uids):
        names = [self.core.name_of(u) for u in uids]
        if not names:
            text = " "
        elif len(names) == 1:
            text = f"<b>{names[0]}</b> печатает…"
        elif len(names) <= 3:
            text = "<b>" + "</b>, <b>".join(names) + "</b> печатают…"
        else:
            text = "Несколько человек печатают…"
        self.typing.setText(text)


class ChatView(QWidget):
    members_toggled = Signal()
    search_requested = Signal()

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.cid = None
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QFrame()
        header.setObjectName("ChatHeader")
        header.setFixedHeight(48)
        h = QHBoxLayout(header)
        h.setContentsMargins(16, 0, 12, 0)
        h.setSpacing(8)
        self.h_icon = QLabel()
        self.h_icon.setPixmap(icons.pixmap("hash", T.c["muted"], 22))
        self.h_name = QLabel()
        self.h_name.setStyleSheet(f"color: {T.c['header']}; font-weight: 700; font-size: {T.px(11)}pt;")
        self.h_sep = QFrame()
        self.h_sep.setFixedSize(1, 24)
        self.h_sep.setStyleSheet(f"background: {T.c['divider']};")
        self.h_topic = QLabel()
        self.h_topic.setStyleSheet(f"color: {T.c['muted']};")
        members = IconButton("users", "Список участников", 22, 34, checkable=True)
        members.setChecked(True)
        members.clicked.connect(self.members_toggled.emit)
        pins = IconButton("pin", "Закреплённые сообщения", 22, 34)
        pins.clicked.connect(self.show_pins)
        self.pins_btn = pins
        search = IconButton("search", "Поиск по сообщениям (Ctrl+F)", 22, 34)
        search.clicked.connect(self.search_requested.emit)
        for w in (self.h_icon, self.h_name, self.h_sep, self.h_topic):
            h.addWidget(w)
        h.addStretch(1)
        h.addWidget(pins)
        h.addWidget(search)
        h.addWidget(members)
        lay.addWidget(header)
        self.list = MessageList(self)
        lay.addWidget(self.list, 1)
        self.composer = Composer(self)
        lay.addWidget(self.composer)

        # bound methods (not lambdas) so Qt drops the connections when the view is rebuilt
        core.message_added.connect(self._on_added)
        core.message_changed.connect(self._on_changed)
        core.message_removed.connect(self._on_removed)
        core.typing_changed.connect(self._on_typing)
        core.file_ready.connect(self.list.refresh_files)
        core.cache_cleared.connect(self.list.refresh_all_files)
        core.seen_changed.connect(self._on_seen)
        core.members_changed.connect(self._on_members)

    def set_channel(self, cid):
        ch = self.core.channel(cid)
        if not ch:
            return
        changed = cid != self.cid
        old, self.cid = self.cid, cid
        dm = ch["kind"] == "dm"
        self.h_icon.setPixmap(icons.pixmap("at" if dm else "hash", T.c["muted"], 22))
        self.h_name.setText(ch["name"])
        topic = ch["topic"]
        if dm:
            version = self.core.peer_version(ch["peer"])
            if version and parse_version(version) < (3, 4):
                topic = f"у собеседника версия {version} — личные сообщения дойдут, когда он обновится"
        self.h_topic.setText(topic)
        self.h_sep.setVisible(bool(topic))
        self.composer.set_placeholder(("@" if dm else "#") + ch["name"],
                                      offline=dm and ch["peer"] not in self.core.mesh.peers_map)
        if changed:
            self.composer.clear_reply()
            self.composer.swap_draft(old, cid)
            self.list.unread_since = self.core.s["read"].get(cid, 0)
            self.list.set_channel(cid, fresh=True)
            self.composer.show_typing(self.core.typers(cid))
        self.core.mark_read(cid)
        self.composer.input.setFocus()

    def reply_to(self, msg):
        self.composer.reply_to(msg)

    def show_pins(self):
        if not self.cid:
            return
        popup = PinsPopup(self)
        popup.adjustSize()
        anchor = self.pins_btn.mapToGlobal(self.pins_btn.rect().bottomRight())
        popup.move(anchor.x() - popup.width(), anchor.y() + 6)
        popup.show()

    def _on_changed(self, cid, mid):
        if cid == self.cid:
            self.list.refresh_message(mid)

    def _on_removed(self, cid, mid):
        if cid == self.cid:
            self.list.set_channel(cid, keep_scroll=True)

    def _on_seen(self, cid):
        if cid == self.cid:
            self.list.update_receipt()

    def save_draft(self):
        self.composer.swap_draft(self.cid, self.cid)

    def _on_typing(self, cid):
        if cid == self.cid:
            self.composer.show_typing(self.core.typers(cid))

    def _on_members(self):
        # someone came, went, changed their name or picture: refresh the headers only — rebuilding
        # every message would stop a video playing in the chat and reload every picture
        if self.cid:
            for _m, w in self.list.shown:
                w.refresh_author()
            self.list.update_receipt()
            ch = self.core.channel(self.cid)
            if ch:
                self.h_name.setText(ch["name"])
            if ch and ch["kind"] == "dm":
                self.composer.set_placeholder("@" + ch["name"],
                                              offline=ch["peer"] not in self.core.mesh.peers_map)

    def _on_added(self, cid, msg):
        if cid == self.cid:
            self.list.append(msg)
            if self.isVisible() and self.window().isActiveWindow():
                self.core.mark_read(cid)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.composer.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
