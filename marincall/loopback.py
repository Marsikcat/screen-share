"""
System sound for a screen share — everything the PC plays except MarinCall itself.

Windows 10 2004+ can capture the audio of "every process except this one"
(WASAPI process loopback, the same thing Discord and OBS use). Excluding our own
process tree leaves out the voices of the call and the app's own sounds, so the
people watching hear the game or the video but never themselves.

The capture hands out 48 kHz stereo s16le at a steady wall-clock pace — silence is
filled in when nothing is playing — because FFmpeg reads it as a raw PCM input and
would otherwise stall the whole stream waiting for audio.
"""

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time
import uuid

SR = 48000
CHANNELS = 2
BYTES_PER_FRAME = 4             # s16 stereo
LAG = 0.05                      # s: how late we hand audio out, so capture bursts are not padded
MAX_BACKLOG = 0.15              # s: more than this and the oldest audio is dropped

HRESULT = ctypes.c_long
S_OK = 0
E_NOINTERFACE = -2147467262      # 0x80004002
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text):
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
        return g


IID_IUnknown = GUID.of("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID.of("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IActivateCompletionHandler = GUID.of("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = GUID.of("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID.of("C8ADBD64-E71E-48a0-A4DE-185C395CD317")


class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wt.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("blob", BLOB)]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                ("cbSize", wt.WORD)]


P = ctypes.c_void_p                  # any pointer argument (byref(...) is accepted as one)


