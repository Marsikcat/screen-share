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
NNBSP = "\u202f"        # narrow no-break space: the "padding" of inline code and mention pills


def _placeholder(store, value):
    store.append(value)
    return f"\x00{len(store) - 1}\x00"


def _restore(text, store):
    return re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], text)


def is_jumbo(text):
    return bool(text) and bool(EMOJI_ONLY_RE.match(text)) and len(text.replace(" ", "")) <= 12


SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.S)


def hide_spoilers(text):
    """For previews and notifications: what is under ||…|| stays hidden."""
    return SPOILER_RE.sub("▒▒▒", text)


def render(text, my_name="", revealed=()):
    """Markdown subset → HTML for QLabel. Everything user-supplied is escaped first.
    revealed: numbers of the ||spoilers|| already clicked open (links "spoiler:N")."""
    c = T.c
    out = []
    counter = [0]
    parts = re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if not i % 2:       # the line break next to a code block is the block's own edge
            if i > 0:
                part = part.removeprefix("\n")
            if i < len(parts) - 1:
                part = part.removesuffix("\n")
        if i % 2:
            body = html.escape(part.strip("\n"))
            out.append(f'<table width="100%" cellpadding="8" style="background-color:{c["code"]}; '
                       f'margin:4px 0"><tr><td><pre style="font-family:Consolas,monospace; '
                       f'font-size:{T.px(9)}pt; margin:0">{body}</pre></td></tr></table>')
        elif part:
            out.append(_inline(part, my_name, counter, set(revealed)))
    return "".join(out)


def _inline(s, my_name, counter=None, revealed=frozenset()):
    c = T.c
    keep = []
    counter = counter if counter is not None else [0]
    s = re.sub(r"`([^`\n]+)`", lambda m: _placeholder(
        keep, f'<span style="font-family:Consolas,monospace; background-color:{c["code"]}">'
              f'{NNBSP}{html.escape(m.group(1))}{NNBSP}</span>'), s)
    s = URL_RE.sub(lambda m: _placeholder(
        keep, f'<a href="{html.escape(m.group(0), quote=True)}" style="color:{c["link"]}; '
              f'text-decoration:none">{html.escape(m.group(0))}</a>'), s)
    s = html.escape(s, quote=False)

    def mention(m):
        name = m.group(1)
        mine = my_name and name.lower() in (my_name.lower(), "все", "everyone")
        bg = mix(c["main"], c["yellow"] if mine else c["accent"], 0.28 if not T.light else 0.2)
        if mine:
            fg = c["header"]
        else:           # readable on both: lighter accent on dark, deeper accent on light
            fg = mix(c["accent"], "#000000", 0.2) if T.light else mix(c["accent"], "#ffffff", 0.45)
        return _placeholder(keep, f'<span style="background-color:{bg}; color:{fg}; '
                                  f'font-weight:600">{NNBSP}@{name}{NNBSP}</span>')

    s = MENTION_RE.sub(mention, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", s)

    def spoiler(m):
        n = counter[0]
        counter[0] += 1
        if n in revealed:
            return f'<span style="background-color:{c["code"]}">{m.group(1)}</span>'
        hidden = c["float"] if not T.light else "#c9ccd1"
        return (f'<a href="spoiler:{n}" style="background-color:{hidden}; color:{hidden}; '
                f'text-decoration:none">{m.group(1)}</a>')
    s = SPOILER_RE.sub(spoiler, s)

    lines = []
    sizes = {"### ": 11, "## ": 13, "# ": 15}
    for line in s.split("\n"):
        if line.startswith("&gt; "):
            line = f'<span style="color:{c["divider"]}">▍</span>&nbsp;{line[5:]}'
        elif line.startswith(("- ", "* ")):                       # a bullet list
            line = f"&nbsp;&nbsp;•&nbsp;&nbsp;{line[2:]}"
        elif re.match(r"\d{1,3}[.)] ", line):                    # a numbered list
            num, rest = line.split(" ", 1)
            line = f"&nbsp;&nbsp;{num}&nbsp;{rest}"
        else:
            for mark, size in sizes.items():                      # # headings
                if line.startswith(mark):
                    line = (f'<span style="font-size:{T.px(size)}pt; font-weight:700; color:{c["header"]}">'
                            f'{line[len(mark):]}</span>')
                    break
        lines.append(line)
    return _restore("<br>".join(lines), keep)


def plain_preview(text, limit=90):
    text = re.sub(r"```.*?```", "[код]", hide_spoilers(text), flags=re.S)
    text = re.sub(r"(?m)^#{1,3} ", "", text)
    text = re.sub(r"[*_~`>|]", "", text).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def emoji_matches(ch, query):
    """Does `query` describe this emoji? Russian words first, then its Unicode name."""
    import unicodedata
    from .emoji_names import NAMES
    q = query.casefold().strip()
    if not q:
        return True
    words = NAMES.get(ch, "")
    try:
        words += " " + unicodedata.name(ch[0]).lower()
    except ValueError:
        pass
    return all(any(w.startswith(part) for w in words.split()) or part in words for part in q.split())


class EmojiPicker(QFrame):
    """Popup grid of emoji with a search box and the ones you used last; emits `picked`."""

    picked = Signal(str)
    RECENT = 24

    def __init__(self, parent=None, settings=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        from PySide6.QtWidgets import QLineEdit
        self.settings = settings
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(f"EmojiPicker {{ background: {T.c['float']}; border-radius: 8px; }}"
                           f"QToolButton {{ border: none; border-radius: 6px; font-size: 17pt;"
                           f"padding: 2px; }} QToolButton:hover {{ background: {T.c['active']}; }}")
        self.setFixedSize(368, 380)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 4, 8)
        outer.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти эмодзи: огонь, смех, сердце…")
        self.search.textChanged.connect(self._fill)
        outer.addWidget(self.search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        self._fill("")
        self.search.setFocus()

    def _recent(self):
        return list((self.settings or {}).get("recent_emoji") or [])

    def _fill(self, query):
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        sections = [("Недавние", self._recent())] if not query and self._recent() else []
        sections += [(title, chars.split()) for title, chars in EMOJI.items()]
        if query:                               # what was found: one grid, no section gaps
            sections = [("", [ch for _, chars in sections for ch in chars])]
        found = 0
        for title, chars in sections:
            chars = [ch for ch in chars if emoji_matches(ch, query)]
            if not chars:
                continue
            found += len(chars)
            if not query:
                cap = QLabel(title.upper())
                cap.setProperty("role", "caption")
                lay.addWidget(cap)
            grid_w = QWidget()
            grid = FlowLayout(grid_w, spacing=2)
            for ch in chars:
                b = QToolButton()
                b.setText(ch)
                b.setFixedSize(40, 40)
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, e=ch: self._pick(e))
                grid.addWidget(b)
            lay.addWidget(grid_w)
        if query and not found:
            empty = QLabel("Ничего не нашлось")
            empty.setProperty("role", "hint")
            lay.addWidget(empty)
        lay.addStretch(1)
        self.scroll.setWidget(body)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            first = next((ch for chars in EMOJI.values() for ch in chars.split()
                          if emoji_matches(ch, self.search.text())), None)
            if first and self.search.text().strip():
                self._pick(first)                  # Enter takes the first match
                return
        super().keyPressEvent(e)

    def _pick(self, e):
        if self.settings is not None:
            recent = [e] + [x for x in self._recent() if x != e]
            self.settings["recent_emoji"] = recent[:self.RECENT]
            self.settings.save()
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
