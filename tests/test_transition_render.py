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


def make_track(
    path: Path,
    tempo: float,
    seconds: float,
    intro: float = 0.0,
    level: float = 0.3,
    soft: float = 0.0,
    fade_out: float = 0.0,
):
    """Бас-барабан на каждую долю с ``intro`` секунды, до неё — один аккорд.

    soft — первые столько секунд вчетверо тише (вступление, которое B играет
    под A); fade_out — столько секунд в конце трек затихает до нуля."""
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
    if soft:
        x[: int(soft * SR)] *= 0.25
    if fade_out:
        m = int(fade_out * SR)
        x[-m:] *= np.linspace(1.0, 0.0, m)
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
        # Аккорд с первой секунды, первый удар — через 0,3 с: затакт.
        "b150_pickup": make_track(d / "b150p.m4a", 150, 60, intro=0.3),
        "b150_late": make_track(d / "b150l.m4a", 150, 60, intro=6.0),
        "loud": make_track(d / "loud.m4a", 123, 60, level=1.0),
        "b123_soft": make_track(d / "b123s.m4a", 123, 60, soft=10.0),
        "a120_fade": make_track(d / "a120f.m4a", 120, 70, fade_out=12.0),
    }


def render(tracks, a, b, parts=None, **extra):
    job = {"a": str(tracks[a]), "b": str(tracks[b]), "tempo_a": None, "tempo_b": None, **extra}
    return rt.render(job, parts=parts)


# ---------------- Выбор способа (числа) ----------------


def test_a_beatless_intro_is_played_whole_without_stretching():
    d = rt.choose(120, 123, True, True, first_beat_b=8.0, voice_b=12.0)
    assert d["case"] == "intro"
    # A уходит раньше голоса и барабана (за четверть секунды), не дольше 16 ударов.
    assert d["fade"] == pytest.approx(min(16 * 0.5, rt.INTRO_MAX, 8.0 - 0.25))


def test_the_overlap_lasts_until_b_comes_in_full_but_not_past_its_voice():
    # Удар 0.5 с. B входит в силу на 8,5 с: 16 ударов от первого (+2) — влезает 16.
    d = rt.choose(120, 120, True, True, first_beat_b=0.5, voice_b=None, rise_b=8.5)
    assert (d["case"], d["beats"]) == ("beatmatch", 16)
    # Тихое вступление длинное — 32 удара (16 с), но не дольше BLEND_MAX.
    assert rt.choose(120, 120, True, True, 0.5, None, rise_b=20.0)["beats"] == 32
    assert rt.choose(60, 60, True, True, 0.5, None, rise_b=60.0)["beats"] == 16
    # Голос раньше: от первого удара до голоса 6 с = 12 ударов — влезает 8.
    assert rt.choose(120, 120, True, True, 0.5, 6.5, rise_b=20.0)["beats"] == 8
    # B сразу в полную силу — короткое наложение, 4 удара.
    assert rt.choose(120, 120, True, True, 0.5, None, rise_b=0.5)["beats"] == 4
    # Где B входит в силу, неизвестно: 8 ударов.
    assert rt.choose(120, 120, True, True, 0.5, None)["beats"] == 8


def test_a_voice_right_away_gets_a_short_cut():
    d = rt.choose(120, 120, True, True, first_beat_b=0.3, voice_b=1.0)
    assert (d["case"], d["together"], d["reason"]) == ("cut", 1, "voice")


def test_tempos_too_far_apart_are_not_stretched():
    d = rt.choose(112, 144, True, True, first_beat_b=0.3, voice_b=20.0)
    assert (d["case"], d["factor"], d["reason"]) == ("cut", 1.0, "tempo")


def test_a_short_cut_in_close_tempo_still_matches_the_tempo():
    """Голос у B сразу — стык короткий, но и на нём удары не должны расходиться."""
    d = rt.choose(120, 123, True, True, first_beat_b=0.3, voice_b=1.0)
    assert d["case"] == "cut" and d["factor"] == pytest.approx(123 / 120)


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
    assert plan["beats"] in (4, 8, 16, 32)


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
    # Голос B на 7-й секунде, а вступление тихое 10 с: наложение — до голоса.
    _, plan = render(tracks, "a120", "b123_soft", voice_b=7.0)
    assert plan["case"] == "beatmatch"
    assert plan["solo_before_voice"] >= 0


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


