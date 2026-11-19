"""Ночной расчёт звучания (scripts/embed_audio.py) без моделей: мел-спектр,
патчи, хранение. Сверка с Essentia — в отчёте 19.11.2026 (косинус 0,9995)."""

import sqlite3

import numpy as np
import pytest

from scripts import embed_audio as e


def test_mel_filters_have_unit_area_and_cover_the_band():
    filters = e._mel_filters()
    assert filters.shape == (e.N_MELS, e.N_FFT // 2 + 1)
    assert (filters >= 0).all()
    # Норма «площадь 1» (Slaney): сумма веса × шаг частоты ≈ 1 у каждого.
    step = e.SAMPLE_RATE / e.N_FFT
    area = filters.sum(axis=1) * step
    assert np.allclose(area[5:-5], 1.0, atol=0.15)


def test_silence_gives_zero_bands_and_frames_start_centred():
    mel = e.melspectrogram(np.zeros(e.SAMPLE_RATE, dtype=np.float32))
    assert np.allclose(mel, 0.0)
    # Полкадра нулей спереди, как у Essentia: кадров на один больше, чем без них.
    plain = 1 + (e.SAMPLE_RATE - e.N_FFT) // e.HOP
    assert len(mel) in (plain + 1, plain + 2)


def test_a_tone_lands_in_its_band():
    t = np.arange(e.SAMPLE_RATE) / e.SAMPLE_RATE
    mel = e.melspectrogram((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    loudest = int(np.argmax(mel[10:-10].mean(axis=0)))
    hz = e.mel_band_centre(loudest)
    assert 850 < hz < 1150


def test_short_audio_still_gives_one_patch():
    mel = np.ones((40, e.N_MELS), dtype=np.float32)
    p = e.patches(mel, e.EFFNET_PATCH, e.EFFNET_HOP)
    assert p.shape == (1, e.EFFNET_PATCH, e.N_MELS)


def test_store_upserts_by_path(tmp_path):
    db = tmp_path / "a.db"
    con = e.connect(db)
    row = dict.fromkeys(e.COLUMNS, 0.5)
    row["embedding"] = np.ones(4, np.float32).tobytes()
    row["styles"] = np.ones(4, np.float16).tobytes()
    e.store(con, "x.m4a", "h1", row)
    row["happy"] = 0.9
    e.store(con, "x.m4a", "h2", row)
    rows = sqlite3.connect(db).execute("SELECT sha256, version, happy FROM sonic").fetchall()
    assert rows == [("h2", e.VERSION, 0.9)]


def test_missing_models_say_how_to_fetch(tmp_path):
    pytest.importorskip("onnxruntime")
    with pytest.raises(SystemExit, match="fetch_models"):
        e.Models(tmp_path)


def test_fetch_keeps_a_good_file_and_drops_a_bad_one(tmp_path, monkeypatch):
    import hashlib
    import io

    from scripts import fetch_models

    good = b"model bytes"
    expected = hashlib.sha256(good).hexdigest()

    def fake_open(url, timeout=0):
        return io.BytesIO(b"tampered" if "bad" in url else good)

    monkeypatch.setattr(fetch_models.urllib.request, "urlopen", fake_open)
    assert fetch_models.fetch("x/good.onnx", expected, tmp_path)
    assert (tmp_path / "good.onnx").read_bytes() == good
    assert not fetch_models.fetch("x/bad.onnx", expected, tmp_path)
    assert not (tmp_path / "bad.onnx").exists()
    assert not list(tmp_path.glob("*.part"))


def test_service_waits_for_exactly_the_models_the_fetcher_downloads():
    from adder import analysis
    from scripts import fetch_models

    assert set(analysis.MODEL_NAMES) == {r.rsplit("/", 1)[-1] for r in fetch_models.FILES}
