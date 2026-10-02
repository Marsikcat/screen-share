"""
Mail for people who are offline: direct messages delivered through the rest of the room.

Writing to someone who is not online, the app seals the new events of the conversation for
them: encrypted to their key (X25519 derived from their ed25519 identity, a libsodium sealed
box, so even the sender is not visible from outside) and hands the envelope to everyone in
the room who is online. Whoever meets the recipient first passes it on. The relays see only
whom it is for, when, and how big — never who wrote it or what. Files go the same way:
encrypted with a random key that travels inside the sealed part, and stored and passed on
like any shared file under the id of their ciphertext.

When the recipient has opened an envelope they publish a receipt signed with their key; it
travels the same way and every relay drops its copy. Envelopes nobody collects are dropped
after TTL.
"""

import base64
import hashlib
import json
import re
import time

import nacl.exceptions
import nacl.public
import nacl.secret
import nacl.signing
import nacl.utils

TTL_MS = 30 * 24 * 3600 * 1000          # envelopes and receipts are forgotten after a month
MAX_BOX = 512 * 1024                    # bytes of one sealed envelope (text, not files)
MAX_TOTAL = 16 * 1024 * 1024            # all envelopes kept for others
MAX_PER_RECIPIENT = 2000
MAX_BLOBS = 300 * 1024 * 1024           # encrypted files kept for others
MAX_BLOB_FILE = 25 * 1024 * 1024 - 64   # a file that still fits the transfer limit sealed
RECEIPT_TAG = b"marincall-mail-receipt:"  # signatures here can never be mistaken for events
UID_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def now_ms():
    return int(time.time() * 1000)


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


# ── keys ────────────────────────────────────────────────────────────
class Keys:
    """Our identity's encryption half: the same 32-byte seed as the signing key."""

    def __init__(self, seed_hex):
        signing = nacl.signing.SigningKey(bytes.fromhex(seed_hex))
        self._secret = signing.to_curve25519_private_key()
        self._box = nacl.public.SealedBox(self._secret)

    @staticmethod
    def seal(to_uid, data):
        public = nacl.signing.VerifyKey(bytes.fromhex(to_uid)).to_curve25519_public_key()
        return nacl.public.SealedBox(public).encrypt(data)

    def open(self, box):
        try:
            return self._box.decrypt(box)
        except (nacl.exceptions.CryptoError, ValueError, TypeError):
            return None


def seal_file(data):
    """(key, ciphertext) — a fresh random key for every file."""
    key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
    return key, bytes(nacl.secret.SecretBox(key).encrypt(data))


def open_file(key, ciphertext):
    try:
        return nacl.secret.SecretBox(key).decrypt(ciphertext)
    except (nacl.exceptions.CryptoError, ValueError, TypeError):
        return None


def blob_id(ciphertext):
    return hashlib.sha256(ciphertext).hexdigest()[:32]


# ── envelopes and receipts ──────────────────────────────────────────
def make_envelope(to_uid, box, blobs=()):
    return {"id": hashlib.sha256(box).hexdigest()[:32], "to": to_uid, "ts": now_ms(),
            "box": base64.b64encode(box).decode(), "blobs": list(blobs)}


def validate(env):
    """A normalised envelope from the network, or None."""
    if not isinstance(env, dict):
        return None
    to, ts, box, blobs = env.get("to"), env.get("ts"), env.get("box"), env.get("blobs") or []
    if not (isinstance(to, str) and UID_RE.match(to) and isinstance(ts, int) and isinstance(box, str)):
        return None
    if len(box) > MAX_BOX * 4 // 3 + 4 or not isinstance(blobs, list) or len(blobs) > 20:
        return None
    if not all(isinstance(b, str) and ID_RE.match(b) for b in blobs):
        return None
    try:
        raw = base64.b64decode(box, validate=True)
    except ValueError:
        return None
    if hashlib.sha256(raw).hexdigest()[:32] != env.get("id"):
        return None
    if ts > now_ms() + 24 * 3600 * 1000 or now_ms() - ts > TTL_MS:
        return None
    return {"id": env["id"], "to": to, "ts": ts, "box": box, "blobs": blobs}


def make_receipt(me, ids, sign):
    body = {"by": me, "ts": now_ms(), "ids": sorted(set(ids))}
    return {**body, "sig": sign(RECEIPT_TAG + _canonical(body)).hex()}


def check_receipt(rec, verify):
    if not isinstance(rec, dict):
        return None
    by, ts, ids, sig = rec.get("by"), rec.get("ts"), rec.get("ids"), rec.get("sig")
    if not (isinstance(by, str) and UID_RE.match(by) and isinstance(ts, int) and isinstance(ids, list)
            and 0 < len(ids) <= 500 and all(isinstance(i, str) and ID_RE.match(i) for i in ids)
            and isinstance(sig, str) and re.fullmatch(r"[0-9a-f]{128}", sig)):
        return None
    if now_ms() - ts > TTL_MS:
        return None
    body = {"by": by, "ts": ts, "ids": sorted(set(ids))}
    if not verify(by, RECEIPT_TAG + _canonical(body), bytes.fromhex(sig)):
        return None
    return {**body, "sig": sig}


