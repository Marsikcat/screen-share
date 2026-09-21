#!/usr/bin/env python3
"""
ScreenShare — P2P screen sharing with GUI (Radmin VPN / LAN).
Supports full desktop or specific window capture.
"""

import ctypes
import ctypes.wintypes
import io
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from queue import Queue

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    import mss
except ImportError:
    mss = None

import tkinter as tk
from tkinter import ttk


# ── Config ──────────────────────────────────────────────────────────
APP_NAME = "ScreenShare"
DEFAULT_PORT = 8888
AUDIO_PORT = 8889
# next to ScreenShare.exe when installed, next to this script when run from source
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
FFMPEG_DIR = APP_DIR / "ffmpeg"
FFMPEG_BIN = FFMPEG_DIR / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")
FFPLAY_BIN = FFMPEG_DIR / ("ffplay.exe" if platform.system() == "Windows" else "ffplay")
CHUNK_SIZE = 1400
VIEWERS_FILE = APP_DIR / "viewers.txt"

QUALITY_PRESETS = {
    "high":   {"bitrate": "20M",  "crf": "18", "preset": "p5", "fps": "60"},
    "medium": {"bitrate": "10M",  "crf": "23", "preset": "p4", "fps": "30"},
    "low":    {"bitrate": "3M",   "crf": "28", "preset": "p3", "fps": "15"},
}

FFMPEG_URLS = {
    "Windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "Linux":   "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "Darwin":  "https://evermeet.cx/ffmpeg/ffmpeg-7.1.zip",
}


# ── Window enumeration (ctypes, no extra deps) ──────────────────────
_EnumWindows = ctypes.windll.user32.EnumWindows
_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
_GetWindowTextW = ctypes.windll.user32.GetWindowTextW
_GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
_IsWindowVisible = ctypes.windll.user32.IsWindowVisible
_GetWindowRect = ctypes.windll.user32.GetWindowRect


def list_windows():
    windows = []
    def callback(hwnd, _):
        if _IsWindowVisible(hwnd):
            length = _GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                _GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    rect = ctypes.wintypes.RECT()
                    _GetWindowRect(hwnd, ctypes.byref(rect))
                    windows.append((hwnd, title, rect))
        return True
    _EnumWindows(_EnumWindowsProc(callback), 0)
    windows.sort(key=lambda x: x[1].lower())
    return windows


def find_window_by_title(title):
    for hwnd, t, _ in list_windows():
        if t == title:
            return hwnd, title
    return None, None


def get_window_rect(title):
    for hwnd, t, rect in list_windows():
        if t == title:
            return rect
    return None


