"""Colour themes and the application stylesheet."""

import ctypes
import sys

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

THEMES = {
    "dark": {
        "label": "Тёмная",
        "rail": "#1e1f22", "side": "#2b2d31", "main": "#313338", "float": "#111214",
        "hover": "#35373c", "active": "#404249", "input": "#383a40", "card": "#2b2d31",
        "border": "#1f2023", "divider": "#3f4147",
        "text": "#dbdee1", "muted": "#949ba4", "header": "#f2f3f5", "icon": "#b5bac1",
        "msg_hover": "#2e3035", "mention_bg": "#444037", "code": "#2b2d31",
    },
    "ash": {
        "label": "Пепельная",
        "rail": "#121214", "side": "#1a1a1e", "main": "#202024", "float": "#0e0e10",
        "hover": "#27272c", "active": "#303036", "input": "#26262b", "card": "#1a1a1e",
        "border": "#101012", "divider": "#2e2e34",
        "text": "#dcdde0", "muted": "#8f929a", "header": "#f3f4f6", "icon": "#aeb1b8",
        "msg_hover": "#1c1c20", "mention_bg": "#3a362d", "code": "#18181b",
    },
    "onyx": {
        "label": "Полночь",
        "rail": "#000000", "side": "#0a0a0b", "main": "#050506", "float": "#000000",
        "hover": "#141416", "active": "#1c1c1f", "input": "#111113", "card": "#0c0c0e",
        "border": "#000000", "divider": "#1e1e21",
        "text": "#d7d8db", "muted": "#85888f", "header": "#f5f5f7", "icon": "#a6a9b0",
        "msg_hover": "#0c0c0e", "mention_bg": "#2e2a20", "code": "#0f0f11",
    },
    "light": {
        "label": "Светлая",
        "rail": "#e3e5e8", "side": "#f2f3f5", "main": "#ffffff", "float": "#ffffff",
        "hover": "#e8e9ec", "active": "#d7d9dc", "input": "#ebedef", "card": "#f2f3f5",
        "border": "#e1e2e4", "divider": "#e1e2e4",
        "text": "#313338", "muted": "#5c5e66", "header": "#060607", "icon": "#4e5058",
        "msg_hover": "#f7f7f8", "mention_bg": "#fdf2dc", "code": "#f2f3f5",
    },
}

ACCENTS = ["#5865f2", "#3ba55c", "#eb459e", "#ed4245", "#faa61a",
           "#00a8fc", "#9b59b6", "#1abc9c"]

GREEN, RED, YELLOW = "#23a55a", "#f23f43", "#f0b232"


def windows_prefers_light():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1
    except OSError:
        return False


def shade(hex_color, factor):
    """factor < 1 darkens, > 1 lightens."""
    c = QColor(hex_color)
    return (c.darker(int(100 / factor)) if factor < 1 else c.lighter(int(100 * factor))).name()


def mix(a, b, t):
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


class Theme:
    """The active palette. Widgets read colours from here (T.c['text'] …)."""

    def __init__(self):
        self.name = "dark"
        self.c = dict(THEMES["dark"])
        self.scale = 1.0

    def load(self, settings):
        name = settings["theme"]
        if name == "system":
            name = "light" if windows_prefers_light() else "dark"
        self.name = name if name in THEMES else "dark"
        c = dict(THEMES[self.name])
        accent = settings["accent"]
        c.update(accent=accent, accent_hover=shade(accent, 0.85),
                 accent_soft=mix(c["main"], accent, 0.18),
                 green=GREEN, red=RED, yellow=YELLOW, white="#ffffff")
        self.c = c
        self.scale = max(0.8, min(1.4, settings["font_scale"] / 100))

    @property
    def light(self):
        return self.name == "light"

    def px(self, n):
        return round(n * self.scale)

    def font(self, size=10, weight=QFont.Normal):
        f = QFont(FONT_FAMILY)
        f.setPointSizeF(size * self.scale)
        f.setWeight(weight)
        return f


T = Theme()
FONT_FAMILY = "Segoe UI Variable Text"


def apply(app: QApplication, settings):
    T.load(settings)
    f = QFont(FONT_FAMILY)
    f.setPointSizeF(10 * T.scale)
    f.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(f)
    app.setStyleSheet(stylesheet())


