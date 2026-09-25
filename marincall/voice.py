"""
Voice engine.

Mic (WASAPI, 48 kHz mono, 20 ms blocks) → WebRTC audio processing (echo
cancellation, noise suppression, gain control — the same code as in Chrome and
Discord) → gate (voice activity or push-to-talk) → Opus → QUIC datagrams to
every peer in the same voice channel. Incoming Opus → per-speaker jitter buffer → mixed into the output stream,
which also plays the UI sound effects and the sound of a screen share we watch (so the echo
canceller hears all of it and the mic does not send it back into the call).
"""

import collections
import math
import struct
import threading
import time
from fractions import Fraction

import av
import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal

from .hotkeys import held

try:
    import pywebrtc_audio as webrtc_audio
except ImportError:            # optional: without it voice works, just unprocessed
    webrtc_audio = None

SR = 48000
FRAME = 960                      # 20 ms
HDR = struct.Struct("!I")         # seq; the sender is known from the QUIC connection
HANGOVER = 15                    # frames the gate stays open after speech (300 ms)
PREROLL = 2                      # frames sent from before the gate opened
JITTER_START = 2                 # frames buffered before a speaker starts playing
JITTER_MAX = 8
STREAM_START = 0.08              # s of screen-share sound buffered before it starts playing
STREAM_MAX = 0.35                # s: more than this queued and the oldest is dropped (stay live)

# ── devices ─────────────────────────────────────────────────────────
def _wasapi():
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i
    return None


