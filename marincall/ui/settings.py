"""Full-window settings (Discord style): categories on the left, the page on the right."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from .. import system
from ..config import APP_NAME, PALETTE, REPO, VERSION
from . import icons
from .theme import ACCENTS, THEMES, T
from .widgets import Avatar, IconButton, Switch, button, label

NAV = [
    ("НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ", None),
    ("profile", ("user", "Мой профиль")),
    ("НАСТРОЙКИ ПРИЛОЖЕНИЯ", None),
    ("appearance", ("palette", "Внешний вид")),
    ("voice", ("mic", "Голос и звук")),
    ("hotkeys", ("keyboard", "Горячие клавиши")),
    ("stream", ("screen", "Демонстрация экрана")),
    ("notifications", ("bell", "Уведомления")),
    ("network", ("globe", "Сеть")),
    ("startup", ("power", "Запуск и трей")),
    ("updates", ("update", "Обновления")),
    ("about", ("info", "О программе")),
]


def section(title, hint=None):
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 18, 0, 6)
    v.setSpacing(4)
    v.addWidget(label(title.upper(), "caption"))
    if hint:
        v.addWidget(label(hint, "hint", wrap=True))
    return w


def switch_row(title, hint, checked, on_toggle):
    w = QFrame()
    w.setStyleSheet(f"QFrame {{ border-bottom: 1px solid {T.c['divider']}; }}"
                    f"QLabel {{ border: none; }}")
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 12, 0, 14)
    col = QVBoxLayout()
    col.setSpacing(2)
    t = QLabel(title)
    t.setStyleSheet(f"color: {T.c['header']}; font-weight: 600;")
    col.addWidget(t)
    if hint:
        col.addWidget(label(hint, "hint", wrap=True))
    h.addLayout(col, 1)
    sw = Switch(checked)
    sw.toggled.connect(on_toggle)
    h.addWidget(sw, 0, Qt.AlignTop)
    return w


class ColorDot(QToolButton):
    def __init__(self, color, selected, size=36):
        super().__init__()
        self.color, self.sel = color, selected
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
        if self.sel:
            p.drawPixmap(self.rect().center().x() - 9, self.rect().center().y() - 9,
                         icons.pixmap("check", "white", 18, 3))


class ThemeCard(QToolButton):
    def __init__(self, key, selected):
        super().__init__()
        self.key, self.sel = key, selected
        self.setFixedSize(112, 96)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(THEMES[key]["label"] if key in THEMES else "Как в Windows")

    @staticmethod
    def _preview(p, r, th):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th["main"]))
        p.drawRoundedRect(r, 8, 8)
        p.setBrush(QColor(th["side"]))
        p.drawRoundedRect(r.adjusted(8, 10, -r.width() // 2, -10), 4, 4)
        p.setBrush(QColor(T.c["accent"]))
        p.drawRoundedRect(r.adjusted(r.width() // 2 + 4, 14, -8, -r.height() + 22), 3, 3)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(3, 3, -3, -25)
        if self.key == "system":
            half = r.width() // 2
            p.setClipRect(r.adjusted(0, 0, -half, 0))
            self._preview(p, r, THEMES["light"])
            p.setClipRect(r.adjusted(r.width() - half, 0, 0, 0))
            self._preview(p, r, THEMES["dark"])
            p.setClipping(False)
        else:
            self._preview(p, r, THEMES[self.key])
        pen_c = QColor(T.c["accent"] if self.sel else T.c["divider"])
        p.setPen(QPen(pen_c, 3 if self.sel else 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 8, 8)
        p.setPen(QColor(T.c["header"] if self.sel else T.c["muted"]))
        f = p.font()
        f.setBold(self.sel)
        p.setFont(f)
        p.drawText(self.rect().adjusted(0, self.height() - 22, 0, 0), Qt.AlignCenter,
                   THEMES[self.key]["label"] if self.key in THEMES else "Системная")


class SettingsView(QWidget):
    closed = Signal()
    appearance_changed = Signal()

    def __init__(self, win, page="profile"):
        super().__init__()
        self.win, self.core, self.s = win, win.core, win.core.s
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"SettingsView {{ background: {T.c['main']}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QWidget()
        side.setAttribute(Qt.WA_StyledBackground, True)
        side.setStyleSheet(f"background: {T.c['side']};")
        sl = QHBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addStretch(1)
        nav = QWidget()
        nav.setFixedWidth(T.px(220))
        self.nav_lay = QVBoxLayout(nav)
        self.nav_lay.setContentsMargins(10, 50, 8, 20)
        self.nav_lay.setSpacing(2)
        self.nav_buttons = {}
        for key, val in NAV:
            if val is None:
                cap = label(key, "caption")
                cap.setContentsMargins(10, 12, 0, 4)
                self.nav_lay.addWidget(cap)
                continue
            b = QToolButton()
            b.setText("  " + val[1])
            b.setIcon(icons.icon(val[0], T.c["icon"], 18))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setStyleSheet(f"QToolButton {{ text-align: left; border: none; border-radius: 4px;"
                            f"padding: 7px 10px; color: {T.c['muted']}; font-weight: 500; }}"
                            f"QToolButton:hover {{ background: {T.c['hover']}; color: {T.c['text']}; }}"
                            f"QToolButton:checked {{ background: {T.c['active']}; color: {T.c['header']}; }}")
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav_buttons[key] = b
            self.nav_lay.addWidget(b)
        self.nav_lay.addStretch(1)
        sl.addWidget(nav)
        h.addWidget(side, 3)

        right = QWidget()
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rl.addWidget(self.scroll, 1)
        close_col = QVBoxLayout()
        close_col.setContentsMargins(0, 50, 30, 0)
        close = IconButton("x", "Закрыть (Esc)", 20, 36)
        close.setStyleSheet(f"QToolButton {{ border: 2px solid {T.c['icon']}; border-radius: 18px; }}")
        close.clicked.connect(self.closed.emit)
        esc = label("ESC", "caption")
        esc.setAlignment(Qt.AlignCenter)
        close_col.addWidget(close)
        close_col.addWidget(esc)
        close_col.addStretch(1)
        rl.addLayout(close_col)
        h.addWidget(right, 5)
        self.page_key = None
        self.show_page(page)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(e)

    def show_page(self, key):
        from . import settings_pages
        if self.page_key == "voice":
            self.core.voice.monitor = False
        self.page_key = key
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(40, 50, 40, 60)
        v.setSpacing(0)
        page.setMaximumWidth(T.px(740))
        own = getattr(self, f"page_{key}", None)
        if own:
            own(v)
        else:
            getattr(settings_pages, f"page_{key}")(self, v)
        v.addStretch(1)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(page, 10)
        wl.addStretch(1)
        self.scroll.setWidget(wrap)
        self.setFocus()

    # ── pages ───────────────────────────────────────────────────────
    def page_profile(self, v):
        v.addWidget(label("Мой профиль", "h2"))
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background: {T.c['rail'] if not T.light else T.c['side']};"
                           f"border-radius: 10px; }}")
        c = QVBoxLayout(card)
        c.setContentsMargins(0, 0, 0, 16)
        banner = QFrame()
        banner.setFixedHeight(72)
        banner.setStyleSheet(f"background: {self.s['color']}; border-top-left-radius: 10px;"
                             f"border-top-right-radius: 10px;")
        c.addWidget(banner)
        row = QHBoxLayout()
        row.setContentsMargins(16, 12, 16, 0)
        self.p_avatar = Avatar(self.s["name"], self.s["color"], 72)
        row.addWidget(self.p_avatar)
        name = QLabel(self.s["name"] or "Без имени")
        name.setStyleSheet(f"color: {T.c['header']}; font-size: {T.px(15)}pt; font-weight: 700;")
        row.addWidget(name, 1, Qt.AlignBottom)
        c.addLayout(row)
        v.addSpacing(16)
        v.addWidget(card)
        v.addWidget(section("Отображаемое имя", "Так вас видят остальные в чате и голосовых каналах."))
        edit = QLineEdit(self.s["name"])
        edit.setMaxLength(32)
        v.addWidget(edit)
        v.addWidget(section("Цвет аватара"))
        colors = QHBoxLayout()
        colors.setSpacing(6)
        self._color = self.s["color"]
        dots = []

        def pick(col):
            self._color = col
            for d in dots:
                d.sel = d.color == col
                d.update()
            self.p_avatar.set(color=col)
            banner.setStyleSheet(f"background: {col}; border-top-left-radius: 10px;"
                                 f"border-top-right-radius: 10px;")
        for col in PALETTE:
            d = ColorDot(col, col == self.s["color"])
            d.clicked.connect(lambda _=False, x=col: pick(x))
            dots.append(d)
            colors.addWidget(d)
        colors.addStretch(1)
        v.addLayout(colors)
        v.addSpacing(18)

        def save():
            n = " ".join(edit.text().split())
            if not n:
                self.win.toast("Имя не может быть пустым", "error")
                return
            self.core.set_profile(n, self._color)
            name.setText(n)
            self.p_avatar.set(name=n)
            self.win.toast("Профиль сохранён")
        edit.returnPressed.connect(save)
        v.addWidget(button("Сохранить изменения", None, save), 0, Qt.AlignLeft)

    def page_appearance(self, v):
        v.addWidget(label("Внешний вид", "h2"))
        v.addWidget(section("Тема"))
        row = QHBoxLayout()
        row.setSpacing(10)
        for key in ("dark", "ash", "onyx", "light", "system"):
            card = ThemeCard(key, self.s["theme"] == key)
            card.clicked.connect(lambda _=False, k=key: self._set("theme", k, restyle=True))
            row.addWidget(card)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(section("Акцентный цвет", "Кнопки, выделение, ссылки на выбранный канал."))
        row = QHBoxLayout()
        row.setSpacing(6)
        for col in ACCENTS:
            d = ColorDot(col, self.s["accent"] == col, 34)
            d.clicked.connect(lambda _=False, x=col: self._set("accent", x, restyle=True))
            row.addWidget(d)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(section("Масштаб текста", f"Сейчас {self.s['font_scale']}%"))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(80, 130)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setValue(self.s["font_scale"])
        slider.sliderReleased.connect(lambda: self._set("font_scale", slider.value(), restyle=True))
        v.addWidget(slider)

    def page_notifications(self, v):
        v.addWidget(label("Уведомления", "h2"))
        v.addWidget(section("Сообщения"))
        v.addWidget(switch_row("Уведомления Windows", "Всплывающее уведомление, когда окно свёрнуто "
                               "или открыт другой канал.", self.s["notify"],
                               lambda on: self._set("notify", on)))
        v.addWidget(switch_row("Только упоминания", "Уведомлять, только если вас упомянули через @имя "
                               "или @все.", self.s["notify_mentions_only"],
                               lambda on: self._set("notify_mentions_only", on)))
        v.addWidget(section("Звуки"))
        v.addWidget(switch_row("Звуковые эффекты", "Вход и выход из голосового канала, выключение "
                               "микрофона, новые сообщения.", self.s["sounds"],
                               lambda on: self._set("sounds", on)))

    def page_startup(self, v):
        v.addWidget(label("Запуск и трей", "h2"))
        v.addWidget(section("Автозапуск"))

        def autostart(on):
            try:
                system.set_autostart(on)
                self._set("autostart", on)
            except OSError as e:
                self.win.toast(f"Не удалось изменить автозапуск: {e}", "error")
        v.addWidget(switch_row("Запускать вместе с Windows", f"{APP_NAME} будет открываться при входе "
                               "в систему — вы сразу в сети для друзей.", system.autostart_enabled(),
                               autostart))
        v.addWidget(switch_row("Запускать свёрнутым в трей", "При автозапуске окно не открывается — "
                               "только значок возле часов.", self.s["start_minimized"],
                               lambda on: self._set("start_minimized", on)))
        v.addWidget(section("Системный трей"))
        v.addWidget(switch_row("Сворачивать в трей при закрытии", "Крестик в углу окна прячет его в трей, "
                               "а голосовой канал и сообщения продолжают работать. Выйти — через "
                               "меню значка в трее.", self.s["close_to_tray"],
                               lambda on: self._set("close_to_tray", on)))

    def page_about(self, v):
        v.addWidget(label(APP_NAME, "h1"))
        v.addWidget(label(f"Версия {VERSION}", "muted"))
        v.addSpacing(10)
        v.addWidget(label("Текстовые и голосовые каналы, демонстрация экрана — напрямую между "
                          "компьютерами в локальной сети или Radmin VPN, без серверов. История "
                          "переписки хранится у каждого участника и досинхронизируется автоматически.",
                          None, wrap=True))
        v.addSpacing(14)
        link = button(f"github.com/{REPO}", "secondary",
                      lambda: __import__("webbrowser").open(f"https://github.com/{REPO}"))
        v.addWidget(link, 0, Qt.AlignLeft)
        v.addWidget(section("Классическая демонстрация экрана",
                            "Старое окно ScreenShare: трансляция по IP без голосовых каналов."))
        v.addWidget(button("Открыть ScreenShare", "secondary", self.win.launch_classic), 0, Qt.AlignLeft)

    # ── helpers ─────────────────────────────────────────────────────
    def _set(self, key, value, restyle=False):
        self.s[key] = value
        self.s.save()
        if restyle:
            self.appearance_changed.emit()