def stylesheet():
    c = T.c
    return f"""
    * {{ outline: none; }}
    QWidget {{ color: {c['text']}; background: transparent; }}
    QMainWindow, #Root {{ background: {c['main']}; }}
    QToolTip {{ background: {c['float']}; color: {c['header']}; border: none;
                padding: 6px 9px; border-radius: 5px; }}

    #Sidebar {{ background: {c['side']}; }}
    #SideHeader {{ background: {c['side']}; border-bottom: 1px solid {c['border']}; }}
    #SideHeader:hover {{ background: {c['hover']}; }}
    #UserPanel {{ background: {mix(c['side'], c['rail'], 0.55)}; }}
    #VoicePanel {{ background: {mix(c['side'], c['rail'], 0.55)};
                   border-bottom: 1px solid {c['divider']}; }}
    #Members {{ background: {c['side']}; }}
    #ChatHeader {{ background: {c['main']}; border-bottom: 1px solid {c['border']}; }}
    #Banner {{ background: {c['accent']}; }}
    #Banner QLabel {{ color: white; }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['rail'] if not T.light else '#c4c9ce'};
                                   border-radius: 3px; min-height: 30px; margin: 0 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['float'] if not T.light else '#a9aeb4'}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page
        {{ height: 0; background: none; }}
    QScrollBar:horizontal {{ height: 0; }}

    QLineEdit, QPlainTextEdit, QTextEdit {{
        background: {c['input']}; color: {c['text']}; border: none; border-radius: 6px;
        padding: 8px 10px; selection-background-color: {c['accent']}; selection-color: white; }}
    QLineEdit:focus {{ background: {shade(c['input'], 1.04) if not T.light else c['input']}; }}
    #Composer QPlainTextEdit {{ background: transparent; padding: 10px 2px; }}
    #ComposerBox {{ background: {c['input']}; border-radius: 8px; }}

    QComboBox {{ background: {c['input']}; border: none; border-radius: 6px; padding: 8px 12px;
                 color: {c['text']}; min-height: 20px; }}
    QComboBox:hover {{ background: {c['active']}; }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox QAbstractItemView {{ background: {c['float']}; color: {c['text']}; border: none;
        padding: 4px; selection-background-color: {c['accent']}; selection-color: white;
        outline: none; }}

    QPushButton {{ background: {c['accent']}; color: white; border: none; border-radius: 4px;
                   padding: 8px 16px; font-weight: 600; }}
    QPushButton:hover {{ background: {c['accent_hover']}; }}
    QPushButton:disabled {{ background: {c['active']}; color: {c['muted']}; }}
    QPushButton[kind="secondary"] {{ background: {c['active']}; color: {c['header']}; }}
    QPushButton[kind="secondary"]:hover {{ background: {shade(c['active'], 1.15) if not T.light else '#c9ccd1'}; }}
    QPushButton[kind="danger"] {{ background: {c['red']}; }}
    QPushButton[kind="danger"]:hover {{ background: {shade(c['red'], 0.85)}; }}
    QPushButton[kind="success"] {{ background: {c['green']}; }}
    QPushButton[kind="success"]:hover {{ background: {shade(c['green'], 0.85)}; }}
    QPushButton[kind="link"] {{ background: transparent; color: {c['text']}; font-weight: 500;
                                padding: 6px 8px; }}
    QPushButton[kind="link"]:hover {{ text-decoration: underline; }}

    QMenu {{ background: {c['float']}; border: none; border-radius: 6px; padding: 6px; }}
    QMenu::item {{ padding: 7px 26px 7px 10px; border-radius: 3px; color: {c['text']}; }}
    QMenu::item:selected {{ background: {c['accent']}; color: white; }}
    QMenu::item[danger="true"] {{ color: {c['red']}; }}
    QMenu::separator {{ height: 1px; background: {c['divider']}; margin: 4px 6px; }}

    QSlider::groove:horizontal {{ height: 8px; background: {c['active']}; border-radius: 4px; }}
    QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 4px; }}
    QSlider::handle:horizontal {{ background: white; width: 12px; height: 22px; margin: -7px 0;
                                  border-radius: 3px; }}

    QCheckBox {{ spacing: 8px; }}
    QRadioButton {{ spacing: 10px; padding: 4px 0; }}
    QRadioButton::indicator {{ width: 18px; height: 18px; border-radius: 10px;
                               border: 2px solid {c['icon']}; }}
    QRadioButton::indicator:checked {{ border: 2px solid {c['accent']};
        background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                    stop:0 {c['accent']}, stop:0.45 {c['accent']}, stop:0.5 transparent); }}

    QListWidget {{ background: {c['card']}; border: none; border-radius: 8px; padding: 4px; }}
    QListWidget::item {{ padding: 8px; border-radius: 4px; }}
    QListWidget::item:selected {{ background: {c['active']}; color: {c['header']}; }}
    QListWidget::item:hover {{ background: {c['hover']}; }}

    QLabel[role="h1"] {{ color: {c['header']}; font-size: {T.px(20)}pt; font-weight: 700; }}
    QLabel[role="h2"] {{ color: {c['header']}; font-size: {T.px(14)}pt; font-weight: 700; }}
    QLabel[role="caption"] {{ color: {c['muted']}; font-size: {T.px(8)}pt; font-weight: 700; }}
    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="hint"] {{ color: {c['muted']}; font-size: {T.px(9)}pt; }}
    """


def style_window(widget):
    """Dark/coloured native title bar on Windows 10/11 to match the theme."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi

        def colorref(hex_color):
            q = QColor(hex_color)
            return ctypes.c_int(q.red() | (q.green() << 8) | (q.blue() << 16))

        dark = ctypes.c_int(0 if T.light else 1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)            # immersive dark
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref(T.c["rail"])), 4)  # caption
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(colorref(T.c["header"])), 4)  # title text
    except (OSError, AttributeError):
        pass
