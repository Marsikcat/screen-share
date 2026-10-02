"""Voice messages and the soundboard: encoding, decoding, built-in sounds."""
import numpy as np

from marincall import audio
from marincall.voice import SR


def test_voice_message_round_trip(tmp_path):
    t = np.arange(SR * 2) / SR
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.4 * 32767).astype(np.int16)
    path = tmp_path / audio.VOICE_NAME
    audio.encode_ogg(pcm, path)
    assert path.stat().st_size < 16_000                 # 2 s of voice is a few kilobytes
    assert abs(audio.duration(path) - 2.0) < 0.1
    back = audio.decode(path)
    assert abs(len(back) / SR - 2.0) < 0.05 and back.dtype == np.float32
    assert 0.3 < float(np.abs(back).max()) < 0.5
    peaks = audio.peaks(back, 20)
    assert len(peaks) == 20 and max(peaks) == 1.0


def test_labels():
    voice = {"id": "a" * 32, "name": audio.VOICE_NAME, "type": audio.VOICE_TYPE, "size": 1}
    assert audio.is_voice(voice)
    assert audio.files_label([voice]) == "🎤 Голосовое сообщение"
    assert audio.files_label([{"name": "отчёт.pdf", "type": "application/pdf"}]) == "📎 отчёт.pdf"
    assert audio.clock(65.4) == "1:05"


def test_builtin_sounds():
    for key in audio.BUILTIN:
        pcm = audio.builtin(key)
        assert 0.3 < len(pcm) / SR <= audio.SOUND_SECONDS
        assert abs(float(np.abs(pcm).max()) - 0.5) < 1e-3      # all equally loud
    assert audio.builtin("nope") is None


def test_trim_silence():
    pcm = np.concatenate([np.zeros(SR), np.full(SR // 2, 0.5, np.float32), np.zeros(SR)])
    assert abs(len(audio.trim_silence(pcm)) / SR - 0.5) < 0.05
