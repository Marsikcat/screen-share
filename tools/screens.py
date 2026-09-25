#!/usr/bin/env python3
"""
Screenshots of every screen and dialog, for checking the design.

    python tools\\screens.py            dark and light
    python tools\\screens.py light      one theme

Runs the app off-screen (no windows appear) with three extra participants in separate
processes: Борис (muted, with a profile picture, replies and reacts), Вика (streams a
test pattern with a tone) and Дима (writes and goes offline). Everything happens in a
throwaway profile folder and a closed room with a random key, so nothing can reach your
real history or your friends. Pictures land in build\\screens\\.
"""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "screens"
PEERS = {"B": ("Борис", "#e67e22", 8), "C": ("Вика", "#1abc9c", 9), "D": ("Дима", "#9b59b6", 10)}


def _env(instance):
    os.environ["MOYDISCORD_INSTANCE"] = str(instance)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    sys.path.insert(0, str(ROOT))


def _settings(name, color, **extra):
    from marincall.config import Settings
    s = Settings()
    s.update(name=name, color=color, room="Скриншоты", room_secret=os.environ["MARINCALL_SCREENS_ROOM"],
             sounds=False, check_updates=False, network_mode="lan", **extra)
    s.save()
    return s


def _run(app, sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return False


def _test_pattern(sender):
    """Вика streams a test pattern and a tone instead of the real screen."""
    def build(source, quality, encoder, audio):
        from marincall.config import FFMPEG_BIN, STREAM_RELAY_PORT
        return [str(FFMPEG_BIN), "-hide_banner", "-loglevel", "error",
                "-re", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30",
                "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "superfast",
                "-tune", "zerolatency", "-b:v", "2500k", "-g", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                "-f", "mpegts", f"udp://127.0.0.1:{STREAM_RELAY_PORT}?pkt_size=1316"], False
    sender.build_command = build


def _avatar_picture():
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
    img = QImage(256, 256, QImage.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, 256, 256)
    g.setColorAt(0, QColor("#ff8a00"))
    g.setColorAt(1, QColor("#e52e71"))
    p.fillRect(img.rect(), g)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 200))
    p.drawEllipse(QRectF(78, 50, 100, 100))
    p.drawEllipse(QRectF(38, 160, 180, 150))
    p.end()
    path = Path(tempfile.gettempdir()) / "marincall-screens-avatar.png"
    img.save(str(path))
    return path


def peer(role, secs):
    name, color, instance = PEERS[role]
    _env(instance)
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication([])
    s = _settings(name, color, muted=role == "B", stream_volume=0, stream_audio="")
    from marincall.core import Core
    c = Core(s)
    if role == "C":
        _test_pattern(c.sender)
    c.start()
    _run(app, 25, lambda: len(c.mesh.peers()) > 0)
    _run(app, 2)
    general = "d:text:general"
    if role == "D":
        c.send_message(general, "Я сегодня рано, до завтра 👋")
        _run(app, 2)
        c.shutdown()
        return
    if role == "B":
        c.set_avatar(str(_avatar_picture()))
        first = next((m for m in c.store.visible_messages(general) if "Жирный" in c.store.text_of(m)), None)
        c.send_message(general, "Выглядит отлично! Особенно подсветка кода.", reply=first["id"] if first else None)
        if first:
            c.toggle_reaction(first["id"], "🔥")
        c.send_message("d:text:media", "Кину сюда скрины с вчерашней катки")
        c.send_message("d:text:media", "@Алиса глянь, тебе понравится")
        c.join_voice("d:voice:general")
    else:
        c.send_message(general, "", files=[str(ROOT / "assets" / "icon.png")])
        c.join_voice("d:voice:general")
        _run(app, 1)
        c.start_stream({"kind": "monitor", "label": "Тестовая таблица", "height": 720, "dxgi": None})
    t0 = time.time()
    stop = Path(os.environ.get("MARINCALL_SCREENS_STOP", "-"))
    while time.time() - t0 < secs and not stop.exists():
        if role == "C":
            c._on_local_speaking(int(time.time()) % 2 == 0)      # blink the speaking ring
        _run(app, 1)
    c.shutdown()


