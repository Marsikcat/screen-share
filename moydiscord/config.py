"""Paths, ports and persistent user settings."""

import json
import os
import secrets
from pathlib import Path

APP_NAME = "МойДискорд"
ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
REPO = "Marsikcat/screen-share"
BRANCH = "master"

# A second instance on the same PC (for testing) gets its own ports and data.
INSTANCE = int(os.environ.get("MOYDISCORD_INSTANCE", "0") or 0)
_OFF = INSTANCE * 100

# Per Windows user, not in the project folder — the folder gets copied between PCs.
DATA = Path(os.environ.get("APPDATA") or Path.home()) / ("MoyDiscord" + (f"-{INSTANCE}" if INSTANCE else ""))
FILES_DIR = DATA / "files"
SETTINGS_FILE = DATA / "settings.json"

FFMPEG_DIR = ROOT / "ffmpeg"
FFMPEG_BIN = FFMPEG_DIR / "ffmpeg.exe"
FFPLAY_BIN = FFMPEG_DIR / "ffplay.exe"

# Ports. 8888/8889 are the ones ScreenShare always used.
STREAM_PORT = 8888 + _OFF          # UDP  MPEG-TS screen share, viewer side
DISCOVERY_PORT = 8890              # UDP  peer announcements (shared by all instances)
CONTROL_PORT = 8891 + _OFF         # TCP  chat sync, presence, signalling
VOICE_PORT = 8892 + _OFF           # UDP  Opus voice
STREAM_RELAY_PORT = 8895 + _OFF    # UDP  local ffmpeg -> relay fan-out (loopback only)

MAX_FILE = 25 * 1024 * 1024
MAX_TEXT = 4000

PALETTE = ["#5865f2", "#3ba55c", "#faa61a", "#ed4245", "#eb459e",
           "#9b59b6", "#1abc9c", "#e67e22", "#607d8b", "#00a8fc"]

DEFAULTS = {
    "name": "",
    "color": None,
    "room": "общая",
    "peers": [],                # manually added IPs
    # appearance
    "theme": "dark",
    "accent": "#5865f2",
    "font_scale": 100,
    # voice
    "input_device": "",         # device name, "" = system default
    "output_device": "",
    "input_volume": 100,
    "output_volume": 100,
    "input_mode": "vad",        # vad | ptt
    "vad_auto": True,
    "vad_threshold": -50,       # dBFS, used when vad_auto is off
    "ptt_key": 0x56,            # V
    "ptt_release_ms": 200,
    "muted": False,
    "deafened": False,
    "user_volumes": {},         # uid -> percent
    "local_mutes": [],
    # screen share
    "stream_quality": "1080p60",
    "stream_encoder": "auto",   # auto | nvenc | cpu
    "stream_audio": "",         # dshow device name, "" = no audio
    # notifications
    "sounds": True,
    "notify": True,
    "notify_mentions_only": False,
    # window & startup
    "autostart": False,
    "start_minimized": True,    # when launched by autostart
    "close_to_tray": True,
    # misc
    "check_updates": True,
    "last_channel": "",
    "read": {},                 # channel id -> last read message sort key
    "window": None,
}


class Settings(dict):
    """A dict that knows how to persist itself."""

    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if not self.get("uid"):
            self["uid"] = secrets.token_hex(12)
        if not self.get("color"):
            self["color"] = secrets.choice(PALETTE)
        self.save()

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)

    def set(self, key, value):
        self[key] = value
        self.save()

    @property
    def uid(self):
        return self["uid"]

    def room_dir(self):
        safe = "".join(ch if ch.isalnum() else "_" for ch in self["room"].lower()) or "room"
        path = DATA / "rooms" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path
