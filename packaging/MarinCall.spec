# PyInstaller build of MarinCall: one folder with MarinCall.exe and ScreenShare.exe
# sharing the same libraries. No ffmpeg.exe: screen share encodes through PyAV in a worker
# process (MarinCall.exe --stream-worker); classic ScreenShare downloads FFmpeg when needed. Run through tools/build.py, which also writes the version
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
# iroh and pywebrtc_audio load native libraries themselves (ctypes / pybind11); PyAV,
# sounddevice and PyNaCl (libsodium) ship theirs next to the package. collect_all picks all of that up.
for pkg in ("iroh", "pywebrtc_audio", "sounddevice", "av", "nacl"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# PyNaCl's libsodium binding is a cffi module: it needs _cffi_backend, which nothing imports
# visibly (sounddevice happens to bring it today — don't rely on that)
hiddenimports += ["_cffi_backend"]

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

# never used, but pulled in by the packages' hooks:
#   software OpenGL and Pillow's AVIF codec (~28 MB); Qt's TLS backends with their own copy of
#   OpenSSL (Qt makes no TLS connections — Python has its own); Qt's translations (our UI is
#   Russian text, Qt's own strings are not shown); platform and image plugins we don't need
DROP = ("opengl32sw.dll", "_avif.", "libcrypto-3-x64", "libssl-3-x64", "qopensslbackend",
        "qcertonlybackend", "qschannelbackend", "qnetworklistmanager", "qdirect2d", "qminimal",
        "qicns", "qtga", "qtiff", "qwbmp")
for a in (app, classic):
    a.binaries = [b for b in a.binaries if not any(d in b[0].lower() for d in DROP)]
    a.datas = [d for d in a.datas if "translations" not in d[0].lower().replace("\\", "/").split("/")]

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="MarinCall",
              console=False, icon=ICON, version=VERSION_INFO, upx=False)
classic_exe = EXE(PYZ(classic.pure), classic.scripts, [], exclude_binaries=True, name="ScreenShare",
                  console=False, icon=ICON, version=VERSION_INFO, upx=False)

COLLECT(app_exe, app.binaries, app.datas,
        classic_exe, classic.binaries, classic.datas,
        name="MarinCall", upx=False)