class Mailbox:
    """Envelopes we keep (for others, and our own until they are collected), receipts, and the
    files we still have to decrypt. Persisted as one JSON file."""

    def __init__(self, path):
        self.path = path
        self.env = {}            # id -> envelope
        self.own = {}            # id -> {"to", "seqs"}: envelopes we wrote, for «Доставлено»
        self.done = {}           # envelope id -> {who signed a receipt for it: ts}
        self.receipts = []       # signed receipts, passed on to everyone
        self.incoming = {}       # file id -> {"blob", "key", "from"}: our files still sealed
        self.blobs = set()       # sealed files on our disk (ours, or kept for others)
        self._keys = set()       # (by, ts, ids) of the receipts we have: a quick «seen it»
        self._load()

    # ── persistence ─────────────────────────────────────────────────
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for env in data.get("env", []):
            env = validate(env)
            if env:
                self.env[env["id"]] = env
        self.own = {k: v for k, v in (data.get("own") or {}).items() if k in self.env}
        self.receipts = [r for r in data.get("receipts", []) if isinstance(r, dict)]
        for r in self.receipts:
            self._keys.add(self._key(r))
            for i in r.get("ids", []):
                self.done.setdefault(i, {})[r.get("by")] = r.get("ts", 0)
        self.incoming = {k: v for k, v in (data.get("incoming") or {}).items()
                         if ID_RE.match(k) and isinstance(v, dict)}
        self.blobs = {b for b in data.get("blobs", []) if isinstance(b, str) and ID_RE.match(b)}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"env": list(self.env.values()), "own": self.own,
                                   "receipts": self.receipts, "incoming": self.incoming,
                                   "blobs": sorted(self.blobs)},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ── envelopes ───────────────────────────────────────────────────
    def size(self):
        return sum(len(e["box"]) for e in self.env.values())

    def add(self, env, own_seqs=None):
        """Keep an envelope; False if we have it, it was collected already, or it does not fit."""
        if env["id"] in self.env or self.collected(env):
            return False
        if own_seqs is None:
            if sum(1 for e in self.env.values() if e["to"] == env["to"]) >= MAX_PER_RECIPIENT:
                return False
            while self.size() + len(env["box"]) > MAX_TOTAL:
                victim = min((e for e in self.env.values() if e["id"] not in self.own),
                             key=lambda e: e["ts"], default=None)
                if victim is None:
                    return False
                self.env.pop(victim["id"])           # the oldest one held for someone else
        self.env[env["id"]] = env
        if own_seqs is not None:
            self.own[env["id"]] = {"to": env["to"], "seqs": list(own_seqs)}
        return True

    def collected(self, env):
        """A receipt from its recipient says so (a receipt from anyone else does not count)."""
        return env["to"] in self.done.get(env["id"], {})

    def for_uid(self, uid):
        return [e for e in self.env.values() if e["to"] == uid]

    def ids_except(self, uid):
        return [i for i, e in self.env.items() if e["to"] != uid]

    def blob_ids(self):
        out = {b for e in self.env.values() for b in e["blobs"]}
        out.update(v["blob"] for v in self.incoming.values())
        return out

    # ── receipts ────────────────────────────────────────────────────
    @staticmethod
    def _key(rec):
        return rec.get("by"), rec.get("ts"), tuple(sorted(set(rec.get("ids") or [])))

    def has_receipt(self, rec):
        try:
            return self._key(rec) in self._keys
        except (AttributeError, TypeError):
            return False

    def receipt_of(self, me, env_id):
        """A receipt we already signed for this envelope."""
        return next((r for r in self.receipts if r["by"] == me and env_id in r["ids"]), None)

    def add_receipt(self, rec):
        """A checked receipt: drop what it covers. Returns ([own envelopes delivered], new?)."""
        key = self._key(rec)
        if key in self._keys:
            return [], False
        self._keys.add(key)
        self.receipts.append(rec)
        delivered = []
        for i in rec["ids"]:
            env = self.env.get(i)
            if env is not None and env["to"] != rec["by"]:
                continue                     # nobody can collect someone else's mail
            self.done.setdefault(i, {})[rec["by"]] = rec["ts"]
            if env is not None:
                self.env.pop(i)
                own = self.own.pop(i, None)
                if own:
                    delivered.append(own)
        return delivered, True

    def expire(self):
        cutoff = now_ms() - TTL_MS
        for i in [i for i, e in self.env.items() if e["ts"] < cutoff]:
            self.env.pop(i)
            self.own.pop(i, None)
        self.receipts = [r for r in self.receipts if r["ts"] >= cutoff]
        self._keys = {self._key(r) for r in self.receipts}
        self.done = {i: {by: ts for by, ts in v.items() if (ts or 0) >= cutoff}
                     for i, v in self.done.items()}
        self.done = {i: v for i, v in self.done.items() if v}
