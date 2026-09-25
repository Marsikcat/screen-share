#!/usr/bin/env python3
"""
Publish a GitHub release of MarinCall.

Usually GitHub Actions builds it: push a tag and the workflow does the rest.

    git tag v3.3.0 && git push origin v3.3.0

It builds the installer, and publishes the release signed if the repository has the
MARINCALL_SIGNING_KEY secret; without it (the safer way: the key never leaves your PC)
the release waits as a draft until you sign it here:

    python tools\\release.py --sign-draft v3.3.0

By hand, from this PC (after tools\\build.bat):

    python tools\\release.py                release VERSION from HEAD (must be pushed)
    python tools\\release.py --ref 826e3ce  release an older commit (uses that commit's VERSION)
    python tools\\release.py --draft        create a draft to review on GitHub first
    python tools\\release.py --dry-run      pack, print the notes, publish nothing

Release notes come from the "## X.Y.Z" section of CHANGELOG.md. Assets:

    MarinCall-Setup-X.Y.Z.exe   the installer people download, and what the app
                                 updates itself with
    MarinCall-X.Y.Z.zip         source archive, for running from Python
    *.sig                        signatures — the app installs only signed updates

Needs git and the GitHub CLI logged in (gh auth login; in CI the job's token).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "Marsikcat/screen-share"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sign  # noqa: E402


def git(*args):
    # safe.directory: the folder is often copied from another PC / Windows user
    cmd = ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args]
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def gh(*args, capture=False):
    r = subprocess.run(["gh", *args, "--repo", REPO], check=True, text=True, encoding="utf-8",
                       capture_output=capture)
    return r.stdout.strip() if capture else ""


def notes_for(version):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def sign_draft(tag):
    """A release the CI built as a draft: download, sign here, upload the signatures, publish."""
    tmp = Path(tempfile.mkdtemp(prefix="marincall-release-"))
    try:
        gh("release", "download", tag, "--dir", str(tmp), "--pattern", "MarinCall-*")
        files = [p for p in tmp.iterdir() if not p.name.endswith(".sig")]
        if not files:
            sys.exit(f"В черновике {tag} нет файлов")
        for p in files:
            print(f"{p.name}: {p.stat().st_size / 1e6:.1f} МБ")
        sigs = sign.sign_files(files)
        gh("release", "upload", tag, *map(str, sigs), "--clobber")
        gh("release", "edit", tag, "--draft=false", "--latest")
        print(f"\n{tag} опубликован и подписан.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Выпустить релиз MarinCall на GitHub")
    ap.add_argument("--ref", default="HEAD", help="коммит для релиза (по умолчанию HEAD)")
    ap.add_argument("--draft", action="store_true", help="создать черновик")
    ap.add_argument("--dry-run", action="store_true", help="только собрать архив и показать описание")
    ap.add_argument("--no-installer", action="store_true", help="выпустить без установщика")
    ap.add_argument("--ci", action="store_true", help="запуск из GitHub Actions (по тегу)")
    ap.add_argument("--sign-draft", metavar="TAG", help="подписать и опубликовать черновик из CI")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # notes have arrows; Windows console is cp1251

    if args.sign_draft:
        sign_draft(args.sign_draft)
        return

    sha = git("rev-parse", args.ref)
    version = git("show", f"{sha}:VERSION")
    tag = f"v{version}"
    notes = notes_for(version)
    if not notes:
        sys.exit(f"В CHANGELOG.md нет раздела «## {version}» — допишите, что изменилось.")
    if args.ci:
        ref = os.environ.get("GITHUB_REF_NAME", "")
        if ref != tag:
            sys.exit(f"Тег {ref} не совпадает с VERSION ({version}) — поправьте VERSION или тег.")
    elif not args.dry_run:
        if args.ref == "HEAD" and git("status", "--porcelain", "--untracked-files=no"):
            sys.exit("Есть незакоммиченные изменения — сначала сделайте коммит.")
        if not git("branch", "-r", "--contains", sha):
            sys.exit("Коммит ещё не на GitHub — сначала git push.")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"MarinCall-{version}.zip"
    git("archive", "--format=zip", "--prefix=MarinCall/", "-o", str(zip_path), sha)
    assets = [zip_path]
    installer = dist / f"MarinCall-Setup-{version}.exe"
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

    key = sign.load_key()
    draft = args.draft
    if key:
        assets += sign.sign_files(assets, key)
    elif args.ci:
        draft = True                         # signed later, on the PC that holds the key
        print(f"Ключа подписи в CI нет — релиз создан черновиком. Подпишите и опубликуйте:\n"
              f"    python tools\\release.py --sign-draft {tag}")
    else:
        sys.exit(f"Нет ключа подписи ({sign.KEY_FILE}) — без подписи приложение не установит обновление.")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(notes)
    cmd = ["release", "create", tag, *map(str, assets), "--target", sha,
           "--title", f"MarinCall {version}", "--notes-file", f.name]
    if draft:
        cmd.append("--draft")
    gh(*cmd)
    Path(f.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