# ── Network helpers ─────────────────────────────────────────────────
def get_local_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ("127.0.0.1",) and ip not in ips:
                ips.append(ip)
    except:
        pass
    try:
        ips.extend([
            ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
            if ip not in ips and ip != "127.0.0.1"
        ])
    except:
        pass
    # ipconfig - most reliable for Radmin VPN virtual adapter
    try:
        r = subprocess.run(["ipconfig"], capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", errors="replace") + r.stderr.decode("utf-8", errors="replace")
        for m in re.finditer(r'IPv4[^.]+:\s*(\d+\.\d+\.\d+\.\d+)', text):
            ip = m.group(1)
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
        for m in re.finditer(r'IP[^.]+:\s*(\d+\.\d+\.\d+\.\d+)', text):
            ip = m.group(1)
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except:
        pass
    ips = list(dict.fromkeys(ips))
    ips.sort(key=lambda x: (not x.startswith("26."), x))
    return ips


def get_radmin_ip():
    for ip in get_local_ips():
        if ip.startswith("26."):
            return ip
    return None


def get_radmin_subnet():
    ip = get_radmin_ip()
    if not ip:
        return None
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


def scan_radmin_peers(timeout=1):
    """Find other Radmin VPN peers via ARP table + quick ping sweep."""
    peers = []
    radmin_ip = get_radmin_ip()
    if not radmin_ip:
        return peers
    prefix = ".".join(radmin_ip.split(".")[:3])

    # Try ARP table first (instant)
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", errors="replace")
        for m in re.finditer(r'\b(26\.\d+\.\d+\.\d+)\b', text):
            ip = m.group(1)
            if ip != radmin_ip and ip not in peers:
                peers.append(ip)
    except:
        pass

    if peers:
        return sorted(peers)

    # Ping sweep
    found = []
    lock = threading.Lock()

    def _ping(host):
        try:
            r = subprocess.run(
                ["ping", "-n", "1", "-l", "0", "-w", "300", host],
                capture_output=True, timeout=2,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                with lock:
                    found.append(host)
        except:
            pass

    threads = []
    for i in range(1, 255):
        ip = f"{prefix}.{i}"
        if ip == radmin_ip:
            continue
        t = threading.Thread(target=_ping, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
        # limit concurrency to avoid overwhelming Windows
        if len(threads) % 50 == 0:
            time.sleep(0.05)

    for t in threads:
        t.join(timeout=2)

    return sorted(found)


def get_default_ip():
    ips = get_local_ips()
    radmin = get_radmin_ip()
    if radmin:
        return radmin
    return ips[0] if ips else "127.0.0.1"


def check_port(ip, port, timeout=2):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(b"ping", (ip, port))
        s.close()
        return True
    except:
        return False


def list_monitors():
    monitors = []
    try:
        with mss.MSS() as sct:
            for i, m in enumerate(sct.monitors):
                label = "All monitors" if i == 0 else f"Monitor {i}"
                monitors.append({
                    "index": i,
                    "label": label,
                    "left": m["left"], "top": m["top"],
                    "width": m["width"], "height": m["height"],
                })
    except:
        monitors.append({"index": 1, "label": "Monitor 1", "left": 0, "top": 0, "width": 1920, "height": 1080})
    return monitors


def get_monitor_region(monitor_index):
    monitors = list_monitors()
    for m in monitors:
        if m["index"] == monitor_index:
            return m
    return None


def check_firewall(port=DEFAULT_PORT):
    if platform.system() != "Windows":
        return None
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=ScreenShare", "verbose"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = r.stdout.decode("utf-8", errors="replace")
        if f" {port}" in out and "UDP" in out:
            return True
        # check all rules if screen share rule not found
        r2 = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=all", "verbose"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out2 = r2.stdout.decode("utf-8", errors="replace")
        for line in out2.split("\n"):
            if f" {port}" in line and "UDP" in line:
                return True
        return False
    except:
        return None


def add_firewall_rule(port=DEFAULT_PORT):
    """Add Windows Firewall rule for inbound UDP port."""
    if platform.system() != "Windows":
        return False
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name=ScreenShare", "dir=in", "action=allow",
             f"protocol=udp", f"localport={port}",
             "profile=any", "description=ScreenShare P2P streaming"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "ok" in r.stdout.lower()
    except:
        return False


def _probe_nvenc():
    if not FFMPEG_BIN.exists():
        return False
    # test the encoder with a real frame
    r = subprocess.run(
        [str(FFMPEG_BIN), "-f", "lavfi", "-i", "color=c=black:s=640x480:d=0.1",
         "-c:v", "h264_nvenc", "-preset", "p1", "-t", "0.1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    return r.returncode == 0


def kill_ffmpeg():
    """Kill any leftover ffmpeg/ffplay processes from this app."""
    try:
        subprocess.run(["taskkill", "/f", "/im", "ffmpeg.exe"],
                       capture_output=True, timeout=3,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except:
        pass
    try:
        subprocess.run(["taskkill", "/f", "/im", "ffplay.exe"],
                       capture_output=True, timeout=3,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except:
        pass


def download_ffmpeg(log_callback=print):
    system = platform.system()
    if system not in FFMPEG_URLS:
        log_callback(f"Не поддерживается для {system}")
        return False
    url = FFMPEG_URLS[system]
    log_callback(f"Скачиваю FFmpeg...")
    tmp = Path(tempfile.gettempdir()) / "screen_share_ffmpeg"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / "ffmpeg.zip"
    try:
        urllib.request.urlretrieve(url, zip_path)
    except Exception as e:
        log_callback(f"Ошибка: {e}")
        return False
    log_callback("Распаковываю...")
    shutil.unpack_archive(str(zip_path), str(tmp))
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    found = False
    if system == "Windows":
        for root, _, files in os.walk(tmp):
            for f in files:
                if f in ("ffmpeg.exe", "ffplay.exe"):
                    shutil.copy2(os.path.join(root, f), FFMPEG_DIR / f)
                    found = True
    else:
        for name in ("ffmpeg", "ffplay"):
            src = tmp / name
            if src.exists():
                shutil.copy2(str(src), FFMPEG_DIR / name)
                os.chmod(FFMPEG_DIR / name, 0o755)
                found = True
    shutil.rmtree(tmp, ignore_errors=True)
    if found:
        log_callback("FFmpeg установлен")
    else:
        log_callback("Не найден ffmpeg в архиве")
    return found


def list_audio_devices():
    """List DirectShow audio input devices via ffmpeg.
    Returns (loopback_devices, mic_devices) where loopback captures system audio output."""
    if not FFMPEG_BIN.exists():
        return [], []
    loopback_kw = ["stereo mix", "стерео микшер", "virtual audio", "what u hear",
                   "wave out", "loopback", "cable input"]
    loopback = []
    mic = []
    try:
        r = subprocess.run(
            [str(FFMPEG_BIN), "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stderr_text = r.stderr.decode("utf-8", errors="replace")
        for line in stderr_text.splitlines():
            if '(audio)' in line and '"' in line:
                start = line.index('"')
                end = line.index('"', start + 1)
                name = line[start + 1:end]
                if any(k in name.lower() for k in loopback_kw):
                    loopback.append(name)
                else:
                    mic.append(name)
        return loopback, mic
    except:
        return [], []


# ── Senders ─────────────────────────────────────────────────────────
class FfmpegSender:
    def __init__(self, destinations, quality, window_title=None, monitor_index=None, log_callback=print, use_tcp=False, audio_device=None):
        self.destinations = destinations if isinstance(destinations, list) else [destinations]
        self.quality = quality
        self.window_title = window_title
        self.monitor_index = monitor_index
        self.log = log_callback
        self.running = False
        self.use_tcp = use_tcp
        self.proc = None
        self.audio_proc = None
        self.audio_device = audio_device
        self._relay_thread = None

    def start(self):
        q = QUALITY_PRESETS[self.quality]
        has_nvenc = _probe_nvenc()
        fps = int(q["fps"])
        if has_nvenc:
            encoder = "h264_nvenc"
            enc_opts = [
                "-rc", "constqp", "-qp", "21",
                "-preset", q["preset"],
                "-g", str(fps), "-keyint_min", str(fps),
            ]
            self.log(f"Codec: h264_nvenc (NVENC) | {q['bitrate']} | {fps} FPS")
        else:
            encoder = "libx264"
            enc_opts = [
                "-preset", "ultrafast", "-crf", q["crf"],
                "-b:v", q["bitrate"],
                "-g", str(fps), "-keyint_min", str(fps),
                "-tune", "zerolatency",
            ]
            self.log(f"Codec: libx264 (CPU) | CRF {q['crf']} | {fps} FPS")

        if platform.system() != "Windows":
            self.log("FFmpeg window capture is Windows-only")
            return

        # must be set before the relay threads start — their loops exit on False
        self.running = True

        vf = []
        if self.window_title:
            input_src = ["-f", "gdigrab", "-framerate", q["fps"],
                         "-i", f"title={self.window_title}"]
            self.log(f"Window: \"{self.window_title}\"")
        elif self.monitor_index is not None and self.monitor_index > 0:
            region = get_monitor_region(self.monitor_index)
            if region:
                input_src = ["-f", "gdigrab", "-framerate", q["fps"],
                             "-offset_x", str(region["left"]),
                             "-offset_y", str(region["top"]),
                             "-video_size", f"{region['width']}x{region['height']}",
                             "-i", "desktop"]
                self.log(f"Monitor {self.monitor_index}: {region['width']}x{region['height']} @ {region['left']},{region['top']}")
            else:
                input_src = ["-f", "gdigrab", "-framerate", q["fps"], "-i", "desktop"]
                self.log("Capture: full screen (region not found)")
        else:
            input_src = ["-f", "gdigrab", "-framerate", q["fps"],
                         "-i", "desktop"]
            self.log("Capture: full screen")

        if self.use_tcp and len(self.destinations) == 1:
            dest = self.destinations[0]
            cmd = [
                str(FFMPEG_BIN), *input_src,
                "-c:v", encoder, *enc_opts,
                "-pix_fmt", "yuv420p",
                "-fflags", "nobuffer",
                *vf,
                "-f", "mpegts",
                "-flush_packets", "1",
                "pipe:1",
            ]
            self.log(f"Streaming to {dest[0]}:{dest[1]} via TCP relay")
            self.log("TCP: connecting to receiver (retrying)...")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._monitor_stderr, daemon=True).start()
            threading.Thread(target=self._tcp_relay, args=(dest[0], dest[1]), daemon=True).start()
        elif len(self.destinations) == 1:
            dest = self.destinations[0]
            cmd = [
                str(FFMPEG_BIN), *input_src,
                "-c:v", encoder, *enc_opts,
                "-pix_fmt", "yuv420p",
                "-fflags", "nobuffer",
                *vf,
                "-f", "mpegts",
                "-flush_packets", "1",
                f"udp://{dest[0]}:{dest[1]}?pkt_size=1316&buffer_size=65536",
            ]
            self.log(f"Streaming to {dest[0]}:{dest[1]} via UDP")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._monitor_stderr, daemon=True).start()
        else:
            local_port = self.destinations[0][1]
            cmd = [
                str(FFMPEG_BIN), *input_src,
                "-c:v", encoder, *enc_opts,
                "-pix_fmt", "yuv420p",
                "-fflags", "nobuffer",
                *vf,
                "-f", "mpegts",
                "-flush_packets", "1",
                f"udp://127.0.0.1:{local_port}?pkt_size=1316&buffer_size=65536",
            ]
            self.log(f"Streaming to {len(self.destinations)} viewer(s) via UDP relay")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._monitor_stderr, daemon=True).start()
            self._relay_thread = threading.Thread(
                target=self._relay_loop, args=(local_port,), daemon=True)
            self._relay_thread.start()
        if self.audio_device:
            for dest_ip, dest_port in self.destinations:
                audio_cmd = [
                    str(FFMPEG_BIN), "-f", "dshow", "-i", f"audio={self.audio_device}",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                    "-fflags", "nobuffer",
                    "-f", "mpegts",
                    f"udp://{dest_ip}:{AUDIO_PORT}?pkt_size=1316&buffer_size=65536",
                ]
                p = subprocess.Popen(
                    audio_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.audio_proc = p  # keep last one for cleanup
            self.log(f"Audio: {self.audio_device}")

    def _monitor_stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            txt = line.decode("utf-8", errors="replace").strip()
            if not txt:
                continue
            low = txt.lower()
            if "error" in low:
                self.log(f"FFmpeg: {txt}")
            elif "fps=" in low and ("kb/s" in low or "kbits/" in low):
                self.log(f"FFmpeg: {txt}")

    def _tcp_relay(self, host, port):
        while self.running:
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.settimeout(10)
                self.log(f"TCP connected to {host}:{port}")
            except socket.timeout:
                self.log("TCP connection timed out, retrying...")
                if not self.running:
                    return
                time.sleep(2)
                continue
            except ConnectionRefusedError:
                self.log("TCP connection refused (receiver not listening?), retrying...")
                if not self.running:
                    return
                time.sleep(2)
                continue
            except OSError as e:
                self.log(f"TCP connection error: {e}, retrying...")
                if not self.running:
                    return
                time.sleep(2)
                continue
            if not sock:
                continue
            try:
                while self.running:
                    # read1: forward whatever is ready instead of waiting for a full 64 KB
                    data = self.proc.stdout.read1(65536)
                    if not data:
                        return
                    sock.sendall(data)
            except (socket.timeout, ConnectionResetError, BrokenPipeError, OSError):
                self.log("TCP connection lost, reconnecting...")
            finally:
                sock.close()

    def stop(self):
        self.running = False
        if self.audio_proc:
            try:
                self.audio_proc.terminate()
                self.audio_proc.wait(timeout=3)
            except:
                try:
                    self.audio_proc.kill()
                except:
                    pass
            self.audio_proc = None
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            self.log("Streaming stopped")

    def _relay_loop(self, local_port):
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay.bind(("127.0.0.1", local_port))
        relay.settimeout(1.0)
        while self.running:
            try:
                data, _ = relay.recvfrom(65535)
                for dest in self.destinations:
                    relay.sendto(data, dest)
            except socket.timeout:
                continue
            except Exception:
                break
        relay.close()


class PySender:
    def __init__(self, destinations, quality, fps=30, window_title=None, monitor_index=None, log_callback=print):
        self.destinations = destinations if isinstance(destinations, list) else [destinations]
        self.fps = fps
        self.window_title = window_title
        self.monitor_index = monitor_index
        self.log = log_callback
        self.running = False
        self.sock = None
        self._thread = None
        jpeg_q = {"high": 80, "medium": 60, "low": 40}
        self.jpeg_quality = jpeg_q.get(quality, 60)
        self.max_width = {"high": 1280, "medium": 960, "low": 640}.get(quality, 960)

    def start(self):
        if mss is None or Image is None or np is None:
            self.log("Error: pip install mss pillow numpy")
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        n = len(self.destinations)
        if self.window_title:
            self.log(f"Window: \"{self.window_title}\" -> {n} viewer(s)")
        elif self.monitor_index is not None and self.monitor_index > 0:
            self.log(f"Monitor {self.monitor_index} -> {n} viewer(s)")
        else:
            self.log(f"Full screen -> {n} viewer(s)")

    def _fragment_data(self, seq, data):
        total = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
        return [
            struct.pack("!IHHH", seq, total, i, len(chunk)) + chunk
            for i in range(total)
            for chunk in [data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]]
        ]

    def _get_capture_region(self, sct):
        if self.window_title:
            rect = get_window_rect(self.window_title)
            if rect:
                return {"left": rect.left, "top": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top}
            self.log("Window not found, capturing full screen")
        if self.monitor_index is not None and self.monitor_index < len(sct.monitors):
            m = sct.monitors[self.monitor_index]
            return {"left": m["left"], "top": m["top"],
                    "width": m["width"], "height": m["height"]}
        return sct.monitors[1]

    def _capture_loop(self):
        with mss.MSS() as sct:
            interval = 1.0 / self.fps
            seq = 0
            log_timer = time.time()
            bytes_sent = 0
            monitor = self._get_capture_region(sct)
            w, h = monitor["width"], monitor["height"]
            self.log(f"Resolution: {w}x{h} -> {self.max_width}px max")

            while self.running:
                t0 = time.perf_counter()
                try:
                    if self.window_title:
                        monitor = self._get_capture_region(sct)
                    img = sct.grab(monitor)
                    pil_img = Image.frombytes("RGB", img.size, img.rgb)
                    # downscale if needed for speed
                    if pil_img.width > self.max_width:
                        ratio = self.max_width / pil_img.width
                        new_size = (self.max_width, int(pil_img.height * ratio))
                        pil_img = pil_img.resize(new_size, Image.BILINEAR)
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=self.jpeg_quality, optimize=False)
                    data = buf.getvalue()
                    for dest in self.destinations:
                        for chunk in self._fragment_data(seq, data):
                            self.sock.sendto(chunk, dest)
                    bytes_sent += len(data) * len(self.destinations)
                    seq += 1

                    if time.time() - log_timer >= 5.0:
                        kbps = bytes_sent * 8 / 5 / 1000
                        self.log(f"Sent: {seq} frames | {w}x{h} | ~{int(kbps)} kbps | ~{len(data)//1024}KB/frame")
                        log_timer = time.time()
                        bytes_sent = 0
                except Exception as e:
                    self.log(f"Ошибка захвата: {e}")
                elapsed = time.perf_counter() - t0
                sleep = interval - elapsed
                if sleep > 0:
                    time.sleep(sleep)

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self.sock:
            self.sock.close()


# ── Receiver ────────────────────────────────────────────────────────
class PyReceiver:
    def __init__(self, port, frame_callback=None, stats_callback=None, log_callback=None):
        self.port = port
        self.frame_callback = frame_callback
        self.stats_callback = stats_callback
        self.log = log_callback
        self.running = False
        self.sock = None
        self._thread = None
        self._pending = {}
        self._lock = threading.Lock()
        self._stats = {"fps": 0, "lost": 0, "last_seq": -1, "received": 0}
        self._fps_counter = 0
        self._fps_timer = time.time()
        self._ffplay_proc = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 ** 24)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.3)
        self.running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        if self.log:
            self.log(f"Listening on port {self.port}")

    def _recv_loop(self):
        warned_no_frames = False
        log_timer = time.time()
        bytes_recv = 0
        last_frame_time = 0
        first = True
        ffmpeg_mode = False

        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
                bytes_recv += len(data)

                # detect FFmpeg MPEG-TS stream: sync byte 0x47 at 188-byte boundaries
                if first and len(data) >= 376 and all(data[i] == 0x47 for i in (0, 188, 376)):
                    ffmpeg_mode = True
                    if self.log:
                        self.log("FFmpeg stream detected, starting decode...")
                    self.sock.close()
                    self._ffmpeg_mode = True
                    threading.Thread(target=self._launch_ffmpeg_pipe, args=(self.port,), daemon=True).start()
                    return

                if len(data) < 10:
                    continue
                if data == b"ping":
                    continue
                seq, total, idx, size = struct.unpack("!IHHH", data[:10])
                payload = data[10:10 + size]

                with self._lock:
                    if seq not in self._pending:
                        self._pending[seq] = {"total": total, "chunks": {}, "ts": time.time()}
                    pen = self._pending[seq]
                    pen["chunks"][idx] = payload

                    if len(pen["chunks"]) == total:
                        jpg = b"".join(pen["chunks"][i] for i in range(total))
                        image_size = len(jpg)
                        del self._pending[seq]
                        if self._stats["last_seq"] >= 0:
                            gap = seq - self._stats["last_seq"] - 1
                            if gap > 0:
                                self._stats["lost"] += gap
                        self._stats["last_seq"] = seq
                        self._stats["received"] += 1
                        warned_no_frames = False
                        last_frame_time = time.time()
                        now = time.time()
                        for s in list(self._pending.keys()):
                            if now - self._pending[s]["ts"] > 2.0:
                                del self._pending[s]

                        if first:
                            first = False
                            if self.log:
                                self.log(f"First frame from {addr[0]}:{addr[1]} | {image_size // 1024}KB")

                        if self.frame_callback:
                            img = Image.open(io.BytesIO(jpg))
                            self.frame_callback(img)

                        self._fps_counter += 1
                        now = time.time()
                        if now - self._fps_timer >= 1.0:
                            self._stats["fps"] = self._fps_counter
                            self._fps_counter = 0
                            self._fps_timer = now
                            if self.stats_callback:
                                self.stats_callback(self._stats)

                if time.time() - log_timer >= 5.0:
                    kbps = bytes_recv * 8 / 5 / 1000
                    if self.log:
                        self.log(f"Received: {self._stats['received']} frames | ~{int(kbps)} kbps | FPS: {self._stats['fps']} | Lost: {self._stats['lost']}")
                    log_timer = time.time()
                    bytes_recv = 0

            except socket.timeout:
                if last_frame_time > 0 and time.time() - last_frame_time > 5.0 and not warned_no_frames:
                    warned_no_frames = True
                    if self.log:
                        self.log(f"No frames for {int(time.time()-last_frame_time)}s — check host")
                pass
            except Exception as e:
                if self.log:
                    self.log(f"Receive error: {e}")

    def _launch_ffmpeg_pipe(self, local_port):
        """Launch ffplay directly on the UDP stream (separate window)."""
        try:
            if not FFPLAY_BIN.exists():
                if self.log:
                    self.log("ffplay not found")
                return
            if self.log:
                self.log(f"Starting ffplay on UDP port {local_port}...")
            cmd = [
                str(FFPLAY_BIN), "-noborder", "-window_title", "ScreenShare",
                "-fflags", "nobuffer", "-flags", "low_delay",
                "-max_delay", "100000",
                "-i", f"udp://0.0.0.0:{local_port}?buffer_size=65536&overrun_nonfatal=1",
            ]
            self._ffplay_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if self.log:
                self.log("ffplay started (separate window)")
            if self.frame_callback:
                self.frame_callback(None)
            self._ffplay_proc.wait()
        except Exception as e:
            if self.log:
                self.log(f"Ошибка ffplay: {e}")
        finally:
            self._ffplay_proc = None
            # signal stop to PyReceiver
            self.running = False

    def stop(self):
        self.running = False
        if self._ffplay_proc:
            try:
                self._ffplay_proc.terminate()
                self._ffplay_proc.wait(timeout=3)
            except:
                try:
                    self._ffplay_proc.kill()
                except:
                    pass
            self._ffplay_proc = None
        self._ffmpeg_mode = False
        if self._thread:
            self._thread.join(timeout=2)
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None


# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════

# ── Design tokens (Catppuccin Mocha) ────────────────────────────────
BASE      = "#1e1e2e"   # window background
MANTLE    = "#181825"   # recessed areas (log, video letterbox)
SURFACE0  = "#313244"   # cards, inputs
SURFACE1  = "#45475a"   # borders, hover
SURFACE2  = "#585b70"   # stronger hover
TEXT      = "#cdd6f4"
SUBTEXT1  = "#bac2de"
SUBTEXT0  = "#a6adc8"   # secondary copy
OVERLAY0  = "#6c7086"   # muted / disabled
BLUE      = "#89b4fa"
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
PEACH     = "#fab387"
YELLOW    = "#f9e2af"

# Legacy aliases — kept so existing call sites keep working.
COLOR_BG = BASE
COLOR_FG = TEXT
COLOR_ACCENT = BLUE
COLOR_SUCCESS = GREEN
COLOR_ERROR = RED
COLOR_SURFACE = SURFACE0
COLOR_RADMIN = PEACH

FONT_FAMILY = "Segoe UI"
FONT = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)
FONT_MED = (FONT_FAMILY, 11)
FONT_BIG = (FONT_FAMILY, 14, "bold")
FONT_HUGE = (FONT_FAMILY, 30, "bold")
FONT_LABEL = (FONT_FAMILY, 9, "bold")   # section captions
FONT_BTN = (FONT_FAMILY, 10, "bold")
FONT_ACTION = (FONT_FAMILY, 13, "bold")  # primary call-to-action
FONT_MONO = ("Consolas", 11)
FONT_MONO_SM = ("Consolas", 10)

PAD = 18        # window gutter
GAP = 10        # gap between cards

# Button variants: resting bg, fg, hover bg
BUTTON_VARIANTS = {
    "primary": (BLUE,     BASE, "#a6c8ff"),
    "success": (GREEN,    BASE, "#bdead9"),
    "danger":  (RED,      BASE, "#f7a8bf"),
    "accent":  (PEACH,    BASE, "#fcc9a4"),
    "ghost":   (SURFACE0, TEXT, SURFACE1),
    "subtle":  (BASE,     SUBTEXT0, SURFACE0),
}

# Text of the host button while streaming — checked by _toggle_host.
HOST_BTN_STOP = "■  Остановить"
HOST_BTN_START = "▶  Начать трансляцию"


class ScreenShareApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("560x520")
        self.root.configure(bg=BASE)
        self.root.resizable(True, True)
        self._init_theme()
        self._center_window()
        self._build_main_menu()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._audio_proc = None

    # ── Theme / widget factories ─────────────────────────────────────
    def _init_theme(self):
        """Style the ttk widgets — the native Windows theme ignores colours,
        so switch to 'clam' first, which honours them."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "SS.TCombobox", fieldbackground=SURFACE0, background=SURFACE0,
            foreground=TEXT, arrowcolor=SUBTEXT0, bordercolor=SURFACE1,
            lightcolor=SURFACE0, darkcolor=SURFACE0, insertcolor=TEXT,
            selectbackground=SURFACE1, selectforeground=TEXT,
            padding=(10, 7), arrowsize=14, relief="flat",
        )
        style.map(
            "SS.TCombobox",
            fieldbackground=[("readonly", SURFACE0), ("disabled", BASE)],
            foreground=[("disabled", OVERLAY0)],
            bordercolor=[("focus", BLUE), ("active", SURFACE2)],
            arrowcolor=[("active", TEXT), ("disabled", OVERLAY0)],
        )
        # the dropdown is a plain Tk listbox and is only reachable via options
        for opt, val in (
            ("background", SURFACE0), ("foreground", TEXT),
            ("selectBackground", BLUE), ("selectForeground", BASE),
            ("borderWidth", 0),
        ):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)

        # drop the stepper arrows — a bare thumb reads far cleaner
        style.layout("SS.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(
            "SS.Vertical.TScrollbar", background=SURFACE1, troughcolor=MANTLE,
            bordercolor=MANTLE, lightcolor=SURFACE1, darkcolor=SURFACE1,
            width=8, relief="flat",
        )
        style.map("SS.Vertical.TScrollbar", background=[("active", SURFACE2)])

        # checkbuttons sit on both the window background and on cards
        for name, bg in (("SS.TCheckbutton", BASE), ("SS.Card.TCheckbutton", SURFACE0)):
            style.configure(
                name, background=bg, foreground=SUBTEXT1,
                indicatorbackground=MANTLE, indicatorforeground=BASE,
                bordercolor=SURFACE2, focuscolor=bg,
                padding=(2, 4), font=FONT_SMALL,
            )
            style.map(
                name,
                background=[("active", bg)],
                foreground=[("active", TEXT), ("disabled", OVERLAY0)],
                indicatorforeground=[("selected", BASE)],
                indicatorbackground=[("selected", BLUE), ("active", SURFACE1),
                                     ("disabled", SURFACE0)],
            )

    def _post(self, fn, *args):
        """Hand work to the Tk thread; a no-op once the app is tearing down."""
        try:
            self.root.after(0, fn, *args)
        except (tk.TclError, RuntimeError):
            pass

    @staticmethod
    def _hover(widget, rest_bg, hover_bg):
        """Flat Tk buttons have no hover state — give them one."""
        widget._rest_bg = rest_bg
        widget._hover_bg = hover_bg
        widget.bind("<Enter>", lambda e: widget.config(bg=widget._hover_bg), add="+")
        widget.bind("<Leave>", lambda e: widget.config(bg=widget._rest_bg), add="+")

    def _paint(self, button, variant):
        """Recolour a button and keep its hover state consistent."""
        bg, fg, hover = BUTTON_VARIANTS[variant]
        button._rest_bg, button._hover_bg = bg, hover
        try:
            under_cursor = button.winfo_containing(
                *button.winfo_pointerxy()) is button
        except tk.TclError:
            under_cursor = False
        button.config(bg=hover if under_cursor else bg, fg=fg,
                      activebackground=hover, activeforeground=fg)

    def _btn(self, parent, text, command, variant="ghost", font=None,
             padx=14, pady=7, **kw):
        bg, fg, hover = BUTTON_VARIANTS[variant]
        b = tk.Button(parent, text=text, command=command, font=font or FONT_BTN,
                      bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                      bd=0, relief=tk.FLAT, highlightthickness=0,
                      cursor="hand2", padx=padx, pady=pady, **kw)
        self._hover(b, bg, hover)
        return b

    def _card(self, parent, **pack_kw):
        """A surface panel with a hairline border."""
        outer = tk.Frame(parent, bg=SURFACE1)
        outer.pack(**pack_kw)
        inner = tk.Frame(outer, bg=SURFACE0)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        return inner

    @staticmethod
    def _caption(parent, text, bg, pady=(12, 6), **kw):
        return tk.Label(parent, text=text.upper(), font=FONT_LABEL, bg=bg,
                        fg=OVERLAY0, anchor="w", **kw)

    def _make_log(self, parent, height, bg=MANTLE):
        """Text widget + themed scrollbar (ScrolledText's own bar can't be styled)."""
        wrap = tk.Frame(parent, bg=bg)
        txt = tk.Text(wrap, height=height, font=FONT_MONO_SM, bg=bg, fg=SUBTEXT0,
                      insertbackground=TEXT, bd=0, relief=tk.FLAT,
                      highlightthickness=0, padx=10, pady=8,
                      state="disabled", wrap="word")
        bar = ttk.Scrollbar(wrap, orient="vertical", style="SS.Vertical.TScrollbar",
                            command=txt.yview)
        txt.config(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return wrap, txt

    def _ip_rows(self, parent, on_copy):
        """Render the local IP list — Radmin addresses get a badge."""
        for ip in get_local_ips():
            is_radmin = ip.startswith("26.")
            row = tk.Frame(parent, bg=SURFACE0)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=ip, font=FONT_MONO, bg=SURFACE0,
                     fg=PEACH if is_radmin else TEXT).pack(side=tk.LEFT)
            if is_radmin:
                tk.Label(row, text=" RADMIN VPN ", font=(FONT_FAMILY, 8, "bold"),
                         bg=SURFACE1, fg=PEACH).pack(side=tk.LEFT, padx=8)
            self._btn(row, "Копировать", lambda i=ip: on_copy(i),
                      variant="ghost", font=FONT_SMALL, padx=10, pady=3
                      ).pack(side=tk.RIGHT)

    def _on_close(self):
        if self._audio_proc:
            try:
                self._audio_proc.terminate()
            except:
                pass
        kill_ffmpeg()
        self.root.destroy()

    def _center_window(self, w=None, h=None):
        self.root.update_idletasks()
        w = w or self.root.winfo_reqwidth()
        h = h or self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _clear(self):
        for w in self.root.winfo_children():
            w.destroy()

    def _set_window(self, w, h, title, min_w=None, min_h=None):
        self._clear()
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(min_w or w, min_h or h)
        self._center_window(w, h)
        self.root.title(f"{APP_NAME} — {title}" if title else APP_NAME)

    def _screen_header(self, title, subtitle, back_command):
        bar = tk.Frame(self.root, bg=BASE)
        bar.pack(fill=tk.X, padx=PAD, pady=(PAD, 4))
        self._btn(bar, "←", back_command, variant="ghost",
                  font=(FONT_FAMILY, 12), padx=11, pady=4).pack(side=tk.LEFT)
        box = tk.Frame(bar, bg=BASE)
        box.pack(side=tk.LEFT, padx=12)
        tk.Label(box, text=title, font=FONT_BIG, bg=BASE, fg=TEXT, anchor="w"
                 ).pack(anchor="w")
        tk.Label(box, text=subtitle, font=FONT_SMALL, bg=BASE, fg=OVERLAY0,
                 anchor="w").pack(anchor="w")
        return bar

    def _menu_card(self, parent, icon, title, subtitle, accent, command):
        """A large clickable panel — the two front-door choices."""
        outer = tk.Frame(parent, bg=SURFACE1, cursor="hand2")
        outer.pack(fill=tk.X, pady=6)
        stripe = tk.Frame(outer, bg=accent, width=4)
        stripe.pack(side=tk.LEFT, fill=tk.Y)
        body = tk.Frame(outer, bg=SURFACE0, cursor="hand2")
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 1), pady=1)

        ico = tk.Label(body, text=icon, font=(FONT_FAMILY, 26), bg=SURFACE0, fg=accent)
        ico.pack(side=tk.LEFT, padx=(18, 14), pady=16)
        txt = tk.Frame(body, bg=SURFACE0)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=16)
        t1 = tk.Label(txt, text=title, font=(FONT_FAMILY, 13, "bold"),
                      bg=SURFACE0, fg=TEXT, anchor="w")
        t1.pack(fill=tk.X)
        t2 = tk.Label(txt, text=subtitle, font=FONT_SMALL, bg=SURFACE0,
                      fg=SUBTEXT0, anchor="w")
        t2.pack(fill=tk.X, pady=(2, 0))
        arrow = tk.Label(body, text="›", font=(FONT_FAMILY, 22), bg=SURFACE0, fg=OVERLAY0)
        arrow.pack(side=tk.RIGHT, padx=18)

        kids = (body, ico, txt, t1, t2, arrow)

        def enter(_=None):
            for w in kids:
                w.config(bg=SURFACE1)
            arrow.config(fg=accent)

        def leave(_=None):
            for w in kids:
                w.config(bg=SURFACE0)
            arrow.config(fg=OVERLAY0)

        for w in (outer,) + kids:
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", leave, add="+")
            w.bind("<Button-1>", lambda e: command(), add="+")
        return outer

    def _build_main_menu(self):
        self._set_window(560, 520, "")

        head = tk.Frame(self.root, bg=BASE)
        head.pack(fill=tk.X, padx=PAD + 6, pady=(44, 0))
        wordmark = tk.Frame(head, bg=BASE)
        wordmark.pack()
        tk.Label(wordmark, text=APP_NAME, font=FONT_HUGE, bg=BASE, fg=TEXT
                 ).pack(side=tk.LEFT)
        tk.Label(wordmark, text="●", font=(FONT_FAMILY, 16), bg=BASE, fg=BLUE
                 ).pack(side=tk.LEFT, padx=(4, 0), pady=(14, 0))
        tk.Label(head, text="Демонстрация экрана P2P  ·  Radmin VPN или локальная сеть",
                 font=FONT, bg=BASE, fg=SUBTEXT0).pack(pady=(6, 0))

        cards = tk.Frame(self.root, bg=BASE)
        cards.pack(fill=tk.X, padx=PAD + 6, pady=(34, 0))
        self._menu_card(cards, "🖥", "Хостить", "Показать свой экран другим",
                        BLUE, self._open_host)
        self._menu_card(cards, "👁", "Подключиться", "Смотреть чужой экран",
                        GREEN, self._open_join)

        bot = tk.Frame(self.root, bg=BASE)
        bot.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD + 6, pady=18)
        status_wrap = tk.Frame(bot, bg=BASE)
        status_wrap.pack(side=tk.LEFT)
        self._ffmpeg_dot = tk.Label(status_wrap, text="●", font=FONT_SMALL,
                                    bg=BASE, fg=OVERLAY0)
        self._ffmpeg_dot.pack(side=tk.LEFT, padx=(0, 6))
        self._ffmpeg_status = tk.Label(status_wrap, text="", font=FONT_SMALL,
                                       bg=BASE, fg=SUBTEXT0)
        self._ffmpeg_status.pack(side=tk.LEFT)

        self._update_ffmpeg_status()

        if not FFMPEG_BIN.exists():
            self._btn(bot, "Скачать FFmpeg", self._dl_ffmpeg, variant="primary",
                      font=FONT_SMALL, padx=12, pady=5).pack(side=tk.RIGHT)

    def _get_ffmpeg_info(self):
        if not FFMPEG_BIN.exists():
            return None, "FFmpeg not found"
        try:
            r = subprocess.run(
                [str(FFMPEG_BIN), "-version"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW)
            ver = r.stdout.split("\n")[0].strip() if r.stdout else "?"
            has_nvenc = "h264_nvenc" in subprocess.run(
                [str(FFMPEG_BIN), "-encoders"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW).stdout
            return ver, "NVENC" if has_nvenc else "CPU only"
        except:
            return None, "Error"

    def _update_ffmpeg_status(self):
        ver, info = self._get_ffmpeg_info()
        if ver:
            # "ffmpeg version 8.1.1-essentials_build-www.gyan.dev Copyright..."
            m = re.search(r"version\s+(\S+)", ver)
            ver_short = m.group(1).split("-")[0] if m else "?"
            badge = "аппаратное ускорение" if info == "NVENC" else "только CPU"
            text, color = f"FFmpeg {ver_short}  ·  {badge}", GREEN
        elif info == "FFmpeg not found":
            text, color = "FFmpeg не установлен — нажмите «Скачать»", RED
        else:
            text, color = "FFmpeg: ошибка запуска", RED
        self._ffmpeg_status.config(text=text, fg=SUBTEXT0)
        if self._widget_alive(getattr(self, "_ffmpeg_dot", None)):
            self._ffmpeg_dot.config(fg=color)

    def _set_ffmpeg_status_text(self, text, ok=None):
        try:
            if ok is None:
                self._ffmpeg_status.config(text=text, fg=COLOR_FG)
            else:
                self._ffmpeg_status.config(text=text, fg=COLOR_SUCCESS if ok else COLOR_ERROR)
        except tk.TclError:
            pass

    def _dl_ffmpeg(self):
        def log(m):
            self._post(self._set_ffmpeg_status_text, m)
        def task():
            ok = download_ffmpeg(log_callback=log)
            self._post(self._set_ffmpeg_status_text,
                       "FFmpeg установлен" if ok else "Не удалось скачать", ok)
        threading.Thread(target=task, daemon=True).start()

    # ── Refresh windows list ─────────────────────────────────────────
    def _refresh_capture_list(self):
        items = []
        monitors = list_monitors()
        for m in monitors:
            label = f"[Monitor {m['index']}] {m['label']}  {m['width']}x{m['height']}"
            if m['index'] == 0:
                label = "[Screen] All monitors  " + f"{m['width']}x{m['height']}"
            items.append((m['index'], label, "monitor"))

        items.append((-1, "--- Windows ---", "sep"))

        try:
            for hwnd, title, rect in list_windows():
                if title and title != "ScreenShare" and not title.startswith("Program Manager"):
                    items.append((hwnd, f"[Window] {title}", "window"))
        except:
            pass

        display = [it[1] for it in items]
        self._capture_menu["values"] = display
        if display and self._capture_var.get() not in display:
            self._capture_var.set(display[0])
        self._capture_items = items

    # ── Host ─────────────────────────────────────────────────────────
    def _open_host(self):
        self._set_window(780, 880, "Хостинг", min_w=680, min_h=640)
        self._host_sender = None
        self._preview_running = False
        self._dest_ips = []

        self._screen_header("Хостинг", "Вы показываете свой экран",
                            self._build_main_menu)

        # ── your address ──────────────────────────────────────────────
        ip_card = self._card(self.root, fill=tk.X, padx=PAD, pady=(GAP, 0))
        inner = tk.Frame(ip_card, bg=SURFACE0)
        inner.pack(fill=tk.X, padx=16, pady=(2, 14))
        self._caption(inner, "Ваш адрес — передайте его зрителям", SURFACE0
                      ).pack(fill=tk.X, pady=(12, 8))
        self._ip_rows(inner, self._copy_ip)

        # ── viewers ───────────────────────────────────────────────────
        v_card = self._card(self.root, fill=tk.X, padx=PAD, pady=(GAP, 0))
        vin = tk.Frame(v_card, bg=SURFACE0)
        vin.pack(fill=tk.X, padx=16, pady=(2, 14))
        self._viewers_caption = self._caption(vin, "Зрители", SURFACE0)
        self._viewers_caption.pack(fill=tk.X, pady=(12, 8))

        lstf = tk.Frame(vin, bg=MANTLE)
        lstf.pack(fill=tk.X)
        self._dest_listbox = tk.Listbox(
            lstf, font=FONT_MONO, bg=MANTLE, fg=TEXT, bd=0, height=4,
            relief=tk.FLAT, highlightthickness=0, activestyle="none",
            selectbackground=SURFACE1, selectforeground=TEXT)
        self._dest_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                padx=(8, 0), pady=6)
        vbar = ttk.Scrollbar(lstf, orient="vertical", style="SS.Vertical.TScrollbar",
                             command=self._dest_listbox.yview)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._dest_listbox.config(yscrollcommand=vbar.set)

        addf = tk.Frame(vin, bg=SURFACE0)
        addf.pack(fill=tk.X, pady=(8, 0))
        entry_wrap = tk.Frame(addf, bg=SURFACE1)
        entry_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._dest_entry = tk.Entry(
            entry_wrap, font=FONT_MONO, bg=MANTLE, fg=TEXT, insertbackground=TEXT,
            bd=0, relief=tk.FLAT, highlightthickness=0)
        self._dest_entry.pack(fill=tk.X, padx=1, pady=1, ipady=6, ipadx=8)
        self._dest_entry.bind("<Return>", lambda e: self._add_dest())
        self._btn(addf, "Добавить", self._add_dest, variant="primary",
                  font=FONT_SMALL, padx=12, pady=6).pack(side=tk.LEFT)
        self._btn(addf, "Удалить", self._remove_dest, font=FONT_SMALL,
                  padx=12, pady=6).pack(side=tk.LEFT, padx=4)
        self._btn(addf, "Вставить", self._paste_to_host, font=FONT_SMALL,
                  padx=12, pady=6).pack(side=tk.LEFT)
        self._btn(addf, "Найти в VPN", self._scan_radmin, variant="accent",
                  font=FONT_SMALL, padx=12, pady=6).pack(side=tk.LEFT, padx=(4, 0))
        self._load_viewers()
        self._update_viewers_caption()

        # ── source & quality ──────────────────────────────────────────
        s_card = self._card(self.root, fill=tk.X, padx=PAD, pady=(GAP, 0))
        sin = tk.Frame(s_card, bg=SURFACE0)
        sin.pack(fill=tk.X, padx=16, pady=(2, 14))
        self._caption(sin, "Что транслировать", SURFACE0).pack(fill=tk.X, pady=(12, 8))

        cf = tk.Frame(sin, bg=SURFACE0)
        cf.pack(fill=tk.X)
        self._capture_var = tk.StringVar()
        self._capture_menu = ttk.Combobox(
            cf, textvariable=self._capture_var, state="readonly",
            style="SS.TCombobox", font=FONT_SMALL, takefocus=0)
        self._capture_menu.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._btn(cf, "⟳", self._refresh_capture_list, font=FONT_MED,
                  padx=11, pady=5).pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_capture_list()

        qf = tk.Frame(sin, bg=SURFACE0)
        qf.pack(fill=tk.X, pady=(10, 0))
        tk.Label(qf, text="Качество", font=FONT_SMALL, bg=SURFACE0, fg=SUBTEXT0
                 ).pack(side=tk.LEFT, padx=(0, 8))
        self._host_quality = tk.StringVar(value="medium")
        ttk.Combobox(qf, textvariable=self._host_quality,
                     values=list(QUALITY_PRESETS.keys()), state="readonly",
                     style="SS.TCombobox", width=9, font=FONT_SMALL,
                     takefocus=0).pack(side=tk.LEFT)

        opts = tk.Frame(qf, bg=SURFACE0)
        opts.pack(side=tk.RIGHT)
        self._use_tcp = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="TCP", variable=self._use_tcp,
                        style="SS.Card.TCheckbutton").pack(side=tk.LEFT, padx=(0, 14))
        self._audio_loopback, self._audio_mics = list_audio_devices()
        has_sys_audio = len(self._audio_loopback) > 0
        has_any_audio = has_sys_audio or len(self._audio_mics) > 0
        self._use_audio = tk.BooleanVar(value=has_sys_audio)
        audio_label = ("Звук системы" if has_sys_audio else
                       "Звук с микрофона" if self._audio_mics else "Звук недоступен")
        ttk.Checkbutton(opts, text=audio_label, variable=self._use_audio,
                        style="SS.Card.TCheckbutton",
                        state="normal" if has_any_audio else "disabled"
                        ).pack(side=tk.LEFT)

        # ── primary action ────────────────────────────────────────────
        self._host_btn = self._btn(self.root, HOST_BTN_START, self._toggle_host,
                                   variant="primary", font=FONT_ACTION, pady=12)
        self._host_btn.pack(fill=tk.X, padx=PAD, pady=(14, 0))

        # ── bottom-anchored blocks (packed first so preview gets the rest) ──
        fw_row = tk.Frame(self.root, bg=BASE)
        fw_row.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD, pady=(4, 10))
        self._fw_status = tk.Label(fw_row, text="", font=FONT_SMALL,
                                   bg=BASE, fg=OVERLAY0, anchor="w")
        self._fw_status.pack(side=tk.LEFT)

        log_wrap, self._host_log = self._make_log(self.root, height=5)
        log_wrap.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD)
        lf = tk.Frame(self.root, bg=BASE)
        lf.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD, pady=(GAP, 4))
        self._caption(lf, "Журнал", BASE).pack(side=tk.LEFT)
        self._btn(lf, "Копировать", self._copy_host_log, variant="subtle",
                  font=FONT_SMALL, padx=10, pady=2).pack(side=tk.RIGHT)

        # ── live preview fills whatever is left ───────────────────────
        pf = tk.Frame(self.root, bg=SURFACE1)
        pf.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=(GAP, 0))
        self._preview_label = tk.Label(pf, text="Предпросмотр появится после старта",
                                       font=FONT_SMALL, bg=MANTLE, fg=OVERLAY0)
        self._preview_label.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._hlog("Добавьте IP зрителей и нажмите «Начать трансляцию»")
        self._hlog("Зрители должны быть в режиме «Подключиться»")

        threading.Thread(target=lambda: self._check_fw_async(self._fw_status),
                         daemon=True).start()

    def _update_viewers_caption(self):
        if self._widget_alive(getattr(self, "_viewers_caption", None)):
            n = len(self._dest_ips)
            self._viewers_caption.config(
                text=f"ЗРИТЕЛИ · {n}" if n else "ЗРИТЕЛИ · НИ ОДНОГО")

    def _paste_to_host(self):
        try:
            text = self.root.clipboard_get().strip()
            self._dest_entry.delete(0, tk.END)
            self._dest_entry.insert(0, text)
        except:
            pass

    def _select_peer(self, ip):
        self._dest_entry.delete(0, tk.END)
        self._dest_entry.insert(0, ip)
        self._hlog(f"Выбран узел: {ip} — нажмите «Добавить» или Enter")

    def _add_dest(self):
        ip = self._dest_entry.get().strip()
        if not ip:
            return
        if ip in self._dest_ips:
            self._hlog(f"Уже в списке: {ip}")
            return
        self._dest_ips.append(ip)
        self._dest_listbox.insert(tk.END, f"  {ip}")
        self._dest_entry.delete(0, tk.END)
        self._save_viewers()
        self._update_viewers_caption()
        self._hlog(f"Зритель добавлен: {ip}  (всего {len(self._dest_ips)})")

    def _remove_dest(self):
        sel = self._dest_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        ip = self._dest_ips.pop(idx)
        self._dest_listbox.delete(idx)
        self._save_viewers()
        self._update_viewers_caption()
        self._hlog(f"Зритель удалён: {ip}  (всего {len(self._dest_ips)})")

    def _save_viewers(self):
        try:
            VIEWERS_FILE.write_text("\n".join(self._dest_ips), encoding="utf-8")
        except:
            pass

    def _load_viewers(self):
        try:
            if VIEWERS_FILE.exists():
                for line in VIEWERS_FILE.read_text(encoding="utf-8").strip().split("\n"):
                    ip = line.strip()
                    if ip and ip not in self._dest_ips:
                        self._dest_ips.append(ip)
                        self._dest_listbox.insert(tk.END, f"  {ip}")
        except:
            pass

    def _scan_radmin(self):
        self._hlog("Сканирую сеть Radmin VPN...")
        self.root.update()
        def scan():
            peers = scan_radmin_peers(timeout=0.5)
            if peers:
                self.root.after(0, lambda p=peers: self._show_scan_results(p, self._select_peer))
                self._hlog(f"Найдено: {', '.join(peers)}")
            else:
                self.root.after(0, lambda: self._hlog("Узлы Radmin VPN не найдены"))
        threading.Thread(target=scan, daemon=True).start()

    def _scan_for_join(self):
        self._jlog("Ищу хосты в сети Radmin VPN...")
        def scan():
            peers = scan_radmin_peers(timeout=0.5)
            if peers:
                def show(pl):
                    self._show_scan_results(pl, lambda ip: self._jlog(f"Найден узел: {ip} — свой адрес передайте хосту"))
                    self._jlog(f"Найдены узлы: {', '.join(pl)}")
                self.root.after(0, lambda p=peers: show(p))
            else:
                self.root.after(0, lambda: self._jlog("Хосты в сети Radmin VPN не найдены"))
        threading.Thread(target=scan, daemon=True).start()

    def _show_scan_results(self, peers, on_select):
        w = tk.Toplevel(self.root)
        w.title("Узлы Radmin VPN")
        w.configure(bg=BASE)
        w.geometry("400x340")
        w.transient(self.root)
        w.grab_set()
        w.bind("<Escape>", lambda e: w.destroy())

        tk.Label(w, text="Найденные узлы", font=FONT_BIG, bg=BASE, fg=TEXT
                 ).pack(pady=(18, 2))
        tk.Label(w, text=f"в сети найдено: {len(peers)}", font=FONT_SMALL,
                 bg=BASE, fg=OVERLAY0).pack(pady=(0, 14))

        for ip in peers:
            outer = tk.Frame(w, bg=SURFACE1)
            outer.pack(fill=tk.X, padx=PAD, pady=3)
            f = tk.Frame(outer, bg=SURFACE0)
            f.pack(fill=tk.X, padx=1, pady=1)
            tk.Label(f, text=ip, font=FONT_MONO, bg=SURFACE0, fg=PEACH
                     ).pack(side=tk.LEFT, padx=12, pady=8)
            self._btn(f, "Выбрать",
                      lambda i=ip, win=w: (on_select(i), win.destroy()),
                      variant="primary", font=FONT_SMALL, padx=12, pady=4
                      ).pack(side=tk.RIGHT, padx=8)

        self._btn(w, "Закрыть", w.destroy, variant="ghost").pack(pady=14)

    def _check_fw_async(self, label):
        st = check_firewall()
        self._post(self._apply_fw_status, label, st)

    def _apply_fw_status(self, label, st):
        try:
            if st is True:
                label.config(text="✓  Брандмауэр: порт 8888 открыт", fg=GREEN)
            elif st is False:
                label.config(text="✕  Брандмауэр: порт 8888 закрыт", fg=RED)
                self._btn(label.master, "Добавить правило", self._add_fw_rule,
                          variant="danger", font=FONT_SMALL, padx=12, pady=3
                          ).pack(side=tk.LEFT, padx=(12, 0))
            else:
                label.config(text="—  Брандмауэр: проверить не удалось", fg=OVERLAY0)
        except tk.TclError:
            pass



    def _fwlog(self, msg):
        """The firewall notice is shown on both screens — log to whichever is live."""
        if self._widget_alive(getattr(self, "_host_log", None)):
            self._hlog(msg)
        elif self._widget_alive(getattr(self, "_join_log", None)):
            self._jlog(msg)

    @staticmethod
    def _widget_alive(widget):
        try:
            return bool(widget) and bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _add_fw_rule(self):
        ok = add_firewall_rule()
        if ok:
            if self._widget_alive(getattr(self, "_fw_status", None)):
                self._fw_status.config(text="Firewall rule added!", fg=COLOR_SUCCESS)
            if self._widget_alive(getattr(self, "_join_fw", None)):
                self._join_fw.config(text="Firewall rule added!", fg=COLOR_SUCCESS)
            self._fwlog("Правило брандмауэра добавлено для UDP-порта 8888")
        else:
            self._fwlog("Не удалось добавить правило (нужны права администратора)")

    def _copy_ip(self, ip):
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self._hlog(f"Адрес {ip} скопирован")

    def _copy_host_log(self):
        text = self._host_log.get("1.0", tk.END).strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._hlog("Журнал скопирован в буфер обмена")

    def _hlog(self, msg):
        # may be called from background threads (ffmpeg/relay) — marshal to the Tk thread
        self._post(self._hlog_ui, msg)

    def _hlog_ui(self, msg):
        t = time.strftime("%H:%M:%S")
        try:
            self._host_log.config(state="normal")
            self._host_log.insert(tk.END, f"[{t}] {msg}\n")
            self._host_log.see(tk.END)
            self._host_log.config(state="disabled")
        except (tk.TclError, AttributeError):
            pass  # host screen was left or never opened

    def _preview_loop(self, monitor_index, window_title):
        self._preview_running = True
        try:
            with mss.MSS() as sct:
                while self._preview_running and self._host_sender and self._host_sender.running:
                    try:
                        if window_title:
                            rect = get_window_rect(window_title)
                            if rect:
                                region = {"left": rect.left, "top": rect.top,
                                          "width": rect.right - rect.left,
                                          "height": rect.bottom - rect.top}
                            else:
                                region = sct.monitors[1]
                        elif monitor_index is not None and monitor_index < len(sct.monitors):
                            m = sct.monitors[monitor_index]
                            region = {"left": m["left"], "top": m["top"],
                                      "width": m["width"], "height": m["height"]}
                        else:
                            region = sct.monitors[1]
                        img = sct.grab(region)
                        pil_img = Image.frombytes("RGB", img.size, img.rgb)
                        self.root.after(0, self._show_preview, pil_img)
                    except:
                        pass
                    time.sleep(0.1)
        except:
            pass
        self._preview_running = False

    def _show_preview(self, img):
        try:
            pw = self._preview_label.winfo_width() or 660
            ph = self._preview_label.winfo_height() or 140
            img = img.copy()
            img.thumbnail((pw, ph), Image.LANCZOS)
            imgtk = ImageTk.PhotoImage(img)
            self._preview_label.imgtk = imgtk
            self._preview_label.config(image=imgtk)
        except:
            pass

    def _reset_host_button(self):
        self._host_btn.config(text=HOST_BTN_START)
        self._paint(self._host_btn, "primary")
        self._preview_label.imgtk = None
        self._preview_label.config(image="", fg=OVERLAY0,
                                   text="Предпросмотр появится после старта")

    def _toggle_host(self):
        if self._host_sender and self._host_sender.running:
            self._preview_running = False
            try:
                self._host_sender.stop()
            except Exception as e:
                self._hlog(f"Ошибка остановки: {e}")
            self._host_sender = None
            self._reset_host_button()
            self._hlog("Трансляция остановлена")
            return

        # fallback: if sender is somehow lost but button says "Stop", stop anyway
        if self._host_btn.cget("text") == HOST_BTN_STOP:
            self._hlog("Отправитель уже остановлен, сбрасываю интерфейс")
            self._reset_host_button()
            return

        if not self._dest_ips:
            self._hlog("Сначала добавьте хотя бы одного зрителя")
            return

        destinations = [(ip, DEFAULT_PORT) for ip in self._dest_ips]
        quality = self._host_quality.get()

        # audio only when the box is ticked; prefer system loopback over a mic
        audio_device = None
        if self._use_audio.get():
            devices = self._audio_loopback or self._audio_mics
            audio_device = devices[0] if devices else None

        # parse capture selection
        window_title = None
        monitor_index = None
        selected = self._capture_var.get()
        for item in getattr(self, "_capture_items", []):
            if item[1] == selected:
                if item[2] == "window":
                    _, title = item[1].split("] ", 1)
                    window_title = title
                elif item[2] == "monitor" and item[0] > 0:
                    monitor_index = item[0]
                break

        # choose backend (FFmpeg only)
        if not FFMPEG_BIN.exists():
            self._hlog("FFmpeg не найден. Сначала запустите setup.bat")
            return
        if len(destinations) == 0:
            self._hlog("Список зрителей пуст")
            return
        kill_ffmpeg()
        use_tcp = self._use_tcp.get()
        proto = "TCP" if use_tcp else "UDP"
        q_preset = QUALITY_PRESETS[quality]
        fps = int(q_preset["fps"])
        has_nvenc = _probe_nvenc()
        self._hlog(f"FFmpeg{' (NVENC)' if has_nvenc else ' (CPU)'}  ·  {fps} FPS  ·  {proto}")
        if window_title:
            self._hlog(f"Окно: «{window_title}»")
        elif monitor_index is not None:
            self._hlog(f"Монитор {monitor_index}")
        self._host_sender = FfmpegSender(
            destinations, quality, window_title=window_title,
            monitor_index=monitor_index, log_callback=self._hlog,
            use_tcp=use_tcp, audio_device=audio_device)

        self._host_sender.start()
        self._host_btn.config(text=HOST_BTN_STOP)
        self._paint(self._host_btn, "danger")

        # start preview thread (works with both backends)
        t = threading.Thread(target=self._preview_loop,
                             args=(monitor_index, window_title), daemon=True)
        t.start()

    # ── Join ─────────────────────────────────────────────────────────
    def _open_join(self):
        self._set_window(960, 780, "Подключение", min_w=720, min_h=560)
        self._join_receiver = None
        self._current_frame = None
        self._ffplay_active = False
        self._fullscreen_win = None

        self._screen_header("Подключение", "Вы смотрите чужой экран",
                            self._stop_join_and_back)

        # ── your address ──────────────────────────────────────────────
        ip_card = self._card(self.root, fill=tk.X, padx=PAD, pady=(GAP, 0))
        inner = tk.Frame(ip_card, bg=SURFACE0)
        inner.pack(fill=tk.X, padx=16, pady=(2, 14))
        head = tk.Frame(inner, bg=SURFACE0)
        head.pack(fill=tk.X, pady=(12, 8))
        self._caption(head, "Ваш адрес — передайте его хосту", SURFACE0
                      ).pack(side=tk.LEFT)
        self._btn(head, "Найти хосты в VPN", self._scan_for_join, variant="accent",
                  font=FONT_SMALL, padx=12, pady=4).pack(side=tk.RIGHT)
        self._ip_rows(inner, self._jcopy_ip)

        # ── controls ──────────────────────────────────────────────────
        ctl = tk.Frame(self.root, bg=BASE)
        ctl.pack(fill=tk.X, padx=PAD, pady=(GAP, 0))
        self._join_use_tcp = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="TCP", variable=self._join_use_tcp,
                        style="SS.TCheckbutton").pack(side=tk.LEFT, padx=(0, 14))
        self._join_use_audio = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Звук", variable=self._join_use_audio,
                        style="SS.TCheckbutton").pack(side=tk.LEFT)

        pill = tk.Frame(ctl, bg=BASE)
        pill.pack(side=tk.RIGHT)
        self._join_dot = tk.Label(pill, text="●", font=FONT_SMALL, bg=BASE, fg=OVERLAY0)
        self._join_dot.pack(side=tk.LEFT, padx=(0, 6))
        self._join_status = tk.Label(pill, text="Ожидание", font=FONT_SMALL,
                                     bg=BASE, fg=OVERLAY0)
        self._join_status.pack(side=tk.LEFT)

        self._join_btn = self._btn(self.root, "▶  Начать приём", self._toggle_join,
                                   variant="primary", font=FONT_ACTION, pady=12)
        self._join_btn.pack(fill=tk.X, padx=PAD, pady=(GAP, 0))

        # ── bottom-anchored blocks ────────────────────────────────────
        fw_row = tk.Frame(self.root, bg=BASE)
        fw_row.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD, pady=(4, 10))
        self._join_fw = tk.Label(fw_row, text="", font=FONT_SMALL,
                                 bg=BASE, fg=OVERLAY0, anchor="w")
        self._join_fw.pack(side=tk.LEFT)

        log_wrap, self._join_log = self._make_log(self.root, height=4)
        log_wrap.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD)
        lf = tk.Frame(self.root, bg=BASE)
        lf.pack(side=tk.BOTTOM, fill=tk.X, padx=PAD, pady=(GAP, 4))
        self._caption(lf, "Журнал", BASE).pack(side=tk.LEFT)
        self._btn(lf, "Копировать", self._copy_join_log, variant="subtle",
                  font=FONT_SMALL, padx=10, pady=2).pack(side=tk.RIGHT)

        self._stats_var = tk.StringVar(value="FPS —   ·   кадров 0   ·   потерь 0")
        tk.Label(self.root, textvariable=self._stats_var, font=FONT_MONO_SM,
                 bg=BASE, fg=OVERLAY0, anchor="w"
                 ).pack(side=tk.BOTTOM, fill=tk.X, padx=PAD, pady=(6, 0))

        # ── video surface takes the rest ──────────────────────────────
        vf = tk.Frame(self.root, bg=SURFACE1)
        vf.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=(GAP, 0))
        stage = tk.Frame(vf, bg=MANTLE)
        stage.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._video_label = tk.Label(stage, bg=MANTLE)
        self._video_label.pack(fill=tk.BOTH, expand=True)
        self._video_label.bind("<Configure>", self._on_video_resize)
        self._video_label.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())

        self._empty = tk.Label(
            stage, bg=MANTLE, fg=OVERLAY0, justify="center",
            font=(FONT_FAMILY, 11),
            text="Скопируйте свой адрес, отправьте его хосту\n"
                 "и нажмите «Начать приём»")
        self._empty.place(relx=0.5, rely=0.5, anchor="center")

        self._fs_btn = self._btn(stage, "⛶", self._toggle_fullscreen,
                                 variant="ghost", font=FONT_MED, padx=8, pady=3)
        self._fs_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-8, y=8)

        self._jlog("1. Скопируйте свой адрес и отправьте хосту")
        self._jlog("2. Нажмите «Начать приём» и ждите")
        self._jlog(f"Приём на UDP-порту {DEFAULT_PORT}")

        threading.Thread(target=lambda: self._check_fw_async(self._join_fw),
                         daemon=True).start()

    def _set_join_status(self, text, color):
        for widget, attr in ((getattr(self, "_join_status", None), "text"),
                             (getattr(self, "_join_dot", None), "dot")):
            if not self._widget_alive(widget):
                continue
            widget.config(fg=color, **({"text": text} if attr == "text" else {}))

    def _show_empty_state(self, show):
        if not self._widget_alive(getattr(self, "_empty", None)):
            return
        if show:
            self._empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self._empty.place_forget()

    def _jlog(self, msg):
        # may be called from background threads (receiver/ffmpeg) — marshal to the Tk thread
        self._post(self._jlog_ui, msg)

    def _jlog_ui(self, msg):
        t = time.strftime("%H:%M:%S")
        try:
            self._join_log.config(state="normal")
            self._join_log.insert(tk.END, f"[{t}] {msg}\n")
            self._join_log.see(tk.END)
            self._join_log.config(state="disabled")
        except (tk.TclError, AttributeError):
            pass  # join screen was left or never opened

    def _copy_join_log(self):
        text = self._join_log.get("1.0", tk.END).strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._jlog("Журнал скопирован в буфер обмена")

    def _toggle_join(self):
        if self._join_receiver and self._join_receiver.running:
            self._stop_join()
            return

        if getattr(self, '_tcp_server', None):
            self._stop_join()
            return

        if self._join_use_tcp.get() and FFPLAY_BIN.exists():
            self._launch_ffplay_tcp()
            return

        self._join_receiver = PyReceiver(
            DEFAULT_PORT,
            frame_callback=self._on_frame,
            stats_callback=self._on_stats,
            log_callback=self._jlog,
        )
        self._join_receiver.start()
        if self._join_use_audio.get() and FFPLAY_BIN.exists():
            threading.Thread(target=self._launch_audio_player, daemon=True).start()
        self._join_btn.config(text="■  Остановить приём")
        self._paint(self._join_btn, "danger")
        self._set_join_status("Слушаю порт 8888", YELLOW)
        self._show_empty_state(False)
        self._jlog("Приём запущен на порту 8888")
        self._jlog("Жду поток от хоста...")
        self._jlog("Убедитесь, что хост начал трансляцию на ваш адрес")

    def _launch_ffplay_tcp(self):
        kill_ffmpeg()
        self._jlog("Запускаю TCP-сервер для потока FFmpeg...")
        self._show_empty_state(False)
        self._set_join_status("Жду TCP-подключение", YELLOW)
        self._join_btn.config(text="■  Остановить")
        self._paint(self._join_btn, "danger")
        self._ffplay_tcp_proc = None
        if self._join_use_audio.get() and FFPLAY_BIN.exists():
            threading.Thread(target=self._launch_audio_player, daemon=True).start()
        t = threading.Thread(target=self._tcp_server_thread, daemon=True)
        t.start()

    def _launch_audio_player(self):
        """Play incoming audio from host via ffplay -nodisp."""
        try:
            self._jlog("Запускаю аудиоплеер...")
            proc = subprocess.Popen(
                [str(FFPLAY_BIN), "-nodisp", "-autoexit",
                 "-fflags", "nobuffer",
                 "-i", f"udp://0.0.0.0:{AUDIO_PORT}?buffer_size=65536"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._audio_proc = proc
            proc.wait()
        except Exception as e:
            self._jlog(f"Ошибка аудиоплеера: {e}")
        finally:
            self._jlog("Аудиоплеер остановлен")

    def _tcp_server_thread(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_server = server
        try:
            server.bind(("0.0.0.0", DEFAULT_PORT))
            server.listen(1)
            server.settimeout(1.0)
            self.root.after(0, lambda: self._jlog(f"TCP-сервер слушает порт {DEFAULT_PORT}"))
        except Exception as e:
            self.root.after(0, lambda e=e: self._jlog(f"Ошибка привязки TCP-сервера: {e}"))
            server.close()
            self._tcp_server = None
            self.root.after(0, self._stop_join)
            return

        while True:
            try:
                conn, addr = server.accept()
                self.root.after(0, lambda a=addr: self._jlog(f"TCP-подключение с {a[0]}:{a[1]}"))
                self.root.after(0, lambda: self._set_join_status("В эфире", GREEN))
            except socket.timeout:
                if not self._join_use_tcp.get():
                    break
                continue
            except OSError:
                break

            try:
                ffplay = subprocess.Popen(
                    [str(FFPLAY_BIN), "-noborder", "-window_title", "ScreenShare",
                     "-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay",
                     "-framedrop",
                     "-i", "pipe:0"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self._ffplay_tcp_proc = ffplay
                self.root.after(0, lambda: setattr(self, '_ffplay_active', True))
            except Exception as e:
                self.root.after(0, lambda e=e: self._jlog(f"Ошибка ffplay: {e}"))
                conn.close()
                continue

            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    ffplay.stdin.write(data)
                    ffplay.stdin.flush()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                self.root.after(0, lambda: setattr(self, '_ffplay_active', False))
                conn.close()
                ffplay.terminate()
                try:
                    ffplay.wait(timeout=3)
                except:
                    ffplay.kill()
                self._ffplay_tcp_proc = None
                self.root.after(0, lambda: self._jlog("TCP отключён, жду переподключения..."))
                self.root.after(0, lambda: self._set_join_status("Жду TCP-подключение", YELLOW))

        server.close()
        self.root.after(0, self._stop_join)

    def _stop_join(self):
        self._exit_fullscreen()
        # kill audio process (always safe even if None)
        if self._audio_proc:
            try:
                self._audio_proc.terminate()
                self._audio_proc.wait(timeout=3)
            except:
                try:
                    self._audio_proc.kill()
                except:
                    pass
            self._audio_proc = None
        # kill ffplay TCP process if running
        if hasattr(self, '_ffplay_tcp_proc') and self._ffplay_tcp_proc:
            try:
                self._ffplay_tcp_proc.terminate()
                self._ffplay_tcp_proc.wait(timeout=3)
            except:
                try:
                    self._ffplay_tcp_proc.kill()
                except:
                    pass
            self._ffplay_tcp_proc = None
        if hasattr(self, '_tcp_server') and self._tcp_server:
            try:
                self._tcp_server.close()
            except:
                pass
            self._tcp_server = None
        self._join_use_tcp.set(False)
        if self._join_receiver:
            self._join_receiver.stop()
            self._join_receiver = None
        self._join_btn.config(text="▶  Начать приём")
        self._paint(self._join_btn, "primary")
        self._set_join_status("Ожидание", OVERLAY0)
        self._stats_var.set("FPS —   ·   кадров 0   ·   потерь 0")
        self._current_frame = None
        if self._widget_alive(getattr(self, "_video_label", None)):
            self._video_label.imgtk = None
            self._video_label.config(image="")
        self._show_empty_state(True)
        self._jlog("Отключено")

    def _jcopy_ip(self, ip):
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self._jlog(f"Адрес {ip} скопирован — отправьте его хосту")

    def _stop_join_and_back(self):
        self._stop_join()
        self._build_main_menu()

    def _on_frame(self, img):
        if img is None:
            self._ffplay_active = True
            return
        self._ffplay_active = False
        self._current_frame = img
        # cancel pending display update to skip intermediate frames
        if hasattr(self, '_display_pending') and self._display_pending:
            self.root.after_cancel(self._display_pending)
        self._display_pending = self.root.after(0, self._display_frame, img)

    def _on_video_resize(self, event=None):
        now = time.monotonic()
        if hasattr(self, '_last_resize') and now - self._last_resize < 0.1:
            return
        self._last_resize = now
        if self._current_frame is not None and self._join_receiver and self._join_receiver.running:
            self._display_frame(self._current_frame)

    def _toggle_fullscreen(self):
        if self._fullscreen_win:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self._current_frame is None:
            self._jlog("Полный экран: кадров ещё нет")
            return
        w = tk.Toplevel(self.root)
        w.title(f"{APP_NAME} — Полный экран")
        w.configure(bg="black")
        sw = w.winfo_screenwidth()
        sh = w.winfo_screenheight()
        w.geometry(f"{sw}x{sh}+0+0")
        w.minsize(400, 300)
        self._fs_label = tk.Label(w, bg="black")
        self._fs_label.pack(fill=tk.BOTH, expand=True)
        self._fs_label.bind("<Double-Button-1>", lambda e: self._toggle_fs_mode(w))
        w.bind("<Escape>", lambda e: self._exit_fullscreen())
        self._fullscreen_win = w
        self._fullscreen_label = self._fs_label
        self._fullscreen_state = False
        w.update_idletasks()
        self._render_to_label(self._current_frame, self._fs_label, sw, sh, allow_upscale=True)
        self._fs_btn.config(text="⛶ Exit")
        w.protocol("WM_DELETE_WINDOW", self._exit_fullscreen)

    def _toggle_fs_mode(self, window):
        self._fullscreen_state = not self._fullscreen_state
        window.attributes("-fullscreen", self._fullscreen_state)
        if not self._fullscreen_state:
            window.overrideredirect(False)
        else:
            window.overrideredirect(True)

    def _exit_fullscreen(self):
        if self._fullscreen_win:
            self._fullscreen_win.destroy()
            self._fullscreen_win = None
            self._fullscreen_label = None
            if self._current_frame:
                vw = max(self._video_label.winfo_width(), 100)
                vh = max(self._video_label.winfo_height(), 100)
                self._render_to_label(self._current_frame, self._video_label, vw, vh, allow_upscale=True)
        self._fs_btn.config(text="⛶")

    def _render_to_label(self, img, label, max_w, max_h, allow_upscale=False, max_render=1920):
        try:
            max_w = max(max_w, 320)
            max_h = max(max_h, 240)
            img = img.copy()
            r = min(max_w / img.width, max_h / img.height)
            if r < 1.0 or (allow_upscale and r > 1.0):
                img = img.resize((int(img.width * r), int(img.height * r)), Image.BILINEAR)
            # cap image to max_render to avoid slow PhotoImage
            if img.width > max_render or img.height > max_render:
                r2 = min(max_render / img.width, max_render / img.height)
                if r2 < 1.0:
                    img = img.resize((int(img.width * r2), int(img.height * r2)), Image.BILINEAR)
            imgtk = ImageTk.PhotoImage(img)
            label.imgtk = imgtk
            label.config(image=imgtk)
        except:
            pass

    def _display_frame(self, img):
        if not self._join_receiver or not self._join_receiver.running:
            return
        if getattr(self, '_ffplay_active', False):
            return
        try:
            in_fs = self._fullscreen_win and hasattr(self, '_fullscreen_label') and self._fullscreen_label
            if in_fs:
                fw = max(self._fullscreen_win.winfo_width(), 100)
                fh = max(self._fullscreen_win.winfo_height(), 100)
                self._render_to_label(img, self._fullscreen_label, fw, fh, allow_upscale=True)
            else:
                vw = max(self._video_label.winfo_width(), 100)
                vh = max(self._video_label.winfo_height(), 100)
                self._render_to_label(img, self._video_label, vw, vh, allow_upscale=True)
            self._set_join_status("В эфире", GREEN)
        except:
            pass

    def _on_stats(self, stats):
        def update():
            self._stats_var.set(
                f"FPS {stats['fps']}   ·   кадров {stats['received']}"
                f"   ·   потерь {stats['lost']}")
        self.root.after(0, update)

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description=f"{APP_NAME}")
    parser.add_argument("mode", nargs="?", choices=["gui", "send", "receive", "download-ffmpeg"],
                        default="gui")
    parser.add_argument("dest_ip", nargs="?")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--quality", choices=list(QUALITY_PRESETS), default="medium")
    parser.add_argument("--backend", choices=["auto", "ffmpeg", "py", "ffplay", "tk"], default="auto")
    parser.add_argument("--window", help="Захват окна по заголовку")
    args = parser.parse_args()

    if args.mode == "gui" or args.mode is None:
        ScreenShareApp().run()
    elif args.mode == "download-ffmpeg":
        download_ffmpeg()
    elif args.mode == "send" and args.dest_ip:
        _cli_send(args)
    elif args.mode == "receive":
        _cli_receive(args)
    else:
        parser.print_help()


def _cli_send(args):
    backend = args.backend
    if backend == "auto":
        backend = "ffmpeg" if FFMPEG_BIN.exists() else "py"

    destinations = [(args.dest_ip, args.port)]
    kw = dict(destinations=destinations, quality=args.quality,
              window_title=args.window, log_callback=print)
    s = FfmpegSender(**kw) if backend == "ffmpeg" else PySender(**kw)
    s.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        s.stop()


def _cli_receive(args):
    if FFPLAY_BIN.exists() and args.backend in ("auto", "ffplay"):
        r = FfplayReceiver(args.port)
        r.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            r.stop()
    else:
        print("Запустите без аргументов для GUI: python screen_share.py")


class FfplayReceiver:
    def __init__(self, port):
        self.port = port
        self.proc = None

    def start(self):
        cmd = [str(FFPLAY_BIN), "-fflags", "nobuffer", "-flags", "low_delay",
               "-framedrop", "-strict", "experimental",
               f"udp://:{self.port}?pkt_size=1316&fifo_size=100000&buffer_size=65536"]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


if __name__ == "__main__":
    main()
