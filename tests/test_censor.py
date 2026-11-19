"""Сверка студийной записи с клипом (adder/censor.py, разбор 19.11.2026).

Звук синтетический: «бас» (синус) + «голос» (шум в полосе 300–3400 Гц). В
«студийной» голос на секунду заглушён — как приглушают слово.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from adder import censor

SR = 16000
REPO = Path(__file__).resolve().parent.parent


def _write(path: Path, x: np.ndarray) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-c:a", "pcm_s16le", str(path)],
        input=x.astype(np.float32).tobytes(), check=True,
    )  # fmt: skip
    return path


def _song(seconds=40, seed=1):
    """(бас, голос) с уровнями как в tests/test_studio_audit.py: голос не тонет в басе."""
    from scipy.signal import butter, sosfiltfilt

    rng = np.random.default_rng(seed)
    t = np.arange(seconds * SR) / SR
    beat = 0.3 * np.sin(2 * np.pi * 60 * t) * (1 + np.sin(2 * np.pi * 2 * t)) / 2
    beat += 0.05 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 660 * t))
    sos = butter(4, [400, 3000], btype="band", fs=SR, output="sos")
    voice = sosfiltfilt(sos, rng.normal(0, 1, len(t)))
    voice *= 0.2 / np.sqrt(np.mean(voice**2))
    return beat, voice


def test_a_word_muted_in_the_studio_recording_is_found_in_its_place(tmp_path):
    bass, voice = _song()
    muted = voice.copy()
    muted[20 * SR : 21 * SR] *= 0.05  # слово на 0:20 заглушено
    clip = _write(tmp_path / "clip.wav", bass + voice)
    studio = _write(tmp_path / "studio.wav", bass + muted)

    compared = censor.compare(clip, studio)

    assert compared["overall"] > 0.9
    assert abs(compared["lag"]) < 0.05
    studio_muted = [s for s in compared["spots"] if s[2] == "studio"]
    assert studio_muted, compared["spots"]
    assert all(19 <= s[0] <= 21.5 for s in studio_muted)


def test_the_place_is_told_in_studio_time_when_the_clip_has_an_intro(tmp_path):
    bass, voice = _song()
    muted = voice.copy()
    muted[20 * SR : 21 * SR] *= 0.05
    intro = np.random.default_rng(7).standard_normal(5 * SR) * 0.2  # 5 с сцены в клипе
    clip = _write(tmp_path / "clip.wav", np.concatenate([intro, bass + voice]))
    studio = _write(tmp_path / "studio.wav", bass + muted)

    compared = censor.compare(clip, studio)

    assert 4.9 < compared["lag"] < 5.1
    synced = [{"at": 10.0, "line": "Просто строка"}, {"at": 19.5, "line": "Курим дурь"},
              {"at": 23.0, "line": "Дальше"}]  # fmt: skip
    verdict = censor.judge(compared, synced)
    assert not verdict.take_studio
    assert "курим" in verdict.why.lower() and "0:2" in verdict.why


SPOT = {"overall": 0.95, "lag": 0.0, "spots": [[30.0, 30.5, "studio"]]}
LINES = [{"at": 25.0, "line": "Вступление"}, {"at": 29.5, "line": "Окси переводит меня в слоу"},
         {"at": 33.0, "line": "Конец"}]  # fmt: skip


def test_judge_takes_the_clip_when_the_studio_is_quiet_on_a_drug_line():
    assert not censor.judge(SPOT, LINES).take_studio


def test_judge_takes_the_studio_when_the_quiet_place_has_no_such_word():
    lines = [{**line, "line": "Обычные слова"} for line in LINES]
    assert censor.judge(SPOT, lines).take_studio


def test_judge_takes_the_studio_when_only_the_clip_is_quiet():
    compared = {**SPOT, "spots": [[30.0, 30.5, "file"]]}
    assert censor.judge(compared, LINES).take_studio


def test_judge_leaves_another_mix_to_the_owner():
    verdict = censor.judge({**SPOT, "overall": 0.5}, LINES)
    assert not verdict.take_studio and "проверь" in verdict.why


def test_studio_audit_keeps_a_clip_whose_explicit_studio_recording_is_muted():
    """06.11: «18+» студийная с заглушёнными словами заменила клип, где слова
    на месте, — условие пропускало версию 18+. Теперь клип остаётся."""
    sys.path.insert(0, str(REPO / "scripts"))
    import studio_audit

    compared = {"file_seconds": 160.0, "studio_seconds": 160.0, "overall": 0.97,
                "head": 0.0, "tail": 0.0, "lag": 0.0, "spots": [[40.0, 40.5, "studio"]]}  # fmt: skip
    group, why = studio_audit.verdict(clip=True, compared=compared, explicit=True)
    assert group == "listen", (group, why)  # не заменять; было «clip» → заменён


def test_judge_takes_the_clip_when_the_studio_is_quiet_on_a_swear_word():
    """3 из 13 подтверждённых заглушённых строк — только мат (разбор 19.11.2026)."""
    lines = [{**line, "line": "Но мне похуй вообще"} for line in LINES]
    assert not censor.judge(SPOT, lines).take_studio


def test_a_long_difference_on_a_drug_line_is_left_to_the_owner():
    """Заглушённая фраза длиннее 3 с — место «other», не «studio»."""
    verdict = censor.judge({**SPOT, "spots": [[29.0, 33.5, "other"]]}, LINES)
    assert not verdict.take_studio and "проверь" in verdict.why


def test_studio_audit_keeps_a_clip_even_when_the_clip_is_quiet_somewhere_too():
    sys.path.insert(0, str(REPO / "scripts"))
    import studio_audit

    compared = {"file_seconds": 160.0, "studio_seconds": 160.0, "overall": 0.97, "head": 0.0,
                "tail": 0.0, "lag": 0.0,
                "spots": [[40.0, 40.5, "studio"], [80.0, 80.4, "file"]]}  # fmt: skip
    assert studio_audit.verdict(clip=True, compared=compared, explicit=True)[0] == "listen"


def test_a_clip_much_longer_than_the_studio_is_not_kept_unchecked():
    """Клип со сценами на 7:45 не ложится в фонотеку вместо песни на 2:41."""
    compared = {**SPOT, "overall": 0.3, "file_seconds": 465.0, "studio_seconds": 161.0}
    verdict = censor.judge(compared, LINES)
    assert verdict.take_studio and "не сверена" in verdict.why


def test_a_quiet_place_far_after_the_last_line_is_not_its_word():
    lines = [{"at": 10.0, "line": "Вступление"}, {"at": 29.5, "line": "Курим дурь"}]
    assert censor.judge({**SPOT, "spots": [[90.0, 90.5, "studio"]]}, lines).take_studio


def test_studio_audit_apply_follows_todays_rules(tmp_path, monkeypatch):
    """Старое «заменить» (06.11) на заглушённую 18+ не применяется."""
    import argparse

    sys.path.insert(0, str(REPO / "scripts"))
    import studio_audit

    swapped = []
    monkeypatch.setattr(studio_audit.ua, "swap", lambda *a, **k: swapped.append(a))
    # Все папки аудита — во временную: apply пишет state.json и бэкапы, а по
    # умолчанию это настоящие файлы (19.11.2026 так затёрся state.json).
    for name in ("KEEP", "WORK", "STATE", "BACKUP", "RESCAN_PENDING"):
        monkeypatch.setattr(studio_audit, name, tmp_path / "audit" / name.lower())
    monkeypatch.setattr(studio_audit.ua, "BACKUP", tmp_path / "ua-backup")
    monkeypatch.setattr(studio_audit.ua, "RESCAN_PENDING", tmp_path / "ua-rescan")
    monkeypatch.setattr(studio_audit.ua, "rescan_navidrome", lambda *a, **k: None)
    studio_audit.KEEP.mkdir(parents=True)
    (studio_audit.KEEP / "vid.m4a").write_bytes(b"x")
    (tmp_path / "A").mkdir()
    song = tmp_path / "A" / "s.m4a"
    song.write_bytes(b"x")
    compared = {"file_seconds": 160.0, "studio_seconds": 160.0, "overall": 0.97, "head": 0.0,
                "tail": 0.0, "spots": [[40.0, 40.5, "studio"], [80.0, 80.4, "file"]]}  # fmt: skip
    entry = {"group": "clip", "clip": True, "explicit": True, "video": "vid",
             "compared": compared, "stamp": list(studio_audit.ua._stamp(song.stat()))}  # fmt: skip
    args = argparse.Namespace(skip=None, groups=None, only=None, uploads_too=False, db=None)
    studio_audit.apply(tmp_path, {"A/s.m4a": entry}, args)
    assert swapped == []
    assert not studio_audit.STATE.exists()


def _audit(monkeypatch, tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    import studio_audit

    from adder import studio

    for name in ("KEEP", "WORK", "STATE", "BACKUP", "RESCAN_PENDING"):
        monkeypatch.setattr(studio_audit, name, tmp_path / "audit" / name.lower())
    song = studio.Song(video_id="vid", title="S", artists=("A",), duration=160, explicit=True)
    found = {"song": song, "video_type": studio.OFFICIAL_VIDEO, "video_title": "A - S"}
    monkeypatch.setattr(studio_audit, "look_up", lambda *a, **k: dict(found))
    monkeypatch.setattr(studio_audit.ua, "download", lambda *a: pytest.fail("скачивание"))
    track = tmp_path / "s.m4a"
    track.write_bytes(b"x")
    return studio_audit, track


COMPARED = {"file_seconds": 160.0, "studio_seconds": 160.0, "overall": 0.97, "head": 0.0,
            "tail": 0.0, "spots": [[40.0, 40.5, "studio"]]}  # fmt: skip


@pytest.mark.parametrize(
    "extra", [{"group": "replaced"}, {"group": "yours-uncensored", "restored": "x"}]
)
def test_again_keeps_done_and_restored_entries_as_they_are(monkeypatch, tmp_path, extra):
    studio_audit, track = _audit(monkeypatch, tmp_path)
    previous = {"video": "vid", "stamp": list(studio_audit.ua._stamp(track.stat())),
                "clip": True, "explicit": False, "compared": COMPARED, **extra}  # fmt: skip
    got = studio_audit.audit_one(None, track, {"duration": 160}, None, previous)
    assert got["group"] == extra["group"]


def test_again_judges_an_old_verdict_by_todays_rules(monkeypatch, tmp_path):
    studio_audit, track = _audit(monkeypatch, tmp_path)
    previous = {"video": "vid", "stamp": list(studio_audit.ua._stamp(track.stat())),
                "group": "same", "clip": True, "explicit": True, "compared": COMPARED}  # fmt: skip
    assert (
        studio_audit.audit_one(None, track, {"duration": 160}, None, previous)["group"] == "listen"
    )
