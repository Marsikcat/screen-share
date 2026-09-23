"""
Serverless chat state.

Every change (message, edit, reaction, new channel…) is an event in its
author's append-only log: id "<uid>:<seq>". Peers replicate logs by
exchanging version vectors ({author: highest contiguous seq}) and sending
what the other side lacks, so anyone who was offline catches up from
whoever is online. State is rebuilt from events with last-writer-wins rules,
so every peer converges to the same result regardless of arrival order.

Authors are ed25519 public keys and every event carries the author's
signature, so a peer relaying history cannot forge or alter someone else's
messages. History from 2.x (unsigned, short ids) is kept as a local,
read-only archive and is not replicated.
"""

import bisect
import json
import re
import time

from .config import MAX_TEXT

UID_RE = re.compile(r"^[0-9a-f]{64}$")           # ed25519 public key
LEGACY_UID_RE = re.compile(r"^[0-9a-f]{24}$")    # 2.x random ids
SIG_RE = re.compile(r"^[0-9a-f]{128}$")
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KINDS = {"msg", "edit", "del", "react", "ch_new", "ch_ren", "ch_del", "profile", "room"}

# Fixed ids so every peer has the same starter channels without coordination.
DEFAULT_CHANNELS = [
    ("d:text:general", "text", "общий", "Болтаем обо всём"),
    ("d:text:media", "text", "медиа", "Картинки, видео, ссылки"),
    ("d:voice:general", "voice", "Общий", ""),
    ("d:voice:games", "voice", "Игровой", ""),
]


def now_ms():
    return int(time.time() * 1000)


def author_of(event_id):
    return str(event_id).split(":", 1)[0]


def clean(value, limit):
    return str(value or "").strip()[:limit]


def text_channel_name(value):
    value = " ".join(str(value or "").split()).lower().replace(" ", "-")
    return re.sub(r"-{2,}", "-", value).strip("-")[:32]