def tour(theme_name):
    _env(7)
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    s = _settings("Алиса", "#ed4245", theme=theme_name, muted=False, deafened=False,
                  last_channel="d:text:general", stream_volume=0)
    from marincall.ui import theme
    theme.apply(app, s)
    from marincall.core import Core
    from marincall.ui import dialogs
    from marincall.ui.mainwindow import MainWindow
    from marincall.ui.richtext import EmojiPicker
    from marincall.ui.voiceview import SourcePicker

    core = Core(s)
    general = "d:text:general"
    core.send_message(general, "Всем привет! Это **MarinCall** — наш *собственный* чат. "
                      "Ссылка на проект: https://github.com/Marsikcat/screen-share")
    core.send_message(general, "**Жирный**, *курсив*, ~~зачёркнутый~~ и `код`:\n"
                      "```\ndef hello():\n    print('привет, мир')\n```\nи текст после блока")
    core.send_message(general, "@Борис ты сегодня в голосовой? И длинное сообщение, чтобы посмотреть "
                      "перенос строк: " + "бла-бла " * 30)
    core.send_message(general, "🔥🎮")
    core.send_message(general, "", files=[str(ROOT / "CHANGELOG.md")])
    win = MainWindow(core, app)
    win.resize(1320, 820)
    win.show()
    core.start()
    OUT.mkdir(parents=True, exist_ok=True)

    def shot(widget, name):
        app.processEvents()
        widget.grab().save(str(OUT / f"{theme_name}_{name}.png"))

    def show_dialog(dlg, name):
        dlg.show()
        app.processEvents()
        dlg.adjustSize()
        app.processEvents()
        shot(dlg, name)
        dlg.close()

    def emoji():
        ep = EmojiPicker(win)
        ep.adjustSize()
        ep.grab().save(str(OUT / f"{theme_name}_26_emoji.png"))
        ep.deleteLater()

    def confirm():
        d = dialogs.Dialog(win, "Удалить канал", "Канал «медиа» пропадёт у всех участников комнаты. "
                                                 "Отменить это нельзя.")
        d.buttons("Удалить", "danger")
        return d

    def streamer():
        return next((u for u, st in core.states.items() if st.get("streaming")), None)

    def watch():
        uid = streamer()
        if uid:
            core.watch(uid)

    def fullscreen():
        win.voiceview.open_fullscreen()
        full = win.voiceview.full
        if full:
            full.resize(1280, 720)

    def fullscreen_shot():
        full = win.voiceview.full
        if full:
            shot(full, "09_stream_fullscreen")
            full.close()

    steps = [
        (16000, lambda: win.open_channel(general)),
        (1500, lambda: shot(win, "01_chat")),
        (200, lambda: win.open_channel("d:voice:general")),
        (800, lambda: shot(win, "03_voice_not_joined")),
        (200, lambda: core.join_voice("d:voice:general")),
        (2500, lambda: shot(win, "04_voice_joined")),
        (200, lambda: win.open_channel("d:voice:games")),
        (600, lambda: shot(win, "05_voice_empty_channel")),
        (200, lambda: win.open_channel("d:text:media")),
        (900, lambda: shot(win, "06_chat_media")),
        (200, lambda: win.open_channel("d:voice:general")),
        (300, watch),
        (4000, lambda: shot(win, "08_watching_stream")),
        (200, fullscreen),
        (1500, fullscreen_shot),
        (300, core.unwatch),
    ]
    for page in ("profile", "appearance", "voice", "hotkeys", "stream", "notifications", "network",
                 "startup", "updates", "about"):
        steps += [(200, lambda p=page: win.open_settings(p)),
                  (900, lambda p=page: shot(win, f"10_settings_{p}"))]
    steps += [
        (200, win.close_settings),
        (300, lambda: show_dialog(dialogs.InviteDialog(win, core), "20_invite")),
        (300, lambda: show_dialog(dialogs.JoinDialog(win, core), "21_join")),
        (300, lambda: show_dialog(dialogs.QuickSwitcher(win, core), "22_quick_switcher")),
        (300, lambda: show_dialog(dialogs.ShortcutsHelp(win, s), "23_shortcuts")),
        (300, lambda: show_dialog(confirm(), "24_confirm")),
        (300, lambda: show_dialog(SourcePicker(core, win), "25_source_picker")),
        (300, emoji),
        (300, lambda: (win.open_channel(general), win.toast("Профиль сохранён"))),
        (400, lambda: shot(win, "27_toast")),
        (300, win.quit),
    ]

    def run(i=0):
        # one step after another: long single-shot timers are coarse and could fire out of order
        if i < len(steps):
            delay, fn = steps[i]

            def fire():
                try:
                    fn()
                except Exception as e:  # keep going, report
                    print(f"шаг {i}: {e!r}", flush=True)
                run(i + 1)
            QTimer.singleShot(delay, fire)
    run()
    app.exec()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2 and sys.argv[1] == "--peer":
        peer(sys.argv[2], float(sys.argv[3]))
        return
    if len(sys.argv) > 2 and sys.argv[1] == "--tour":
        tour(sys.argv[2])
        return
    themes = [a for a in sys.argv[1:] if a in ("dark", "ash", "onyx", "light")] or ["dark", "light"]
    for theme_name in themes:
        profiles = tempfile.mkdtemp(prefix="marincall-screens-")
        stop = Path(profiles) / "stop"
        env = dict(os.environ, APPDATA=profiles, MARINCALL_SCREENS_ROOM=secrets.token_hex(16),
                   MARINCALL_SCREENS_STOP=str(stop), PYTHONIOENCODING="utf-8")
        me = [sys.executable, str(Path(__file__).resolve())]
        tour_proc = subprocess.Popen(me + ["--tour", theme_name], env=env)
        time.sleep(3)
        peers = [subprocess.Popen(me + ["--peer", r, "95" if r != "D" else "1"], env=env) for r in PEERS]
        tour_proc.wait()
        stop.touch()                 # the peers leave properly (and stop Вика's FFmpeg)
        for p in peers:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        shutil.rmtree(profiles, ignore_errors=True)
        print(f"{theme_name}: {len(list(OUT.glob(theme_name + '_*.png')))} скриншотов в {OUT}")


if __name__ == "__main__":
    main()
