"""scripts/studio_audit.py: telling a muted word, a clip and a better copy apart.

The audio is synthetic: a "beat" (60 Hz) and a "voice" (noise in 300-3400 Hz),
so a muted word is the voice going quiet while the beat goes on -- the shape
measured on «Export» (Baby Melo), 05.11.2026.
"""

import importlib.util
from pathlib import Path

import pytest

# numpy and scipy come with the analysis extras (CI's first job installs them).
np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

spec = importlib.util.spec_from_file_location(
    "studio_audit", Path(__file__).resolve().parents[1] / "scripts" / "studio_audit.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

RATE = audit.RATE


def song(seconds=30.0, seed=1):
    """(beat, voice): the beat is a pulsing bass and a chord in the voice's
    own band, as instruments are; the voice is band-limited noise."""
    from scipy.signal import butter, sosfiltfilt

    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    beat = 0.3 * np.sin(2 * np.pi * 60 * t) * (1 + np.sin(2 * np.pi * 2 * t)) / 2
    beat += 0.05 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 660 * t))
    sos = butter(4, [400, 3000], btype="band", fs=RATE, output="sos")
    voice = sosfiltfilt(sos, rng.normal(0, 1, len(t)))
    voice *= 0.2 / np.sqrt(np.mean(voice**2))
    return beat, voice


def mute(voice, *spots):
    voice = voice.copy()
    for start, end in spots:
        voice[int(start * RATE) : int(end * RATE)] *= 0.05
    return voice


def compare(monkeypatch, file_audio, studio_audio):
    decoded = {"file": file_audio.astype(np.float32), "studio": studio_audio.astype(np.float32)}
    monkeypatch.setattr(audit, "_decode", lambda path: decoded[Path(path).name])
    return audit.compare(Path("file"), Path("studio"))


def test_a_word_muted_in_the_file_is_found(monkeypatch):
    beat, voice = song()
    compared = compare(monkeypatch, beat + mute(voice, (10, 10.6), (20, 20.4)), beat + voice)
    group, why = audit.verdict(False, compared)
    assert group == "censored"
    assert why == "похоже, заглушено: 0:10, 0:20"


def test_a_word_muted_in_the_studio_recording_keeps_the_file(monkeypatch):
    beat, voice = song()
    compared = compare(monkeypatch, beat + voice, beat + mute(voice, (12, 12.5)))
    assert audit.verdict(False, compared)[0] == "yours-uncensored"
    # Even a clip, when the studio recording is the clean one.
    assert audit.verdict(True, compared, explicit=False)[0] == "yours-uncensored"


def test_a_clip_that_differs_in_the_voice_is_a_clip_maybe_muted(monkeypatch):
    """«DEVO4KA»: the clip's voice drops where the lyrics have no swearing."""
    beat, voice = song()
    compared = compare(monkeypatch, beat + mute(voice, (10, 10.6)), beat + voice)
    group, why = audit.verdict(True, compared)
    assert group == "clip"
    assert "возможно, заглушено: 0:10" in why
    # The file's voice louder than the explicit studio track: the clip's own
    # ad-lib, not a censored studio recording.
    louder = compare(monkeypatch, beat + voice, beat + mute(voice, (12, 12.5)))
    assert audit.verdict(True, louder, explicit=True)[0] == "clip"


def test_the_same_audio_is_the_same(monkeypatch):
    beat, voice = song()
    compared = compare(monkeypatch, beat + voice, beat + voice)
    assert audit.verdict(True, compared) == ("same", "")


def test_a_clips_extra_scene_is_the_clips(monkeypatch):
    beat, voice = song()
    scene = np.random.default_rng(5).normal(0, 0.1, 6 * RATE)
    compared = compare(monkeypatch, np.concatenate([beat + voice, scene]), beat + voice)
    assert compared["tail"] == pytest.approx(6, abs=0.2)
    assert audit.verdict(True, compared)[0] == "clip"
    # Someone's upload with the same extra goes to its own group, not "clip".
    assert audit.verdict(False, compared)[0] == "edges"


def test_silence_around_the_song_is_padding(monkeypatch):
    beat, voice = song()
    padded = np.concatenate([np.zeros(3 * RATE), beat + voice, np.zeros(2 * RATE)])
    compared = compare(monkeypatch, padded, beat + voice)
    assert (compared["head"], compared["tail"]) == (0.0, 0.0)
    assert audit.verdict(True, compared)[0] == "same"


