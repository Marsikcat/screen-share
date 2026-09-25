"""Small reusable widgets painted to match the theme."""

from PySide6.QtCore import (Property, QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QLayout, QPushButton, QSizePolicy, QToolButton, QWidget)

from . import icons
from .theme import T


def label(text="", role=None, wrap=False, selectable=False):
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    lb.setWordWrap(wrap)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def button(text, kind=None, on_click=None):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if kind:
        b.setProperty("kind", kind)
    if on_click:
        b.clicked.connect(on_click)
    return b


def hbox(*widgets, spacing=8, margins=(0, 0, 0, 0)):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    return w


def divider():
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {T.c['divider']};")
    return f


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


class Avatar(QWidget):
    """Coloured circle with an initial, optional speaking ring and presence dot."""

    def __init__(self, name="", color="#5865f2", size=32, parent=None):
        super().__init__(parent)
        self._name, self._color, self._size = name, color, size
        self.speaking = False
        self.status = None      # None | "online" | "offline"
        pad = 3 if size < 60 else 5
        self.setFixedSize(size + pad * 2, size + pad * 2)
        self._pad = pad
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set(self, name=None, color=None):
        if name is not None:
            self._name = name
        if color is not None:
            self._color = color
        self.update()

    def set_speaking(self, on):
        if on != self.speaking:
            self.speaking = on
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        s, pad = self._size, self._pad
        rect = QRectF(pad, pad, s, s)
        # the presence dot sits in a transparent notch (like Discord), so it looks right on
        # any background: hover, selection, every theme
        dot = hole = None
        if self.status:
            d = max(8.0, round(s * 0.3))
            gap = max(2.0, round(s * 0.08))
            c = min(pad + s * 0.85, self.width() - d / 2 - gap)
            dot = QRectF(c - d / 2, c - d / 2, d, d)
            hole = QPainterPath()
            hole.addEllipse(dot.adjusted(-gap, -gap, gap, gap))
        if self.speaking:
            w = max(2.0, s / 16)
            r = s / 2 + pad - w / 2 - 0.5
            ring = QPainterPath()
            ring.addEllipse(rect.center(), r + w / 2, r + w / 2)
            inner = QPainterPath()
            inner.addEllipse(rect.center(), r - w / 2, r - w / 2)
            ring = ring.subtracted(inner)
            p.setBrush(QColor(T.c["green"]))
            p.drawPath(ring.subtracted(hole) if hole else ring)
        body = QPainterPath()
        body.addEllipse(rect)
        if hole:
            body = body.subtracted(hole)
        p.setBrush(QColor(self._color))
        p.drawPath(body)
        f = QFont(p.font())
        f.setPixelSize(max(9, int(s * 0.42)))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, Qt.AlignCenter, (self._name[:1] or "?").upper())
        if dot:
            p.setPen(Qt.NoPen)
            mark = QPainterPath()
            mark.addEllipse(dot)
            if self.status == "offline":        # hollow grey ring
                k = dot.width() * 0.27
                inner = QPainterPath()
                inner.addEllipse(dot.adjusted(k, k, -k, -k))
                mark = mark.subtracted(inner)
            p.setBrush(QColor(T.c["green"] if self.status == "online" else T.c["muted"]))
            p.drawPath(mark)


class IconButton(QToolButton):
    """Flat icon button that recolours on hover / when checked ('danger' shows red)."""

    def __init__(self, name, tip="", size=20, box=32, checkable=False, danger_when_checked=False,
                 parent=None):
        super().__init__(parent)
        self._name, self._size = name, size
        self.danger_when_checked = danger_when_checked
        self.hover_bg = None
        self.setCheckable(checkable)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(box, box)
        self.setIconSize(QSize(size, size))
        self._hover = False
        self.toggled.connect(lambda _: self._refresh())
        self._refresh()

    def set_icon(self, name):
        self._name = name
        self._refresh()

    def _refresh(self):
        if self.isChecked() and self.danger_when_checked:
            color = T.c["red"]
        elif self._hover:
            color = T.c["header"]
        else:
            color = T.c["icon"]
        self.setIcon(icons.icon(self._name, color, self._size))
        bg = self.hover_bg or T.c["active"]
        self.setStyleSheet(
            f"QToolButton {{ border: none; border-radius: 4px; background: "
            f"{bg if self._hover else 'transparent'}; }}")

    def enterEvent(self, e):
        self._hover = True
        self._refresh()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._refresh()
        super().leaveEvent(e)


