"""The screen-share encoder process: a test pattern and a tone in, MPEG-TS with both out."""
import io
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import av

ROOT = Path(__file__).resolve().parent.parent


def test_worker_streams_video_and_sound():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(30)
    cfg = {"source": {"kind": "test", "label": "test", "height": 720}, "fps": 30, "height": 720,
           "kbps": 2000, "encoder": "x264", "audio": "test",
           "port": listener.getsockname()[1], "parent_pid": 0}
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--stream-worker", json.dumps(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        conn, _ = listener.accept()
        data = bytearray()
        t0 = time.time()
        while time.time() - t0 < 3:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        conn.close()                                   # the app stops the share
        assert proc.wait(timeout=15) == 0              # …and the worker leaves on its own
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.stderr.read().decode("utf-8", "replace").startswith("OK 1280x720 libx264")

    container = av.open(io.BytesIO(bytes(data)), format="mpegts")
    assert sorted(s.type for s in container.streams) == ["audio", "video"]
    frames = samples = 0
    for packet in container.demux():
        if packet.size:
            for frame in packet.decode():
                if packet.stream.type == "video":
                    frames += 1
                else:
                    samples += frame.samples
    assert frames >= 45                                # ~3 s at 30 fps, paced like a live screen
    assert abs(samples / 48000 - frames / 30) < 0.5   # sound keeps up with the picture