def test_a_different_intro_is_not_a_muted_word(monkeypatch):
    """«Альтуха»: the upload's first two seconds differ from the studio's."""
    beat, voice = song()
    other = beat + voice
    other[: 2 * RATE] = np.random.default_rng(9).normal(0, 0.3, 2 * RATE)
    compared = compare(monkeypatch, beat + voice, other)
    assert audit.verdict(False, compared)[0] == "same"


def test_another_mix_all_the_way_is_not_censorship(monkeypatch):
    """«Манхэттен»: overall 0.52, quiet places all over -- another mix."""
    beat, voice = song()
    _, other_voice = song(seed=2)
    compared = compare(monkeypatch, beat + voice * 0.3, beat + other_voice)
    assert audit.verdict(True, compared) == ("clip", "другой звук по всей песне")
    assert audit.verdict(False, compared)[0] == "listen"


def test_the_wrong_video_is_told_by_its_length(monkeypatch):
    beat, voice = song(seconds=20)
    album = np.tile(beat + voice, 5)
    compared = compare(monkeypatch, album, beat + voice)
    assert audit.verdict(False, compared) == ("wrong", "файл 1:40, песня 0:20")


def test_a_recheck_with_the_same_studio_track_does_not_download_again(monkeypatch, tmp_path):
    """--again после правки поиска: совпала та же запись — лимит YouTube не тратится."""
    from adder import studio

    track = tmp_path / "x.m4a"
    track.write_bytes(b"audio")
    stamp = list(audit.ua._stamp(track.stat()))
    song = studio.Song("vid", "Song", ("A",), 200.0, True, "")
    monkeypatch.setattr(
        audit, "look_up", lambda *a, **k: {"video_type": None, "video_title": "", "song": song}
    )
    monkeypatch.setattr(audit.ua, "download", lambda *a: pytest.fail("downloaded again"))
    previous = {"group": "clip", "video": "vid", "stamp": stamp, "why": "w"}

    monkeypatch.setattr(audit, "KEEP", tmp_path / "keep")
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "vid.m4a").write_bytes(b"studio")
    entry = audit.audit_one(None, track, {"duration": 200}, "old", previous)

    assert entry["group"] == "clip" and entry["rechecked"]
    # Запись к замене пропала (её забрала замена дубля) — качается снова.
    (tmp_path / "keep" / "vid.m4a").unlink()
    with pytest.raises(pytest.fail.Exception):
        audit.audit_one(None, track, {"duration": 200}, "old", previous)
    with pytest.raises(pytest.fail.Exception):
        audit.audit_one(None, track, {"duration": 200}, "old", {**previous, "video": "other"})


def test_apply_leaves_other_peoples_uploads_alone(monkeypatch, tmp_path):
    """Правило пользователя 06.11.2026: чужие загрузки не менять и в группе «клип»."""
    import argparse

    from adder import studio

    root, keep = tmp_path / "lib", tmp_path / "keep"
    root.mkdir()
    keep.mkdir()
    state = {}
    for name, kind in (("ugc", studio.UPLOAD), ("omv", studio.OFFICIAL_VIDEO)):
        track = root / f"{name}.m4a"
        track.write_bytes(b"old")
        (keep / f"{name}.m4a").write_bytes(b"new")
        state[track.name] = {
            "group": "clip",
            "video": name,
            "video_type": kind,
            "stamp": list(audit.ua._stamp(track.stat())),
        }
    swapped = []
    monkeypatch.setattr(audit, "KEEP", keep)
    monkeypatch.setattr(audit, "WORK", tmp_path / "work")
    monkeypatch.setattr(audit, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(audit.ua, "swap", lambda path, *a, **k: swapped.append(path.name))
    monkeypatch.setattr(audit.ua, "rescan_navidrome", lambda: None)
    args = argparse.Namespace(skip=None, groups=None, only=None, db=None, uploads_too=False)

    audit.apply(root, state, args)
    assert swapped == ["omv.m4a"]

    state["ugc.m4a"]["group"] = "clip"
    audit.apply(root, state, argparse.Namespace(**{**vars(args), "uploads_too": True}))
    assert swapped == ["omv.m4a", "ugc.m4a"]
