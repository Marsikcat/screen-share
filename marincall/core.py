"""Core: glues the store, the mesh, voice and screen share together for the UI."""

import base64
import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

import iroh
from PySide6.QtCore import QObject, QTimer, Signal

from . import audio, linkpreview, mailbox
from .audio import files_label
from .config import FILES_DIR, MAX_FILE, VERSION
from .net import CAMERA_MAGIC, Mesh, make_invite, parse_invite
from .store import UID_RE, Store, author_of, text_channel_name
from .system import idle_seconds

IDLE_AFTER = 10 * 60          # s without keyboard or mouse: «Отошёл»
from .stream import CAMERA, TEST_CAMERA, CameraHub, SourcePreview, StreamSender, StreamViewer, list_cameras
from .voice import VoiceEngine

FILE_CHUNK = 192 * 1024
TYPING_TTL = 6.0


class Core(QObject):
    channels_changed = Signal()
    message_added = Signal(str, dict)       # channel id, message
    message_changed = Signal(str, str)      # channel id, message id
    message_removed = Signal(str, str)
    members_changed = Signal()
    voice_changed = Signal()
    speaking_changed = Signal(str, bool)
    typing_changed = Signal(str)
    file_ready = Signal(str)
    cache_cleared = Signal()
    room_changed = Signal()
    read_changed = Signal()
    seen_changed = Signal(str)              # a conversation: the other person read further
    stream_changed = Signal()
    sounds_changed = Signal()               # the room's soundboard list
    sound_played = Signal(str, str)         # who, what («🥁 Ба-дум-тсс»)
    _embed_ready = Signal(str, dict)        # message id, link card (from the fetch thread)
    toast = Signal(str, str)                # text, kind: info | error
    notify = Signal(str, str, str)          # title, body, channel id

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.me = settings.uid
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        key = iroh.SecretKey.from_bytes(bytes.fromhex(settings["secret_key"]))
        self._sign = lambda data: key.sign(data).to_bytes()
        self.store = Store(settings.room_dir(), self.me, self._sign, self._verify)
        # direct messages: one small log per conversation, synced only between those two
        self.dms = {}               # other uid -> Store
        self.dm_peer = {}           # "dm:…" channel id -> other uid
        self._load_dms()
        self.states = {}            # uid -> state dict from the peer
        self.my_voice = None
        self.typing = {}            # channel -> {uid: expiry}
        self.watchers = set()       # peers watching my stream
        self.wanted = {}            # file id -> set of peers already asked
        self.downloads = {}         # file id -> {path, size, got}
        self.stream_info = ""
        self._last_typing = 0.0
        self._avatar_files = {}     # file id -> path (None: not here yet)
        self._private_files = {}    # file id -> the only peer to ask (direct messages)
        self._auto_idle = False     # away from the keyboard (the chosen status stays «В сети»)
        self._idle_check = 0

        self.mesh = Mesh(settings, self._hello_payload)
        self.mesh.peer_up.connect(self._on_peer_up)
        self.mesh.peer_down.connect(self._on_peer_down)
        self.mesh.received.connect(self._on_received)
        self.mesh.error.connect(lambda m: self.toast.emit(m, "error"))
        self.mesh.addr_changed.connect(self._on_addr_changed)

        self.voice = VoiceEngine(settings, self.mesh)
        self.voice.speaking.connect(self._on_local_speaking)
        self.voice.failed.connect(lambda m: self.toast.emit(m, "error"))
        self.sender = StreamSender(self.mesh)
        self.preview = SourcePreview()      # what the streamer sees of their own stream
        self.sender.stopped.connect(self._on_stream_stopped)
        self.sender.warning.connect(lambda m: self.toast.emit(m, "error"))
        self.viewer = StreamViewer(self.mesh, self.voice)
        self.viewer.closed.connect(self._on_viewer_closed)
        # webcams: ours goes to everyone in the voice channel who asks, theirs come to us
        self.camera = StreamSender(self.mesh, header=CAMERA_MAGIC)
        self.camera.stopped.connect(self._on_camera_stopped)
        self.camera.warning.connect(lambda m: self.toast.emit(m, "error"))
        self.camera.tap = lambda chunk: self.cameras.feed_local(self.me, chunk)
        self.cameras = CameraHub(self.mesh)
        self.cam_watchers = set()   # peers receiving my camera
        self._cam_subs = set()      # peers whose camera I receive

        self._sb_pcm = {}           # custom sound file id -> samples
        self._sb_recent = {}        # uid -> times of their last soundboard sounds (rate limit)
        self._sb_slot = 0
        self._embed_ready.connect(self._publish_embed)
        # direct messages for people who are offline, carried by the rest of the room
        self.keys = mailbox.Keys(settings["secret_key"])
        self.mail = mailbox.Mailbox(settings.room_dir() / "mailbox.json")
        self.mail.expire()
        self._mail_save = QTimer(self, singleShot=True, interval=1500, timeout=self.mail.save)
        self._mail_clock = 0

        self._timer = QTimer(self, interval=1000, timeout=self._tick)

    def start(self):
        self.mesh.start()
        self.voice.start()
        self._timer.start()
        if self.s.room_closed() and not self.store.room_name[2] and self.s["room"] != "Комната друга":
            self.set_room_name(self.s["room"])      # we created it: publish its name
        if not self.store.profiles.get(self.me) or \
                self.store.profiles[self.me]["name"] != self.s["name"]:
            if self.s["name"]:
                self._publish("profile", name=self.s["name"], color=self.s["color"])
        if (self.s.get("avatar") or "") != self.store.avatar_of(self.me):
            self._publish("avatar", file=self.s.get("avatar") or "")

    def shutdown(self):
        self.preview.stop()
        self.sender.stop()
        self.camera.stop()
        self.cameras.stop()
        self.viewer.stop()
        self.voice.shutdown()
        self.mesh.stop()

    # ── identity & presence ─────────────────────────────────────────
    def _my_state(self):
        return {"voice": self.my_voice, "muted": bool(self.s["muted"]),
                "deafened": bool(self.s["deafened"]), "streaming": self.sender.running,
                "speaking": self.voice.gate, "status": self.my_status(), "camera": self.camera.running}

    def my_status(self):
        chosen = self.s.get("status") or "online"
        return "idle" if chosen == "online" and self._auto_idle else chosen

    def set_status(self, status):
        self.s["status"] = status
        self.s.save()
        self._broadcast_state()
        self.members_changed.emit()

    def do_not_disturb(self):
        return self.my_status() == "dnd"

    @staticmethod
    def _verify(author, data, sig):
        try:
            iroh.EndpointId.from_bytes(bytes.fromhex(author)).verify(data, iroh.Signature.from_bytes(sig))
            return True
        except Exception:
            return False

    def _hello_payload(self):
        return {"name": self.s["name"], "color": self.s["color"], "version": VERSION,
                "state": self._my_state()}

    def _broadcast_state(self):
        self.mesh.broadcast({"t": "state", "state": self._my_state()})
        self._update_voice_peers()
        self.voice_changed.emit()

    def online(self):
        return set(self.mesh.peers()) | {self.me}

    def member(self, uid):
        prof = self.store.profiles.get(uid)
        if uid in (self.me, self.s.get("legacy_uid")):     # our 2.x archive id is still us
            name, color = self.s["name"], self.s["color"]
        else:
            hello = self.mesh.peers().get(uid, {})
            name = (prof or {}).get("name") or hello.get("name") or "Участник"
            color = (prof or {}).get("color") or hello.get("color") or "#5865f2"
        state = self._my_state() if uid == self.me else self.states.get(uid, {})
        online = uid in self.online()
        status = (state.get("status") or "online") if online else "offline"
        if status not in ("online", "idle", "dnd", "offline"):
            status = "online"
        return {"uid": uid, "name": name, "color": color, "online": online, "status": status,
                "state": state, "avatar": self.avatar_path(uid)}

    def avatar_path(self, uid):
        """The picture someone chose, if we have it (it is fetched from peers like any file)."""
        mine = uid in (self.me, self.s.get("legacy_uid"))
        fid = (self.s.get("avatar") or "") if mine else self.store.avatar_of(uid)
        if not fid:
            return None
        if fid not in self._avatar_files:
            path = self.file_path(fid)
            self._avatar_files[fid] = str(path) if path else None
            if not path:
                self.request_file(fid)
        return self._avatar_files[fid]

    def set_avatar(self, path):
        """path: a prepared square picture, or None to go back to the letter."""
        fid = ""
        if path:
            meta = self.import_file(path)
            if not meta:
                return False
            fid = meta["id"]
            self._avatar_files.pop(fid, None)
        self.s["avatar"] = fid
        self.s.save()
        self._publish("avatar", file=fid)
        self.members_changed.emit()
        return True

    def members(self):
        # 2.x archive authors (short ids) show up in old messages, not in the member list
        uids = {u for u in self.store.profiles if UID_RE.match(u)} | self.online()
        return sorted((self.member(u) for u in uids), key=lambda m: m["name"].lower())

    def name_of(self, uid):
        return self.member(uid)["name"]

    def set_profile(self, name, color):
        self.s["name"], self.s["color"] = name, color
        self.s.save()
        self._publish("profile", name=name, color=color)
        self.members_changed.emit()

    def room_name(self):
        return self.store.display_room_name(self.s["room"])

    # ── direct messages ─────────────────────────────────────────────
    def dm_cid(self, uid):
        """The same id on both sides: both sign the very same channel name into their messages."""
        pair = "".join(sorted((self.me, uid)))
        return "dm:" + hashlib.sha256(pair.encode()).hexdigest()[:32]

    def _dm_dir(self):
        return self.s.room_dir() / "dm"

    def _load_dms(self):
        folder = self._dm_dir()
        if folder.is_dir():
            for sub in folder.iterdir():
                if sub.is_dir() and UID_RE.match(sub.name):
                    self.dm_store(sub.name)

    def dm_store(self, uid, create=True):
        st = self.dms.get(uid)
        if st is None and create and UID_RE.match(uid) and uid != self.me:
            path = self._dm_dir() / uid
            path.mkdir(parents=True, exist_ok=True)
            st = Store(path, self.me, self._sign, self._verify)
            self.dms[uid] = st
            self.dm_peer[self.dm_cid(uid)] = uid
        return st

    def open_dm(self, uid):
        """The channel id of the conversation with `uid` (created if new)."""
        if self.dm_store(uid) is None:
            return None
        self.channels_changed.emit()
        return self.dm_cid(uid)

    def dm_list(self):
        """[(uid, channel id)] newest conversation first."""
        rows = [(uid, self.dm_cid(uid), st.last_ts(self.dm_cid(uid))) for uid, st in self.dms.items()]
        return [(u, c) for u, c, _ in sorted(rows, key=lambda r: -r[2])]

    def store_for(self, cid):
        if cid and cid.startswith("dm:"):
            uid = self.dm_peer.get(cid)
            return self.dms.get(uid) if uid else self.store
        return self.store

    def store_of_msg(self, mid):
        if mid in self.store.msg_by_id:
            return self.store
        return next((st for st in self.dms.values() if mid in st.msg_by_id), self.store)

    def channel(self, cid):
        """A text/voice channel of the room, or a conversation: {id, kind, name, topic}."""
        if cid and cid.startswith("dm:"):
            uid = self.dm_peer.get(cid)
            return {"id": cid, "kind": "dm", "name": self.name_of(uid), "topic": "", "peer": uid} if uid else None
        return self.store.channel(cid)

    def peer_version(self, uid):
        return str(self.mesh.peers().get(uid, {}).get("version") or "")

    def _accept_dm(self, uid, ev):
        """Only the two of us write into our conversation, and only into it."""
        if not isinstance(ev, dict) or ev.get("a") not in (self.me, uid):
            return False
        if ev.get("k") not in ("msg", "edit", "del", "react", "pin", "embed"):
            return False
        return ev.get("k") != "msg" or ev.get("ch") == self.dm_cid(uid)

    # ── mail: direct messages through the room while the other person is offline ──
    def _mail_peer(self, uid):
        """Does this peer understand mail (3.7+)?"""
        from .updater import parse_version
        return parse_version(self.peer_version(uid) or "0") >= (3, 7, 0)

    def _mail_changed(self):
        self._mail_save.start()

    def _post_mail(self, peer, events):
        """Seal new events of our conversation (and their files) for `peer`."""
        files, blobs = {}, []
        for ev in events:
            fids = [f["id"] for f in ev.get("files") or []] if ev["k"] == "msg" else []
            if ev["k"] == "embed" and ev.get("image"):
                fids.append(ev["image"])
            for fid in fids:
                path = self.file_path(fid)
                if not path or path.stat().st_size > mailbox.MAX_BLOB_FILE:
                    continue                  # too big to carry: it comes when you meet
                key, sealed = mailbox.seal_file(path.read_bytes())
                bid = mailbox.blob_id(sealed)
                (FILES_DIR / bid).write_bytes(sealed)
                self.mail.blobs.add(bid)
                files[fid] = {"blob": bid, "key": key.hex()}
                blobs.append(bid)
        payload = json.dumps({"v": 1, "events": events, "files": files}, ensure_ascii=False).encode()
        if len(payload) > mailbox.MAX_BOX - 64:
            return
        env = mailbox.make_envelope(peer, self.keys.seal(peer, payload), blobs)
        self.mail.add(env, own_seqs=[e["s"] for e in events])
        self._mail_changed()
        for uid in list(self.mesh.peers_map):
            if self._mail_peer(uid):
                self._send_mail(uid, [env])
        self.seen_changed.emit(self.dm_cid(peer))

    def _send_mail(self, uid, envelopes):
        batch, size = [], 0
        for env in envelopes:
            batch.append(env)
            size += len(env["box"])
            if size > 900_000:               # well under the 4 MB line limit
                self.mesh.send(uid, {"t": "mail", "e": batch})
                batch, size = [], 0
        if batch:
            self.mesh.send(uid, {"t": "mail", "e": batch})

    def _mail_hello(self, uid):
        if not self._mail_peer(uid):
            return
        self._send_mail(uid, self.mail.for_uid(uid))
        if self.mail.receipts:
            self.mesh.send(uid, {"t": "mail_done", "r": self.mail.receipts[-1000:]})
        ids = self.mail.ids_except(uid)
        if ids:
            self.mesh.send(uid, {"t": "mail_ids", "ids": ids[:5000]})

    def _blob_room(self):
        used = 0
        for b in self.mail.blobs:
            p = FILES_DIR / b
            try:
                used += p.stat().st_size
            except OSError:
                pass
        return used < mailbox.MAX_BLOBS

    def _on_mail(self, uid, envelopes):
        fresh, opened = [], []
        for env in envelopes:
            env = mailbox.validate(env)
            if not env:
                continue
            if env["to"] == self.me:
                if self._open_mail(env, uid):
                    opened.append(env["id"])
            elif env["to"] != uid and self.s.get("relay_mail", True) and self.mail.add(env):
                fresh.append(env)
        if opened:
            self._send_receipt(opened, uid)
        if not fresh:
            return
        self._mail_changed()
        for env in fresh:
            if env["blobs"] and self._blob_room():
                for b in env["blobs"]:
                    self.mail.blobs.add(b)
                    if not self.file_path(b):
                        self.request_file(b, prefer=uid)
        for other in list(self.mesh.peers_map):          # pass it on (the recipient first)
            if other != uid and self._mail_peer(other):
                self._send_mail(other, [e for e in fresh if e["to"] == other] +
                                [e for e in fresh if e["to"] != other])

    def _open_mail(self, env, via):
        """An envelope for us: open it. Always True — a receipt is due either way: what we
        cannot open, nobody else can, and it should stop travelling."""
        if self.me in self.mail.done.get(env["id"], {}):
            return True
        data = self.keys.open(base64.b64decode(env["box"]))
        try:
            payload = json.loads(data) if data else None
            events, files = payload["events"], payload.get("files") or {}
        except (ValueError, KeyError, TypeError):
            return True
        if not isinstance(events, list) or not events or not isinstance(files, dict):
            return True
        sender = events[0].get("a") if isinstance(events[0], dict) else None
        if not isinstance(sender, str) or not UID_RE.match(sender) or sender == self.me:
            return True
        if not all(isinstance(e, dict) and e.get("a") == sender and self._accept_dm(sender, e)
                   for e in events):
            return True
        st = self.dm_store(sender)
        new_conversation = not st.messages
        for fid, info in list(files.items())[:20]:
            if not (isinstance(info, dict) and mailbox.ID_RE.match(str(fid))
                    and mailbox.ID_RE.match(str(info.get("blob", "")))
                    and re.fullmatch(r"[0-9a-f]{64}", str(info.get("key", "")))):
                continue
            if not self.file_path(fid):
                self.mail.incoming[fid] = {"blob": info["blob"], "key": info["key"], "from": sender}
                self.mail.blobs.add(info["blob"])
                if self.file_path(info["blob"]):
                    self._unseal(info["blob"])
                else:
                    self.request_file(info["blob"], prefer=via)
        fresh = [ev for ev in (st.add(e) for e in events) if ev]
        for ev in fresh:
            self._on_event(ev, live=True, store=st)
        if fresh and new_conversation:
            self.channels_changed.emit()
        self._mail_changed()
        return True

    def _unseal(self, blob):
        """One of our sealed files arrived: open it into the file it really is."""
        path = FILES_DIR / blob
        for fid, info in list(self.mail.incoming.items()):
            if info["blob"] != blob:
                continue
            try:
                plain = mailbox.open_file(bytes.fromhex(info["key"]), path.read_bytes())
            except OSError:
                return
            if plain is not None and hashlib.sha256(plain).hexdigest()[:32] == fid:
                tmp = FILES_DIR / f"{fid}.part"
                tmp.write_bytes(plain)
                tmp.replace(FILES_DIR / fid)
                self.wanted.pop(fid, None)
                self.file_ready.emit(fid)
            self.mail.incoming.pop(fid, None)
        self._drop_unused_blobs()
        self._mail_changed()

    def _send_receipt(self, ids, via):
        old = [r for r in (self.mail.receipt_of(self.me, i) for i in ids) if r]
        new_ids = [i for i in ids if not self.mail.receipt_of(self.me, i)]
        fresh = []
        if new_ids:
            rec = mailbox.make_receipt(self.me, new_ids, self._sign)
            self.mail.add_receipt(rec)
            fresh.append(rec)
            self._mail_changed()
        if fresh:
            for uid in list(self.mesh.peers_map):
                if self._mail_peer(uid):
                    self.mesh.send(uid, {"t": "mail_done", "r": fresh})
        if old:                                   # they still carry mail we took long ago
            self.mesh.send(via, {"t": "mail_done", "r": old})

    def _on_receipts(self, uid, receipts):
        fresh = []
        for rec in receipts:
            if self.mail.has_receipt(rec):
                continue
            rec = mailbox.check_receipt(rec, self._verify)
            if not rec:
                continue
            delivered, new = self.mail.add_receipt(rec)
            if new:
                fresh.append(rec)
            for own in delivered:
                self._mark_delivered(own["to"], own["seqs"])
        if fresh:
            self._drop_unused_blobs()
            self._mail_changed()
            for other in list(self.mesh.peers_map):
                if other != uid and self._mail_peer(other):
                    self.mesh.send(other, {"t": "mail_done", "r": fresh})

    def _drop_unused_blobs(self):
        used = self.mail.blob_ids()
        busy = False
        for b in list(self.mail.blobs - used):
            try:
                (FILES_DIR / b).unlink(missing_ok=True)
            except OSError:                  # still being sent to someone: try again later
                busy = True
                continue
            self.mail.blobs.discard(b)
            self.wanted.pop(b, None)
        if busy:
            QTimer.singleShot(30_000, self._drop_unused_blobs)

    def _mark_delivered(self, uid, seqs=(), upto=None):
        """The other person has these events of ours (seqs), or all of them up to `upto`."""
        cid = self.dm_cid(uid)
        d = self.s.setdefault("dm_delivered", {}).setdefault(cid, {"n": 0, "s": []})
        n = max(int(d.get("n") or 0), int(upto or 0) if isinstance(upto, int) else 0)
        extra = sorted({int(x) for x in (*d.get("s", []), *seqs) if int(x) > n})[-300:]
        if n == d.get("n") and extra == d.get("s"):
            return
        d["n"], d["s"] = n, extra
        self.s.save()
        self.seen_changed.emit(cid)

    def dm_delivery(self, cid, msg):
        """My message in a conversation: "read", "delivered", "pending" (or None)."""
        if msg["author"] != self.me or not self.dm_peer.get(cid):
            return None
        if msg["ts"] <= self.seen_by_peer(cid):
            return "read"
        d = self.s.get("dm_delivered", {}).get(cid) or {}
        seq = int(str(msg["id"]).rsplit(":", 1)[1])
        if seq <= int(d.get("n") or 0) or seq in d.get("s", []):
            return "delivered"
        return "pending"

    def mail_stats(self):
        """(envelopes kept for others, their bytes with files)."""
        others = [e for i, e in self.mail.env.items() if i not in self.mail.own]
        size = sum(len(e["box"]) * 3 // 4 for e in others)
        for b in self.mail.blobs:
            try:
                size += (FILES_DIR / b).stat().st_size
            except OSError:
                pass
        return len(others), size

    def _send_dm_vector(self, uid, ask):
        st = self.dms.get(uid)
        if st is not None:
            self.mesh.send(uid, {"t": "dm_vv", "v": st.vector(), "ask": ask})

    # ── search & pins ───────────────────────────────────────────────
    def search(self, query, cid=None, limit=200):
        """[(channel id, message)] newest first — the room's channels and our conversations."""
        stores = [self.store_for(cid)] if cid else [self.store, *self.dms.values()]
        found = [m for st in stores for m in st.search(query, cid, limit)]
        found = [m for m in found if self.channel(m["ch"])]          # not in deleted channels
        found.sort(key=Store.sort_key, reverse=True)
        return found[:limit]

    def toggle_pin(self, mid):
        st = self.store_of_msg(mid)
        self._publish("pin", store=st, target=mid, on=not st.is_pinned(mid))

    # ── invites & known peers (for connecting over the internet) ─────
    def _remember(self, uid, info):
        if uid == self.me or not UID_RE.match(uid):
            return
        known = dict(self.s["known_peers"] or {})
        old = known.get(uid, {})
        entry = {"room": self.s.room_id(),
                 "name": str(info.get("name") or old.get("name") or "")[:32],
                 "relay": info.get("relay") or old.get("relay"),
                 "addrs": [str(a) for a in (info.get("addrs") or old.get("addrs") or [])][:8]}
        if {k: old.get(k) for k in entry} != entry:
            entry["seen"] = int(time.time())
            known[uid] = entry
            self.s["known_peers"] = known       # replaced, not mutated: the network thread reads it
            self.s.save()

    def forget_peer(self, uid):
        known = dict(self.s["known_peers"] or {})
        if known.pop(uid, None) is not None:
            self.s["known_peers"] = known
            self.s.save()

    def _on_addr_changed(self):
        self.mesh.broadcast({"t": "addr", "addr": {**self.mesh.my_addr, "name": self.s["name"]}})

    def create_invite(self):
        if self.s.room_closed():
            # the name of a closed room travels as an event, so the invite can stay short
            if not self.store.room_name[2]:
                self.set_room_name(self.s["room"])
            return make_invite(self.me, self.mesh.my_addr["relay"], secret=self.s.room_secret())
        return make_invite(self.me, self.mesh.my_addr["relay"], room=self.s["room"])

    def create_closed_room(self, name):
        """A fresh room with a random secret: only people you invite can join. Needs a restart."""
        self.s["room"], self.s["room_secret"] = name, secrets.token_hex(16)
        self.s.save()

    def join_invite(self, code):
        """Returns True if the app has to restart (a different room), False if we just dial."""
        inv = parse_invite(code)
        peer = inv["peer"]
        if peer["id"] == self.me:
            raise ValueError("это ваше собственное приглашение — отправьте его другу")
        if inv["secret"]:
            restart = inv["secret"] != self.s["room_secret"]
            if restart:
                self.s["room"], self.s["room_secret"] = "Комната друга", inv["secret"]
        else:
            restart = self.s.room_closed() or inv["room"].strip().lower() != self.s["room"].strip().lower()
            if restart:
                self.s["room"], self.s["room_secret"] = inv["room"], ""
        self._remember(peer["id"], peer)
        if not restart:
            self.mesh.dial(peer["id"], peer.get("relay"), peer.get("addrs") or [])
        self.s.save()
        return restart

    def set_room_name(self, name):
        self._publish("room", name=name)

    # ── mesh events ─────────────────────────────────────────────────
    def _on_peer_up(self, uid):
        hello = self.mesh.peers().get(uid, {})
        self._set_state(uid, hello.get("state") or {})
        self._remember(uid, {**(hello.get("addr") or {}), "name": hello.get("name")})
        self.mesh.send(uid, {"t": "vv", "v": self.store.vector()})
        # peer exchange: whoever joins through one of us learns about everyone else
        known = self.s["known_peers"] or {}
        self.mesh.send(uid, {"t": "pex", "peers": {u: known[u] for u in self.mesh.peers()
                                                   if u != uid and u in known}})
        for fid in list(self.wanted):
            self.request_file(fid)
        self._send_dm_vector(uid, ask=True)
        if uid in self.dms:
            self._send_seen(self.dm_cid(uid))
        self._mail_hello(uid)
        self.members_changed.emit()

    def _on_peer_down(self, uid):
        self._set_state(uid, None)
        self.cam_watchers.discard(uid)
        self.camera.set_viewers(self._cam_watcher_uids())
        self.watchers.discard(uid)
        self.sender.set_viewers(self._watcher_uids())
        if self.viewer.uid == uid:
            self.viewer.stop()
            self.stream_changed.emit()
        self.members_changed.emit()

    def _set_state(self, uid, state):
        old = self.states.get(uid, {})
        if state is None:
            self.states.pop(uid, None)
            state = {}
        else:
            self.states[uid] = state
        if self.my_voice:
            was_here = old.get("voice") == self.my_voice
            is_here = state.get("voice") == self.my_voice
            if is_here and not was_here:
                self.voice.play("peer_join")
            elif was_here and not is_here:
                self.voice.play("peer_leave")
        if old.get("streaming") and not state.get("streaming") and self.viewer.uid == uid:
            self.viewer.stop()
            self.toast.emit(f"{self.name_of(uid)} завершил(а) трансляцию", "info")
            self.stream_changed.emit()
        self._update_voice_peers()
        self.voice_changed.emit()

    def _on_received(self, uid, msg):
        t = msg.get("t")
        if t == "hello":
            self._set_state(uid, msg.get("state") or {})
        elif t == "vv" and isinstance(msg.get("v"), dict):
            missing = self.store.missing_for(msg["v"])
            for i in range(0, len(missing), 300):
                self.mesh.send(uid, {"t": "ev", "e": missing[i:i + 300]})
        elif t == "ev" and isinstance(msg.get("e"), list):
            fresh = [ev for ev in (self.store.add(e) for e in msg["e"][:1000]) if ev]
            if fresh:
                self.mesh.broadcast({"t": "ev", "e": fresh}, exclude=uid)  # gossip onwards
                for ev in fresh:
                    self._on_event(ev, live=len(msg["e"]) < 5)
        elif t == "dm_read":
            cid = str(msg.get("ch", ""))
            ts = msg.get("ts")
            if self.dm_peer.get(cid) == uid and isinstance(ts, int) and ts > self.seen_by_peer(cid):
                self.s.setdefault("dm_seen", {})[cid] = ts
                self.s.save()
                self.seen_changed.emit(cid)
        elif t == "dm_vv" and isinstance(msg.get("v"), dict):
            if uid in self.dms:
                self._mark_delivered(uid, upto=msg["v"].get(self.me))
            # they have something for us (or we for them): open the conversation on our side too
            st = self.dm_store(uid, create=bool(msg["v"]) or uid in self.dms)
            if st is not None:
                missing = [ev for ev in st.missing_for(msg["v"]) if self._accept_dm(uid, ev)]
                for i in range(0, len(missing), 300):
                    self.mesh.send(uid, {"t": "dm_ev", "e": missing[i:i + 300]})
                if msg.get("ask"):
                    self._send_dm_vector(uid, ask=False)
        elif t == "dm_ev" and isinstance(msg.get("e"), list):
            events = [e for e in msg["e"][:1000] if self._accept_dm(uid, e)]
            if events:
                st = self.dm_store(uid)
                new_conversation = not st.messages
                fresh = [ev for ev in (st.add(e) for e in events) if ev]
                for ev in fresh:
                    self._on_event(ev, live=len(events) < 5, store=st)
                if fresh and new_conversation:
                    self.channels_changed.emit()
        elif t == "state" and isinstance(msg.get("state"), dict):
            self._set_state(uid, msg["state"])
        elif t == "sb" and isinstance(msg.get("s"), str):
            self._on_sound(uid, msg["s"][:120])
        elif t == "spk":
            st = self.states.setdefault(uid, {})
            st["speaking"] = bool(msg.get("on"))
            self.speaking_changed.emit(uid, st["speaking"])
        elif t == "typing":
            cid = str(msg.get("ch", ""))
            if cid.startswith("dm:") and self.dm_peer.get(cid) != uid:
                return                   # only the other person writes in our conversation
            self.typing.setdefault(cid, {})[uid] = time.monotonic() + TYPING_TTL
            self.typing_changed.emit(cid)
        elif t == "cam":
            (self.cam_watchers.add if msg.get("on") else self.cam_watchers.discard)(uid)
            self.camera.set_viewers(self._cam_watcher_uids())
        elif t == "watch":
            (self.watchers.add if msg.get("on") else self.watchers.discard)(uid)
            self.sender.set_viewers(self._watcher_uids())
            self.stream_changed.emit()
        elif t == "pex" and isinstance(msg.get("peers"), dict):
            for u, info in list(msg["peers"].items())[:200]:
                if isinstance(info, dict) and UID_RE.match(str(u)) and u != self.me:
                    self._remember(u, info)
                    if u not in self.mesh.peers_map:
                        self.mesh.dial(u, info.get("relay"), info.get("addrs") or [])
        elif t == "addr" and isinstance(msg.get("addr"), dict):
            self._remember(uid, msg["addr"])
        elif t == "file_get":
            self._serve_file(uid, str(msg.get("id", "")))
        elif t == "file":
            self._receive_chunk(uid, msg)
        elif t == "file_missing":
            self.request_file(str(msg.get("id", "")))
        elif t == "mail" and isinstance(msg.get("e"), list):
            self._on_mail(uid, msg["e"][:300])
        elif t == "mail_ids" and isinstance(msg.get("ids"), list):
            if self.s.get("relay_mail", True):
                want = [i for i in msg["ids"][:5000] if isinstance(i, str) and mailbox.ID_RE.match(i)
                        and i not in self.mail.env and not self.mail.done.get(i)]
                if want:
                    self.mesh.send(uid, {"t": "mail_get", "ids": want[:2000]})
        elif t == "mail_get" and isinstance(msg.get("ids"), list):
            self._send_mail(uid, [self.mail.env[i] for i in msg["ids"][:2000]
                                  if isinstance(i, str) and i in self.mail.env])
        elif t == "mail_done" and isinstance(msg.get("r"), list):
            self._on_receipts(uid, msg["r"][:1000])

    # ── events → UI ─────────────────────────────────────────────────
    def _publish(self, event_kind, store=None, **payload):
        store = store or self.store
        ev = store.create(event_kind, **payload)
        if ev:
            if store is self.store:
                self.mesh.broadcast({"t": "ev", "e": [ev]})
            else:                      # a conversation: to that one person, never gossiped
                peer = next(u for u, st in self.dms.items() if st is store)
                if peer in self.mesh.peers_map:
                    self.mesh.send(peer, {"t": "dm_ev", "e": [ev]})
                    self._mark_delivered(peer, [ev["s"]])
                else:                  # offline: sealed for them, carried by whoever is online
                    self._post_mail(peer, [ev])
            self._on_event(ev, live=True, store=store)
        return ev

    def _on_event(self, ev, live, store=None):
        store = store or self.store
        k = ev["k"]
        if k == "msg":
            msg = store.msg_by_id[ev["id"]]
            peer = self.dm_peer.get(msg["ch"])
            for f in msg["files"]:
                if not self.file_path(f["id"]):
                    self.request_file(f["id"], prefer=msg["author"], only=peer)
            self.typing.get(msg["ch"], {}).pop(msg["author"], None)
            self.message_added.emit(msg["ch"], msg)
            if live and msg["author"] != self.me:
                self._maybe_notify(msg, store)
        elif k in ("edit", "react", "pin"):
            m = store.msg_by_id.get(ev["target"])
            if m:
                self.message_changed.emit(m["ch"], m["id"])
        elif k == "del":
            m = store.msg_by_id.get(ev["target"])
            if m:
                self.message_removed.emit(m["ch"], m["id"])
        elif k in ("ch_new", "ch_ren", "ch_del"):
            if k == "ch_del" and ev["target"] == self.my_voice:
                self.leave_voice()
            self.channels_changed.emit()
        elif k == "profile":
            self.members_changed.emit()
        elif k == "room":
            self.room_changed.emit()
        elif k == "avatar":
            if ev["file"] and not self.file_path(ev["file"]):
                self.request_file(ev["file"], prefer=ev["a"])
            self.members_changed.emit()
        elif k == "embed":
            m = store.msg_by_id.get(ev["target"])
            if ev["image"] and not self.file_path(ev["image"]):
                self.request_file(ev["image"], prefer=ev["a"], only=self.dm_peer.get(m["ch"]) if m else None)
            if m:
                self.message_changed.emit(m["ch"], m["id"])
        elif k == "sound":
            if ev["file"] and not self.file_path(ev["file"]):
                self.request_file(ev["file"], prefer=ev["a"])      # sounds must play instantly
            self.sounds_changed.emit()

    def mentions_me(self, text):
        name = re.escape(self.s["name"])
        return bool(name and re.search(rf"@({name}|все|everyone)(?!\w)", text, re.IGNORECASE))

    def _maybe_notify(self, msg, store):
        if self.do_not_disturb():                # «Не беспокоить»: no pop-ups, no sounds
            return
        text = store.text_of(msg)
        personal = msg["ch"].startswith("dm:")          # a direct message counts as a mention
        if self.s["notify_mentions_only"] and not (personal or self.mentions_me(text)):
            return
        text = re.sub(r"\|\|(.+?)\|\|", "▒▒▒", text, flags=re.S)        # spoilers stay hidden
        body = text or files_label(msg["files"])
        if personal:
            title = f"{self.name_of(msg['author'])}  ·  лично"
        else:
            ch = self.store.channel(msg["ch"])
            title = f"{self.name_of(msg['author'])}  ·  #{ch['name'] if ch else ''}"
        self.notify.emit(title, body[:200], msg["ch"])

    # ── unread ──────────────────────────────────────────────────────
    def mark_read(self, cid):
        msgs = self.store_for(cid).messages.get(cid)
        if msgs:
            last = msgs[-1]["ts"]
            if self.s["read"].get(cid, 0) < last:
                self.s["read"][cid] = last
                self.s.save()
                self.read_changed.emit()
                self._send_seen(cid)

    def _send_seen(self, cid):
        """In a conversation: let the other person know how far we have read («Прочитано»)."""
        peer = self.dm_peer.get(cid)
        if peer and self.s["read"].get(cid):
            self.mesh.send(peer, {"t": "dm_read", "ch": cid, "ts": self.s["read"][cid]})

    def seen_by_peer(self, cid):
        return int(self.s.get("dm_seen", {}).get(cid, 0) or 0)

    def unread(self, cid):
        """(unread count, mentions) for a text channel."""
        since = self.s["read"].get(cid, 0)
        store = self.store_for(cid)
        personal = cid.startswith("dm:")
        count = mentions = 0
        for m in reversed(store.messages.get(cid, [])):
            if m["ts"] <= since:
                break
            if m["author"] != self.me and m["id"] not in store.deleted:
                count += 1
                mentions += personal or self.mentions_me(store.text_of(m))
        return count, mentions

    # ── text actions ────────────────────────────────────────────────
    def send_message(self, cid, text, files=(), reply=None):
        metas = []
        for path in files:
            meta = self.import_file(path)
            if meta:
                metas.append(meta)
        text = text.strip()
        if text or metas:
            ev = self._publish("msg", store=self.store_for(cid), ch=cid, text=text, reply=reply, files=metas)
            self.mark_read(cid)
            if ev:
                self._make_previews(ev["id"], text)

    def edit_message(self, mid, text):
        if author_of(mid) == self.me:
            self._publish("edit", store=self.store_of_msg(mid), target=mid, text=text.strip())
            self._make_previews(mid, text)

    # ── link previews ───────────────────────────────────────────────
    def _make_previews(self, mid, text):
        """Open the links of my message (off the UI thread) and publish a card for each."""
        if not self.s.get("link_previews", True):
            return
        have = {e["url"] for e in self.store_of_msg(mid).embeds_of(mid)}
        urls = [u for u in linkpreview.links(text) if u not in have]
        if not urls:
            return

        def fetch_all():
            for url in urls:
                try:
                    card = linkpreview.fetch(url)
                except Exception:
                    continue
                if not card:
                    continue
                data = card.pop("image_bytes", None)
                card["image_path"] = ""
                if data:
                    tmp = Path(tempfile.mkdtemp(prefix="marincall-link-")) / "preview"
                    try:
                        if linkpreview.thumbnail(data, tmp):
                            card["image_path"] = str(tmp)
                    except Exception:
                        pass
                if card["title"] or card["image_path"]:
                    self._embed_ready.emit(mid, card)
        threading.Thread(target=fetch_all, daemon=True).start()

    def _publish_embed(self, mid, card):
        st = self.store_of_msg(mid)
        if mid not in st.msg_by_id or mid in st.deleted:
            return
        fid = ""
        if card.get("image_path"):
            meta = self.import_file(card["image_path"])
            fid = meta["id"] if meta else ""
            shutil.rmtree(Path(card["image_path"]).parent, ignore_errors=True)
        if card["title"] or fid:
            self._publish("embed", store=st, target=mid, url=card["url"], title=card["title"],
                          desc=card["desc"], site=card["site"], image=fid)

    # ── soundboard ──────────────────────────────────────────────────
    def soundboard(self):
        """[(key, name, emoji, custom sound or None)]: the built-in sounds, then the room's own."""
        out = [(f"b:{k}", name, emoji, None) for k, (name, emoji, _) in audio.BUILTIN.items()]
        out += [(f"c:{x['id']}", x["name"], x["emoji"], x) for x in self.store.sound_list()]
        return out

    def _sound(self, key):
        """(samples, label) of a soundboard key, or (None, label) if its file is not here yet."""
        if key.startswith("b:") and key[2:] in audio.BUILTIN:
            name, emoji, _ = audio.BUILTIN[key[2:]]
            return audio.builtin(key[2:]), f"{emoji} {name}"
        x = self.store.sounds.get(key[2:]) if key.startswith("c:") else None
        if not x:
            return None, ""
        label = f"{x['emoji']} {x['name']}"
        pcm = self._sb_pcm.get(x["file"])
        if pcm is None:
            path = self.file_path(x["file"])
            if not path:
                self.request_file(x["file"], prefer=x["by"])
                return None, label
            try:
                pcm = audio.decode(path, audio.SOUND_SECONDS)
            except Exception:
                return None, label
            self._sb_pcm[x["file"]] = pcm
        return pcm, label

    def play_sound(self, key):
        """Play a soundboard sound for everyone in my voice channel."""
        if not self.my_voice or not self._sb_allowed(self.me):
            return False
        pcm, label = self._sound(key)
        if pcm is None:
            self.toast.emit("Этот звук ещё скачивается", "error")
            return False
        for uid in list(self.voice.targets):
            self.mesh.send(uid, {"t": "sb", "s": key})
        self._play_pcm(pcm)
        self.sound_played.emit(self.me, label)
        return True

    def _sb_allowed(self, uid):
        """No more than 4 sounds in 5 seconds from anyone (and one at least 0.4 s apart)."""
        now = time.monotonic()
        recent = [t for t in self._sb_recent.get(uid, []) if now - t < 5]
        if len(recent) >= 4 or (recent and now - recent[-1] < 0.4):
            return False
        self._sb_recent[uid] = recent + [now]
        return True

    def _play_pcm(self, pcm):
        self._sb_slot = (self._sb_slot + 1) % 4         # up to four overlap
        gain = self.s.get("soundboard_volume", 60) / 100.0
        self.voice.play_clip(f"sb{self._sb_slot}", pcm, gain=gain, call=True)

    def _on_sound(self, uid, key):
        if not self.my_voice or uid not in self.voice.allowed or uid in self.s["local_mutes"]:
            return
        if not self._sb_allowed(uid):
            return
        pcm, label = self._sound(key)
        if label:
            self.sound_played.emit(uid, label)
        if pcm is not None and self.s.get("soundboard", True) and not self.s["deafened"]:
            self._play_pcm(pcm)

    def preview_sound(self, key):
        """Just for me (the soundboard's hover/right-click preview and Settings)."""
        pcm, _ = self._sound(key)
        if pcm is not None:
            self._play_pcm(pcm)

    def add_sound(self, path, name, emoji=""):
        """A sound file → the room's soundboard (cut to five seconds, re-encoded small)."""
        try:
            pcm = audio.trim_silence(audio.decode(path, 30))[:audio.SR * audio.SOUND_SECONDS]
        except Exception:
            self.toast.emit("Не получилось прочитать этот звук", "error")
            return False
        if len(pcm) < audio.SR // 10:
            self.toast.emit("В этом файле тишина", "error")
            return False
        top = float(abs(pcm).max()) or 1.0
        pcm = pcm / top * 0.7
        folder = Path(tempfile.mkdtemp(prefix="marincall-sound-"))
        try:
            out = folder / "sound.ogg"
            audio.encode_ogg(pcm, out, bitrate=48000)
            meta = self.import_file(out)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if not meta:
            return False
        self._publish("sound", file=meta["id"], name=name.strip()[:32] or "Звук",
                      emoji=emoji.strip()[:16], target="")
        return True

    def remove_sound(self, sid):
        if sid in self.store.sounds:
            self._publish("sound", file="", name="", emoji="", target=sid)

    def delete_message(self, mid):
        if author_of(mid) == self.me:
            self._publish("del", store=self.store_of_msg(mid), target=mid)

    def toggle_reaction(self, mid, emoji):
        st = self.store_of_msg(mid)
        on = self.me not in st.reactions_of(mid).get(emoji, [])
        self._publish("react", store=st, target=mid, emoji=emoji, on=on)

    def send_typing(self, cid):
        now = time.monotonic()
        if now - self._last_typing > 3:
            self._last_typing = now
            peer = self.dm_peer.get(cid)
            if peer:                       # who writes to whom stays between the two of them
                self.mesh.send(peer, {"t": "typing", "ch": cid})
            else:
                self.mesh.broadcast({"t": "typing", "ch": cid})

    def typers(self, cid):
        now = time.monotonic()
        return [u for u, exp in self.typing.get(cid, {}).items() if exp > now]

    def _tick(self):
        self._idle_check += 1
        if self._idle_check >= 5:                # every 5 s: away from the keyboard?
            self._idle_check = 0
            idle = idle_seconds() > IDLE_AFTER
            if idle != self._auto_idle:
                self._auto_idle = idle
                self._broadcast_state()
                self.members_changed.emit()
        self._mail_clock += 1
        if self._mail_clock >= 3600:
            self._mail_clock = 0
            self.mail.expire()
            self._drop_unused_blobs()
            self._mail_changed()
        now = time.monotonic()
        for cid, users in list(self.typing.items()):
            if any(exp <= now for exp in users.values()):
                self.typing[cid] = {u: e for u, e in users.items() if e > now}
                self.typing_changed.emit(cid)

    # ── channels ────────────────────────────────────────────────────
    def create_channel(self, kind, name):
        name = text_channel_name(name) if kind == "text" else " ".join(name.split())[:32]
        if name:
            return self._publish("ch_new", kind=kind, name=name)

    def rename_channel(self, cid, name, topic=None):
        ch = self.store.channel(cid)
        if ch:
            self._publish("ch_ren", target=cid, name=name or ch["name"],
                          topic=ch["topic"] if topic is None else topic)

    def delete_channel(self, cid):
        if self.store.channel(cid):
            self._publish("ch_del", target=cid)

    # ── voice ───────────────────────────────────────────────────────
    def voice_members(self, cid):
        uids = [u for u, st in self.states.items() if st.get("voice") == cid and u in self.online()]
        if self.my_voice == cid:
            uids.append(self.me)
        return sorted(uids, key=lambda u: self.name_of(u).lower())

    def join_voice(self, cid):
        if self.my_voice == cid:
            return
        if self.my_voice:
            self.leave_voice(quiet=True)
        self.my_voice = cid
        self.s["last_voice"] = cid
        self.s.save()
        self.voice.start_input()
        self.voice.play("join")
        self._broadcast_state()

    def leave_voice(self, quiet=False):
        if not self.my_voice:
            return
        self.stop_stream()
        self.stop_camera()
        self.unwatch()
        self.my_voice = None
        self.voice.stop_input()
        if not quiet:
            self.voice.play("leave")
        self._broadcast_state()

    def toggle_mute(self):
        if self.s["deafened"]:
            self.s["deafened"] = False
            self.s["muted"] = False
        else:
            self.s["muted"] = not self.s["muted"]
        self.s.save()
        self.voice.play("mute" if self.s["muted"] else "unmute")
        self._broadcast_state()

    def toggle_deafen(self):
        self.s["deafened"] = not self.s["deafened"]
        self.s.save()
        self.voice.play("mute" if self.s["deafened"] else "unmute")
        self._broadcast_state()

    def _update_voice_peers(self):
        self._update_cameras()
        if not self.my_voice:
            self.voice.set_peers([])
            return
        connected = self.mesh.peers_map
        self.voice.set_peers([u for u, st in self.states.items()
                              if st.get("voice") == self.my_voice and u in connected])

    # ── cameras ─────────────────────────────────────────────────────
    def _update_cameras(self):
        """Receive the camera of everyone in my voice channel who has it on; show mine too."""
        want = set()
        if self.my_voice:
            connected = self.mesh.peers_map
            want = {u for u, st in self.states.items()
                    if st.get("voice") == self.my_voice and st.get("camera") and u in connected}
        for uid in want - self._cam_subs:
            self.mesh.send(uid, {"t": "cam", "on": True})
        for uid in self._cam_subs - want:
            self.mesh.send(uid, {"t": "cam", "on": False})
        self._cam_subs = want
        self.cameras.set_sources(want | ({self.me} if self.camera.running else set()))

    def _cam_watcher_uids(self):
        connected = self.mesh.peers_map
        return [u for u in self.cam_watchers if u in connected and
                self.states.get(u, {}).get("voice") == self.my_voice and self.my_voice]

    def camera_on(self):
        return self.camera.running

    def toggle_camera(self):
        if self.camera.running:
            self.stop_camera()
        else:
            self.start_camera()

    def start_camera(self):
        if not self.my_voice:
            self.toast.emit("Сначала зайдите в голосовой канал", "error")
            return
        device = self.s.get("camera_device") or ""
        if device == TEST_CAMERA:
            source = {"kind": "camera_test", "label": "Тестовая камера"}
        else:
            cams = list_cameras()
            if device not in cams:
                device = cams[0] if cams else ""
            if not device:
                self.toast.emit("Камера не найдена — подключите веб-камеру", "error")
                return
            source = {"kind": "camera", "device": device, "label": device}
        if self.camera.start(source, CAMERA, self.s["stream_encoder"], ""):
            self.camera.set_viewers(self._cam_watcher_uids())
        self._broadcast_state()

    def stop_camera(self):
        if self.camera.stop():
            self.cam_watchers.clear()
            self._broadcast_state()

    def _on_camera_stopped(self, reason):
        if reason:
            self.toast.emit(reason.replace("Трансляция прервалась", "Камера отключилась"), "error")
        self.cam_watchers.clear()
        self._broadcast_state()

    def _on_local_speaking(self, on):
        self.mesh.broadcast({"t": "spk", "on": on})
        self.speaking_changed.emit(self.me, on)

    def is_speaking(self, uid):
        if uid == self.me:
            return self.voice.gate
        return bool(self.states.get(uid, {}).get("speaking"))

    # ── screen share ────────────────────────────────────────────────
    def start_stream(self, source):
        if not self.my_voice:
            self.toast.emit("Сначала зайдите в голосовой канал", "error")
            return
        ok = self.sender.start(source, self.s["stream_quality"], self.s["stream_encoder"],
                               self.s["stream_audio"])
        if ok:
            self.sender.set_viewers(self._watcher_uids())
            self.stream_info = f"{source['label']} · {self.sender.encoder_label}"
            self.preview.start(source)
            self.voice.play("stream")
        self._broadcast_state()
        self.stream_changed.emit()

    def stop_stream(self):
        if self.sender.stop():
            self.preview.stop()
            self.watchers.clear()
            self._broadcast_state()
            self.stream_changed.emit()

    def _on_stream_stopped(self, reason):
        if reason:
            self.toast.emit(reason, "error")
        self.preview.stop()
        self.watchers.clear()
        self._broadcast_state()
        self.stream_changed.emit()

    def watcher_names(self, limit=3):
        """«Борис, Вика и ещё 2» — who watches my screen share right now."""
        names = sorted(self.name_of(u) for u in self._watcher_uids())
        if not names:
            return ""
        shown = ", ".join(names[:limit])
        return shown + (f" и ещё {len(names) - limit}" if len(names) > limit else "")

    def _watcher_uids(self):
        return [u for u in self.watchers if u in self.mesh.peers_map]

    def watch(self, uid):
        if self.viewer.uid:
            self.unwatch()
        self.viewer.watch(uid)
        self.mesh.send(uid, {"t": "watch", "on": True})
        self.stream_changed.emit()

    def unwatch(self):
        uid = self.viewer.uid
        if uid:
            self.viewer.stop()
            self.mesh.send(uid, {"t": "watch", "on": False})
            self.stream_changed.emit()

    def _on_viewer_closed(self, uid):
        """The stream stopped by itself (it could not be decoded, or the data ended)."""
        if self.viewer.uid == uid:
            self.viewer.stop()
            self.mesh.send(uid, {"t": "watch", "on": False})
            if self.states.get(uid, {}).get("streaming"):
                self.toast.emit(f"Не удалось показать трансляцию {self.name_of(uid)}", "error")
            self.stream_changed.emit()

    # ── files ───────────────────────────────────────────────────────
    @staticmethod
    def file_path(fid):
        p = FILES_DIR / fid
        return p if p.exists() else None

    def _kept_files(self):
        """Files the cache cleanup must not touch: what I sent (I may be the only one who has
        it), avatars and soundboard sounds (small, shown everywhere)."""
        keep = {self.s.get("avatar") or ""}
        for st in (self.store, *list(self.dms.values())):      # may run off the UI thread
            keep.update(v[2] for v in list(st.avatars.values()))
            keep.update(getattr(st, "sound_files", lambda: ())())
            for msgs in list(st.messages.values()):
                for m in list(msgs):
                    if m["author"] in (self.me, self.s.get("legacy_uid")):
                        keep.update(f["id"] for f in m["files"])
            for mid, per in list(st.embeds.items()):         # pictures of my link cards
                if author_of(mid) == self.me:
                    keep.update(card[2]["image"] for card in list(per.values()))
        keep.update(self.mail.blob_ids())                  # sealed mail files
        keep.discard("")
        return keep

    def cache_size(self):
        """(bytes the cleanup would free, bytes kept) in the file cache."""
        keep = self._kept_files()
        free = kept = 0
        for p in FILES_DIR.iterdir() if FILES_DIR.exists() else ():
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if p.name in keep:
                kept += size
            elif p.name not in self.downloads and f"{p.stem}" not in self.downloads:
                free += size
        return free, kept

    def clear_cache(self):
        """Delete other people's files; they come back from the room when you open them."""
        keep = self._kept_files()
        freed = 0
        for p in list(FILES_DIR.iterdir()) if FILES_DIR.exists() else ():
            if p.name in keep or p.stem in self.downloads:
                continue
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
            except OSError:
                pass
        tmp = Path(tempfile.gettempdir()) / "MarinCall"        # copies made to open files
        shutil.rmtree(tmp, ignore_errors=True)
        self.cache_cleared.emit()
        return freed

    def import_file(self, path):
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_FILE:
            self.toast.emit(f"«{path.name}» больше 25 МБ", "error")
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        fid = h.hexdigest()[:32]
        dest = FILES_DIR / fid
        if not dest.exists():
            shutil.copyfile(path, dest)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"id": fid, "name": path.name[:120], "size": size, "type": ctype}

    def request_file(self, fid, prefer=None, only=None):
        """only: a file from a direct message is asked of that person alone."""
        if not re.fullmatch(r"[0-9a-f]{32}", fid) or self.file_path(fid) or fid in self.downloads:
            return
        if only:
            self._private_files[fid] = only
        only = only or self._private_files.get(fid)
        asked = self.wanted.setdefault(fid, set())
        peers = [u for u in self.mesh.peers() if u not in asked and (only is None or u == only)]
        if prefer in peers:
            peers.remove(prefer)
            peers.insert(0, prefer)
        if peers:
            asked.add(peers[0])
            self.mesh.send(peers[0], {"t": "file_get", "id": fid})

    def _serve_file(self, uid, fid):
        path = self.file_path(fid) if re.fullmatch(r"[0-9a-f]{32}", fid) else None
        if not path:
            self.mesh.send(uid, {"t": "file_missing", "id": fid})
            return

        def run():
            size = path.stat().st_size
            with open(path, "rb") as f:
                off = 0
                while True:
                    chunk = f.read(FILE_CHUNK)
                    self.mesh.send(uid, {"t": "file", "id": fid, "off": off, "size": size,
                                         "data": base64.b64encode(chunk).decode(),
                                         "end": off + len(chunk) >= size})
                    off += len(chunk)
                    if off >= size:
                        break

        threading.Thread(target=run, daemon=True).start()

    def _receive_chunk(self, uid, msg):
        fid = str(msg.get("id", ""))
        if fid not in self.wanted or not re.fullmatch(r"[0-9a-f]{32}", fid):
            return
        size = int(msg.get("size") or 0)
        if size > MAX_FILE:
            return
        dl = self.downloads.setdefault(fid, {"path": FILES_DIR / f"{fid}.part", "got": 0})
        if int(msg.get("off", -1)) != dl["got"]:
            return  # out of order — a second sender; ignore
        data = base64.b64decode(msg.get("data") or "")
        with open(dl["path"], "ab" if dl["got"] else "wb") as f:
            f.write(data)
        dl["got"] += len(data)
        if msg.get("end"):
            self.downloads.pop(fid, None)
            with open(dl["path"], "rb") as f:
                ok = hashlib.sha256(f.read()).hexdigest()[:32] == fid
            if ok:
                dl["path"].replace(FILES_DIR / fid)
                self.wanted.pop(fid, None)
                self.file_ready.emit(fid)
                if fid in self.mail.blobs:
                    self._unseal(fid)
                if fid in self._avatar_files:          # someone's picture arrived
                    self._avatar_files.pop(fid)
                    self.members_changed.emit()
            else:
                dl["path"].unlink(missing_ok=True)
                self.request_file(fid)
