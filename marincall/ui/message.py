"""One chat message row (Discord layout: avatar + name on the first message of a group)."""

import datetime as dt
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QToolButton, QVBoxLayout, QWidget)

from . import icons, richtext
from .theme import T, mix
from .widgets import Avatar, FlowLayout, IconButton, human_size

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
_thumbs = {}


def when(ts, full=True):
    d = dt.datetime.fromtimestamp(ts / 1000)
    today = dt.date.today()
    hm = d.strftime("%H:%M")
    if not full:
        return hm
    if d.date() == today:
        return f"Сегодня в {hm}"
    if d.date() == today - dt.timedelta(days=1):
        return f"Вчера в {hm}"
    return d.strftime("%d.%m.%Y ") + hm


def day_title(ts):
    d = dt.datetime.fromtimestamp(ts / 1000)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year} г."


def readable(color):
    """Name colours must stay readable on the light theme too."""
    return mix(color, "#000000", 0.25) if T.light else mix(color, "#ffffff", 0.25)


def rounded(pm, radius):
    """Picture with rounded corners, like Discord's attachments."""
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(pm))
    p.drawRoundedRect(QRectF(0, 0, pm.width(), pm.height()), radius, radius)
    p.end()
    return out


def open_file(core, meta, save=False):
    src = core.file_path(meta["id"])
    if not src:
        return
    if save:
        dest, _ = QFileDialog.getSaveFileName(None, "Сохранить файл",
                                              str(Path.home() / "Downloads" / meta["name"]))
        if dest:
            shutil.copyfile(src, dest)
        return
    # stored under its hash; give the OS a copy with the real name so it picks the right app
    tmp = Path(tempfile.gettempdir()) / "MarinCall" / meta["id"]
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / meta["name"]
    if not target.exists():
        shutil.copyfile(src, target)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


class DateDivider(QWidget):
    def __init__(self, ts):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 6)
        for side in (0, 1):
            line = QFrame()
            line.setFixedHeight(1)
            line.setStyleSheet(f"background: {T.c['divider']};")
            lay.addWidget(line, 1)
            if side == 0:
                lb = QLabel(day_title(ts))
                lb.setStyleSheet(f"color: {T.c['muted']}; font-size: {T.px(8)}pt; font-weight: 600;")
                lay.addWidget(lb)


class EditBox(QPlainTextEdit):
    save = Signal(str)
    cancel = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            self.save.emit(self.toPlainText())
        elif e.key() == Qt.Key_Escape:
            self.cancel.emit()
        else:
            super().keyPressEvent(e)


