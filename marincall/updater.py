"""
Updates from GitHub Releases.

Installed build: download MarinCall-Setup-X.exe from the release and run it silently —
it replaces the app and starts it again. From source: download the source zip and copy
it over the project folder.

Either way the download must come with a valid `<file>.sig` from the release key
(signing.py): an unsigned or tampered file is never installed.
"""

import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import signing
from .config import FROZEN, RELEASE_KEY, REPO, ROOT, VERSION

# never touched by an update
SKIP = {".git", ".claude", "venv", "python", "ffmpeg", "__pycache__"}
UA = {"User-Agent": "MarinCall-updater"}
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def asset_name(version):
    return f"MarinCall-Setup-{version}.exe" if FROZEN else f"MarinCall-{version}.zip"


def _wanted(name):
    name = name.lower()
    return name.endswith(".exe") and "setup" in name if FROZEN else name.endswith(".zip")


_installer = None      # downloaded setup, run by restart()


def _get(url, timeout=15, accept=None):
    headers = dict(UA, Accept=accept) if accept else UA
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def _result(tag, **extra):
    latest = tag.lstrip("vV")
    return {"current": VERSION, "latest": latest, "tag": tag,
            "available": parse_version(latest) > parse_version(VERSION),
            "title": f"MarinCall {latest}", "notes": "", "date": "", "size": 0,
            "url": f"{RELEASES_PAGE}/tag/{tag}", "asset": asset_name(latest),
            "download": f"{RELEASES_PAGE}/download/{tag}/{asset_name(latest)}", **extra}


def check():
    """Latest release -> {current, latest, available, title, notes (Markdown), date, url, download}."""
    try:
        rel = json.loads(_get(API_LATEST, accept="application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("в репозитории пока нет релизов") from None
        return _check_without_api()   # 403/429: API rate limit (60 requests/hour per IP)
    assets = rel.get("assets") or []
    asset = next((a for a in assets if _wanted(a["name"])), None)
    extra = {"notes": rel.get("body") or "", "date": (rel.get("published_at") or "")[:10],
             "url": rel["html_url"]}
    if rel.get("name"):
        extra["title"] = rel["name"]
    if asset:
        extra.update(download=asset["browser_download_url"], size=asset["size"], asset=asset["name"])
        sig = next((a for a in assets if a["name"] == asset["name"] + ".sig"), None)
        extra["signature"] = sig["browser_download_url"] if sig else ""
    else:
        extra.update(download="", signature="")
    return _result(rel["tag_name"], **extra)


def _check_without_api():
    """github.com/…/releases/latest redirects to …/releases/tag/<tag> and has no rate limit."""
    req = urllib.request.Request(f"{RELEASES_PAGE}/latest", headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as r:
        final = r.geturl()
    if "/releases/tag/" not in final:
        raise RuntimeError("в репозитории пока нет релизов")
    return _result(final.rstrip("/").rsplit("/", 1)[-1])


def _find_root(folder):
    """The extracted folder that holds app.py (archives wrap everything in one top dir)."""
    for cand in [folder, *[p for p in folder.iterdir() if p.is_dir()]]:
        if (cand / "app.py").exists() and ((cand / "marincall").is_dir() or (cand / "moydiscord").is_dir()):
            return cand
    raise RuntimeError("архив не похож на MarinCall — обновление отменено")


def _verified(info):
    """The release file, downloaded and checked against the release key."""
    name = info.get("asset") or asset_name(info["latest"])
    if name != asset_name(info["latest"]):       # the signed name carries the version
        raise RuntimeError("в релизе нет подходящего файла — скачайте обновление со страницы релизов")
    sig_url = info.get("signature", info["download"] + ".sig")
    if not info.get("download") or not sig_url:
        raise RuntimeError("релиз не подписан — установите обновление вручную со страницы релизов")
    try:
        sig = _get(sig_url, timeout=30).decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        raise RuntimeError("релиз не подписан — установите обновление вручную со страницы релизов") from None
    data = _get(info["download"], timeout=300)
    if info.get("size") and len(data) != info["size"]:
        raise RuntimeError("файл скачался не полностью — попробуйте ещё раз")
    try:
        signing.verify(RELEASE_KEY, name, data, sig)
    except ValueError as e:
        raise RuntimeError(f"подпись обновления не сходится ({e}) — установка отменена") from None
    return data


def apply(info=None):
    """Download the release archive and copy it over the app folder. Returns the new version."""
    global _installer
    info = info or check()
    data = _verified(info)
    if FROZEN:
        if not info["download"].lower().endswith(".exe"):
            raise RuntimeError("в релизе нет установщика — скачайте его со страницы релизов")
        path = Path(tempfile.gettempdir()) / asset_name(info["latest"])
        path.write_bytes(data)
        _installer = path
        return info["latest"]
    tmp = Path(tempfile.mkdtemp(prefix="marincall-update-"))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        top = _find_root(tmp)
        for item in top.iterdir():
            if item.name in SKIP:
                continue
            dest = ROOT / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        return (top / "VERSION").read_text(encoding="utf-8").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restart():
    if FROZEN and _installer:
        # the installer closes what's left of us, replaces the files and starts the new version
        subprocess.Popen([str(_installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS"], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return
    if FROZEN:
        subprocess.Popen([sys.executable, "--restarted"], cwd=str(ROOT),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(ROOT / "app.py"), "--restarted"], cwd=str(ROOT),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
