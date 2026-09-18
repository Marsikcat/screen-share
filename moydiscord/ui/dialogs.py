"""Small modal dialogs styled like the rest of the app."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from ..config import APP_NAME, PALETTE
from .settings import ColorDot
from .theme import T
from .widgets import Avatar, button, label


class Dialog(QDialog):
    def __init__(self, parent, title, text=None, width=440):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        self.setStyleSheet(f"QDialog {{ background: {T.c['main']}; }}")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(22, 20, 22, 18)
        self.v.setSpacing(10)
        self.v.addWidget(label(title, "h2"))
        if text:
            self.v.addWidget(label(text, "muted", wrap=True))

    def buttons(self, ok_text, kind=None, cancel=True):
        row = QHBoxLayout()
        row.addStretch(1)
        if cancel:
            row.addWidget(button("Отмена", "link", self.reject))
        ok = button(ok_text, kind, self.accept)
        ok.setDefault(True)
        row.addWidget(ok)
        self.v.addSpacing(6)
        self.v.addLayout(row)
        return ok


def ask_text(parent, title, caption, value="", placeholder="", ok="Сохранить", text=None):
    d = Dialog(parent, title, text)
    d.v.addWidget(label(caption.upper(), "caption"))
    edit = QLineEdit(value)
    edit.setPlaceholderText(placeholder)
    edit.setMaxLength(32)
    edit.selectAll()
    d.v.addWidget(edit)
    d.buttons(ok)
    edit.returnPressed.connect(d.accept)
    if d.exec() == QDialog.Accepted:
        return " ".join(edit.text().split())
    return None


def confirm(parent, title, text, ok="Удалить", kind="danger"):
    d = Dialog(parent, title, text)
    d.buttons(ok, kind)
    return d.exec() == QDialog.Accepted


def onboarding(parent, settings):
    """First launch: pick a name and a colour. Returns (name, color) or None."""
    d = Dialog(parent, f"Добро пожаловать в {APP_NAME}!",
               "Как вас называть? Имя увидят друзья в чате и в голосовых каналах. "
               "Его всегда можно поменять в настройках.", width=480)
    state = {"color": settings["color"]}
    head = QHBoxLayout()
    avatar = Avatar(settings["name"] or "?", state["color"], 56)
    head.addWidget(avatar)
    edit = QLineEdit(settings["name"])
    edit.setPlaceholderText("Ваше имя")
    edit.setMaxLength(32)
    edit.textChanged.connect(lambda t: avatar.set(name=t or "?"))
    head.addWidget(edit, 1)
    d.v.addLayout(head)
    d.v.addWidget(label("ЦВЕТ", "caption"))
    row = QHBoxLayout()
    row.setSpacing(4)
    dots = []

    def pick(col):
        state["color"] = col
        avatar.set(color=col)
        for dot in dots:
            dot.sel = dot.color == col
            dot.update()
    for col in PALETTE:
        dot = ColorDot(col, col == state["color"], 32)
        dot.clicked.connect(lambda _=False, x=col: pick(x))
        dots.append(dot)
        row.addWidget(dot)
    row.addStretch(1)
    d.v.addLayout(row)
    ok = d.buttons("Поехали", cancel=False)
    ok.setEnabled(bool(edit.text().strip()))
    edit.textChanged.connect(lambda t: ok.setEnabled(bool(t.strip())))
    edit.returnPressed.connect(lambda: ok.isEnabled() and d.accept())
    d.setWindowFlag(Qt.WindowCloseButtonHint, False)
    if d.exec() == QDialog.Accepted and edit.text().strip():
        return " ".join(edit.text().split()), state["color"]
    return None


def _fuzzy(query, name):
    """Substring match scores best; otherwise all letters in order (Discord-style)."""
    q, n = query.lower(), name.lower()
    if not q:
        return 1
    if n.startswith(q):
        return 3
    if q in n:
        return 2
    it = iter(n)
    return 1 if all(ch in it for ch in q) else 0


class QuickSwitcher(Dialog):
    """Ctrl+K: type part of a channel name, Enter to jump."""

    def __init__(self, parent, core):
        super().__init__(parent, "Быстрый переход", width=520)
        from PySide6.QtWidgets import QListWidget
        from . import icons
        self.core, self.icons = core, icons
        self.cid = None
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Куда перейти? Начните вводить название канала")
        self.edit.addAction(icons.icon("search", T.c["muted"], 18), QLineEdit.LeadingPosition)
        self.list = QListWidget()
        self.list.setMinimumHeight(300)
        self.v.addWidget(self.edit)
        self.v.addWidget(self.list)
        self.v.addWidget(label("↑ ↓ — выбор   ·   Enter — перейти   ·   Esc — закрыть", "hint"))
        self.edit.textChanged.connect(self._fill)
        self.edit.returnPressed.connect(self._go)
        self.list.itemActivated.connect(lambda _: self._go())
        self.edit.installEventFilter(self)
        self._fill("")

    def _fill(self, text):
        from PySide6.QtWidgets import QListWidgetItem
        self.list.clear()
        rows = []
        for kind in ("text", "voice"):
            for ch in self.core.store.channel_list(kind):
                score = _fuzzy(text.strip(), ch["name"])
                if score:
                    unread = self.core.unread(ch["id"])[0] if kind == "text" else 0
                    who = len(self.core.voice_members(ch["id"])) if kind == "voice" else 0
                    rows.append((-score, -(unread > 0), ch, unread, who))
        rows.sort(key=lambda r: (r[0], r[1]))
        for _, _, ch, unread, who in rows:
            extra = (f"   ·   непрочитанных: {unread}" if unread else "") + \
                    (f"   ·   в канале: {who}" if who else "")
            item = QListWidgetItem(self.icons.icon("hash" if ch["kind"] == "text" else "volume",
                                                   T.c["muted"], 18), ch["name"] + extra)
            item.setData(Qt.UserRole, ch["id"])
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == e.Type.KeyPress and e.key() in (Qt.Key_Up, Qt.Key_Down):
            row = self.list.currentRow() + (1 if e.key() == Qt.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
            return True
        return super().eventFilter(obj, e)

    def _go(self):
        item = self.list.currentItem()
        if item:
            self.cid = item.data(Qt.UserRole)
            self.accept()


class ShortcutsHelp(Dialog):
    """Ctrl+/: every shortcut in one place."""

    def __init__(self, parent, settings):
        super().__init__(parent, "Горячие клавиши", width=620)
        from PySide6.QtWidgets import QGridLayout, QLabel
        from .. import hotkeys
        from .settings_pages import keycaps

        def table(rows):
            grid = QGridLayout()
            grid.setVerticalSpacing(7)
            grid.setHorizontalSpacing(20)
            for i, (keys, what) in enumerate(rows):
                k = QLabel(keycaps(keys) if keys else f"<span style='color:{T.c['muted']}'>не назначено</span>")
                k.setTextFormat(Qt.RichText)
                grid.addWidget(k, i, 0)
                grid.addWidget(label(what, wrap=True), i, 1)
            grid.setColumnStretch(1, 1)
            return grid

        self.v.addWidget(label("ГЛОБАЛЬНЫЕ — РАБОТАЮТ И В ИГРАХ", "caption"))
        bound = settings["hotkeys"]
        self.v.addLayout(table([(hotkeys.describe(bound.get(a)) if bound.get(a) else "", title)
                                for a, (title, _) in hotkeys.ACTIONS.items()]))
        self.v.addSpacing(8)
        self.v.addWidget(label("ВНУТРИ ПРИЛОЖЕНИЯ", "caption"))
        self.v.addLayout(table(hotkeys.IN_APP))
        self.v.addWidget(label("Изменить глобальные сочетания: Настройки → Горячие клавиши.", "hint"))
        self.buttons("Понятно", cancel=False)


class InviteDialog(Dialog):
    """Shows an invite code for the current room."""

    def __init__(self, parent, core):
        super().__init__(parent, "Пригласить друга", width=520)
        from PySide6.QtWidgets import QApplication, QPlainTextEdit
        code = core.create_invite()
        self.v.addWidget(label("Отправьте этот код другу — например, в Telegram. Друг вставит его в "
                               "«Присоединиться по коду» и попадёт в комнату «" + core.room_name() + "», "
                               "даже если он в другом городе.", "muted", wrap=True))
        box = QPlainTextEdit(code)
        box.setReadOnly(True)
        box.setFixedHeight(92)
        box.setStyleSheet("font-family: Consolas;")
        self.v.addWidget(box)
        if core.s["network_mode"] != "internet":
            self.v.addWidget(label("Сейчас включён режим «только локальная сеть» — по коду смогут "
                                   "войти только из вашей сети.", "hint", wrap=True))
        self.v.addWidget(label("Код открывает доступ к комнате — не публикуйте его в открытых чатах.",
                               "hint", wrap=True))
        ok = self.buttons("Скопировать и закрыть", cancel=False)
        ok.clicked.connect(lambda: QApplication.clipboard().setText(code))


class JoinDialog(Dialog):
    """Paste an invite code: join that room (restarting if it is a different one)."""

    def __init__(self, parent, core):
        super().__init__(parent, "Присоединиться по коду", width=520)
        from PySide6.QtWidgets import QPlainTextEdit
        self.core, self.win = core, parent
        self.v.addWidget(label("Вставьте код приглашения, который прислал друг.", "muted", wrap=True))
        self.box = QPlainTextEdit()
        self.box.setPlaceholderText("moyd:…")
        self.box.setFixedHeight(92)
        self.box.setStyleSheet("font-family: Consolas;")
        self.v.addWidget(self.box)
        self.error = label("", "hint", wrap=True)
        self.error.setStyleSheet(f"color: {T.c['red']};")
        self.v.addWidget(self.error)
        ok = self.buttons("Присоединиться")
        ok.clicked.disconnect()
        ok.clicked.connect(self._join)

    def _join(self):
        try:
            restart = self.core.join_invite(self.box.toPlainText())
        except ValueError as e:
            self.error.setText(str(e).capitalize())
            return
        self.accept()
        if restart:
            self.win.toast("Переходим в комнату из приглашения — перезапуск…")
            from PySide6.QtCore import QTimer
            QTimer.singleShot(800, self.win.restart)
        else:
            self.win.toast("Соединяемся с другом…")
