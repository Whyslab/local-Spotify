#!/usr/bin/env python3
"""Переход «как диджей»: связка между концом одного трека и началом другого.

Плеер на ноутбуке играет треки обычным <audio>, а на стыке подменяет звук
готовой связкой. Связка — короткий файл, сведённый здесь, а не в браузере:
поиск ударов (librosa) и растяжение без смены высоты (rubberband) браузеру не
по силам, а так звучит ровно то, что одобрено на слух (проба 08.10).

Устройство связки (секунды файла связки):

    [0, lead)              чистый конец A с позиции a_from — по нему плеер
                           находит, где сейчас играет A, и встаёт на него
    [lead, b_solo_at)      сам переход (один из трёх способов ниже)
    [b_solo_at, length)    чистое начало B с позиции b_from, в своём темпе —
                           по нему плеер подводит под связку элемент B

Три способа, по порядку проверки:

1. «intro»: у B вступление без барабана (4 с и больше). Подгонять нечего: B с
   самого начала, A затихает поверх вступления и уходит раньше голоса и барабана.
2. «beatmatch»: темпы ближе 8 % и до голоса B есть место. B растянут под темп A
   на время наложения, удар на удар (подстройка фазы по атакам), наложение
   16, 8 или 4 удара — чтобы кончилось за 4 удара до голоса B; басы меняются
   посередине; B громко уже с первой четверти; B с самого начала.
3. «cut»: голос у B сразу или темпы далеко. A затихает на ударе за 4 удара
   без басов, B входит с самого начала на последнем ударе (голос сразу) или на
   первом.

Где у B голос — первая строка синхронного текста (её передаёт служба);
текста нет — наложение 8 ударов.

Громкость: части A и B умножены на поправки ReplayGain (только вниз, как в
плеере), поэтому связка играет на громкости ползунка. Где сумма двух треков
громче предела, её прижимает ограничитель — только там, края связки не
трогаются: по ним плеер стыкуется.

Вход — JSON в stdin: a, b (пути), out (FLAC), gain_a, gain_b (дБ или null),
tempo_a, tempo_b (уд/мин из разбора фонотеки или null), voice_b (с от начала
файла B или null). Выход — план в stdout (JSON). Нужны numpy, scipy, librosa,
soundfile (scripts/requirements-analysis.txt) и ffmpeg с фильтром rubberband.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

# Как в analyze_audio.py: numba кэширует рядом с librosa, а там только чтение.
os.environ.setdefault(
    "NUMBA_CACHE_DIR", str(Path(__file__).resolve().parents[1] / "adder" / "numba-cache")
)

if TYPE_CHECKING:
    import numpy as np

VERSION = 1
SR = 48000
LEAD = 4.0  # с чистого A в начале связки
TAIL = 8.0  # с чистого B в конце связки
WINDOW_A = 40.0  # сколько конца A разбирать
WINDOW_B = 50.0  # сколько начала B разбирать
SILENCE_DB = -45.0
MAX_STRETCH = 0.08
INTRO_SOLO_BEATS = 4  # столько ударов начало B звучит одно, до голоса
BEATLESS_INTRO = 4.0  # с: вступление без барабана, которого хватает на способ «intro»
VOICE_SOON = 3.0  # с: голос раньше — способ «cut»
CEILING = 0.999  # прижимается только сумма двух треков: каждый сам по себе не выше 1


def gain_factor(gain_db: float | None) -> float:
    """Как gainFactor() в плеере: только вниз."""
    if not isinstance(gain_db, (int, float)):
        return 1.0
    return min(1.0, 10 ** (gain_db / 20))


# ---------------- Решение (без звука: проверяется числами) ----------------


def choose(
    tempo_a: float,
    tempo_b: float,
    beats_a_ok: bool,
    beats_b_ok: bool,
    first_beat_b: float,
    voice_b: float | None,
) -> dict:
    """Способ перехода и длина наложения в ударах A.

    first_beat_b и voice_b — секунды от начала звука B (после срезанной тишины).
    """
    tb_eff = min((tempo_b, tempo_b * 2, tempo_b / 2), key=lambda t: abs(t / tempo_a - 1))
    off = tb_eff / tempo_a - 1
    beat = 60.0 / tempo_a
    voice_soon = voice_b is not None and voice_b < VOICE_SOON
    base = {"tempo_a": tempo_a, "tempo_b": tb_eff, "off": off, "beat": beat}
    if first_beat_b >= BEATLESS_INTRO and not voice_soon:
        limit = min(first_beat_b, voice_b) if voice_b is not None else first_beat_b
        fade = float(min(8 * beat, max(1.0, limit - 1.0)))
        return {**base, "case": "intro", "fade": fade, "factor": 1.0, "beats": 0}
    can_stretch = abs(off) <= MAX_STRETCH and beats_a_ok and beats_b_ok
    factor = tb_eff / tempo_a if can_stretch else 1.0
    if voice_b is not None:
        room = (voice_b - first_beat_b) * factor / beat - INTRO_SOLO_BEATS
        nb = next((k for k in (16, 8, 4) if k <= room), 0)
    else:
        nb = 8
    if can_stretch and nb:
        return {**base, "case": "beatmatch", "factor": factor, "beats": nb}
    together = 1 if voice_soon or (voice_b is not None and voice_b < first_beat_b + 4 * beat) else 4
    reason = "voice" if together == 1 else "tempo"
    return {
        **base,
        "case": "cut",
        "factor": 1.0,
        "beats": 4,
        "together": together,
        "reason": reason,
    }


# ---------------- Звук ----------------


def decode(path: Path, start: float, length: float) -> np.ndarray:
    import numpy as np

    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-ss", f"{max(0.0, start):.6f}", "-t", f"{length:.6f}",
            "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-",
        ],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def loud_edges(x: np.ndarray) -> tuple[int, int]:
    """Первый и последний отсчёт громче SILENCE_DB (окна по 50 мс)."""
    import numpy as np

    hop = int(0.05 * SR)
    frames = len(x) // hop
    if frames == 0:
        return 0, len(x)
    rms = np.sqrt(np.mean(x[: frames * hop].reshape(frames, hop, 2) ** 2, axis=(1, 2)) + 1e-12)
    loud = np.nonzero(20 * np.log10(rms) > SILENCE_DB)[0]
    if len(loud) == 0:
        return 0, len(x)
    return int(loud[0] * hop), int(min(len(x), (loud[-1] + 1) * hop))


def find_beats(x: np.ndarray, prior: float | None) -> tuple[float, np.ndarray]:
    """Удары куска. Темп всего трека — подсказка: на коротком куске librosa
    без неё часто берёт 2/3 или половину темпа."""
    import librosa
    import numpy as np

    mono = x.mean(axis=1)

    def fixed(bpm: float) -> np.ndarray:
        _, frames = librosa.beat.beat_track(y=mono, sr=SR, units="time", bpm=bpm, tightness=400)
        return np.asarray(frames, dtype=float)

    if prior:
        _, frames = librosa.beat.beat_track(
            y=mono, sr=SR, units="time", start_bpm=prior, tightness=400
        )
        frames = np.asarray(frames, dtype=float)
        if len(frames) > 8:
            local = 60.0 / float(np.median(np.diff(frames)))
            for k in (1.0, 2.0, 0.5):
                if abs(local * k / prior - 1) <= 0.06:
                    return local * k, frames if k == 1.0 else fixed(local * k)
        return float(prior), fixed(prior)
    tempo, frames = librosa.beat.beat_track(y=mono, sr=SR, units="time")
    return float(np.atleast_1d(tempo)[0]), np.asarray(frames, dtype=float)


def beat_lag(a: np.ndarray, b: np.ndarray, window: float) -> float:
    """На сколько секунд атаки B раньше атак A (пик корреляции огибающих)."""
    import librosa
    import numpy as np

    ea = librosa.onset.onset_strength(y=a.mean(axis=1), sr=SR, hop_length=256)
    eb = librosa.onset.onset_strength(y=b.mean(axis=1), sr=SR, hop_length=256)
    n = min(len(ea), len(eb))
    ea, eb = ea[:n] - ea[:n].mean(), eb[:n] - eb[:n].mean()
    step = 256 / SR
    m = max(1, int(window / step))
    lags = range(-m, m + 1)
    score = [
        float(np.dot(ea[max(0, k) : n + min(0, k)], eb[max(0, -k) : n - max(0, k)])) for k in lags
    ]
    return lags[int(np.argmax(score))] * step


def stretch(x: np.ndarray, factor: float) -> np.ndarray:
    """Длительность × factor, высота та же (rubberband внутри ffmpeg)."""
    import numpy as np

    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
            "-af", f"rubberband=tempo={1 / factor:.6f}:pitchq=quality:transients=crisp",
            "-f", "f32le", "-ac", "2", "-ar", str(SR), "-",
        ],
        input=np.ascontiguousarray(x, dtype=np.float32).tobytes(),
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()


def highpass(x: np.ndarray, hz: float = 220.0) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, hz, btype="highpass", fs=SR, output="sos")
    return sosfiltfilt(sos, x, axis=0).astype("float32")


def ramp(n: int) -> np.ndarray:
    import numpy as np

    return np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]


def limit(x: np.ndarray, start: int, stop: int) -> np.ndarray:
    """Прижать пики выше CEILING в [start, stop) плавным усилением; вне отрезка
    звук не меняется. Минимум по окну 100 мс, затем среднее по 50 мс: среднее
    лежит внутри окна минимума, поэтому кривая нигде не выше нужной."""
    import numpy as np
    from scipy.ndimage import maximum_filter1d, uniform_filter1d

    seg = x[start:stop]
    if len(seg) == 0:
        return x
    peak = np.abs(seg).max(axis=1)
    if peak.max() <= CEILING:
        return x
    need = np.minimum(1.0, CEILING / np.maximum(peak, 1e-9))
    floor = -maximum_filter1d(-need, size=int(0.1 * SR) + 1)
    gain = np.minimum(uniform_filter1d(floor, size=int(0.05 * SR)), need)
    out = x.copy()
    out[start:stop] = seg * gain[:, None].astype(np.float32)
    return out


def render(job: dict, beats_hook=None) -> tuple[np.ndarray, dict]:
    """Свести связку. beats_hook(x, prior) -> (tempo, beats) — для тестов."""
    import numpy as np

    beat_finder = beats_hook or find_beats
    path_a, path_b = Path(job["a"]), Path(job["b"])
    fa, fb = gain_factor(job.get("gain_a")), gain_factor(job.get("gain_b"))
    dur_a = duration(path_a)
    a0 = max(0.0, dur_a - WINDOW_A)  # позиция в файле A первого отсчёта окна
    a = decode(path_a, a0, dur_a - a0 + 1.0) * fa
    b = decode(path_b, 0.0, WINDOW_B) * fb
    _, a_end_i = loud_edges(a)
    b_start_i, _ = loud_edges(b)
    a_end = a_end_i / SR  # секунды в окне A
    b_start = b_start_i / SR  # секунды файла B
    voice = job.get("voice_b")
    voice_b = (float(voice) - b_start) if isinstance(voice, (int, float)) else None
    if voice_b is not None and voice_b < 0:
        voice_b = 0.0

    a_body = a[:a_end_i]
    b_body = b[b_start_i:]
    tempo_a, beats_a = beat_finder(a_body[-int(30 * SR) :], job.get("tempo_a"))
    # Удары конца A — в секундах окна A.
    beats_a = beats_a + max(0.0, a_end - 30.0)
    tempo_b, beats_b = beat_finder(b_body[: int(30 * SR)], job.get("tempo_b"))
    first_beat_b = float(beats_b[0]) if len(beats_b) else 0.0
    decision = choose(tempo_a, tempo_b, len(beats_a) > 20, len(beats_b) > 4, first_beat_b, voice_b)
    beat = decision["beat"]

    def a_from(span: float) -> float:
        """Удар A, после которого до конца звука A не меньше span секунд."""
        k = max(0, int(np.searchsorted(beats_a, a_end - span)) - 1)
        return float(beats_a[k]) if len(beats_a) else max(0.0, a_end - span)

    case = decision["case"]
    if case == "intro":
        fade = decision["fade"]
        mix_a = a_from(fade)  # секунды окна A, где начинается переход
        n = int(round(fade * SR))
        a_mix = a[int(round(mix_a * SR)) : int(round(mix_a * SR)) + n]
        b_mix = b_body[:n]
        n = min(len(a_mix), len(b_mix))
        t = ramp(n)
        mixed = a_mix[:n] * np.cos(t * np.pi / 2) + b_mix[:n] * np.sin(
            np.clip(t * 4, 0, 1) * np.pi / 2
        )
        b_used = n  # отсчётов B (с начала звука) уже в переходе
        b_stretched = False
        seam = None
    elif case == "beatmatch":
        factor = decision["factor"]
        nb = decision["beats"]
        L = nb * beat
        head = b_body[: int((first_beat_b + L / factor + 2.0) * SR)]
        b_head = stretch(head, factor)
        first_b = first_beat_b * factor
        start_a = a_from(L)
        nL = int(round(L * SR))
        sa = int(round(start_a * SR))
        lag = beat_lag(
            a[sa : sa + nL],
            b_head[int(first_b * SR) : int(first_b * SR) + nL],
            window=30.0 / tempo_a,
        )
        first_b = max(0.0, first_b - lag)
        fb_i = int(round(first_b * SR))
        pre = min(fb_i, sa)  # начало B до его первого удара
        mix_a = (sa - pre) / SR
        a_mix = a[sa - pre : sa + nL]
        b_mix = b_head[fb_i - pre : fb_i + nL]
        n = min(len(a_mix), len(b_mix))
        a_mix, b_mix = a_mix[:n], b_mix[:n]
        t = ramp(n)
        swap = np.clip((t - 0.5) * 16 + 0.5, 0, 1)
        a_bass = a_mix * (1 - swap) + highpass(a_mix) * swap
        b_bass = highpass(b_mix) * (1 - swap) + b_mix * swap
        gain_b = np.sin(np.clip(t * 4, 0, 1) * np.pi / 2)
        gain_a = np.cos(np.clip((t - 0.25) / 0.75, 0, 1) * np.pi / 2)
        mixed = a_bass * gain_a + b_bass * gain_b
        # Сколько исходника B сыграно растянутым: дальше он идёт в своём темпе.
        b_used = int(round((fb_i - pre + n) / factor))
        b_stretched = True
        # Растянутый и исходный звук отсчёт в отсчёт не совпадают: шов в 20 мс.
        seam = b_head[fb_i - pre + n : fb_i - pre + n + int(0.02 * SR)]
    else:
        together = decision["together"]
        start_a = a_from(4 * beat)
        sa = int(round(start_a * SR))
        a_mix = a[sa : sa + int(round(4 * beat * SR))]
        n = len(a_mix)
        lead_in = n - min(n, int(round(together * beat * SR)))  # A один до входа B
        b_mix = b_body[: n - lead_in]
        curve = ramp(n) if together > 1 else np.clip((ramp(n) - 0.5) * 2, 0, 1)
        mixed = highpass(a_mix) * np.cos(curve * np.pi / 2)
        rise = np.sin(np.clip(ramp(len(b_mix)) * (3 if together > 1 else 40), 0, 1) * np.pi / 2)
        mixed[lead_in : lead_in + len(b_mix)] += b_mix * rise
        mix_a = start_a
        b_used = len(b_mix)
        b_stretched = False
        seam = None

    lead_i = int(round(LEAD * SR))
    mix_i = int(round(mix_a * SR))
    if mix_i < lead_i:
        raise ValueError("A слишком короткий для связки")
    lead = a[mix_i - lead_i : mix_i]
    tail = b_body[b_used : b_used + int(round(TAIL * SR))].copy()
    if len(tail) < int(TAIL * SR * 0.5):
        raise ValueError("B слишком короткий для связки")
    if seam is not None and len(seam):
        r = ramp(len(seam))
        tail[: len(seam)] = seam * (1 - r) + tail[: len(seam)] * r
    bridge = np.concatenate([lead, mixed, tail]).astype(np.float32)
    bridge = limit(bridge, lead_i, lead_i + len(mixed))
    b_solo_at = (lead_i + len(mixed)) / SR

    voice_after = None
    if voice_b is not None:
        voice_after = round(voice_b - b_used / SR, 3)
    plan = {
        "version": VERSION,
        "case": case,
        "sample_rate": SR,
        "a_from": round(a0 + (mix_i - lead_i) / SR, 6),
        "lead": LEAD,
        "b_from": round(b_start + b_used / SR, 6),
        "b_solo_at": round(b_solo_at, 6),
        "length": round(len(bridge) / SR, 6),
        "tempo_a": round(decision["tempo_a"], 2),
        "tempo_b": round(decision["tempo_b"], 2),
        "stretch": round(decision["factor"], 5) if b_stretched else 1.0,
        "beats": decision["beats"],
        "voice_b": None if voice_b is None else round(voice_b + b_start, 3),
        "solo_before_voice": voice_after,
    }
    return bridge, plan


def write_flac(path: Path, x: np.ndarray) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".flac", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sf.write(tmp_path, x, SR, format="FLAC", subtype="PCM_16")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    try:
        job = json.loads(sys.stdin.read())
        bridge, plan = render(job)
        write_flac(Path(job["out"]), bridge)
    except Exception as exc:  # noqa: BLE001 — причина уходит службе одной строкой
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 1
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