def refresh_devices():
    """PortAudio caches the device list at init — reinit to see hot-plugged devices."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def list_devices():
    api = _wasapi()
    ins, outs = [], []
    for d in sd.query_devices():
        if api is not None and d["hostapi"] != api:
            continue
        if d["max_input_channels"] > 0:
            ins.append(d["name"])
        if d["max_output_channels"] > 0:
            outs.append(d["name"])
    return ins, outs


def _resolve(name, kind):
    api = _wasapi()
    if name:
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d[f"max_{kind}_channels"] > 0 and (api is None or d["hostapi"] == api):
                return i
    if api is not None:
        idx = sd.query_hostapis(api)[f"default_{kind}_device"]
        if idx >= 0:
            return idx
    return None


def _extra():
    try:
        return sd.WasapiSettings(auto_convert=True)
    except TypeError:
        return None


# ── sound effects ───────────────────────────────────────────────────
def _tone(freqs, dur=0.09, vol=0.18, gap=0.0):
    out = []
    for f in freqs:
        t = np.arange(int(SR * dur)) / SR
        env = np.minimum(1, t / 0.005) * np.exp(-t * 18)
        out.append(np.sin(2 * np.pi * f * t) * env * vol)
        if gap:
            out.append(np.zeros(int(SR * gap)))
    return np.concatenate(out).astype(np.float32)


EFFECTS = {
    "join": _tone([587.3, 880.0], 0.11),
    "leave": _tone([880.0, 587.3], 0.11),
    "peer_join": _tone([659.3, 987.8], 0.08, 0.12),
    "peer_leave": _tone([987.8, 659.3], 0.08, 0.12),
    "mute": _tone([440.0], 0.06, 0.15),
    "unmute": _tone([660.0], 0.06, 0.15),
    "message": _tone([1046.5, 1318.5], 0.07, 0.10),
    "stream": _tone([523.3, 659.3, 784.0], 0.07, 0.12),
}


class VoiceEngine(QObject):
    speaking = Signal(bool)      # local gate opened / closed
    failed = Signal(str)

    def __init__(self, settings, mesh):
        super().__init__()
        self.s = settings
        self.mesh = mesh
        mesh.on_voice = self._on_voice
        self.running = False
        self.targets = []            # uids we send to
        self.allowed = set()         # uids we accept audio from
        self._apm = None             # WebRTC AudioProcessor, rebuilt when settings change
        self._far = collections.deque(maxlen=16)   # what we played: the echo reference
        self.in_stream = None
        self.out_stream = None
        self.monitor = False         # mic test: hear yourself
        self.level = -90.0           # dBFS after input gain, for meters
        self.threshold = -50.0       # current effective VAD threshold
        self.gate = False
        self._floor = -60.0
        self._hang = 0
        self._ptt_until = 0.0
        self._seq = 0
        self._preroll = collections.deque(maxlen=PREROLL)
        self._monitor_buf = collections.deque(maxlen=4)
        self._enc = None
        self._decoders = {}
        self._jbuf = collections.defaultdict(collections.deque)
        self._playing = {}
        self._last_rx = {}
        self._fx = []                # [array, position]
        self._lock = threading.Lock()
        self._sbuf = collections.deque()     # screen-share sound: float32 (n, 2) chunks
        self._sbuf_len = 0
        self._sbuf_playing = False
        self._slock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self):
        self.running = True
        self.rebuild_processing()
        self.restart_output()

    def shutdown(self):
        self.running = False
        self.stop_input()
        self._close(self.out_stream)
        self.out_stream = None

    @staticmethod
    def processing_available():
        return webrtc_audio is not None

    def rebuild_processing(self):
        s = self.s
        if webrtc_audio is None or not (s["aec"] or s["ns"] or s["agc"]):
            self._apm = None
            return
        delay = 60
        for st in (self.in_stream, self.out_stream):
            if st is not None:
                delay += int(st.latency * 1000)
        self._apm = webrtc_audio.AudioProcessor(
            sample_rate=SR, num_channels=1, echo_cancellation=bool(s["aec"]),
            noise_suppression=bool(s["ns"]), high_pass_filter=True,
            auto_gain_control=bool(s["agc"]), ns_level=int(s["ns_level"]), stream_delay_ms=delay)

    @staticmethod
    def _close(stream):
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def restart_output(self):
        self._close(self.out_stream)
        self.out_stream = None
        dev = _resolve(self.s["output_device"], "output")
        for extra in (_extra(), None):
            try:
                self.out_stream = sd.OutputStream(
                    samplerate=SR, channels=2, dtype="float32", blocksize=FRAME,
                    device=dev, latency="low", callback=self._out_cb, extra_settings=extra)
                self.out_stream.start()
                return
            except Exception as e:
                err = e
        self.failed.emit(f"Не удалось открыть устройство вывода: {err}")

    def start_input(self):
        if self.in_stream is not None:
            return True
        self._enc = self._make_encoder()
        dev = _resolve(self.s["input_device"], "input")
        for extra in (_extra(), None):
            try:
                self.in_stream = sd.InputStream(
                    samplerate=SR, channels=1, dtype="int16", blocksize=FRAME,
                    device=dev, latency="low", callback=self._in_cb, extra_settings=extra)
                self._far.clear()
                self.in_stream.start()
                self.rebuild_processing()
                return True
            except Exception as e:
                err = e
        self.in_stream = None
        self.failed.emit(f"Микрофон недоступен: {err}")
        return False

    def stop_input(self):
        self._close(self.in_stream)
        self.in_stream = None
        self.level = -90.0
        self._set_gate(False)

    def restart_input(self):
        if self.in_stream is not None:
            self.stop_input()
            self.start_input()

    # ── session ─────────────────────────────────────────────────────
    def set_peers(self, uids):
        """uids of everyone else in my voice channel."""
        with self._lock:
            self.targets = list(uids)
            self.allowed = set(uids)
            for uid in list(self._decoders):
                if uid not in self.allowed:
                    self._decoders.pop(uid, None)
                    self._jbuf.pop(uid, None)
                    self._playing.pop(uid, None)

    def is_speaking(self, uid):
        return time.monotonic() - self._last_rx.get(uid, 0) < 0.3

    def play(self, name):
        if self.s["sounds"] and name in EFFECTS:
            self._fx.append([EFFECTS[name], 0])

    # ── the sound of a screen share we watch ────────────────────────
    def push_stream_audio(self, chunk):
        """48 kHz stereo float32 from the stream decoder (its own thread)."""
        with self._slock:
            self._sbuf.append(np.ascontiguousarray(chunk, np.float32))
            self._sbuf_len += len(chunk)
            while self._sbuf_len > SR * STREAM_MAX and len(self._sbuf) > 1:
                self._sbuf_len -= len(self._sbuf.popleft())

    def clear_stream_audio(self):
        with self._slock:
            self._sbuf.clear()
            self._sbuf_len = 0
            self._sbuf_playing = False

    def _pull_stream(self, frames):
        with self._slock:
            if not self._sbuf_playing:
                if self._sbuf_len < SR * STREAM_START:
                    return None
                self._sbuf_playing = True
            if self._sbuf_len < frames:            # ran dry: buffer a little again
                self._sbuf_playing = False
                return None
            out = np.empty((frames, 2), np.float32)
            pos = 0
            while pos < frames:
                chunk = self._sbuf[0]
                take = min(len(chunk), frames - pos)
                out[pos:pos + take] = chunk[:take]
                if take == len(chunk):
                    self._sbuf.popleft()
                else:
                    self._sbuf[0] = chunk[take:]
                pos += take
            self._sbuf_len -= frames
            return out

    # ── encoding ────────────────────────────────────────────────────
    @staticmethod
    def _make_encoder():
        enc = av.CodecContext.create("libopus", "w")
        enc.sample_rate, enc.layout, enc.format = SR, "mono", "s16"
        enc.bit_rate = 64000
        enc.time_base = Fraction(1, SR)
        enc.options = {"application": "voip", "frame_duration": "20",
                       "packet_loss": "10", "fec": "1"}
        enc.open()
        return enc

    @staticmethod
    def _make_decoder():
        dec = av.CodecContext.create("libopus", "r")
        dec.sample_rate, dec.layout, dec.format = SR, "mono", "s16"
        return dec

    def _set_gate(self, on):
        if on != self.gate:
            self.gate = on
            self.speaking.emit(on)

    def _in_cb(self, indata, frames, t, status):
        pcm = indata[:, 0].astype(np.float32)
        gain = self.s["input_volume"] / 100.0
        if gain != 1.0:
            pcm *= gain
        np.clip(pcm, -32768, 32767, out=pcm)
        apm = self._apm
        if apm is not None:
            far = self._far.popleft() if self._far else None
            if far is None or len(far) != frames:
                far = np.zeros(frames, np.int16)
            try:
                pcm = np.asarray(apm.process(pcm.astype(np.int16), far if self.s["aec"] else None),
                                 np.float32).reshape(-1)
            except Exception:
                pass
        rms = math.sqrt(float(np.mean(pcm * pcm)) + 1e-9)
        self.level = level = 20 * math.log10(rms / 32768 + 1e-9)

        # gate
        if self.s["input_mode"] == "ptt":
            if held(self.s["hotkeys"].get("ptt")):
                self._ptt_until = time.monotonic() + self.s["ptt_release_ms"] / 1000
            open_ = time.monotonic() < self._ptt_until
        else:
            if self.s["vad_auto"]:
                # track the noise floor: fall fast, rise slowly
                self._floor = level if level < self._floor else self._floor + 0.05
                self.threshold = min(max(self._floor + 12, -62), -28)
            else:
                self.threshold = float(self.s["vad_threshold"])
            if level > self.threshold:
                self._hang = HANGOVER
            elif self._hang > 0:
                self._hang -= 1
            open_ = self._hang > 0
        muted = self.s["muted"] or self.s["deafened"]

        samples = pcm.astype(np.int16)
        if self.monitor:
            self._monitor_buf.append(samples.astype(np.float32) / 32768 * (1.0 if open_ else 0.0))

        # encode every frame so the encoder state stays continuous
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SR
        frame.pts = self._seq * FRAME
        self._seq += 1
        packets = [HDR.pack(self._seq) + bytes(p) for p in self._enc.encode(frame)]

        send = open_ and not muted
        with self._lock:
            targets = list(self.targets)
        if send and targets:
            burst = (list(self._preroll) if not self.gate else []) + packets
            for pkt in burst:
                for uid in targets:
                    self.mesh.send_datagram(uid, pkt)
            self._preroll.clear()
        else:
            self._preroll.extend(packets)
        self._set_gate(send)

    # ── receiving & playback ────────────────────────────────────────
    def _on_voice(self, uid, data):
        """A voice datagram from a peer (network thread)."""
        if len(data) <= HDR.size:
            return
        with self._lock:
            if uid not in self.allowed:
                return
            dec = self._decoders.get(uid)
            if dec is None:
                dec = self._decoders[uid] = self._make_decoder()
        try:
            for f in dec.decode(av.Packet(data[HDR.size:])):
                self._jbuf[uid].append(f.to_ndarray()[0].astype(np.float32) / 32768)
        except Exception:
            return
        self._last_rx[uid] = time.monotonic()

    def _out_cb(self, outdata, frames, t, status):
        mix = np.zeros(frames, np.float32)
        if not self.s["deafened"]:
            vols = self.s["user_volumes"]
            muted = self.s["local_mutes"]
            for uid, buf in list(self._jbuf.items()):
                playing = self._playing.get(uid, False) or len(buf) >= JITTER_START
                if playing and buf:
                    chunk = buf.popleft()
                    if uid not in muted and len(chunk) == frames:
                        mix += chunk * (vols.get(uid, 100) / 100.0)
                else:
                    playing = False
                while len(buf) > JITTER_MAX:
                    buf.popleft()
                self._playing[uid] = playing
            mix *= self.s["output_volume"] / 100.0
        share = self._pull_stream(frames)
        if share is not None and not self.s["deafened"]:
            share *= self.s["output_volume"] / 100.0 * self.s.get("stream_volume", 100) / 100.0
        else:
            share = None
        for fx in list(self._fx):
            arr, pos = fx
            piece = arr[pos:pos + frames]
            mix[:len(piece)] += piece
            fx[1] += frames
            if fx[1] >= len(arr):
                self._fx.remove(fx)
        if self.in_stream is not None and self._apm is not None:
            # echo reference: everything the speakers play except our own mic-test loopback
            far = mix if share is None else mix + share.mean(axis=1)
            self._far.append((np.clip(far, -1.0, 1.0) * 32767).astype(np.int16))
        if self.monitor and self._monitor_buf:
            chunk = self._monitor_buf.popleft()
            if len(chunk) == frames:
                mix += chunk
        outdata[:, 0] = mix
        outdata[:, 1] = mix
        if share is not None:
            outdata += share
        np.clip(outdata, -1.0, 1.0, out=outdata)
