"""
Networking on iroh (Rust QUIC): peers are dialled by public key, not by IP.

- Identity is our ed25519 key; the QUIC/TLS handshake authenticates every peer
  and encrypts everything on the wire.
- Finding peers: LAN broadcast (Radmin VPN carries it too), invite codes, and
  remembered peers (relay URL + addresses) for the internet. iroh punches
  through NAT and, when it can't, relays the (still end-to-end encrypted)
  traffic through n0's public relays.
- One connection per peer: a bidirectional stream of JSON lines (chat sync,
  presence, signalling), datagrams for voice, one-way streams for screen share.
- Before anything else both sides prove they know the room secret with an
  HMAC bound to both public keys.
"""

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time

import iroh
from PySide6.QtCore import QObject, Signal

from .config import DISCOVERY_PORT, PEER_PORT, PROTOCOL
from .store import UID_RE

APP_ID = "moydiscord"
ALPN = b"moydiscord/3"
MAX_LINE = 4 * 1024 * 1024
DGRAM_VOICE = 1
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IPV4 = r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=5, creationflags=NO_WINDOW).stdout
        return out.decode("cp866", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def interfaces():
    """[(ip, broadcast)] for every IPv4 adapter, Radmin VPN (26.x) first."""
    res = []
    text = _run(["ipconfig"])
    for m in re.finditer(r"IPv4[^:\n]*:\s*" + IPV4 + r"[^\n]*\n[^\n:]*:\s*" + IPV4, text):
        ip, mask = m.group(1), m.group(2)
        if ip.startswith(("127.", "169.254.")):
            continue
        try:
            bcast = str(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False).broadcast_address)
        except ValueError:
            bcast = None
        res.append((ip, bcast))
    return sorted(res, key=lambda x: (not x[0].startswith("26."), x[0]))


def local_ips():
    return [ip for ip, _ in interfaces()]


def radmin_neighbours():
    return list(dict.fromkeys(re.findall(r"\b(26\.\d+\.\d+\.\d+)\b", _run(["arp", "-a"]))))


# ── invites ─────────────────────────────────────────────────────────
def make_invite(room, secret, peer):
    """peer = {"id", "relay", "addrs", "name"} of whoever hands out the code."""
    data = {"v": PROTOCOL, "room": room, "secret": secret.hex(), "peer": peer}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return "moyd:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_invite(code):
    code = "".join(str(code).split())
    if not code.startswith("moyd:"):
        raise ValueError("это не код приглашения МойДискорд")
    body = code[5:]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        peer = data["peer"]
        if not UID_RE.match(peer["id"]) or len(data["secret"]) != 64:
            raise ValueError
        return {"room": str(data["room"])[:32], "secret": data["secret"],
                "peer": {"id": peer["id"], "relay": peer.get("relay"),
                         "addrs": [str(a) for a in peer.get("addrs") or []][:8],
                         "name": str(peer.get("name", ""))[:32]}}
    except (ValueError, KeyError, TypeError):
        raise ValueError("код приглашения повреждён — скопируйте его целиком") from None


class LineReader:
    def __init__(self, recv):
        self.recv = recv
        self.buf = b""

    async def next(self):
        while b"\n" not in self.buf:
            try:
                chunk = await self.recv.read(65536)
            except iroh.IrohError:
                return None
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > MAX_LINE:
                return None
        line, self.buf = self.buf.split(b"\n", 1)
        return line


class Peer:
    def __init__(self, conn, send, recv, outgoing):
        self.conn, self.send, self.recv = conn, send, recv
        self.uid = conn.remote_id().to_bytes().hex()
        self.outgoing = outgoing
        self.hello = {}
        self.queue = asyncio.Queue()
        self.tasks = []
        self.replaced = False
        self.closed = False

    def path(self):
        """The path in use: via_relay (bool), via (remote address), rtt (ms)."""
        try:
            for p in self.conn.paths():
                if p.is_selected:
                    return {"via_relay": p.is_relay, "via": p.remote_addr, "rtt": p.rtt_ms}
        except Exception:
            pass
        return {"via_relay": False, "via": "", "rtt": None}