def canonical(ev):
    """The exact bytes that get signed: the normalised event without its signature."""
    body = {k: v for k, v in ev.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def validate(ev, legacy=False, need_sig=True):
    """Normalise an event; None if it is malformed. Signatures are checked by Store.add."""
    if not isinstance(ev, dict):
        return None
    a, s, k = str(ev.get("a", "")), ev.get("s"), ev.get("k")
    if not (LEGACY_UID_RE if legacy else UID_RE).match(a) or not isinstance(s, int) or s < 1             or k not in KINDS:
        return None
    if ev.get("id") != f"{a}:{s}" or not isinstance(ev.get("ts"), int):
        return None
    out = {"id": ev["id"], "a": a, "s": s, "k": k, "ts": ev["ts"]}
    if not legacy and need_sig:
        if not SIG_RE.match(str(ev.get("sig", ""))):
            return None
        out["sig"] = ev["sig"]
    if k == "msg":
        out["ch"] = clean(ev.get("ch"), 64)
        out["text"] = clean(ev.get("text"), MAX_TEXT)
        out["reply"] = clean(ev.get("reply"), 64) or None
        files = []
        for f in (ev.get("files") or [])[:10]:
            if isinstance(f, dict) and FILE_ID_RE.match(str(f.get("id", ""))):
                files.append({"id": f["id"], "name": clean(f.get("name"), 120) or "file",
                              "size": int(f.get("size") or 0), "type": clean(f.get("type"), 100)})
        out["files"] = files
        if not out["ch"] or not (out["text"] or files):
            return None
    elif k in ("edit", "del", "react", "ch_ren", "ch_del"):
        out["target"] = clean(ev.get("target"), 64)
        if not out["target"]:
            return None
        if k == "edit":
            out["text"] = clean(ev.get("text"), MAX_TEXT)
        elif k == "react":
            out["emoji"] = clean(ev.get("emoji"), 16)
            out["on"] = bool(ev.get("on"))
            if not out["emoji"]:
                return None
        elif k == "ch_ren":
            out["name"] = clean(ev.get("name"), 32)
            out["topic"] = clean(ev.get("topic"), 200)
    elif k == "ch_new":
        out["kind"] = "voice" if ev.get("kind") == "voice" else "text"
        out["name"] = clean(ev.get("name"), 32)
        if not out["name"]:
            return None
    elif k == "profile":
        out["name"] = clean(ev.get("name"), 32)
        out["color"] = clean(ev.get("color"), 7)
    elif k == "room":
        out["name"] = clean(ev.get("name"), 48)
    return out


class Store:
    def __init__(self, room_dir, me, sign, verify):
        """sign(bytes) -> 64-byte signature; verify(author_hex, bytes, sig_bytes) -> bool."""
        self.me = me
        self.sign, self.verify = sign, verify
        self.log_path = room_dir / "events3.jsonl"
        self.legacy_path = room_dir / "events.jsonl"      # 2.x history
        self.events = {}
        self.seqs = {}              # author -> set of seqs we hold
        self.channels = {}
        self.messages = {}          # channel id -> [msg] sorted by key
        self.msg_by_id = {}
        self.edits = {}             # msg id -> (ts, event id, text)
        self.deleted = set()
        self.reactions = {}         # msg id -> {emoji: {uid: (ts, event id, on)}}
        self.profiles = {}          # uid -> {name, color, _v}
        self.room_name = (0, "", "")
        for i, (cid, kind, name, topic) in enumerate(DEFAULT_CHANNELS):
            self.channels[cid] = {"id": cid, "kind": kind, "name": name, "topic": topic,
                                  "order": i, "deleted": False, "_v": (0, "")}
        self._load()

    # ── persistence ─────────────────────────────────────────────────
    def _load(self):
        # our own disk: signatures were verified when the events arrived
        for path, legacy in ((self.legacy_path, True), (self.log_path, False)):
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = validate(json.loads(line), legacy=legacy)
                    except ValueError:
                        continue
                    if ev and ev["id"] not in self.events:
                        self._apply(ev)

    def _append(self, ev):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    # ── replication ─────────────────────────────────────────────────
    def vector(self):
        """Highest seq per author with no gaps below it."""
        vec = {}
        for author, seqs in self.seqs.items():
            if not UID_RE.match(author):
                continue                     # 2.x archive is local only
            n = 0
            while n + 1 in seqs:
                n += 1
            vec[author] = n
        return vec

    def missing_for(self, vec):
        out = []
        for ev in self.events.values():
            if "sig" in ev and ev["s"] > int(vec.get(ev["a"], 0) or 0):
                out.append(ev)
        out.sort(key=lambda e: (e["a"], e["s"]))
        return out

    def add(self, ev):
        """Store an event from the network. Returns the normalised event if it was new and
        genuinely signed by its author."""
        ev = validate(ev)
        if not ev or ev["id"] in self.events:
            return None
        if not self.verify(ev["a"], canonical(ev), bytes.fromhex(ev["sig"])):
            return None
        self._apply(ev)
        self._append(ev)
        return ev

    def create(self, event_kind, **payload):
        seq = max(self.seqs.get(self.me, {0})) + 1
        ev = validate({"id": f"{self.me}:{seq}", "a": self.me, "s": seq, "k": event_kind,
                       "ts": now_ms(), **payload}, need_sig=False)
        if not ev:
            return None
        ev["sig"] = self.sign(canonical(ev)).hex()
        return self.add(ev)

    # ── state rebuild ───────────────────────────────────────────────
    def _apply(self, ev):
        self.events[ev["id"]] = ev
        self.seqs.setdefault(ev["a"], set()).add(ev["s"])
        k, v = ev["k"], (ev["ts"], ev["id"])
        if k == "msg":
            msg = {"id": ev["id"], "author": ev["a"], "ch": ev["ch"], "ts": ev["ts"],
                   "text": ev["text"], "reply": ev["reply"], "files": ev["files"]}
            bisect.insort(self.messages.setdefault(ev["ch"], []), msg, key=self.sort_key)
            self.msg_by_id[msg["id"]] = msg
        elif k == "edit":
            if author_of(ev["target"]) == ev["a"]:
                cur = self.edits.get(ev["target"])
                if cur is None or v > cur[:2]:
                    self.edits[ev["target"]] = (*v, ev["text"])
        elif k == "del":
            if author_of(ev["target"]) == ev["a"]:
                self.deleted.add(ev["target"])
        elif k == "react":
            per = self.reactions.setdefault(ev["target"], {}).setdefault(ev["emoji"], {})
            cur = per.get(ev["a"])
            if cur is None or v > cur[:2]:
                per[ev["a"]] = (*v, ev["on"])
        elif k == "ch_new":
            name = text_channel_name(ev["name"]) if ev["kind"] == "text" else ev["name"]
            self.channels[ev["id"]] = {"id": ev["id"], "kind": ev["kind"], "name": name or "канал",
                                       "topic": "", "order": ev["ts"], "deleted": False, "_v": v}
        elif k == "ch_ren":
            ch = self.channels.get(ev["target"])
            if ch and v > ch["_v"]:
                name = text_channel_name(ev["name"]) if ch["kind"] == "text" else ev["name"]
                ch["name"] = name or ch["name"]
                ch["topic"] = ev.get("topic", ch["topic"])
                ch["_v"] = v
        elif k == "ch_del":
            ch = self.channels.get(ev["target"])
            if ch:
                ch["deleted"] = True
        elif k == "profile":
            cur = self.profiles.get(ev["a"])
            if cur is None or v > cur["_v"]:
                self.profiles[ev["a"]] = {"name": ev["name"], "color": ev["color"], "_v": v}
        elif k == "room":
            if v > self.room_name[:2]:
                self.room_name = (*v, ev["name"])

    # ── queries ─────────────────────────────────────────────────────
    @staticmethod
    def sort_key(msg):
        return (msg["ts"], msg["id"])

    def channel_list(self, kind):
        chans = [c for c in self.channels.values() if c["kind"] == kind and not c["deleted"]]
        return sorted(chans, key=lambda c: (c["order"], c["id"]))

    def channel(self, cid):
        ch = self.channels.get(cid)
        return ch if ch and not ch["deleted"] else None

    def visible_messages(self, cid):
        return [m for m in self.messages.get(cid, []) if m["id"] not in self.deleted]

    def text_of(self, msg):
        edit = self.edits.get(msg["id"])
        return edit[2] if edit else msg["text"]

    def is_edited(self, msg):
        return msg["id"] in self.edits

    def reactions_of(self, mid):
        out = {}
        for emoji, users in self.reactions.get(mid, {}).items():
            on = [uid for uid, st in users.items() if st[2]]
            if on:
                out[emoji] = on
        return out

    def display_room_name(self, fallback):
        return self.room_name[2] or fallback
