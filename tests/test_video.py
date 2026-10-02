"""Video files in the chat: the first frame, playback against the sound clock."""
import threading
import time

import av
import numpy as np

from marincall import video
from marincall.voice import SR


def make_video(path, seconds=2, sound=True):
    with av.open(str(path), "w") as out:
        vs = out.add_stream("libx264", rate=30)
        vs.width, vs.height, vs.pix_fmt = 160, 120, "yuv420p"
        a = out.add_stream("aac", rate=48000) if sound else None
        if a is not None:
            a.layout = "stereo"
        for i in range(seconds * 30):
            img = np.full((120, 160, 3), (i * 4) % 255, np.uint8)
            f = av.VideoFrame.from_ndarray(img, format="rgb24")
            f.pts = i
            for pk in vs.encode(f):
                out.mux(pk)
        if a is not None:
            st = np.zeros((2, 48000 * seconds), np.float32)
            for i in range(0, st.shape[1], 1024):
                fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(st[:, i:i + 1024]), format="fltp",
                                                layout="stereo")
                fr.sample_rate = 48000
                fr.pts = i
                for pk in a.encode(fr):
                    out.mux(pk)
            for pk in a.encode(None):
                out.mux(pk)
        for pk in vs.encode(None):
            out.mux(pk)


class FakeVoice:
    """Moves clips on in real time; plays nothing."""

    def __init__(self):
        self.clips, self.lock = {}, threading.Lock()
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.alive:
            time.sleep(0.02)
            with self.lock:
                for k, c in list(self.clips.items()):
                    c[1] += int(SR * 0.02)
                    if c[1] >= len(c[0]):
                        del self.clips[k]

    def play_clip(self, key, samples, gain=1.0, start=0, call=False):
        with self.lock:
            self.clips[key] = [samples, start]

    def stop_clip(self, key):
        with self.lock:
            self.clips.pop(key, None)

    def clip_pos(self, key):
        c = self.clips.get(key)
        return c[1] if c else None


def test_probe_and_kind(tmp_path):
    path = tmp_path / "clip.mp4"
    make_video(path)
    info = video.probe(path)
    assert abs(info["duration"] - 2.0) < 0.2 and info["audio"]
    assert (info["width"], info["height"]) == (160, 120) and info["thumb"].width() == 160
    assert video.is_video({"type": "video/mp4", "name": "a.mp4"})
    assert video.is_video({"type": "application/octet-stream", "name": "a.MKV"})
    assert not video.is_video({"type": "image/png", "name": "a.png"})


def test_playback_follows_the_sound(tmp_path):
    path = tmp_path / "clip.mp4"
    make_video(path)
    voice = FakeVoice()
    pb = video.Playback(voice, path, 2.0)
    pb.play()
    deadline = time.time() + 10
    while pb.take_frame() is None and time.time() < deadline:
        time.sleep(0.02)
    assert pb.take_frame() is not None
    time.sleep(0.6)
    assert video.CLIP in voice.clips and 0.3 < pb.position() < 2.0
    pb.pause()
    at = pb.position()
    time.sleep(0.3)
    assert pb.position() == at and video.CLIP not in voice.clips
    pb.seek(1.5)
    assert pb.position() == 1.5
    pb.stop()
    voice.alive = False
