"""Core: glues the store, the mesh, voice and screen share together for the UI."""

import base64
import hashlib
import mimetypes
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

import iroh
from PySide6.QtCore import QObject, QTimer, Signal

from .config import FILES_DIR, MAX_FILE, VERSION
from .net import Mesh, make_invite, parse_invite
from .store import UID_RE, Store, author_of, text_channel_name
from .stream import SourcePreview, StreamSender, StreamViewer
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
    room_changed = Signal()
    read_changed = Signal()
    stream_changed = Signal()
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
        self.viewer.stop()
        self.voice.shutdown()
        self.mesh.stop()

    # ── identity & presence ─────────────────────────────────────────
    def _my_state(self):
        return {"voice": self.my_voice, "muted": bool(self.s["muted"]),
                "deafened": bool(self.s["deafened"]), "streaming": self.sender.running,
                "speaking": self.voice.gate}

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
        return {"uid": uid, "name": name, "color": color, "online": uid in self.online(),
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
        if ev.get("k") not in ("msg", "edit", "del", "react", "pin"):
            return False
        return ev.get("k") != "msg" or ev.get("ch") == self.dm_cid(uid)

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
        self.members_changed.emit()

    def _on_peer_down(self, uid):
        self._set_state(uid, None)
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
        elif t == "dm_vv" and isinstance(msg.get("v"), dict):
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

    # ── events → UI ─────────────────────────────────────────────────
    def _publish(self, event_kind, store=None, **payload):
        store = store or self.store
        ev = store.create(event_kind, **payload)
        if ev:
            if store is self.store:
                self.mesh.broadcast({"t": "ev", "e": [ev]})
            else:                      # a conversation: to that one person, never gossiped
                peer = next(u for u, st in self.dms.items() if st is store)
                self.mesh.send(peer, {"t": "dm_ev", "e": [ev]})
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

    def mentions_me(self, text):
        name = re.escape(self.s["name"])
        return bool(name and re.search(rf"@({name}|все|everyone)(?!\w)", text, re.IGNORECASE))

    def _maybe_notify(self, msg, store):
        text = store.text_of(msg)
        personal = msg["ch"].startswith("dm:")          # a direct message counts as a mention
        if self.s["notify_mentions_only"] and not (personal or self.mentions_me(text)):
            return
        body = text or "📎 " + ", ".join(f["name"] for f in msg["files"])
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
            self._publish("msg", store=self.store_for(cid), ch=cid, text=text, reply=reply, files=metas)
            self.mark_read(cid)

    def edit_message(self, mid, text):
        if author_of(mid) == self.me:
            self._publish("edit", store=self.store_of_msg(mid), target=mid, text=text.strip())

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
        if not self.my_voice:
            self.voice.set_peers([])
            return
        connected = self.mesh.peers_map
        self.voice.set_peers([u for u, st in self.states.items()
                              if st.get("voice") == self.my_voice and u in connected])

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
                if fid in self._avatar_files:          # someone's picture arrived
                    self._avatar_files.pop(fid)
                    self.members_changed.emit()
            else:
                dl["path"].unlink(missing_ok=True)
                self.request_file(fid)
