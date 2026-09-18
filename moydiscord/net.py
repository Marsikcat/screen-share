"""
Peer-to-peer networking — no server.

Discovery: every peer announces itself over UDP (subnet broadcasts, which
Radmin VPN carries like a LAN, plus unicast to manually added IPs and to
Radmin neighbours from the ARP table). Peers in the same room then open a
TCP connection to each other (a full mesh) for chat sync, presence and
signalling. Voice and screen share go over their own UDP ports.
"""

import ipaddress
import json
import queue
import re
import socket
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Signal

from .config import CONTROL_PORT, DISCOVERY_PORT
from .store import UID_RE

APP_ID = "moydiscord"
PROTO = 1
MAX_LINE = 4 * 1024 * 1024
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
    # "IPv4 Address . . : 1.2.3.4" followed by "Subnet Mask . . : 255.255.255.0" (any locale)
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


def split_address(addr, default_port=CONTROL_PORT):
    """'1.2.3.4' or '1.2.3.4:9000' -> (ip, port)."""
    host, _, port = str(addr).strip().partition(":")
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return host, default_port


def radmin_neighbours():
    return list(dict.fromkeys(re.findall(r"\b(26\.\d+\.\d+\.\d+)\b", _run(["arp", "-a"]))))


class PeerConn:
    """One TCP link to a peer: JSON lines, a reader and a writer thread."""

    def __init__(self, mesh, sock, outgoing):
        self.mesh = mesh
        self.sock = sock
        self.ip = sock.getpeername()[0]
        self.outgoing = outgoing
        self.uid = None
        self.hello = {}
        self.replaced = False
        self.closed = False
        self.q = queue.Queue()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(45)

    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()
        self.send(self.mesh.make_hello())

    def send(self, obj):
        if not self.closed:
            self.q.put(obj)

    def _writer(self):
        while True:
            try:
                obj = self.q.get(timeout=15)
            except queue.Empty:
                obj = {"t": "ping"}
            if obj is None:
                break
            data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            try:
                self.sock.sendall(data)
            except OSError:
                break
        self.close()

    def _reader(self):
        f = self.sock.makefile("rb")
        try:
            while True:
                line = f.readline(MAX_LINE)
                if not line or not line.endswith(b"\n"):
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("t") == "ping":
                    continue
                if self.uid is None:
                    if msg.get("t") != "hello" or not self.mesh._on_hello(self, msg):
                        break
                    continue
                if msg.get("t") == "hello":
                    self.hello = msg
                self.mesh.received.emit(self.uid, msg)
        except (OSError, ValueError):
            pass
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.q.put(None)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.mesh._on_closed(self)


