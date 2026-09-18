"""Update check and self-update from the GitHub repository."""

import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import BRANCH, REPO, ROOT, VERSION

# never touched by an update
SKIP = {".git", ".claude", "venv", "python", "ffmpeg", "__pycache__"}
UA = {"User-Agent": "MoyDiscord-updater"}


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check():
    """-> {current, latest, available, notes: [str], url}. Raises on network errors."""
    latest = _get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION?t={int(time.time())}")
    latest = latest.decode("utf-8").strip()
    notes = []
    try:
        commits = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&per_page=8"))
        notes = [c["commit"]["message"].split("\n")[0] for c in commits]
    except Exception:
        pass
    return {"current": VERSION, "latest": latest, "notes": notes,
            "available": parse_version(latest) > parse_version(VERSION),
            "url": f"https://github.com/{REPO}"}


def apply():
    """Download the branch archive and copy it over the app folder. Returns the new version."""
    data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=120)
    tmp = Path(tempfile.mkdtemp(prefix="moydiscord-update-"))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        top = next(tmp.iterdir())
        if not (top / "app.py").exists() or not (top / "moydiscord").is_dir():
            raise RuntimeError("архив не похож на МойДискорд — обновление отменено")
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
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(ROOT / "app.py"), "--restarted"], cwd=str(ROOT),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
