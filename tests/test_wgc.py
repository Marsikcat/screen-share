"""Window capture (Windows.Graphics.Capture) on an off-screen window of our own, and the
streamer's side of stream self-healing."""
import time

import pytest

from marincall import wgc


@pytest.fixture
def probe():
    from probe_window import ProbeWindow
    win = ProbeWindow(320, 180)
    win.pump()
    yield win
    win.close()


def test_window_capture(probe):
    try:
        cap = wgc.WindowCapture(probe.hwnd)
    except OSError as e:                      # no GPU / no desktop here (some CI machines)
        pytest.skip(f"Windows.Graphics.Capture unavailable: {e}")
    try:
        img, end = None, time.time() + 5
        while img is None and time.time() < end:
            probe.pump()
            img = cap.grab()
            time.sleep(0.02)
    finally:
        cap.close()
    assert img is not None and img.shape == (180, 320, 4)
    assert img[90, 160].tolist()[:3] == [255, 0, 0]          # BGRA of the blue fill
    assert img[10, 10].tolist()[:3] == [255, 255, 255]       # the white corner: upright


def test_resized_window_is_letterboxed():
    import numpy as np
    wide = np.full((100, 400, 4), 200, np.uint8)
    out = wgc._fit(wide, (180, 320))
    assert out.shape == (180, 320, 4)
    assert out[5, 160].tolist() == [0, 0, 0, 0]              # a black bar above
    assert out[90, 160].tolist()[:3] == [200, 200, 200]


class _Out:
    def __init__(self):
        self.closed = self.failed = False
        self.started = time.monotonic() - 10

    def close(self):
        self.closed = True


class _Mesh:
    def __init__(self):
        self.peers_map = {"v": object()}
        self.opened = []

    def open_stream(self, uid, header=b""):
        out = _Out()
        self.opened.append((uid, out))
        return out


def test_broken_streams_are_reopened():
    from marincall.stream import StreamSender
    mesh = _Mesh()
    s = StreamSender(mesh)
    s.running = True
    s.set_viewers(["v"])
    first = s.outs["v"]
    s.heal()
    assert s.outs["v"] is first                              # healthy: left alone
    first.failed = True
    s.heal()
    assert s.outs["v"] is not first and first.closed          # broke by itself: a new one
    again = s.outs["v"]
    s.restart_viewer("v")                                     # the viewer asked again
    assert s.outs["v"] is not again and again.closed
    s.running = False
