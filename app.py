#!/usr/bin/env python3
"""
МойДискорд — text & voice channels and screen share over LAN / Radmin VPN, no server.

    python app.py              open the app
    python app.py --autostart  launched by Windows at logon (may start in the tray)
"""

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REQUIRED = ("PySide6", "sounddevice", "av", "numpy", "iroh")


def _missing():
    import importlib.util
    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def _message(text, icon):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "МойДискорд", icon)
    except Exception:
        print(text)


def _fatal(text):
    _message(text, 0x10)


def _install_requirements():
    """After a self-update the new code may need packages the old version didn't have."""
    import subprocess
    _message("МойДискорд обновился — доустанавливаю новые компоненты.\n"
             "Это займёт минуту, потом окно откроется само.", 0x40)
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _setup_logging():
    from moydiscord.config import DATA
    DATA.mkdir(parents=True, exist_ok=True)
    log = open(DATA / "app.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or sys.stderr is None:   # pythonw has no console
        sys.stdout = sys.stderr = log

    def hook(exc_type, exc, tb):
        log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = hook


def main():
    missing = _missing()
    if missing and "PySide6" not in missing:
        _install_requirements()
        missing = _missing()
    if missing:
        _fatal("Не установлены модули: " + ", ".join(missing) + "\n\nЗапустите setup.bat — он всё поставит.")
        return 1
    _setup_logging()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    from moydiscord.config import APP_NAME, INSTANCE, Settings

    args = sys.argv[1:]
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # one instance per Windows user: a second launch just brings the window up
    key = f"MoyDiscord-{os.environ.get('USERNAME', 'user')}-{INSTANCE}"
    wait_until = time.time() + (15 if "--restarted" in args else 0)
    while True:
        sock = QLocalSocket()
        sock.connectToServer(key)
        if not sock.waitForConnected(300):
            break
        if time.time() < wait_until:      # the old process is still shutting down
            sock.disconnectFromServer()
            time.sleep(0.3)
            continue
        sock.write(b"show")
        sock.waitForBytesWritten(1000)
        return 0
    QLocalServer.removeServer(key)
    server = QLocalServer()
    server.listen(key)

    settings = Settings()
    from moydiscord.ui import dialogs, theme
    theme.apply(app, settings)
    if not settings["name"]:
        result = dialogs.onboarding(None, settings)
        if not result:
            return 0
        settings["name"], settings["color"] = result
        settings.save()

    from moydiscord.core import Core
    from moydiscord.ui.mainwindow import MainWindow

    core = Core(settings)
    hidden = "--autostart" in args and settings["start_minimized"]
    win = MainWindow(core, app, start_hidden=hidden)

    def on_second_instance():
        conn = server.nextPendingConnection()
        if conn:
            conn.readyRead.connect(win.show_window)
            conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(on_second_instance)

    core.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