class OutStream:
    """Screen share to one viewer: a one-way QUIC stream fed from any thread. If the
    viewer can't keep up, the oldest chunks are dropped instead of piling up delay."""

    def __init__(self, mesh, uid, limit=1_500_000):
        self.mesh, self.uid, self.limit = mesh, uid, limit
        self.chunks = collections.deque()
        self.size = 0
        self.dropped = 0
        self.closed = False
        self.lock = threading.Lock()
        self.wake = None

    def push(self, data):
        with self.lock:
            self.chunks.append(data)
            self.size += len(data)
            while self.size > self.limit and len(self.chunks) > 1:
                self.size -= len(self.chunks.popleft())
                self.dropped += 1
        if self.wake is not None:
            self.mesh.loop.call_soon_threadsafe(self.wake.set)

    def close(self):
        self.closed = True
        if self.wake is not None:
            self.mesh.loop.call_soon_threadsafe(self.wake.set)

    async def run(self):
        self.wake = asyncio.Event()
        peer = self.mesh.peers_map.get(self.uid)
        if not peer:
            return
        try:
            stream = await peer.conn.open_uni()
            while not self.closed:
                await self.wake.wait()
                self.wake.clear()
                while True:
                    with self.lock:
                        batch, n = [], 0
                        while self.chunks and n < 64 * 1024:
                            c = self.chunks.popleft()
                            batch.append(c)
                            n += len(c)
                        self.size -= n
                    if not batch:
                        break
                    await stream.write_all(b"".join(batch))
            await stream.finish()
        except Exception:
            pass


