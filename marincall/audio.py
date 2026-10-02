"""
Recorded and stored sound: voice messages and soundboard sounds.

Voice messages are recorded from the microphone (48 kHz mono, its own input stream, so it
works in and out of a call) and sent as an ordinary file, Ogg Opus «Голосовое сообщение.ogg»:
older versions show it as a file you can open. Any audio file PyAV understands can be decoded
for the in-chat player or the soundboard; playback goes through the voice engine's output,
so the echo canceller hears it and the mic does not send it back into a call.
"""

import threading
import time

import av
import numpy as np
import sounddevice as sd

from .voice import FRAME, SR, _extra, _resolve

VOICE_NAME = "Голосовое сообщение.ogg"
VOICE_TYPE = "audio/ogg"
MAX_SECONDS = 5 * 60                 # a voice message is at most five minutes long
SOUND_SECONDS = 5                    # soundboard sounds are cut to five seconds
PLAYABLE_SECONDS = 15 * 60           # longer audio files open in the system player instead


def is_voice(meta):
    return meta.get("type") == VOICE_TYPE and meta.get("name") == VOICE_NAME


def files_label(files):
    """How a message with only attachments reads in a notification or a reply."""
    if any(is_voice(f) for f in files):
        return "🎤 Голосовое сообщение"
    return "📎 " + ", ".join(f["name"] for f in files) if files else ""


def clock(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class Recorder:
    """The microphone into memory, until stop() or MAX_SECONDS."""

    def __init__(self, settings):
        self.s = settings
        self.stream = None
        self.blocks = []
        self.frames = 0
        self.level = 0.0                 # 0..1 peak of the latest block, for the live meter
        self.levels = []                 # one peak per 100 ms: the waveform while recording
        self._acc = 0.0
        self._lock = threading.Lock()

    def start(self):
        """None on success, otherwise why the mic could not be opened."""
        dev = _resolve(self.s["input_device"], "input")
        err = None
        for extra in (_extra(), None):
            try:
                self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=FRAME,
                                             device=dev, callback=self._cb, extra_settings=extra)
                self.stream.start()
                self.started = time.monotonic()
                return None
            except Exception as e:
                err = e
                self.stream = None
        return str(err)

    def _cb(self, indata, frames, t, status):
        if self.frames >= SR * MAX_SECONDS:
            return
        block = indata[:, 0].copy()
        gain = self.s["input_volume"] / 100.0
        if gain != 1.0:
            block = np.clip(block.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
        peak = float(np.abs(block.astype(np.int32)).max()) / 32768 if len(block) else 0.0
        with self._lock:
            self.blocks.append(block)
            self.frames += len(block)
            self.level = peak
            self._acc = max(self._acc, peak)
            if self.frames // (SR // 10) > len(self.levels):
                self.levels.append(self._acc)
                self._acc = 0.0

    @property
    def seconds(self):
        return self.frames / SR

    @property
    def full(self):
        return self.frames >= SR * MAX_SECONDS

    def stop(self):
        """The recording as int16 samples."""
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        with self._lock:
            pcm = np.concatenate(self.blocks) if self.blocks else np.zeros(0, np.int16)
            self.blocks = []
        return pcm[:SR * MAX_SECONDS]


def encode_ogg(pcm, path, bitrate=32000):
    """int16 or float32 mono 48 kHz → Ogg Opus file."""
    if pcm.dtype != np.int16:
        pcm = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    with av.open(str(path), "w", format="ogg") as out:
        st = out.add_stream("libopus", rate=SR)
        st.codec_context.layout = "mono"
        st.codec_context.format = "s16"
        st.codec_context.bit_rate = bitrate
        st.codec_context.options = {"application": "voip"}
        for i in range(0, len(pcm), FRAME):
            chunk = pcm[i:i + FRAME]
            if len(chunk) < FRAME:
                chunk = np.concatenate([chunk, np.zeros(FRAME - len(chunk), np.int16)])
            frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = SR
            frame.pts = i
            for packet in st.encode(frame):
                out.mux(packet)
        for packet in st.encode(None):
            out.mux(packet)


def duration(path):
    """Seconds, from the container (cheap); None if unknown."""
    try:
        with av.open(str(path)) as c:
            if c.duration:
                return c.duration / 1_000_000
            st = c.streams.audio[0]
            if st.duration and st.time_base:
                return float(st.duration * st.time_base)
    except Exception:
        pass
    return None


def decode(path, max_seconds=None):
    """Any audio file → mono float32 at 48 kHz."""
    out, n = [], 0
    with av.open(str(path)) as c:
        st = c.streams.audio[0]
        res = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in c.decode(st):
            for f in res.resample(frame):
                a = f.to_ndarray().reshape(-1)
                out.append(a)
                n += len(a)
            if max_seconds and n >= SR * max_seconds:
                break
        for f in res.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    pcm = np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)
    return pcm[:int(SR * max_seconds)] if max_seconds else pcm


def peaks(pcm, bars=48):
    """Waveform: `bars` values 0..1 (loudness, gently compressed so quiet speech shows)."""
    if not len(pcm):
        return [0.0] * bars
    a = np.abs(pcm.astype(np.float32) / (32768 if pcm.dtype == np.int16 else 1.0))
    edges = np.linspace(0, len(a), bars + 1).astype(int)
    vals = np.array([a[edges[i]:max(edges[i + 1], edges[i] + 1)].max() for i in range(bars)])
    top = float(vals.max()) or 1.0
    return [float(v) for v in np.sqrt(vals / top)]


