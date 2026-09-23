"""
Global hotkeys.

Polls the keyboard state (GetAsyncKeyState) instead of RegisterHotKey, so a
binding never swallows the key from the game underneath, mouse buttons 4/5
work, and a bare modifier (e.g. Left Ctrl for push-to-talk) can be bound.

A binding is {"vk": int, "mods": ["ctrl", "shift", "alt", "win"]}.
"""

import ctypes

from PySide6.QtCore import QObject, QTimer, Signal

from .system import key_name

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

MOD_KEYS = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,), "win": (0x5B, 0x5C)}
MOD_ORDER = ("ctrl", "shift", "alt", "win")
# specific left/right modifier keys and which generic modifier they belong to
MODIFIER_VK = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win", 0x5C: "win",
               0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl", 0xA4: "alt", 0xA5: "alt"}
IGNORED_VK = {0x01, 0x02, 0x0D}       # left/right click, Enter (confirms dialogs)

# action id -> (title, hint)
ACTIONS = {
    "toggle_mute": ("Вкл/выкл микрофон", ""),
    "toggle_deafen": ("Вкл/выкл звук", "Выключает и звук, и микрофон"),
    "ptt": ("Режим рации", "Держите — микрофон включён. Работает, если в «Голос и звук» выбран режим рации"),
    "toggle_stream": ("Демонстрация экрана", "Начать (откроется выбор экрана) или остановить"),
    "join_voice": ("Зайти в голосовой канал", "В тот, где вы были последним"),
    "leave_voice": ("Отключиться от голосового канала", ""),
    "show_window": ("Показать / скрыть окно", ""),
}

# shortcuts bound inside the window (MainWindow) — listed in settings and the Ctrl+/ help
IN_APP = [
    ("Ctrl+K", "Быстрый переход к каналу"),
    ("Alt+↑ / Alt+↓", "Предыдущий / следующий канал"),
    ("Alt+Shift+↑ / Alt+Shift+↓", "Предыдущий / следующий непрочитанный канал"),
    ("Ctrl+E", "Эмодзи"),
    ("Ctrl+Shift+U", "Прикрепить файл"),
    ("↑", "Изменить своё последнее сообщение (в пустом поле)"),
    ("Esc", "Отменить ответ или отметить канал прочитанным"),
    ("Page Up / Page Down", "Прокрутка сообщений"),
    ("Shift+Enter", "Новая строка в сообщении"),
    ("Ctrl+,", "Настройки"),
    ("Ctrl+/", "Список горячих клавиш"),
]

DEFAULTS = {
    "toggle_mute": {"vk": 0x4D, "mods": ["ctrl", "shift"]},     # Ctrl+Shift+M
    "toggle_deafen": {"vk": 0x44, "mods": ["ctrl", "shift"]},   # Ctrl+Shift+D
    "ptt": {"vk": 0x56, "mods": []},                            # V
    "toggle_stream": None,
    "join_voice": None,
    "leave_voice": None,
    "show_window": None,
}


def _down(vk):
    return bool(_user32 and _user32.GetAsyncKeyState(int(vk)) & 0x8000)


def mods_down():
    return {name for name, vks in MOD_KEYS.items() if any(_down(v) for v in vks)}


def _required(binding):
    """Modifiers that must be down: the listed ones plus the key itself if it is a modifier."""
    req = set(binding.get("mods") or [])
    if binding["vk"] in MODIFIER_VK:
        req.add(MODIFIER_VK[binding["vk"]])
    return req


def held(binding):
    """For push-to-talk: key and its modifiers are down (extra modifiers are fine —
    you may be holding Shift to sprint)."""
    if not binding:
        return False
    return _down(binding["vk"]) and all(any(_down(v) for v in MOD_KEYS[m]) for m in binding.get("mods") or [])


def describe(binding):
    if not binding:
        return "Не назначено"
    parts = [m.capitalize() if m != "win" else "Win" for m in MOD_ORDER if m in (binding.get("mods") or [])]
    return " + ".join(parts + [key_name(binding["vk"])])


def normalized(hotkeys):
    """Saved bindings merged over defaults (a cleared binding stays None)."""
    out = {k: (dict(v) if v else None) for k, v in DEFAULTS.items()}
    for k, v in (hotkeys or {}).items():
        if k in out and (v is None or (isinstance(v, dict) and isinstance(v.get("vk"), int))):
            out[k] = v
    return out


class HotkeyManager(QObject):
    """Fires `triggered(action)` once per press of a bound combination."""

    triggered = Signal(str)

    def __init__(self, settings, is_app_active, is_typing):
        super().__init__()
        self.s = settings
        self.is_app_active = is_app_active
        self.is_typing = is_typing
        self.paused = False
        self._prev = {}
        self._tap = {}           # bare-modifier bindings: action -> still a clean tap?
        self._timer = QTimer(self, interval=25, timeout=self._poll)

    def start(self):
        self._timer.start()

    def _poll(self):
        if self.paused:
            return
        active = self.is_app_active()
        if not active and not self.s["hotkeys_global"]:
            self._prev.clear()
            self._tap.clear()
            return
        mods = mods_down()
        for action, b in self.s["hotkeys"].items():
            if not b or action == "ptt":
                continue
            if b["vk"] in MODIFIER_VK:
                self._poll_tap(action, b, mods)
                continue
            now = _down(b["vk"]) and mods == _required(b)
            if now and not self._prev.get(action):
                # plain letters are text while you type in our own window
                if not (active and self.is_typing() and not set(b.get("mods") or []) - {"shift"}):
                    self.triggered.emit(action)
            self._prev[action] = now

    def _poll_tap(self, action, b, mods):
        """A bare modifier fires when tapped on its own — so Ctrl+C never triggers a Ctrl binding."""
        if _down(b["vk"]):
            clean = mods == _required(b) and not _other_key_down()
            self._tap[action] = clean if action not in self._tap else (self._tap[action] and clean)
        elif self._tap.pop(action, False):
            self.triggered.emit(action)


def _other_key_down():
    return any(_down(vk) for vk in range(0x01, 0xFF) if vk not in MODIFIER_VK)


class Recorder(QObject):
    """Captures the next combination: Esc cancels, a lone modifier tap binds that modifier."""

    captured = Signal(object)     # binding dict, or None when cancelled

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self, interval=20, timeout=self._poll)
        self._ticks = 0

    def start(self):
        self._ticks = 0
        self._armed = False       # wait until everything (incl. the click) is released
        self._last_mod = None
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _finish(self, binding):
        self._timer.stop()
        self.captured.emit(binding)

    def _poll(self):
        self._ticks += 1
        if self._ticks > 500:                      # 10 s
            return self._finish(None)
        pressed = [vk for vk in range(0x01, 0xFF) if vk not in IGNORED_VK and _down(vk)]
        if not self._armed:
            self._armed = not pressed and not _down(0x01)
            return
        mods_vk = [vk for vk in pressed if vk in MODIFIER_VK]
        keys = [vk for vk in pressed if vk not in MODIFIER_VK]
        if 0x1B in keys:                           # Esc
            return self._finish(None)
        if keys:
            mods = sorted({MODIFIER_VK[v] for v in mods_vk}, key=MOD_ORDER.index)
            return self._finish({"vk": keys[0], "mods": mods})
        specific = [v for v in mods_vk if v >= 0xA0 or v in (0x5B, 0x5C)]
        if specific:
            self._last_mod = specific[0]
        elif self._last_mod is not None:
            # modifiers pressed and released on their own: bind the modifier itself
            return self._finish({"vk": self._last_mod, "mods": []})
