"""Paths, ports and persistent user settings."""

import copy
import hashlib
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

# Ports. Only the first two need to be open in the firewall; the rest are loopback.
DISCOVERY_PORT = 8890              # UDP  LAN announcements (shared by all instances)
PEER_PORT = 8891 + _OFF            # UDP  iroh QUIC: chat sync, voice, screen share — everything
STREAM_PORT = 8888 + _OFF          # UDP  loopback: incoming screen share -> ffplay
STREAM_RELAY_PORT = 8895 + _OFF    # UDP  loopback: ffmpeg -> relay to viewers
PROTOCOL = 3

MAX_FILE = 25 * 1024 * 1024
MAX_TEXT = 4000

PALETTE = ["#5865f2", "#3ba55c", "#faa61a", "#ed4245", "#eb459e",
           "#9b59b6", "#1abc9c", "#e67e22", "#607d8b", "#00a8fc"]

DEFAULTS = {
    "name": "",
    "color": None,
    "room": "общая",
    "room_secret": "",          # hex; empty = derived from the room name (open LAN room)
    "network_mode": "internet", # internet (relays + NAT traversal) | lan (direct only)
    "known_peers": {},          # uid -> {name, relay, addrs, seen}: reconnect over the internet
    "secret_key": "",           # hex ed25519 key: our identity and message signatures
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
    "ptt_key": 0x56,            # legacy single-key PTT, migrated into "hotkeys"
    "ptt_release_ms": 200,
    "aec": True,                # WebRTC echo cancellation
    "ns": True,                 # WebRTC noise suppression
    "ns_level": 2,              # 0 low … 3 very high
    "agc": True,                # WebRTC automatic gain control
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
    # hotkeys: action -> {"vk", "mods"} (see hotkeys.py)
    "hotkeys": None,
    "hotkeys_global": True,     # also when the window is not focused (games)
    "last_voice": "",
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
        super().__init__(copy.deepcopy(DEFAULTS))
        try:
            self.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        from .hotkeys import normalized
        first_time = self["hotkeys"] is None
        self["hotkeys"] = normalized(self["hotkeys"])
        if first_time and self["ptt_key"] != DEFAULTS["ptt_key"]:
            self["hotkeys"]["ptt"] = {"vk": self["ptt_key"], "mods": []}
        self._ensure_identity()
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

    def _ensure_identity(self):
        """uid = our ed25519 public key (hex). 2.x used a random 24-hex uid — kept as legacy_uid."""
        import iroh
        uid = self.get("uid") or ""
        if len(uid) == 24:
            self["legacy_uid"] = uid
        if len(self["secret_key"]) != 64:
            self["secret_key"] = secrets.token_hex(32)
        key = iroh.SecretKey.from_bytes(bytes.fromhex(self["secret_key"]))
        self["uid"] = key.public().to_bytes().hex()

    @property
    def uid(self):
        return self["uid"]

    def room_secret(self):
        """Joining a room = knowing its secret. Named rooms derive it from the name, so friends
        on the same LAN who type the same name meet; invite codes carry a random one."""
        if len(self["room_secret"]) == 64:
            return bytes.fromhex(self["room_secret"])
        return hashlib.sha256(("moydiscord-room:" + self["room"].strip().lower()).encode()).digest()

    def room_id(self):
        return hashlib.sha256(self.room_secret()).hexdigest()[:32]

    def room_dir(self):
        safe = "".join(ch if ch.isalnum() else "_" for ch in self["room"].lower()) or "room"
        path = DATA / "rooms" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path