class Mesh(QObject):
    peer_up = Signal(str)
    peer_down = Signal(str)
    received = Signal(str, dict)
    discovery_changed = Signal()
    addr_changed = Signal()
    error = Signal(str)

    def __init__(self, settings, hello_fn):
        super().__init__()
        self.s = settings
        self.me = settings.uid
        self.hello_fn = hello_fn
        self.lock = threading.RLock()
        self.peers_map = {}
        self.seen = {}               # LAN announcements: uid -> {ip, port, name, first, last}
        self.dialing = set()
        self.attempts = {}           # uid -> (last attempt, relay rotation index)
        self.loop = None
        self.ep = None
        self.port = PEER_PORT
        self.my_addr = {"relay": None, "addrs": []}
        self.relays = []
        self.running = False
        self.on_voice = None         # callback(uid, bytes) — called on the network thread
        self.on_stream = None        # callback(uid, bytes | None) — None when the stream ends
        self._stopped = threading.Event()
        self._ifaces, self._neighbours = [], []

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self):
        self.running = True
        threading.Thread(target=self._thread_main, daemon=True, name="iroh").start()
        if not os.environ.get("MOYDISCORD_NO_LAN"):       # tests: force the internet path
            threading.Thread(target=self._discover, daemon=True, name="discovery").start()

    def stop(self):
        self.running = False
        if self.loop and self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            try:
                fut.result(timeout=4)
            except Exception:
                pass

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            self._stopped.set()

    async def _main(self):
        iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
        internet = self.s["network_mode"] == "internet"
        err = None
        for bind in (f"0.0.0.0:{PEER_PORT}", "0.0.0.0:0"):
            try:
                preset = iroh.preset_n0() if internet else iroh.preset_minimal()
                self.ep = await iroh.Endpoint.bind(iroh.EndpointOptions(
                    preset=preset, alpns=[ALPN], bind_addr=bind,
                    secret_key=bytes.fromhex(self.s["secret_key"])))
                break
            except iroh.IrohError as e:
                err = e
        if self.ep is None:
            self.error.emit(f"Не удалось запустить сеть: {err.message() if err else '?'}")
            return
        for sock in self.ep.bound_sockets():
            if sock.startswith("0.0.0.0:"):
                self.port = int(sock.rsplit(":", 1)[1])
        if internet:
            self.relays = iroh.RelayMode.default_mode().relay_map().urls()
        self._refresh_addr()
        tasks = [asyncio.create_task(self._accept_loop()), asyncio.create_task(self._maintain())]
        while self.running:
            await asyncio.sleep(0.5)
        for t in tasks:
            t.cancel()

    async def _shutdown(self):
        self.running = False
        for p in list(self.peers_map.values()):
            try:
                p.conn.close(0, b"bye")
            except Exception:
                pass
        if self.ep:
            try:
                await asyncio.wait_for(self.ep.close(), 2)
            except Exception:
                pass

    def _refresh_addr(self):
        a = self.ep.addr()
        new = {"relay": a.relay_url(), "addrs": a.direct_addresses()[:8]}
        if new != self.my_addr:
            self.my_addr = new
            self.addr_changed.emit()

    # ── hello & room proof ──────────────────────────────────────────
    def _proof(self, a_hex, b_hex):
        return hmac.new(self.s.room_secret(), b"moydiscord-proof" + bytes.fromhex(a_hex) + bytes.fromhex(b_hex),
                        hashlib.sha256).hexdigest()

    def make_hello(self, remote):
        return {"t": "hello", "app": APP_ID, "proto": PROTOCOL, "uid": self.me,
                "room": self.s.room_id(), "proof": self._proof(self.me, remote),
                "addr": self.my_addr, **self.hello_fn()}

    def _hello_ok(self, remote, hello):
        return (isinstance(hello, dict) and hello.get("app") == APP_ID and hello.get("proto") == PROTOCOL
                and hello.get("uid") == remote and hello.get("room") == self.s.room_id()
                and hmac.compare_digest(str(hello.get("proof", "")), self._proof(remote, self.me)))

    @staticmethod
    def _line(obj):
        return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    # ── connections ─────────────────────────────────────────────────
    async def _accept_loop(self):
        while self.running:
            try:
                inc = await self.ep.accept_next()
            except Exception:
                break
            if inc is None:
                break
            asyncio.create_task(self._incoming(inc))

    async def _incoming(self, inc):
        try:
            conn = await asyncio.wait_for((await inc.accept()).connect(), 15)
            bi = await asyncio.wait_for(conn.accept_bi(), 15)
            await self._handshake(conn, bi.send(), bi.recv(), outgoing=False)
        except Exception:
            pass

    def dial(self, uid, relay=None, addrs=()):
        """Thread-safe: try to connect to a peer we know by key (+ hints where it might be)."""
        if self.loop and self.running:
            asyncio.run_coroutine_threadsafe(self._dial(uid, relay, list(addrs)), self.loop)

    async def _dial(self, uid, relay, addrs):
        if uid == self.me or uid in self.dialing or uid in self.peers_map or not self.ep:
            return
        self.dialing.add(uid)
        try:
            target = iroh.EndpointAddr(iroh.EndpointId.from_bytes(bytes.fromhex(uid)), relay, addrs)
            conn = await asyncio.wait_for(self.ep.connect(target, ALPN), 20)
            bi = await conn.open_bi()
            await self._handshake(conn, bi.send(), bi.recv(), outgoing=True)
        except Exception:
            pass
        finally:
            self.dialing.discard(uid)

    async def _handshake(self, conn, send, recv, outgoing):
        peer = Peer(conn, send, recv, outgoing)
        await send.write_all(self._line(self.make_hello(peer.uid)))
        reader = LineReader(recv)
        raw = await asyncio.wait_for(reader.next(), 15)
        try:
            hello = json.loads(raw) if raw else None
        except ValueError:
            hello = None
        if not self._hello_ok(peer.uid, hello):
            conn.close(1, b"wrong room")
            return
        peer.hello = hello
        with self.lock:
            old = self.peers_map.get(peer.uid)
            if old is not None and not old.closed:
                # both sides dialled at once: keep the link opened by the lower key
                initiator = self.me if outgoing else peer.uid
                if initiator != min(self.me, peer.uid):
                    conn.close(0, b"duplicate")
                    return
                old.replaced = True
                old.conn.close(0, b"duplicate")
            self.peers_map[peer.uid] = peer
        peer.tasks = [asyncio.create_task(c) for c in (
            self._read_lines(peer, reader), self._write_lines(peer),
            self._read_datagrams(peer), self._accept_streams(peer))]
        self.peer_up.emit(peer.uid)
        try:
            await peer.conn.closed()
        except Exception:
            pass
        await self._closed(peer)

    async def _closed(self, peer):
        peer.closed = True
        for t in peer.tasks:
            t.cancel()
        with self.lock:
            gone = self.peers_map.get(peer.uid) is peer
            if gone:
                del self.peers_map[peer.uid]
        if gone and not peer.replaced:
            self.peer_down.emit(peer.uid)

    async def _read_lines(self, peer, reader):
        while True:
            line = await reader.next()
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("t") != "ping":
                if msg.get("t") == "hello":
                    peer.hello = msg
                self.received.emit(peer.uid, msg)
        peer.conn.close(0, b"stream closed")

    async def _write_lines(self, peer):
        while True:
            try:
                obj = await asyncio.wait_for(peer.queue.get(), 15)
            except asyncio.TimeoutError:
                obj = {"t": "ping"}
            if obj is None:
                break
            try:
                await peer.send.write_all(self._line(obj))
            except Exception:
                break

    async def _read_datagrams(self, peer):
        while True:
            try:
                data = await peer.conn.read_datagram()
            except Exception:
                break
            if data and data[0] == DGRAM_VOICE and self.on_voice:
                self.on_voice(peer.uid, data[1:])

    async def _accept_streams(self, peer):
        while True:
            try:
                rs = await peer.conn.accept_uni()
            except Exception:
                break
            asyncio.create_task(self._read_stream(peer.uid, rs))

    async def _read_stream(self, uid, rs):
        try:
            while True:
                chunk = await rs.read(64 * 1024)
                if not chunk:
                    break
                if self.on_stream:
                    self.on_stream(uid, chunk)
        except Exception:
            pass
        if self.on_stream:
            self.on_stream(uid, None)

    # ── sending (any thread) ────────────────────────────────────────
    def send(self, uid, obj):
        peer = self.peers_map.get(uid)
        if peer and self.loop:
            self.loop.call_soon_threadsafe(peer.queue.put_nowait, obj)

    def broadcast(self, obj, exclude=None):
        for uid in list(self.peers_map):
            if uid != exclude:
                self.send(uid, obj)

    def send_datagram(self, uid, data):
        """Voice: unreliable, unordered, lowest latency. Safe from the audio thread."""
        peer = self.peers_map.get(uid)
        if peer:
            try:
                peer.conn.send_datagram(bytes([DGRAM_VOICE]) + data)
            except Exception:
                pass

    def open_stream(self, uid):
        out = OutStream(self, uid)
        if self.loop:
            asyncio.run_coroutine_threadsafe(out.run(), self.loop)
        return out

    # ── queries ─────────────────────────────────────────────────────
    def peers(self):
        with self.lock:
            return {u: {**p.hello, **p.path()} for u, p in self.peers_map.items()}

    def discovered(self):
        now = time.time()
        with self.lock:
            return {u: dict(v) for u, v in self.seen.items() if now - v["last"] < 15}

    def my_card(self):
        """What goes into an invite code / peer exchange about us."""
        return {"id": self.me, "relay": self.my_addr["relay"], "addrs": self.my_addr["addrs"],
                "name": self.s["name"]}

    # ── reconnecting to known peers ─────────────────────────────────
    async def _maintain(self):
        while self.running:
            await asyncio.sleep(2)
            try:
                self._refresh_addr()
            except Exception:
                pass
            now = time.time()
            for uid, info in list(self.seen.items()):
                if uid in self.peers_map or now - info["last"] > 15:
                    continue
                # the lower key dials first; the other side steps in if that fails (one-way firewall)
                if self.me < uid or now - info["first"] > 6:
                    asyncio.create_task(self._dial(uid, None, [f"{info['ip']}:{info['port']}"]))
            if self.s["network_mode"] != "internet":
                continue
            room = self.s.room_id()
            for uid, info in list((self.s["known_peers"] or {}).items()):
                if uid in self.peers_map or uid in self.dialing or info.get("room") != room:
                    continue
                last, idx = self.attempts.get(uid, (0, 0))
                if now - last < 25:
                    continue
                # try the relay it last used, then rotate through the others
                relays = [info.get("relay")] + [r for r in self.relays if r != info.get("relay")]
                relay = relays[idx % len(relays)] if relays else None
                self.attempts[uid] = (now, idx + 1)
                asyncio.create_task(self._dial(uid, relay, info.get("addrs") or []))

    # ── LAN discovery (plain UDP broadcast, works over Radmin VPN too) ──
    def _announcement(self):
        return json.dumps({"app": APP_ID, "proto": PROTOCOL, "id": self.me, "room": self.s.room_id(),
                           "port": self.port, "name": self.s["name"]}).encode()

    def _discover(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError as e:
            self.error.emit(f"Порт обнаружения {DISCOVERY_PORT} занят: {e.strerror or e}")
            return
        sock.settimeout(0.5)
        next_announce = next_ifaces = 0.0
        while self.running:
            now = time.time()
            if now >= next_ifaces:
                self._ifaces = interfaces()
                self._neighbours = radmin_neighbours()
                next_ifaces = now + 20
            if now >= next_announce and self.ep is not None:
                self._announce(sock)
                next_announce = now + 2
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                break
            self._on_announcement(sock, data, ip)
        sock.close()

    def _announce(self, sock, only=None):
        payload = self._announcement()
        targets = [only] if only else (["255.255.255.255"] + [b for _, b in self._ifaces if b]
                                       + self._neighbours)
        for t in dict.fromkeys(targets):
            try:
                sock.sendto(payload, (t, DISCOVERY_PORT))
            except OSError:
                pass

    def _on_announcement(self, sock, data, ip):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        uid = str(msg.get("id", ""))
        if msg.get("app") != APP_ID or msg.get("proto") != PROTOCOL or uid == self.me or not UID_RE.match(uid):
            return
        if msg.get("room") != self.s.room_id() or not isinstance(msg.get("port"), int):
            return
        now = time.time()
        with self.lock:
            entry = self.seen.get(uid)
            fresh = entry is None or now - entry["last"] > 15
            self.seen[uid] = {"ip": ip, "port": msg["port"], "name": str(msg.get("name", ""))[:32],
                              "first": now if fresh else entry["first"], "last": now}
        if fresh:
            self._announce(sock, only=ip)   # answer directly so they find us without waiting
            self.discovery_changed.emit()
