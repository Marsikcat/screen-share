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