def trim_silence(pcm, threshold=0.01):
    """Cut leading and trailing silence (soundboard sounds start right away)."""
    loud = np.flatnonzero(np.abs(pcm) > threshold)
    if not len(loud):
        return pcm[:0]
    return pcm[max(0, loud[0] - SR // 100):loud[-1] + SR // 50]


# ── built-in soundboard sounds (synthesised: nothing to download, no licences) ──────
def _env(n, attack=0.004, decay=6.0):
    t = np.arange(n) / SR
    return np.minimum(1, t / attack) * np.exp(-t * decay)


def _sine(freq, dur, vol=0.3, decay=6.0):
    t = np.arange(int(SR * dur)) / SR
    f = np.broadcast_to(freq, t.shape) if np.isscalar(freq) else freq
    phase = 2 * np.pi * np.cumsum(f) / SR
    return (np.sin(phase) * _env(len(t), decay=decay) * vol).astype(np.float32)


def _noise(dur, vol=0.3, decay=20.0, seed=1):
    n = int(SR * dur)
    rng = np.random.default_rng(seed)
    return (rng.uniform(-1, 1, n) * _env(n, 0.001, decay) * vol).astype(np.float32)


def _cat(*parts, gap=0.0):
    out = []
    for p in parts:
        out.append(p)
        if gap:
            out.append(np.zeros(int(SR * gap), np.float32))
    return np.concatenate(out)


def _mix(*parts):
    n = max(len(p) for p in parts)
    out = np.zeros(n, np.float32)
    for p in parts:
        out[:len(p)] += p
    return out


def _at(sound, offset):
    return np.concatenate([np.zeros(int(SR * offset), np.float32), sound])


def _rimshot():
    snare = lambda s: _mix(_noise(0.18, 0.35, 18, s), _sine(190, 0.12, 0.3, 30))    # noqa: E731
    cymbal = _noise(1.2, 0.22, 2.6, 9)
    cymbal = np.diff(cymbal, prepend=0).astype(np.float32) * 1.6                    # brighter
    return _mix(snare(1), _at(snare(2), 0.16), _at(cymbal, 0.42))


def _trombone():
    parts = []
    for i, (f, d) in enumerate(((392, 0.42), (370, 0.42), (349, 0.42), (330, 1.3))):
        t = np.arange(int(SR * d)) / SR
        wobble = f * (1 + (0.015 * np.sin(2 * np.pi * 5 * t) if i == 3 else 0)) * (1 - 0.02 * t / d)
        tone = sum(_sine(wobble * k, d, 0.22 / k, 1.2 if i == 3 else 2.5) for k in (1, 2, 3))
        parts.append(tone)
    return _cat(*parts, gap=0.03)


def _tada():
    chord = lambda fs, d, v: _mix(*(_sine(f, d, v, 2.2) for f in fs))                # noqa: E731
    return _cat(chord((523, 659), 0.12, 0.14), chord((523, 659, 784, 1047), 1.0, 0.12), gap=0.02)


def _boing():
    t = np.arange(int(SR * 0.6)) / SR
    f = 180 + 260 * np.exp(-t * 7) + 30 * np.sin(2 * np.pi * 14 * t) * np.exp(-t * 4)
    return _sine(f, 0.6, 0.35, 4.5)


def _clap():
    claps = []
    rng = np.random.default_rng(4)
    for i in range(26):
        claps.append(_at(_noise(0.06, 0.12 + 0.08 * rng.random(), 45, i + 10), rng.random() * 1.6))
    return _mix(*claps)


def _crickets():
    out = []
    for i in range(3):
        chirp = _mix(*(_at(_sine(4400, 0.03, 0.12, 30), k * 0.045) for k in range(4)))
        out.append(_at(chirp, i * 0.7))
    return _mix(*out)


def _ding():
    return _mix(_sine(1318.5, 1.4, 0.22, 2.4), _sine(2637, 1.0, 0.06, 4.0))


def _airhorn():
    d = 0.9
    t = np.arange(int(SR * d)) / SR
    env = np.minimum(1, t / 0.02) * np.minimum(1, (d - t) / 0.08)
    saw = sum(np.sin(2 * np.pi * f * k * t) / k for f in (415, 420, 523) for k in range(1, 6))
    return (saw * env * 0.07).astype(np.float32)


BUILTIN = {               # key -> (name, emoji, maker)
    "rimshot": ("Ба-дум-тсс", "🥁", _rimshot),
    "trombone": ("Грустный тромбон", "🎺", _trombone),
    "tada": ("Та-да!", "🎉", _tada),
    "boing": ("Боинг", "🤪", _boing),
    "clap": ("Аплодисменты", "👏", _clap),
    "crickets": ("Сверчки", "🦗", _crickets),
    "ding": ("Дзинь", "🔔", _ding),
    "airhorn": ("Гудок", "📯", _airhorn),
}
_built = {}


def builtin(key):
    if key not in BUILTIN:
        return None
    if key not in _built:
        pcm = BUILTIN[key][2]()
        top = float(np.abs(pcm).max()) or 1.0
        _built[key] = (pcm / top * 0.5).astype(np.float32)        # all at the same loudness
    return _built[key]
