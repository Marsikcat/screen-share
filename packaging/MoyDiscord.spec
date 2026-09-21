# PyInstaller build of МойДискорд: one folder with MoyDiscord.exe and ScreenShare.exe
# sharing the same libraries. Run through tools/build.py, which also writes the version
# resource this spec expects and wraps the result into an installer.
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "assets" / "icon.ico")
VERSION_INFO = str(ROOT / "build" / "version_info.txt")

datas = [(str(ROOT / "VERSION"), ".")]
binaries = []
hiddenimports = []
# iroh and pywebrtc_audio load native libraries themselves (ctypes / pybind11); PyAV and
# sounddevice ship theirs next to the package. collect_all picks all of that up.
for pkg in ("iroh", "pywebrtc_audio", "sounddevice", "av"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Qt modules we never import, to keep the download small
EXCLUDE_QT = ["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtPdf",
              "PySide6.QtPdfWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
              "PySide6.QtDBus", "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtXml",
              "PySide6.QtConcurrent", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools"]

app = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
               hiddenimports=hiddenimports, excludes=["tkinter"] + EXCLUDE_QT, noarchive=False)
classic = Analysis([str(ROOT / "screen_share.py")], pathex=[str(ROOT)],
                   excludes=["PySide6", "iroh", "av", "sounddevice", "pywebrtc_audio"] + EXCLUDE_QT,
                   noarchive=False)

# software OpenGL fallback and Pillow's AVIF codec: never used, ~28 MB
DROP = ("opengl32sw.dll", "_avif.")
for a in (app, classic):
    a.binaries = [b for b in a.binaries if not any(d in b[0].lower() for d in DROP)]

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="MoyDiscord",
              console=False, icon=ICON, version=VERSION_INFO, upx=False)
classic_exe = EXE(PYZ(classic.pure), classic.scripts, [], exclude_binaries=True, name="ScreenShare",
                  console=False, icon=ICON, version=VERSION_INFO, upx=False)

COLLECT(app_exe, app.binaries, app.datas,
        classic_exe, classic.binaries, classic.datas,
        name="MoyDiscord", upx=False)