def _call(obj, index, restype, argtypes=(), *args):
    """Call method #index of a COM interface pointer."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        _call(obj, 2, wt.ULONG)


_k32 = ctypes.windll.kernel32 if os.name == "nt" else None
if _k32:
    _k32.CreateEventW.restype = wt.HANDLE
    _k32.CreateEventW.argtypes = [P, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    _k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _k32.CloseHandle.argtypes = [wt.HANDLE]


def _check(hr, what):
    if hr != S_OK:
        raise OSError(f"{what}: 0x{hr & 0xFFFFFFFF:08X}")


class _CompletionHandler:
    """A minimal COM object for ActivateAudioInterfaceAsync (agile, so any thread may call it)."""

    _QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    _REF = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)
    _DONE = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self):
        self.done = threading.Event()
        self._fns = (self._QI(self._query), self._REF(lambda this: 1), self._REF(lambda this: 1),
                     self._DONE(self._completed))
        self._vtbl = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in self._fns])
        self._obj = (ctypes.c_void_p * 1)(ctypes.cast(self._vtbl, ctypes.c_void_p).value)
        self.ptr = ctypes.cast(self._obj, ctypes.c_void_p)

    def _query(self, this, riid, ppv):
        wanted = bytes(riid.contents.data)
        if wanted in (bytes(IID_IUnknown.data), bytes(IID_IAgileObject.data),
                      bytes(IID_IActivateCompletionHandler.data)):
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _completed(self, this, op):
        self.done.set()
        return S_OK


class LoopbackCapture:
    """open() in the thread that will read; then read() blocks until audio is due."""

    def __init__(self, exclude_pid=None):
        self.exclude_pid = exclude_pid or os.getpid()
        self._client = self._capture = self._op = None
        self._event = None
        self._pending = bytearray()
        self._start = 0.0
        self._handed = 0            # frames handed out since _start

    # ── setup ───────────────────────────────────────────────────────
    def open(self):
        """Raises OSError when process loopback is unavailable (older Windows)."""
        ctypes.windll.ole32.CoInitializeEx(None, 0)          # multithreaded apartment
        params = ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                   PROCESS_LOOPBACK_PARAMS(self.exclude_pid,
                                                           PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE))
        self._params = params
        var = PROPVARIANT(vt=VT_BLOB, blob=BLOB(ctypes.sizeof(params),
                                                ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)))
        handler = _CompletionHandler()
        self._handler = handler
        op = ctypes.c_void_p()
        try:
            activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        except (OSError, AttributeError) as e:
            raise OSError("захват звука приложений недоступен") from e
        activate.restype = HRESULT
        activate.argtypes = [wt.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(PROPVARIANT),
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        _check(activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IID_IAudioClient),
                        ctypes.byref(var), handler.ptr, ctypes.byref(op)), "ActivateAudioInterfaceAsync")
        self._op = op
        if not handler.done.wait(5):
            raise OSError("Windows не ответила на запрос захвата звука")
        hr, client = HRESULT(), ctypes.c_void_p()
        _check(_call(op, 3, HRESULT, (P, P), ctypes.byref(hr), ctypes.byref(client)), "GetActivateResult")
        _check(hr.value, "активация захвата звука")
        self._client = client

        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, CHANNELS, SR, SR * BYTES_PER_FRAME, BYTES_PER_FRAME, 16, 0)
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                 AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM)
        init = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int, wt.DWORD, ctypes.c_int64,
                                  ctypes.c_int64, ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p)
        vtbl = ctypes.cast(client, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        _check(init(vtbl[3])(client, AUDCLNT_SHAREMODE_SHARED, flags, 200000, 0, ctypes.byref(fmt), None),
               "IAudioClient::Initialize")
        self._event = _k32.CreateEventW(None, False, False, None)
        _check(_call(client, 13, HRESULT, (wt.HANDLE,), self._event), "SetEventHandle")
        cap = ctypes.c_void_p()
        _check(_call(client, 14, HRESULT, (P, P), ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cap)),
               "GetService(IAudioCaptureClient)")
        self._capture = cap
        _check(_call(client, 10, HRESULT), "IAudioClient::Start")
        self.restart_clock()

    def restart_clock(self):
        """Start the steady timeline now (the consumer only begins reading after it has started)."""
        self._pending.clear()
        self._start = time.perf_counter()
        self._handed = 0

    def close(self):
        if self._client:
            try:
                _call(self._client, 11, HRESULT)             # Stop
            except OSError:
                pass
        for ptr in (self._capture, self._client, self._op):
            try:
                _release(ptr)
            except OSError:
                pass
        self._capture = self._client = self._op = None
        if self._event:
            _k32.CloseHandle(self._event)
            self._event = None

    # ── reading ─────────────────────────────────────────────────────
    def _drain(self):
        size = ctypes.c_uint32()
        data, frames, flags = ctypes.c_void_p(), ctypes.c_uint32(), wt.DWORD()
        pos, qpc = ctypes.c_uint64(), ctypes.c_uint64()
        while True:
            _check(_call(self._capture, 5, HRESULT, (P,), ctypes.byref(size)), "GetNextPacketSize")
            if not size.value:
                return
            _check(_call(self._capture, 3, HRESULT, (P, P, P, P, P), ctypes.byref(data),
                         ctypes.byref(frames), ctypes.byref(flags), ctypes.byref(pos),
                         ctypes.byref(qpc)), "GetBuffer")
            n = frames.value * BYTES_PER_FRAME
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                self._pending += bytes(n)
            else:
                self._pending += ctypes.string_at(data.value, n)
            _call(self._capture, 4, HRESULT, (ctypes.c_uint32,), frames.value)    # ReleaseBuffer

    def read(self):
        """The next slice of the steady 48 kHz timeline (captured audio, or silence)."""
        _k32.WaitForSingleObject(self._event, 20)
        self._drain()
        due = int((time.perf_counter() - self._start - LAG) * SR) - self._handed
        if due <= 0:
            return b""
        excess = len(self._pending) // BYTES_PER_FRAME - int(MAX_BACKLOG * SR)
        if excess > 0:                   # capture runs ahead of the clock: drop the oldest
            del self._pending[:excess * BYTES_PER_FRAME]
        take = min(due, len(self._pending) // BYTES_PER_FRAME)
        out = bytes(self._pending[:take * BYTES_PER_FRAME])
        del self._pending[:take * BYTES_PER_FRAME]
        out += bytes((due - take) * BYTES_PER_FRAME)          # nothing playing: silence
        self._handed += due
        return out


def available():
    """True if this Windows can capture everything-but-us (Windows 10 2004 / build 19041+)."""
    try:
        return int(getattr(__import__("sys").getwindowsversion(), "build", 0)) >= 19041
    except Exception:
        return False
