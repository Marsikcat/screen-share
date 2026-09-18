"""Text channel view: header, message list, composer."""

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from . import icons
from .message import DateDivider, MessageWidget, day_title
from .richtext import QUICK_REACTIONS, EmojiPicker
from .theme import T
from .widgets import IconButton, label

PAGE = 80
GROUP_GAP_MS = 7 * 60 * 1000


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
        self.edit = IconButton("edit", "Изменить", 18, 30)
        self.edit.clicked.connect(lambda: self.target and self.target.start_edit())
        self.delete = IconButton("trash", "Удалить", 18, 30)
        self.delete.clicked.connect(lambda: self.target and view.core.delete_message(self.target.msg["id"]))
        for b in (self.more, self.reply, self.edit, self.delete):
            b.hover_bg = T.c["hover"]
            lay.addWidget(b)
        self.hide()

    def attach(self, w):
        self.target = w
        mine = w.msg["author"] == self.view.core.me
        self.edit.setVisible(mine)
        self.delete.setVisible(mine)
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
        picker = EmojiPicker(self.window())
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
        self._loading = False

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
                item.widget().deleteLater()
        self.shown = []

    def set_channel(self, cid, keep_scroll=False):
        old = self.verticalScrollBar().value()
        self.cid = cid
        msgs = self.core.store.visible_messages(cid)
        if not keep_scroll:
            self.start = max(0, len(msgs) - PAGE)
        self.start = min(self.start, max(0, len(msgs) - 1)) if msgs else 0
        self._clear()
        if self.start == 0:
            self.col.addWidget(self._welcome())
        prev = None
        for m in msgs[self.start:]:
            self._add(m, prev)
            prev = m
        if keep_scroll:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(old))
        else:
            self.scroll_bottom()

    def _welcome(self):
        ch = self.core.store.channel(self.cid) or {"name": ""}
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 24, 16, 8)
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

    def _add(self, m, prev, index=None):
        widgets = []
        if prev is None or day_title(prev["ts"]) != day_title(m["ts"]):
            widgets.append(DateDivider(m["ts"]))
        w = MessageWidget(self.core, m, first=not self._group_with(m, prev))
        w.hovered.connect(self.toolbar.attach)
        w.reply_clicked.connect(self.jump_to)
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
        msgs = self.core.store.visible_messages(self.cid)
        if msgs and msgs[-1]["id"] == msg["id"]:
            prev = self.shown[-1][0] if self.shown else None
            self._add(msg, prev)
        else:  # arrived out of order (sync) — rebuild in place
            self.set_channel(self.cid, keep_scroll=not stick)
        if stick:
            self.scroll_bottom()

    def widget_for(self, mid):
        return next((w for m, w in self.shown if m["id"] == mid), None)

    def refresh_message(self, mid):
        w = self.widget_for(mid)
        if w and not w.editing:
            w.refresh()

    def refresh_files(self, fid):
        for m, w in self.shown:
            if any(f["id"] == fid for f in m["files"]):
                w.refresh()

    def jump_to(self, mid):
        w = self.widget_for(mid)
        if w:
            self.ensureWidgetVisible(w, 0, 120)

    def edit_last_own(self):
        for m, w in reversed(self.shown):
            if m["author"] == self.core.me:
                w.start_edit()
                self.ensureWidgetVisible(w)
                return

    def _on_scroll(self, value):
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


class InputBox(QPlainTextEdit):
    submit = Signal()
    files_pasted = Signal(list)
    edit_last = Signal()
    escape = Signal()

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
        attach = IconButton("plus_circle", "Прикрепить файл", 22, 36)
        attach.hover_bg = "transparent"
        attach.clicked.connect(self.pick_files)
        self.input = InputBox()
        self.input.submit.connect(self.send)
        self.input.files_pasted.connect(self.add_files)
        self.input.edit_last.connect(lambda: view.list.edit_last_own())
        self.input.escape.connect(self.clear_reply)
        self.input.textChanged.connect(self._typing)
        emoji = IconButton("smile", "Эмодзи", 22, 36)
        emoji.hover_bg = "transparent"
        emoji.clicked.connect(lambda: self._emoji(emoji))
        h.addWidget(attach, 0, Qt.AlignBottom)
        h.addWidget(self.input, 1)
        h.addWidget(emoji, 0, Qt.AlignBottom)
        lay.addWidget(box)

        self.typing = QLabel(" ")
        self.typing.setFixedHeight(T.px(22))
        self.typing.setStyleSheet(f"color: {T.c['text']}; font-size: {T.px(8)}pt; padding-left: 4px;")
        lay.addWidget(self.typing)

    def set_placeholder(self, name):
        self.input.setPlaceholderText(f"Написать в #{name}")

    def _typing(self):
        if self.input.toPlainText().strip() and self.view.cid:
            self.core.send_typing(self.view.cid)

    def _emoji(self, anchor):
        picker = EmojiPicker(self.window())
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
            h.addWidget(QLabel(Path(p).name[:40]))
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
        for w in (self.h_icon, self.h_name, self.h_sep, self.h_topic):
            h.addWidget(w)
        h.addStretch(1)
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
        core.members_changed.connect(self._on_members)

    def set_channel(self, cid):
        ch = self.core.store.channel(cid)
        if not ch:
            return
        changed = cid != self.cid
        self.cid = cid
        self.h_name.setText(ch["name"])
        self.h_topic.setText(ch["topic"])
        self.h_sep.setVisible(bool(ch["topic"]))
        self.composer.set_placeholder(ch["name"])
        if changed:
            self.composer.clear_reply()
            self.list.set_channel(cid)
            self.composer.show_typing(self.core.typers(cid))
        self.core.mark_read(cid)
        self.composer.input.setFocus()

    def reply_to(self, msg):
        self.composer.reply_to(msg)

    def _on_changed(self, cid, mid):
        if cid == self.cid:
            self.list.refresh_message(mid)

    def _on_removed(self, cid, mid):
        if cid == self.cid:
            self.list.set_channel(cid, keep_scroll=True)

    def _on_typing(self, cid):
        if cid == self.cid:
            self.composer.show_typing(self.core.typers(cid))

    def _on_members(self):
        if self.cid:
            self.list.set_channel(self.cid, keep_scroll=True)

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