def _kicks(part, offset=0.0, rel=0.3):
    """Секунды атак бас-барабана в части перехода (A или B порознь — их
    отдаёт render(parts=...)), от начала связки: offset — где часть в ней."""
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, 100, btype="lowpass", fs=SR, output="sos")
    low = np.abs(sosfiltfilt(sos, part[:, 0].astype(np.float64)))
    hop = int(0.002 * SR)
    env = low[: len(low) // hop * hop].reshape(-1, hop).max(axis=1)
    above = env > env.max() * rel
    rises = np.nonzero(above[1:] & ~above[:-1])[0] + 1
    return [offset + r * hop / SR for r in rises]


@pytest.mark.parametrize("voice_b", [None, 0.6], ids=["together4", "voice-right-away"])
def test_the_first_beat_of_b_lands_on_a_beat_of_a(tracks, voice_b):
    """15.11.2026, «заикание»: B входил в сетку A первым звуком, и затакт
    (0,3 с до первого удара) сдвигал его удары мимо ударов A."""
    parts = {}
    bridge, plan = render(tracks, "a120", "b150_pickup", parts=parts, voice_b=voice_b)
    assert plan["case"] == "cut"
    (first, *_) = _kicks(parts["b"], plan["lead"])
    # Удары A в его файле — ровно каждые 0,5 с; в связке время A = a_from + t.
    phase = (plan["a_from"] + first) % 0.5
    assert min(phase, 0.5 - phase) < 0.02, (first, phase)
    # И звук B до удара (аккорд затакта) в связке есть, а не срезан: B
    # звучит в связке с (b_solo_at − b_from) — за 0,3 с до своего удара.
    b_sound = plan["b_solo_at"] - plan["b_from"]
    assert abs(first - b_sound - 0.3) < 0.03, (first, b_sound)


def test_a_long_start_without_drums_is_not_cut_off_for_the_beat(tracks):
    """Первый удар B на 6-й секунде, голос сразу: подвести удар под удар A
    можно, только срезав начало B — а с ним первые слова. Тогда B входит
    первым звуком, как раньше (рецензент 15.11)."""
    bridge, plan = render(tracks, "a120", "b150_late", voice_b=0.6)
    assert plan["case"] == "cut"
    # Из B в переходе сыграно меньше секунды — с самого его начала.
    assert plan["b_from"] < 1.0, plan


def test_a_track_without_beats_still_gets_a_bridge(tracks):
    """Без ударов librosa отвечает темпом 0: сведение не делит на него, а
    просто не подгоняет темп."""
    import numpy as np

    def no_beats(x, prior):
        return 0.0, np.array([])

    job = {"a": str(tracks["a120"]), "b": str(tracks["b123"]), "tempo_a": None, "tempo_b": None}
    bridge, plan = rt.render(job, beats_hook=no_beats)
    assert plan["stretch"] == 1.0
    assert plan["length"] == pytest.approx(len(bridge) / SR, abs=1e-5)


# ---------------- Аудит 23.11.2026: что слышалось на 150 парах ----------------


def test_the_start_of_a_cut_keeps_the_bass_of_a_no_click(tracks):
    """Басы A срезались фильтром разом на первом отсчёте перехода — щелчок в
    60 из 102 «cut». Теперь басы A в начале перехода — его же басы, а уходят
    они плавно на первом ударе B."""
    parts = {}
    bridge, plan = render(tracks, "a120", "b150", parts=parts)
    assert plan["case"] == "cut"
    lead = int(plan["lead"] * SR)
    a = whole(tracks["a120"], plan["a_from"] + plan["lead"], 0.3)
    low = rt.low_band
    lag, resid = lag_and_residual(low(a), low(parts["a"][: len(a)]))
    assert abs(lag) <= NEAR and resid < -30, (lag, resid)
    step = abs(float(bridge[lead, 0] - bridge[lead - 1, 0]))
    typical = float(np.median(np.abs(np.diff(bridge[lead - 2000 : lead + 2000, 0]))))
    assert step < 8 * typical + 1e-4, (step, typical)


def test_the_fade_out_of_a_is_replaced_by_b(tracks):
    """Связка в затухание A проваливалась по громкости: переход — до него."""
    _, plan = render(tracks, "a120_fade", "b123")
    a_len = rt.duration(tracks["a120_fade"])
    mix_end = plan["a_from"] + plan["b_solo_at"]  # где по A кончается переход
    # Из 12 с затухания громким (до −9 дБ от обычного) остаётся ~4,5 с.
    assert plan["a_dropped"] >= 5.0, plan
    assert mix_end <= a_len - 5.0, (mix_end, a_len)


def test_two_tracks_together_are_not_louder_than_either(tracks):
    """«Сначала громко, потом тихо»: сумма двух треков на стыке была громче
    обоих на 2–7 LU. Теперь — не громче прямой между ними (+1 LU допуск)."""
    bridge, plan = render(tracks, "loud", "loud")
    lead, solo = int(plan["lead"] * SR), int(plan["b_solo_at"] * SR)
    before = float(rt.lufs(rt.power_curve(bridge[lead - 3 * SR : lead], 3.0))[0])
    after = float(rt.lufs(rt.power_curve(bridge[solo : solo + 3 * SR], 3.0))[0])
    level = rt.lufs(rt.power_curve(bridge[lead:solo], 1.0))
    assert level.max() <= max(before, after) + 1.5, (level.max(), before, after)


def test_kicks_stay_together_through_a_long_overlap(tracks):
    """Галоп: удары B уплывали от ударов A к концу наложения (медиана 39 мс).
    На 16+ ударах каждый удар B — рядом с ударом A."""
    parts = {}
    _, plan = render(tracks, "a120", "b123_soft", parts=parts)
    assert plan["case"] == "beatmatch" and plan["beats"] >= 16, plan
    # Удары A в его файле — ровно каждые 0,5 с; часть B начинается в связке на
    # lead. Удары B — по тарелке на каждой доле: басы B входят лишь посередине.
    import librosa

    mono = parts["b"].mean(axis=1)
    env = librosa.onset.onset_strength(y=mono, sr=SR, hop_length=128)
    frames = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=128, backtrack=False)
    # Сильные атаки — удары; слабые между ними — щелчок обрезанного хвоста
    # бочки в самом тестовом треке (на +0,12 с), не сдвиг.
    frames = frames[env[frames] >= 0.4 * env[frames].max()]
    kb = librosa.frames_to_time(frames, sr=SR, hop_length=128) + plan["a_from"] + plan["lead"]
    assert len(kb) >= 8, kb
    phase = kb % 0.5
    worst = float(np.max(np.minimum(phase, 0.5 - phase)))
    assert worst < 0.012, (worst, kb)