class Mesh(QObject):
    peer_up = Signal(str)
    peer_down = Signal(str)
    received = Signal(str, dict)
    discovery_changed = Signal()
    error = Signal(str)

    def __init__(self, settings, hello_fn):
        super().__init__()
        self.settings = settings
        self.me = settings.uid
        self.hello_fn = hello_fn
        self.lock = threading.RLock()
        self.conns = {}          # uid -> PeerConn
        self.connecting = set()  # ips with a dial in flight
        self.seen = {}           # uid -> {ip, name, first, last}
        self.dialed = {}         # ip -> last manual dial time
        self.running = False
        self._ifaces = []
        self._neighbours = []

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self):
        self.running = True
        for fn in (self._serve, self._discover, self._maintain):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self):
        self.running = False
        with self.lock:
            conns = list(self.conns.values())
        for c in conns:
            c.close()

    def make_hello(self):
        return {"t": "hello", "app": APP_ID, "proto": PROTO, "uid": self.me,
                "room": self.settings["room"], **self.hello_fn()}

    # ── sending ─────────────────────────────────────────────────────
    def send(self, uid, obj):
        with self.lock:
            c = self.conns.get(uid)
        if c:
            c.send(obj)

    def broadcast(self, obj, exclude=None):
        with self.lock:
            conns = [c for u, c in self.conns.items() if u != exclude]
        for c in conns:
            c.send(obj)

    def peers(self):
        with self.lock:
            return {u: {"ip": c.ip, **c.hello} for u, c in self.conns.items()}


    def discovered(self):
        now = time.time()
        with self.lock:
            return {u: dict(v) for u, v in self.seen.items() if now - v["last"] < 15}

    # ── connection bookkeeping (called from socket threads) ─────────
    def _on_hello(self, conn, msg):
        uid = str(msg.get("uid", ""))
        if msg.get("app") != APP_ID or not UID_RE.match(uid) or uid == self.me:
            return False
        if msg.get("room") != self.settings["room"]:
            return False
        with self.lock:
            existing = self.conns.get(uid)
            if existing is not None and not existing.closed:
                # both sides dialled at once: keep the link opened by the lower uid
                initiator = self.me if conn.outgoing else uid
                if initiator != min(self.me, uid):
                    return False
                existing.replaced = True
                existing.close()
            conn.uid, conn.hello = uid, msg
            self.conns[uid] = conn
        self.peer_up.emit(uid)
        return True

    def _on_closed(self, conn):
        with self.lock:
            gone = conn.uid is not None and self.conns.get(conn.uid) is conn
            if gone:
                del self.conns[conn.uid]
        if gone and not conn.replaced:
            self.peer_down.emit(conn.uid)

    def dial(self, ip, port=CONTROL_PORT):
        key = (ip, port)
        with self.lock:
            if key in self.connecting:
                return
            self.connecting.add(key)

        def run():
            try:
                sock = socket.create_connection((ip, port), timeout=3)
                PeerConn(self, sock, outgoing=True).start()
            except OSError:
                pass
            finally:
                with self.lock:
                    self.connecting.discard(key)

        threading.Thread(target=run, daemon=True).start()

    # ── threads ─────────────────────────────────────────────────────
    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("0.0.0.0", CONTROL_PORT))
        except OSError as e:
            self.error.emit(f"Порт {CONTROL_PORT} занят: {e.strerror or e}")
            return
        srv.listen(16)
        srv.settimeout(1.0)
        while self.running:
            try:
                sock, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                PeerConn(self, sock, outgoing=False).start()
            except OSError:
                sock.close()
        srv.close()

    def _announcement(self):
        return json.dumps({"app": APP_ID, "proto": PROTO, "uid": self.me,
                           "room": self.settings["room"], "port": CONTROL_PORT,
                           "name": self.settings["name"]}).encode()

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
            if now >= next_announce:
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
        targets = [only] if only else (
            ["255.255.255.255"] + [b for _, b in self._ifaces if b]
            + self._neighbours + [split_address(p)[0] for p in self.settings["peers"]])
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
        uid = str(msg.get("uid", ""))
        if msg.get("app") != APP_ID or uid == self.me or not UID_RE.match(uid):
            return
        if msg.get("room") != self.settings["room"]:
            return
        now = time.time()
        with self.lock:
            entry = self.seen.get(uid)
            fresh = entry is None or now - entry["last"] > 15
            port = msg.get("port") if isinstance(msg.get("port"), int) else CONTROL_PORT
            self.seen[uid] = {"ip": ip, "port": port, "name": str(msg.get("name", ""))[:32],
                              "first": now if fresh else entry["first"], "last": now}
        if fresh:
            self._announce(sock, only=ip)  # answer directly so they find us without waiting
            self.discovery_changed.emit()

    def _maintain(self):
        while self.running:
            time.sleep(1.0)
            now = time.time()
            with self.lock:
                seen = dict(self.seen)
                connected = set(self.conns)
                links = {(c.ip, c.hello.get("port", CONTROL_PORT)) for c in self.conns.values()}
            for uid, info in seen.items():
                if uid in connected or now - info["last"] > 15:
                    continue
                # the lower uid dials; the other side steps in if that fails (one-way firewall)
                if self.me < uid or now - info["first"] > 5:
                    self.dial(info["ip"], info["port"])
            for addr in self.settings["peers"]:
                ip, port = split_address(addr)
                if (ip, port) not in links and now - self.dialed.get(addr, 0) > 10:
                    self.dialed[addr] = now
                    self.dial(ip, port)
