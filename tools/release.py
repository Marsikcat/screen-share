#!/usr/bin/env python3
"""
Publish a GitHub release of МойДискорд.

    python tools\\release.py                release VERSION from HEAD (must be pushed)
    python tools\\release.py --ref 826e3ce  release an older commit (uses that commit's VERSION)
    python tools\\release.py --draft        create a draft to review on GitHub first
    python tools\\release.py --dry-run      pack, print the notes, publish nothing

Release notes come from the "## X.Y.Z" section of CHANGELOG.md. Two assets go up:

    MoyDiscord-Setup-X.Y.Z.exe   the installer people download, and what the app
                                 updates itself with — build it first: tools\\build.bat
    MoyDiscord-X.Y.Z.zip         source archive, for running from Python

Needs git and the GitHub CLI logged in (gh auth login).
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "Marsikcat/screen-share"


def git(*args):
    # safe.directory: the folder is often copied from another PC / Windows user
    cmd = ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args]
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def notes_for(version):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def main():
    ap = argparse.ArgumentParser(description="Выпустить релиз МойДискорд на GitHub")
    ap.add_argument("--ref", default="HEAD", help="коммит для релиза (по умолчанию HEAD)")
    ap.add_argument("--draft", action="store_true", help="создать черновик")
    ap.add_argument("--dry-run", action="store_true", help="только собрать архив и показать описание")
    ap.add_argument("--no-installer", action="store_true", help="выпустить без установщика")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # notes have arrows; Windows console is cp1251

    sha = git("rev-parse", args.ref)
    version = git("show", f"{sha}:VERSION")
    tag = f"v{version}"
    notes = notes_for(version)
    if not notes:
        sys.exit(f"В CHANGELOG.md нет раздела «## {version}» — допишите, что изменилось.")
    if not args.dry_run:
        if args.ref == "HEAD" and git("status", "--porcelain", "--untracked-files=no"):
            sys.exit("Есть незакоммиченные изменения — сначала сделайте коммит.")
        if not git("branch", "-r", "--contains", sha):
            sys.exit("Коммит ещё не на GitHub — сначала git push.")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"MoyDiscord-{version}.zip"
    git("archive", "--format=zip", "--prefix=MoyDiscord/", "-o", str(zip_path), sha)
    assets = [zip_path]
    installer = dist / f"MoyDiscord-Setup-{version}.exe"
    if installer.exists():
        assets.insert(0, installer)          # what people download and the app updates with
    elif not args.no_installer:
        sys.exit(f"Нет {installer.name} — соберите установщик: tools\\build.bat "
                 f"(или --no-installer, чтобы выпустить только исходники)")
    for a in assets:
        print(f"{tag} ({sha[:7]}): {a.name}, {a.stat().st_size / 1e6:.1f} МБ")
    print(f"\n{notes}\n")
    if args.dry_run:
        return

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(notes)
    cmd = ["gh", "release", "create", tag, *map(str, assets), "--repo", REPO, "--target", sha,
           "--title", f"МойДискорд {version}", "--notes-file", f.name]
    if args.draft:
        cmd.append("--draft")
    subprocess.run(cmd, check=True)
    Path(f.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