class Switch(QAbstractButton):
    """iOS/Discord-style toggle."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self, duration=140,
                                        easingCurve=QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self):
        return self._pos

    def set_knob(self, v):
        self._pos = v
        self.update()

    knob = Property(float, get_knob, set_knob)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor(T.c["muted"]), QColor(T.c["green"])
        t = self._pos
        bg = QColor(round(off.red() + (on.red() - off.red()) * t),
                    round(off.green() + (on.green() - off.green()) * t),
                    round(off.blue() + (on.blue() - off.blue()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, 42, 24), 12, 12)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(3 + t * 18, 3, 18, 18))

    def sizeHint(self):
        return QSize(42, 24)


class FlowLayout(QLayout):
    """Wraps children onto new lines (reaction chips, emoji grid)."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _layout(self, rect, dry):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + self._sp, 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class Toast(QLabel):
    """Transient message at the bottom of the window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.hide()
        self._fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self, duration=250)
        self._timer = QTimer(self, singleShot=True, timeout=self._fade)

    def show_text(self, text, kind="info"):
        # always a dark pill with white text (the light theme's "float" is white)
        bg = T.c["red"] if kind == "error" else ("#313338" if T.light else T.c["float"])
        self.setStyleSheet(f"background: {bg}; color: white; border-radius: 8px; padding: 10px 16px;"
                           f"font-weight: 600;")
        self.setText(text)
        self.ensurePolished()               # the bold style-sheet font, for the measurement below
        w = min(520, self.parent().width() - 40, self.fontMetrics().horizontalAdvance(text) + 48)
        self.setFixedWidth(w)
        self.adjustSize()
        self.move((self.parent().width() - w) // 2, self.parent().height() - self.height() - 90)
        self.raise_()
        self.show()
        self._anim.stop()
        self._fx.setOpacity(1.0)
        self._timer.start(4500 if kind == "error" else 3000)

    def _fade(self):
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)
        self._anim.start()


class LevelMeter(QWidget):
    """Mic level bar with the voice-activity threshold marker (dBFS −90…0)."""

    threshold_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = -90.0
        self.threshold = -50.0
        self.editable = True
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

    @staticmethod
    def _frac(db):
        return max(0.0, min(1.0, (db + 90) / 90))

    def set_values(self, level, threshold):
        self.level, self.threshold = level, threshold
        self.update()

    def mousePressEvent(self, e):
        self._drag(e)

    def mouseMoveEvent(self, e):
        self._drag(e)

    def _drag(self, e):
        if self.editable:
            db = e.position().x() / max(1, self.width()) * 90 - 90
            self.threshold = round(max(-80.0, min(-10.0, db)))
            self.threshold_changed.emit(self.threshold)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bar = QRectF(0, h / 2 - 5, w, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.c["active"]))
        p.drawRoundedRect(bar, 5, 5)
        lv = self._frac(self.level) * w
        th = self._frac(self.threshold) * w
        path = QPainterPath()
        path.addRoundedRect(bar, 5, 5)
        p.setClipPath(path)
        p.setBrush(QColor(T.c["yellow"]))
        p.drawRect(QRectF(0, bar.y(), min(lv, th), 10))
        if lv > th:
            p.setBrush(QColor(T.c["green"]))
            p.drawRect(QRectF(th, bar.y(), lv - th, 10))
        p.setClipping(False)
        p.setBrush(QColor(T.c["header"] if T.light else "white"))   # visible on a light bar too
        p.drawRoundedRect(QRectF(th - 3, 2, 6, h - 4), 3, 3)
