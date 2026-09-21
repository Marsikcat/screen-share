#!/usr/bin/env python3
"""
Build the Windows app and its installer.

    tools\\build.bat            (creates .build\\ with everything needed, then runs this)
    python tools\\build.py      (inside an environment with requirements.txt + pyinstaller)

Result:
    dist\\MoyDiscord\\                 MoyDiscord.exe, ScreenShare.exe, ffmpeg\\, _internal\\
    dist\\MoyDiscord-Setup-X.Y.Z.exe   installer (Inno Setup 6)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP_DIR = DIST / "MoyDiscord"


def version():
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def write_version_info(ver):
    """Windows 'Details' tab of the exe: product name, version, copyright."""
    nums = ([int(x) for x in ver.split(".")] + [0, 0, 0, 0])[:4]
    tup = ", ".join(map(str, nums))
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'Marsikcat'),
      StringStruct('FileDescription', 'МойДискорд — чат, голос и демонстрация экрана без серверов'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'MoyDiscord'),
      StringStruct('LegalCopyright', 'github.com/Marsikcat/screen-share'),
      StringStruct('OriginalFilename', 'MoyDiscord.exe'),
      StringStruct('ProductName', 'МойДискорд'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""
    BUILD.mkdir(exist_ok=True)
    (BUILD / "version_info.txt").write_text(text, encoding="utf-8")


def pyinstaller():
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "MoyDiscord.spec"),
                    "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller")],
                   check=True, cwd=ROOT)


def bundle_ffmpeg():
    """Screen share needs ffmpeg.exe / ffplay.exe next to the app."""
    src = ROOT / "ffmpeg"
    if not (src / "ffmpeg.exe").exists() or not (src / "ffplay.exe").exists():
        print("FFmpeg не найден в ffmpeg\\ — скачиваю…")
        sys.path.insert(0, str(ROOT))
        import screen_share
        if not screen_share.download_ffmpeg():
            sys.exit("Не удалось скачать FFmpeg")
    dest = APP_DIR / "ffmpeg"
    dest.mkdir(exist_ok=True)
    for name in ("ffmpeg.exe", "ffplay.exe"):
        shutil.copy2(src / name, dest / name)


def find_iscc():
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles")):
        if base:
            p = Path(base) / ("Programs" if base == os.environ.get("LOCALAPPDATA") else "") / "Inno Setup 6" / "ISCC.exe"
            if p.exists():
                return p
    return shutil.which("ISCC")


def installer(ver):
    iscc = find_iscc()
    if not iscc:
        sys.exit("Не найден Inno Setup 6. Установите: winget install JRSoftware.InnoSetup")
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={ver}", str(ROOT / "packaging" / "installer.iss")],
                   check=True, cwd=ROOT / "packaging")
    return DIST / f"MoyDiscord-Setup-{ver}.exe"


def size_mb(path):
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main():
    ver = version()
    print(f"МойДискорд {ver}: сборка exe…")
    write_version_info(ver)
    pyinstaller()
    bundle_ffmpeg()
    print("Установщик…")
    setup = installer(ver)
    print(f"\nГотово:\n  {APP_DIR}  ({size_mb(APP_DIR):.0f} МБ)\n  {setup}  ({size_mb(setup):.0f} МБ)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
