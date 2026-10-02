"""The soundboard: short sounds everyone in the voice channel hears (built-in and the room's own)."""

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from . import icons
from .theme import T, mix
from .widgets import IconButton, label

AUDIO_FILES = "Звук (*.mp3 *.ogg *.opus *.wav *.m4a *.aac *.flac *.webm)"


class SoundButton(QToolButton):
    def __init__(self, emoji, name, custom=False):
        super().__init__()
        c = T.c
        self.setText(f"{emoji}  {name}")
        self.setToolTip(f"{name} — услышат все в канале\nПравая кнопка — послушать самому"
                        + ("\nСредняя кнопка — убрать из комнаты" if custom else ""))
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(40)
        self.setMinimumWidth(180)
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setStyleSheet(f"QToolButton {{ background: {c['side']}; color: {c['header']}; border: none;"
                           f"border-radius: 8px; padding: 0 10px; text-align: left; font-weight: 600; }}"
                           f"QToolButton:hover {{ background: {mix(c['side'], c['accent'], 0.25)}; }}"
                           f"QToolButton:pressed {{ background: {mix(c['side'], c['accent'], 0.45)}; }}")


class Soundboard(QFrame):
    def __init__(self, parent, core):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.core, self.win = core, parent
        c = T.c
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("Soundboard")
        self.setStyleSheet(f"#Soundboard {{ background: {c['float']}; border: 1px solid {c['border']};"
                           f"border-radius: 10px; }}")
        self.setFixedWidth(430)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("Звуковая панель")
        title.setStyleSheet(f"color: {c['header']}; font-weight: 700; font-size: {T.px(11)}pt;")
        head.addWidget(title)
        head.addStretch(1)
        add = IconButton("plus", "Добавить свой звук (до 5 секунд) — он появится у всех в комнате", 18, 28)
        add.clicked.connect(self._add)
        head.addWidget(add)
        v.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.scroll.setFixedHeight(260)
        v.addWidget(self.scroll)
        vol = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("volume", c["muted"], 16))
        vol.addWidget(ic)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(int(core.s.get("soundboard_volume", 60)))
        slider.setToolTip("Громкость звуков — у вас")
        slider.valueChanged.connect(self._volume)
        vol.addWidget(slider, 1)
        v.addLayout(vol)
        self._fill()
        core.sounds_changed.connect(self._fill)

    def _fill(self):
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setSpacing(6)
        sounds = self.core.soundboard()
        row = 0
        builtin = [x for x in sounds if x[3] is None]
        custom = [x for x in sounds if x[3] is not None]
        for caption, items in (("ВСТРОЕННЫЕ", builtin), ("ЗВУКИ КОМНАТЫ", custom)):
            if not items and caption != "ЗВУКИ КОМНАТЫ":
                continue
            grid.addWidget(label(caption, "caption"), row, 0, 1, 2)
            row += 1
            if not items:
                hint = label("Пока нет. Нажмите «+» сверху, чтобы добавить свой звук из файла.", "hint",
                             wrap=True)
                grid.addWidget(hint, row, 0, 1, 2)
                row += 1
            for i, (key, name, emoji, sound) in enumerate(items):
                b = SoundButton(emoji, name, custom=sound is not None)
                b.clicked.connect(lambda _=False, k=key: self._play(k))
                b.mousePressEvent = self._press_handler(b, key, sound)
                grid.addWidget(b, row + i // 2, i % 2)
            row += (len(items) + 1) // 2
        grid.setRowStretch(row, 1)
        self.scroll.setWidget(body)

    def _press_handler(self, b, key, sound):
        original = QToolButton.mousePressEvent

        def press(e):
            if e.button() == Qt.RightButton:
                self.core.preview_sound(key)
            elif e.button() == Qt.MiddleButton and sound:
                from .dialogs import confirm
                core, win = self.core, self.win
                self.close()                         # a dialog would close the popup anyway
                if confirm(win, "Убрать звук?", f"«{sound['name']}» пропадёт из звуковой "
                           f"панели у всех в комнате.", ok="Убрать"):
                    core.remove_sound(sound["id"])
            else:
                original(b, e)
        return press

    def _play(self, key):
        if not self.core.my_voice:
            self.core.toast.emit("Звуки слышно только в голосовом канале", "error")
            return
        self.core.play_sound(key)

    def _volume(self, x):
        self.core.s["soundboard_volume"] = x
        self.core.s.save()

    def _add(self):
        core, win = self.core, self.win
        self.close()                                 # dialogs would close the popup anyway
        path, _ = QFileDialog.getOpenFileName(win, "Звук для звуковой панели", "", AUDIO_FILES)
        if not path:
            return
        from .dialogs import ask_text
        name = ask_text(win, "Новый звук", "Название", Path(path).stem[:32], ok="Добавить")
        if name is None:
            return
        if core.add_sound(path, name):
            core.toast.emit(f"Звук «{name or 'Звук'}» добавлен — он есть у всех в комнате", "info")

    def popup_above(self, widget):
        self.adjustSize()
        pos = widget.mapToGlobal(QPoint(widget.width() // 2 - self.width() // 2, -self.height() - 10))
        screen = widget.screen().availableGeometry()
        pos.setX(max(screen.left() + 4, min(pos.x(), screen.right() - self.width() - 4)))
        pos.setY(max(screen.top() + 4, pos.y()))
        self.move(pos)
        self.show()


def open_soundboard(anchor, core):
    board = Soundboard(anchor.window(), core)
    board.popup_above(anchor)
    return board

