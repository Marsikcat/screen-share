"""Render the app icon (same drawing as the tray icon) into assets/icon.ico and icon.png."""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ACCENT = "#5865f2"


def render(size):
    from marincall.ui import icons
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    m = size * 8 / 256
    p.drawRoundedRect(QRectF(m, m, size - 2 * m, size - 2 * m), size / 4, size / 4)
    glyph = int(size * 160 / 256)
    p.drawPixmap(int(size * 48 / 256), int(size * 52 / 256),
                 icons.pixmap("message", "#ffffff", glyph, 2.2).scaled(glyph, glyph, Qt.KeepAspectRatio,
                                                                     Qt.SmoothTransformation))
    p.end()
    buf = QBuffer()
    buf.open(QBuffer.ReadWrite)
    pm.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data())))


def main():
    QApplication.instance() or QApplication([])
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    big = render(256)
    big.save(out / "icon.png")
    # every size drawn natively, so the small ones stay crisp
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [render(s) for s in sizes]
    frames[-1].save(out / "icon.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("assets/icon.ico, assets/icon.png")


if __name__ == "__main__":
    main()
