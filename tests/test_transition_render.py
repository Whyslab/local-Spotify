"""scripts/render_transition.py: связка перехода «как диджей».

Главное свойство связки — её края. Плеер находит, где играет трек A, по
началу связки и подводит трек B под её конец; если края хоть немного не тот
звук, переход «заикнётся». Поэтому края сверяются с самими треками, а выбор
способа перехода — числами.

Треки здесь искусственные: ровный бас-барабан в известном темпе, тарелка и
аккорд. На них librosa находит удары без сомнений, и тест проверяет сведение,
а не чутьё librosa на настоящей музыке (его слышно в пробе, а не в тесте).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("librosa")
pytest.importorskip("scipy")
pytest.importorskip("soundfile")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_transition.py"
spec = importlib.util.spec_from_file_location("render_transition", SCRIPT)
assert spec and spec.loader
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

SR = rt.SR


def make_track(path: Path, tempo: float, seconds: float, intro: float = 0.0, level: float = 0.3):
    """Бас-барабан на каждую долю с ``intro`` секунды, до неё — один аккорд."""
    rng = np.random.default_rng(int(tempo * 10 + intro))
    n = int(seconds * SR)
    t = np.arange(n) / SR
    pad = 0.15 * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 277 * t + 1)) * level
    x = pad.copy()
    beat = 60.0 / tempo
    k = int(0.12 * SR)
    kick = np.sin(2 * np.pi * 55 * np.arange(k) / SR) * np.exp(-np.arange(k) / (0.03 * SR))
    hat = rng.standard_normal(int(0.01 * SR)) * np.hanning(int(0.01 * SR)) * 0.3
    at = intro
    while at < seconds - 0.2:
        i = int(at * SR)
        x[i : i + k] += kick[: n - i] * 2.5 * level
        x[i : i + len(hat)] += hat[: n - i] * level
        at += beat
    x *= min(1.0, 0.98 / np.abs(x).max())  # громкий трек — у самого предела, но не за ним
    stereo = np.stack([x, x * 0.9], axis=1).astype(np.float32)
    raw = path.with_suffix(".wav")
    import soundfile as sf

    sf.write(raw, stereo, SR, subtype="FLOAT")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c:a", "aac", "-b:a", "256k", str(path)],
        check=True,
    )
    raw.unlink()
    return path


def lag_and_residual(x, y):
    """Сдвиг y относительно x (отсчёты) и остаток после совмещения, дБ.

    Сдвиг до ~10 мс — это перемотка ffmpeg внутри AAC (связка режется из куска,
    прочитанного с перемоткой, а проверка читает трек с начала); плееру он не
    мешает: тот находит место по звуку, а не по числу."""
    a, b = x[:, 0].astype(np.float64), y[:, 0].astype(np.float64)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    m = 2000
    c = np.fft.irfft(np.fft.rfft(a, 2 * n) * np.conj(np.fft.rfft(b, 2 * n)))
    c = np.concatenate([c[-m:], c[: m + 1]])
    lag = int(np.argmax(c)) - m
    if lag >= 0:
        a, b = a[lag:], b[: n - lag]
    else:
        a, b = a[: n + lag], b[-lag:]
    resid = a - b
    return lag, 20 * np.log10(np.sqrt(np.mean(resid**2)) / np.sqrt(np.mean(a**2)) + 1e-12)


NEAR = int(0.015 * SR)  # 15 мс


def whole(path, start, length):
    """Кусок трека, прочитанного целиком с начала — без перемотки внутри AAC."""
    x = rt.decode(path, 0.0, start + length + 1.0)
    return x[int(round(start * SR)) : int(round((start + length) * SR))]


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    d = tmp_path_factory.mktemp("tracks")
    return {
        "a120": make_track(d / "a120.m4a", 120, 70),
        "b123": make_track(d / "b123.m4a", 123, 60),
        "b123_intro": make_track(d / "b123i.m4a", 123, 60, intro=8.0),
        "b150": make_track(d / "b150.m4a", 150, 60),
        "loud": make_track(d / "loud.m4a", 123, 60, level=1.0),
    }


def render(tracks, a, b, **extra):
    job = {"a": str(tracks[a]), "b": str(tracks[b]), "tempo_a": None, "tempo_b": None, **extra}
    return rt.render(job)


# ---------------- Выбор способа (числа) ----------------


def test_a_beatless_intro_is_played_whole_without_stretching():
    d = rt.choose(120, 123, True, True, first_beat_b=8.0, voice_b=12.0)
    assert d["case"] == "intro"
    # A уходит раньше голоса и барабана, но не дольше 8 ударов.
    assert d["fade"] == pytest.approx(min(8 * 0.5, 8.0 - 1.0))


def test_the_overlap_ends_four_beats_before_the_voice():
    # Удар 0.5 с; от первого удара до голоса 6 с = 12 ударов, минус 4 — влезает 8.
    d = rt.choose(120, 120, True, True, first_beat_b=0.5, voice_b=6.5)
    assert (d["case"], d["beats"]) == ("beatmatch", 8)
    assert rt.choose(120, 120, True, True, 0.5, 30.0)["beats"] == 16
    # Текста нет — где голос, неизвестно: 8 ударов.
    assert rt.choose(120, 120, True, True, 0.5, None)["beats"] == 8


def test_a_voice_right_away_gets_a_short_cut():
    d = rt.choose(120, 120, True, True, first_beat_b=0.3, voice_b=1.0)
    assert (d["case"], d["together"], d["reason"]) == ("cut", 1, "voice")


def test_tempos_too_far_apart_are_not_stretched():
    d = rt.choose(112, 144, True, True, first_beat_b=0.3, voice_b=20.0)
    assert (d["case"], d["factor"], d["reason"]) == ("cut", 1.0, "tempo")


def test_double_and_half_tempo_count_as_close():
    d = rt.choose(80, 161, True, True, first_beat_b=0.3, voice_b=None)
    assert d["case"] == "beatmatch"
    assert d["tempo_b"] == pytest.approx(80.5)


# ---------------- Сведение (звук) ----------------


@pytest.mark.parametrize(
    ("b", "case"), [("b123", "beatmatch"), ("b123_intro", "intro"), ("b150", "cut")]
)
def test_the_edges_of_the_bridge_are_the_tracks_themselves(tracks, b, case):
    bridge, plan = render(tracks, "a120", b)
    assert plan["case"] == case
    lead = int(plan["lead"] * SR)
    a = whole(tracks["a120"], plan["a_from"], plan["lead"])
    lag, resid = lag_and_residual(a[:lead], bridge[:lead])
    assert abs(lag) <= NEAR and resid < -30, (lag, resid)
    solo = int(plan["b_solo_at"] * SR)
    # Первые 20 мс — шов после растянутого куска; дальше — сам B.
    skip = int(0.03 * SR)
    b_ref = whole(tracks[b], plan["b_from"] + skip / SR, 4.0)
    lag, resid = lag_and_residual(b_ref, bridge[solo + skip : solo + skip + len(b_ref)])
    assert abs(lag) <= NEAR and resid < -30, (lag, resid)
    assert plan["length"] == pytest.approx(len(bridge) / SR, abs=1e-5)


def test_the_tempo_of_b_is_matched_only_while_they_overlap(tracks):
    _, plan = render(tracks, "a120", "b123")
    assert plan["stretch"] == pytest.approx(123 / 120, rel=0.01)
    assert plan["beats"] in (8, 16)


def test_the_whole_beatless_intro_is_in_the_bridge(tracks):
    bridge, plan = render(tracks, "a120", "b123_intro")
    b_start = rt.decode(tracks["b123_intro"], 0.0, 3.0)
    start, _ = rt.loud_edges(b_start)
    first = b_start[start : start + SR]
    # Самое начало B (аккорд до барабана) лежит сразу после чистого A.
    lead = int(plan["lead"] * SR)
    window = bridge[lead : lead + 2 * SR]
    lag, _ = lag_and_residual(first, window[: len(first)])
    assert abs(lag) <= NEAR


def test_the_overlap_ends_before_the_voice(tracks):
    # Голос B на 7-й секунде: наложение должно кончиться за 4 удара до него.
    _, plan = render(tracks, "a120", "b123", voice_b=7.0)
    assert plan["case"] == "beatmatch"
    assert plan["solo_before_voice"] >= 4 * 60 / 123 * 0.9


def test_replaygain_is_in_the_bridge(tracks):
    bridge, plan = render(tracks, "a120", "b123", gain_a=-6.0)
    lead = int(plan["lead"] * SR)
    a = rt.decode(tracks["a120"], plan["a_from"], plan["lead"])
    ratio = np.sqrt(np.mean(bridge[:lead] ** 2)) / np.sqrt(np.mean(a[:lead] ** 2))
    assert ratio == pytest.approx(10 ** (-6 / 20), rel=0.01)
    # Поправка вверх не применяется — как в плеере.
    assert rt.gain_factor(4.0) == 1.0


def test_two_loud_tracks_together_do_not_clip(tracks):
    bridge, plan = render(tracks, "loud", "loud")
    lead, solo = int(plan["lead"] * SR), int(plan["b_solo_at"] * SR)
    assert np.abs(bridge[lead:solo]).max() <= rt.CEILING + 1e-6
    # Края не тронуты ограничителем — по ним плеер стыкуется.
    lead = int(plan["lead"] * SR)
    a = rt.decode(tracks["loud"], plan["a_from"], plan["lead"])
    assert lag_and_residual(a[:lead], bridge[:lead])[1] < -30


def test_the_script_writes_flac_and_prints_the_plan(tracks, tmp_path):
    out = tmp_path / "x.flac"
    job = {"a": str(tracks["a120"]), "b": str(tracks["b123"]), "out": str(out)}
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(job), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stdout + run.stderr
    plan = json.loads(run.stdout)
    assert plan["version"] == rt.VERSION and out.stat().st_size > 10_000
    import soundfile as sf

    info = sf.info(out)
    assert info.samplerate == SR and info.frames == round(plan["length"] * SR)


def test_a_failure_is_one_json_line_and_a_nonzero_exit(tmp_path):
    job = {"a": str(tmp_path / "missing.m4a"), "b": str(tmp_path / "x.m4a"), "out": "x"}
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(job), capture_output=True, text=True
    )
    assert run.returncode == 1
    assert "error" in json.loads(run.stdout)
