"""Message formatting (a safe Markdown subset → Qt rich text) and the emoji picker."""

import html
import re

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget

from .theme import T, mix
from .widgets import FlowLayout

URL_RE = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,:;!?)\]']")
MENTION_RE = re.compile(r"@([\w.\-]{1,32})", re.UNICODE)
EMOJI_ONLY_RE = re.compile(r"^[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍\U0001F3FB-\U0001F3FF\s]{1,24}$")

EMOJI = {
    "Смайлы": "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 😉 😊 😇 🥰 😍 🤩 😘 😋 😛 😜 🤪 😎 🤓 🧐 🤔 🤨 😐 😑 😶 🙄 😏 "
              "😬 😴 🤤 😷 🤒 🤕 🤢 🤮 🥵 🥶 🥴 😵 🤯 🤠 🥳 😕 😟 🙁 😮 😯 😲 😳 🥺 😦 😧 😨 😰 😥 😢 😭 "
              "😱 😖 😣 😞 😓 😩 😫 🥱 😤 😡 😠 🤬 😈 💀 🤡 👻 👽 🤖 💩",
    "Жесты": "👍 👎 👌 🤌 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👇 ☝️ ✋ 🤚 🖐️ 🖖 👋 🤝 🙏 👏 🙌 👐 💪 🫡 🫶 🤷 🤦 🙈 🙉 🙊",
    "Сердца": "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 🔥 ✨ ⭐ 🌟 💯 ✅ ❌ ❗ ❓ 💤 💢 💥",
    "Разное": "🎮 🕹️ 🎧 🎤 🎬 📺 💻 🖥️ ⌨️ 🖱️ 📱 📷 🎵 🎶 🏆 🥇 🎉 🎊 🎁 🍕 🍔 🍟 🌭 🍿 🍩 🍪 🍺 🍻 ☕ "
              "🥤 🚀 🛸 🚗 🏠 ⏰ 💡 📌 📎 🔒 🔑 💰 🐱 🐶 🦊 🐸 🐧 🦄",
}
QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🔥"]


def _placeholder(store, value):
    store.append(value)
    return f"\x00{len(store) - 1}\x00"


def _restore(text, store):
    return re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], text)


def is_jumbo(text):
    return bool(text) and bool(EMOJI_ONLY_RE.match(text)) and len(text.replace(" ", "")) <= 12


def render(text, my_name=""):
    """Markdown subset → HTML for QLabel. Everything user-supplied is escaped first."""
    c = T.c
    out = []
    parts = re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2:
            body = html.escape(part.strip("\n"))
            out.append(f'<table width="100%" cellpadding="8" style="background-color:{c["code"]}; '
                       f'margin:4px 0"><tr><td><pre style="font-family:Consolas,monospace; '
                       f'font-size:{T.px(9)}pt; margin:0">{body}</pre></td></tr></table>')
        elif part:
            out.append(_inline(part, my_name))
    return "".join(out)


def _inline(s, my_name):
    c = T.c
    keep = []
    s = re.sub(r"`([^`\n]+)`", lambda m: _placeholder(
        keep, f'<span style="font-family:Consolas,monospace; background-color:{c["code"]}">'
              f'&nbsp;{html.escape(m.group(1))}&nbsp;</span>'), s)
    s = URL_RE.sub(lambda m: _placeholder(
        keep, f'<a href="{html.escape(m.group(0), quote=True)}" style="color:#00a8fc; '
              f'text-decoration:none">{html.escape(m.group(0))}</a>'), s)
    s = html.escape(s, quote=False)

    def mention(m):
        name = m.group(1)
        mine = my_name and name.lower() in (my_name.lower(), "все", "everyone")
        bg = mix(c["main"], c["yellow"] if mine else c["accent"], 0.28)
        fg = c["header"] if mine else mix(c["accent"], "#ffffff", 0.45)
        return _placeholder(keep, f'<span style="background-color:{bg}; color:{fg}; '
                                  f'font-weight:600">&nbsp;@{name}&nbsp;</span>')

    s = MENTION_RE.sub(mention, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", s)
    lines = []
    for line in s.split("\n"):
        if line.startswith("&gt; "):
            line = f'<span style="color:{c["divider"]}">▍</span>&nbsp;{line[5:]}'
        lines.append(line)
    return _restore("<br>".join(lines), keep)


def plain_preview(text, limit=90):
    text = re.sub(r"```.*?```", "[код]", text, flags=re.S)
    text = re.sub(r"[*_~`>|]", "", text).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


class EmojiPicker(QFrame):
    """Popup grid of emoji; emits `picked`."""

    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(f"QFrame {{ background: {T.c['float']}; border-radius: 8px; }}"
                           f"QToolButton {{ border: none; border-radius: 6px; font-size: 17pt;"
                           f"padding: 2px; }} QToolButton:hover {{ background: {T.c['active']}; }}")
        self.setFixedSize(368, 340)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 4, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        for title, chars in EMOJI.items():
            cap = QLabel(title.upper())
            cap.setProperty("role", "caption")
            lay.addWidget(cap)
            grid_w = QWidget()
            grid = FlowLayout(grid_w, spacing=2)
            for ch in chars.split():
                b = QToolButton()
                b.setText(ch)
                b.setFixedSize(40, 40)
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, e=ch: self._pick(e))
                grid.addWidget(b)
            lay.addWidget(grid_w)
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _pick(self, e):
        self.picked.emit(e)
        self.close()

    def popup_at(self, global_pos: QPoint):
        screen = self.screen().availableGeometry() if self.screen() else None
        x, y = global_pos.x() - self.width(), global_pos.y() - self.height() - 8
        if screen:
            x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
            y = max(screen.top() + 4, y)
        self.move(x, y)
        self.show()
