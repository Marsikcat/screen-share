"""Paths, ports and persistent user settings."""

import copy
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

APP_NAME = "MarinCall"
# Installed build (PyInstaller): ROOT is the folder with MarinCall.exe, bundled files are
# in sys._MEIPASS. From source: ROOT is the project folder.
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
VERSION = (BUNDLE / "VERSION").read_text(encoding="utf-8").strip()
REPO = "Marsikcat/screen-share"
BRANCH = "master"
# Updates are installed only when signed with this key (see signing.py, tools/sign.py).
RELEASE_KEY = "28068bf159615a88d564818b37ee9bb419ff9733db3e6c5281235d74b1bcf195"

# A second instance on the same PC (for testing) gets its own ports and data.
INSTANCE = int(os.environ.get("MOYDISCORD_INSTANCE", "0") or 0)
_OFF = INSTANCE * 100

# Per Windows user, not in the project folder — the folder gets copied between PCs.
_APPDATA = Path(os.environ.get("APPDATA") or Path.home())
_SUFFIX = f"-{INSTANCE}" if INSTANCE else ""
DATA = _APPDATA / ("MarinCall" + _SUFFIX)
_OLD_DATA = _APPDATA / ("MoyDiscord" + _SUFFIX)      # the app was called МойДискорд until 3.2
if not DATA.exists() and _OLD_DATA.exists():
    try:
        _OLD_DATA.rename(DATA)                       # keep the key, history and settings
    except OSError:
        DATA = _OLD_DATA
FILES_DIR = DATA / "files"
SETTINGS_FILE = DATA / "settings.json"
BACKUP_FILE = DATA / "settings.backup.json"     # the last good copy: the key must never be lost

# Ports — both need to be open in the firewall (the screen-share encoder talks to the app over
# a loopback TCP port the system picks).
DISCOVERY_PORT = 8890              # UDP  LAN announcements (shared by all instances)
PEER_PORT = 8891 + _OFF            # UDP  iroh QUIC: chat sync, voice, screen share — everything
PROTOCOL = 3

MAX_FILE = 25 * 1024 * 1024
MAX_TEXT = 4000

PALETTE = ["#5865f2", "#3ba55c", "#faa61a", "#ed4245", "#eb459e",
           "#9b59b6", "#1abc9c", "#e67e22", "#607d8b", "#00a8fc"]

DEFAULTS = {
    "name": "",
    "color": None,
    "avatar": "",               # file id of the profile picture, "" = the coloured letter
    "status": "online",         # online | idle | dnd — what you chose (away is also automatic)
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
    "stream_audio": "system",   # "system" = the PC minus MarinCall, a dshow device, "" = none
    "stream_volume": 100,       # the sound of streams we watch, percent
    "camera_device": "",        # DirectShow video device for video calls, "" = the first one
    "pause_preview_inactive": True,  # own share/camera preview pauses while another window is active
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
    "auto_update": True,        # download in the background, install when the app closes
    "last_channel": "",
    "read": {},                 # channel id -> last read message sort key
    "drafts": {},               # channel id -> text you started but did not send
    "dm_seen": {},              # conversation id -> how far the other person has read
    "dm_delivered": {},         # conversation id -> {"n": contiguous seq, "s": [more seqs]} they have
    "relay_mail": True,         # keep and pass on others' sealed direct messages
    "recent_emoji": [],         # last picked emoji, newest first
    "link_previews": True,      # fetch a title/picture card for links we send
    "animate_gifs": True,       # GIFs move in the chat (while you can see them)
    "soundboard": True,         # play the soundboard sounds others send
    "soundboard_volume": 60,
    "window": None,
}


def _read_json(path):
    """The dict in `path`; None if there is no such file; ValueError if it is damaged. Waits a
    little while another program (an antivirus, a backup tool) holds the file."""
    for _ in range(10):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.3)
            continue
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    raise PermissionError(f"файл занят другой программой: {path}")


def _load_settings():
    """settings.json — or, if it is damaged, the backup copy (the damaged file is kept aside).
    A file we cannot read at all stops the start: carrying on would make a new identity and
    write it over the real one."""
    for path in (SETTINGS_FILE, BACKUP_FILE):
        try:
            data = _read_json(path)
        except ValueError:
            try:
                path.replace(path.with_name(f"{path.stem}.damaged-{int(time.time())}.json"))
            except OSError:
                pass
            continue
        except PermissionError:
            if path == SETTINGS_FILE:
                raise
            continue
        if data:
            return data
    return {}


class Settings(dict):
    """A dict that knows how to persist itself."""

    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))
        self._backed_up = float("-inf")
        self.update(_load_settings())
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
        text = json.dumps(self, ensure_ascii=False, indent=2)
        targets = [SETTINGS_FILE]
        if time.monotonic() - self._backed_up > 60:        # the backup a minute behind at most
            targets.append(BACKUP_FILE)
            self._backed_up = time.monotonic()
        for target in targets:
            tmp = target.with_suffix(".tmp")
            for attempt in range(5):
                try:
                    tmp.write_text(text, encoding="utf-8")
                    os.replace(tmp, target)
                    break
                except PermissionError:          # held open by someone for a moment
                    time.sleep(0.1 * (attempt + 1))
            else:
                print(f"settings: could not write {target}", flush=True)

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

    def room_closed(self):
        """Closed rooms have a random secret and are reachable only by invite."""
        return len(self["room_secret"]) in (32, 64)

    def room_secret(self):
        """Joining a room = knowing its secret. Open rooms derive it from the name, so friends
        on the same LAN who type the same name meet; closed rooms have a random one."""
        if self.room_closed():
            return bytes.fromhex(self["room_secret"])
        return hashlib.sha256(("moydiscord-room:" + self["room"].strip().lower()).encode()).digest()

    def room_id(self):
        return hashlib.sha256(self.room_secret()).hexdigest()[:32]

    def room_dir(self):
        if self.room_closed():   # a closed room's name can change (it syncs as an event)
            safe = "closed-" + self.room_id()[:12]
        else:
            safe = "".join(ch if ch.isalnum() else "_" for ch in self["room"].lower()) or "room"
        path = DATA / "rooms" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path