class MessageWidget(QFrame):
    hovered = Signal(object)
    reply_clicked = Signal(str)      # jump to the replied message

    def __init__(self, core, msg, first):
        super().__init__()
        self.core, self.msg, self.first = core, msg, first
        self.editing = False
        self.time_lb = None
        self.setAutoFillBackground(True)
        self._set_bg(False)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(16, 16 if first else 1, 48, 1)
        self.lay.setSpacing(0)
        self.gutter = QWidget()
        self.gutter.setFixedWidth(56)
        g = QVBoxLayout(self.gutter)
        g.setContentsMargins(0, 0, 0, 0)
        if first:
            member = core.member(msg["author"])
            self.avatar = Avatar(member["name"], member["color"], 40)
            g.addWidget(self.avatar, 0, Qt.AlignTop | Qt.AlignLeft)
        else:
            self.time_lb = QLabel(when(msg["ts"], full=False))
            self.time_lb.setStyleSheet(f"color: transparent; font-size: {T.px(7)}pt;")
            g.addWidget(self.time_lb, 0, Qt.AlignTop | Qt.AlignLeft)
            g.setContentsMargins(2, 4, 0, 0)
        g.addStretch(1)
        self.lay.addWidget(self.gutter)
        self.body = QWidget()
        self.col = QVBoxLayout(self.body)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(2)
        self.lay.addWidget(self.body, 1)
        self.refresh()

    # ── building ────────────────────────────────────────────────────
    def refresh(self):
        while self.col.count():
            item = self.col.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        store, msg, c = self.core.store, self.msg, T.c
        if msg.get("reply"):
            ref = store.msg_by_id.get(msg["reply"])
            text = (f"<b style='color:{readable(self.core.member(ref['author'])['color'])}'>"
                    f"@{self.core.name_of(ref['author'])}</b>&nbsp; "
                    f"{richtext.html.escape(richtext.plain_preview(store.text_of(ref)))}"
                    if ref and ref["id"] not in store.deleted else "<i>Исходное сообщение удалено</i>")
            rb = QLabel(f"<span style='color:{c['muted']}'>↱&nbsp;</span>{text}")
            rb.setStyleSheet(f"color: {c['muted']}; font-size: {T.px(9)}pt;")
            rb.setCursor(Qt.PointingHandCursor)
            rb.mousePressEvent = lambda e: self.reply_clicked.emit(msg["reply"])
            self.col.addWidget(rb)
        if self.first:
            member = self.core.member(msg["author"])
            head = QLabel(f"<span style='color:{readable(member['color'])}; font-weight:600'>"
                          f"{richtext.html.escape(member['name'])}</span>&nbsp;&nbsp;"
                          f"<span style='color:{c['muted']}; font-size:{T.px(8)}pt'>"
                          f"{when(msg['ts'])}</span>")
            self.col.addWidget(head)
        if self.editing:
            self._build_editor()
        else:
            text = store.text_of(msg)
            if text:
                jumbo = richtext.is_jumbo(text)
                html = richtext.render(text, self.core.s["name"])
                if store.is_edited(msg):
                    html += (f"<span style='color:{c['muted']}; font-size:{T.px(7)}pt'>"
                             f"&nbsp;(изменено)</span>")
                lb = QLabel(html)
                lb.setWordWrap(True)
                lb.setTextFormat(Qt.RichText)
                lb.setOpenExternalLinks(True)
                lb.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                lb.setStyleSheet(f"color: {c['text']};" + (f"font-size: {T.px(28)}pt;" if jumbo else ""))
                if self.core.mentions_me(text) and msg["author"] != self.core.me:
                    self._mention = True
                    self._set_bg(False)
                self.col.addWidget(lb)
        for meta in msg["files"]:
            self.col.addWidget(self._attachment(meta))
        reactions = store.reactions_of(msg["id"])
        if reactions:
            box = QWidget()
            flow = FlowLayout(box, spacing=4)
            for emoji, users in reactions.items():
                flow.addWidget(self._reaction_chip(emoji, users))
            self.col.addWidget(box)

    def _reaction_chip(self, emoji, users):
        c = T.c
        mine = self.core.me in users
        b = QToolButton()
        b.setText(f"{emoji}  {len(users)}")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(", ".join(self.core.name_of(u) for u in users))
        border = c["accent"] if mine else "transparent"
        bg = c["accent_soft"] if mine else c["side"]
        b.setStyleSheet(f"QToolButton {{ background: {bg}; border: 1px solid {border}; border-radius: 8px;"
                        f"padding: 2px 8px; color: {c['header'] if mine else c['text']}; font-weight: 600; }}"
                        f"QToolButton:hover {{ border: 1px solid {c['muted']}; }}")
        b.clicked.connect(lambda: self.core.toggle_reaction(self.msg["id"], emoji))
        return b

    def _attachment(self, meta):
        c = T.c
        path = self.core.file_path(meta["id"])
        if meta["type"].startswith("image/") and path:
            pm = _thumbs.get(meta["id"])
            if pm is None:
                src = QPixmap(str(path))
                if not src.isNull():
                    pm = src.scaled(QSize(420, 320), Qt.KeepAspectRatio, Qt.SmoothTransformation) \
                        if src.width() > 420 or src.height() > 320 else src
                    pm = rounded(pm, 8)
                    _thumbs[meta["id"]] = pm
            if pm is not None:
                lb = QLabel()
                lb.setPixmap(pm)
                lb.setCursor(Qt.PointingHandCursor)
                lb.setToolTip(f"{meta['name']} · {human_size(meta['size'])} — открыть")
                lb.mousePressEvent = lambda e: open_file(self.core, meta)
                wrap = QWidget()
                h = QHBoxLayout(wrap)
                h.setContentsMargins(0, 4, 0, 4)
                h.addWidget(lb)
                h.addStretch(1)
                return wrap
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background: {c['side']}; border: 1px solid {c['border']};"
                           f"border-radius: 8px; }} QLabel {{ border: none; background: transparent; }}")
        card.setMaximumWidth(420)
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 10, 10, 10)
        ic = QLabel()
        ic.setPixmap(icons.pixmap("file", c["accent"], 30, 1.6))
        h.addWidget(ic)
        info = QVBoxLayout()
        info.setSpacing(0)
        name = QLabel(f"<span style='color:{c['link']}'>{richtext.html.escape(meta['name'])}</span>")
        size = QLabel(human_size(meta["size"]) if path else "Загрузка у участников…")
        size.setProperty("role", "hint")
        info.addWidget(name)
        info.addWidget(size)
        h.addLayout(info, 1)
        if path:
            for icon_name, tip, save in (("eye", "Открыть", False), ("download", "Сохранить как…", True)):
                b = IconButton(icon_name, tip, 18, 30)
                b.clicked.connect(lambda _=False, s=save: open_file(self.core, meta, s))
                h.addWidget(b)
        return card

    # ── editing ─────────────────────────────────────────────────────
    def start_edit(self):
        self.editing = True
        self.refresh()

    def _build_editor(self):
        box = EditBox()
        box.setPlainText(self.core.store.text_of(self.msg))
        box.setFixedHeight(max(44, min(200, int(box.document().size().height() * 20) + 24)))
        box.save.connect(self._save_edit)
        box.cancel.connect(self._cancel_edit)
        link = T.c["link"]
        hint = QLabel(f"Esc — <span style='color:{link}'>отмена</span> • Enter — "
                      f"<span style='color:{link}'>сохранить</span>")
        hint.setProperty("role", "hint")
        self.col.addWidget(box)
        self.col.addWidget(hint)
        box.setFocus()
        cur = box.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        box.setTextCursor(cur)

    def _save_edit(self, text):
        self.editing = False
        if text.strip() and text.strip() != self.core.store.text_of(self.msg):
            self.core.edit_message(self.msg["id"], text)
        self.refresh()

    def _cancel_edit(self):
        self.editing = False
        self.refresh()

    # ── hover ───────────────────────────────────────────────────────
    _mention = False

    def _set_bg(self, hover):
        pal = self.palette()
        if self._mention:
            color = mix(T.c["main"], T.c["yellow"], 0.16 if hover else 0.12)
        else:
            color = T.c["msg_hover"] if hover else T.c["main"]
        pal.setColor(QPalette.Window, QColor(color))
        self.setPalette(pal)
        if self.time_lb:
            self.time_lb.setStyleSheet(
                f"color: {T.c['muted'] if hover else 'transparent'}; font-size: {T.px(7)}pt;")

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._mention:                   # Discord's yellow bar on messages that mention you
            p = QPainter(self)
            p.fillRect(0, 0, 2, self.height(), QColor(T.c["yellow"]))

    def enterEvent(self, e):
        self._set_bg(True)
        self.hovered.emit(self)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._set_bg(False)
        super().leaveEvent(e)
