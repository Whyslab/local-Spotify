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

Где переход. Не в самом хвосте A, а там, где A начинает затихать сам: его
затухание (а с ним «дыру» в громкости) заменяет начало B. A дальше этой точки
не звучит. Аудит 23.11.2026 (150 пар из Monday): связка, сведённая в
затухание A, проваливалась на 6+ LU у каждой седьмой пары.

Три способа, по порядку проверки:

1. «intro»: у B вступление без барабана (4 с и больше). Подгонять нечего: B с
   самого начала, A затихает поверх вступления и уходит раньше голоса и барабана.
2. «beatmatch»: темпы ближе 8 %. B растянут под темп A на время наложения,
   удар на удар. Наложение — 32, 16, 8 или 4 удара: тихое вступление B идёт
   под A, и кончается оно, когда B входит в силу (или до его голоса). B
   поднимается за первую половину, A уходит за вторую, басы меняются на ударе
   посередине. Темп B после растяжки проверяется по самому звуку: если B к
   концу наложения уплывает от ударов A, растяжка поправляется (аудит:
   медиана расхождения ударов была 39 мс — на слух «галоп»).
3. «cut»: голос у B сразу или темпы далеко. Первый удар B ложится на удар A
   (последний — при голосе сразу, иначе за четыре удара до конца A); затакт B
   звучит поверх A; басы меняются на первом ударе B, A уходит за удар.

Басы и верх делятся фильтром без сдвига фазы (низ + остаток = сам звук),
поэтому, пока усиления равны единице, звук не меняется ни на отсчёт. До
23.11.2026 у A в «cut» басы срезались фильтром разом на первом же отсчёте
перехода — щелчок в 60 из 102 связок «cut».

Громкость: части A и B умножены на поправки ReplayGain (только вниз, как в
плеере), поэтому связка играет на громкости ползунка. Пока звучат оба трека,
громкость суммы ведётся к прямой между громкостью A до перехода и B после
(только вниз, плавно, к краям — без поправки): иначе переход звучал «сначала
громко, потом тихо». Где сумма громче предела, её прижимает ограничитель —
только там, края связки не трогаются: по ним плеер стыкуется.

Где у B голос — первая строка синхронного текста (её передаёт служба).

Вход — JSON в stdin: a, b (пути), out (FLAC), gain_a, gain_b (дБ или null),
tempo_a, tempo_b (уд/мин из разбора фонотеки или null), voice_b (с от начала
файла B или null). Выход — план в stdout (JSON). Нужны numpy, scipy, librosa,
soundfile (scripts/requirements-analysis.txt) и ffmpeg с фильтром rubberband.
"""

from __future__ import annotations

import json
import math
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

# 2 — первый удар B на ударе A в «cut» (15.11.2026)
# 3 — переход до затухания A, деление на басы и верх без щелчка, длинное
#     наложение под вступление B, подстройка растяжки по звуку, громкость
#     суммы (23.11.2026)
VERSION = 3
SR = 48000
LEAD = 4.0  # с чистого A в начале связки
TAIL = 10.0  # с чистого B в конце связки: плееру — время подвести B
WINDOW_A = 60.0  # сколько конца A разбирать
WINDOW_B = 60.0  # сколько начала B разбирать
SILENCE_DB = -45.0
MAX_STRETCH = 0.08
BEATLESS_INTRO = 4.0  # с: вступление без барабана, которого хватает на способ «intro»
VOICE_SOON = 3.0  # с: голос раньше — способ «cut»
CUT_EXTRA_MAX = 8  # ударов A сверх четырёх под длинный затакт B; длиннее — B входит первым звуком
CROSSOVER = 150.0  # Гц: ниже — басы, они меняются на ударе, а не плавно
BLEND_BEATS = (32, 16, 8, 4)
BLEND_MAX = 20.0  # с: наложение не дольше
INTRO_MAX = 12.0  # с: A поверх вступления B не дольше
FADE_DB = 6.0  # A «затихает сам», когда тише своей обычной громкости на столько
FADE_KEEP_MAX = 20.0  # с: затухания A отбрасывается не больше
RISE_DB = 6.0  # B «вошёл в силу», когда тише своей обычной громкости не больше чем на столько
GRID_OK = 0.6  # доля найденных ударов на ровной сетке, чтобы ей верить
LEVEL_TOLERANCE = 1.0  # LU: сумма громче прямой A→B на столько — ещё не поправляется
LEVEL_CUT_MAX = 6.0  # дБ: поправка громкости суммы не больше
HOP = 0.05  # с: шаг кривой громкости
CEILING = 0.999  # прижимается только сумма двух треков; чистые края — как есть, чтобы
# совпадать с самим треком отсчёт в отсчёт (редкий пик выше 1 после декодера срежет FLAC)


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
    rise_b: float | None = None,
    steady: bool = True,
) -> dict:
    """Способ перехода и длина наложения в ударах A.

    first_beat_b, voice_b и rise_b (где B входит в силу) — секунды от начала
    звука B (после срезанной тишины). steady — удары обоих треков ложатся на
    ровную сетку: без неё темп известен неточно, и длинное наложение уплыло бы.
    """
    tb_eff = min((tempo_b, tempo_b * 2, tempo_b / 2), key=lambda t: abs(t / tempo_a - 1))
    off = tb_eff / tempo_a - 1
    beat = 60.0 / tempo_a
    voice_soon = voice_b is not None and voice_b < VOICE_SOON
    base = {"tempo_a": tempo_a, "tempo_b": tb_eff, "off": off, "beat": beat}
    if first_beat_b >= BEATLESS_INTRO and not voice_soon:
        limit = min(first_beat_b, voice_b) if voice_b is not None else first_beat_b
        fade = float(min(16 * beat, INTRO_MAX, max(1.0, limit - 0.25)))
        return {**base, "case": "intro", "fade": fade, "factor": 1.0, "beats": 0}
    can_stretch = abs(off) <= MAX_STRETCH and beats_a_ok and beats_b_ok
    factor = tb_eff / tempo_a if can_stretch else 1.0
    if can_stretch:
        # Наложение — от первого удара B до того, как B войдёт в силу (+2 удара:
        # вход в силу — на ударе, и A к нему уже почти ушёл), но не дольше голоса.
        room = 8.0 if rise_b is None else max(4.0, (rise_b - first_beat_b) * factor / beat + 2)
        if voice_b is not None:
            room = min(room, (voice_b - first_beat_b) * factor / beat)
        # Без ровной сетки темп известен хуже — не дольше 16 ударов: тихое
        # вступление B под громким A прячет неточность, а «дыра» после
        # короткого наложения слышна сразу (пара 68: 14 LU на 10 с).
        room = min(room, BLEND_MAX / beat, BLEND_MAX / beat if steady else 16)
        nb = next((k for k in BLEND_BEATS if k <= room), 0)
        if nb:
            return {**base, "case": "beatmatch", "factor": factor, "beats": nb}
    together = 1 if voice_soon or (voice_b is not None and voice_b < first_beat_b + 4 * beat) else 4
    reason = "voice" if together == 1 else ("room" if can_stretch else "tempo")
    return {
        **base,
        "case": "cut",
        # Темп близко — начало B на время стыка растягивается под A и ставится
        # удар в удар: короткий стык тоже «галопирует», если удары разные.
        "factor": factor,
        "beats": 4,
        "together": together,
        "reason": reason,
    }


def fit_grid(beats: np.ndarray, tempo: float) -> tuple[float, float, float]:
    """Ровная сетка по найденным ударам: (период, фаза, доля ударов на ней).

    Найденные удары дрожат на кадр librosa (±12–25 мс), а темп librosa
    округлён до её сетки темпов: на 16 ударах это уже десятки миллисекунд
    расхождения. Прямая по всем ударам (без выбросов) точнее обоих."""
    import numpy as np

    period = 60.0 / tempo if tempo > 0 else 0.5
    if len(beats) < 4:
        return period, float(beats[0]) if len(beats) else 0.0, 0.0
    beats = np.asarray(beats, dtype=float)
    # Фаза — та, на которую ложится больше всего ударов: librosa посреди куска
    # бывает перескакивает на полдоли, и прямая «по всем» легла бы между.
    tol = 0.07 * period
    best = max(
        beats,
        key=lambda b0: int(
            (np.abs(beats - b0 - np.round((beats - b0) / period) * period) < tol).sum()
        ),
    )
    k = np.round((beats - best) / period)
    keep = np.abs(beats - best - k * period) < tol
    p, ph = period, float(best)
    for _ in range(3):
        if keep.sum() < 4:
            break
        p_new, ph_new = np.polyfit(k[keep], beats[keep], 1)
        if abs(p_new / period - 1) > 0.03:
            break  # прямая ушла от темпа — оставить темп и только фазу
        p, ph = float(p_new), float(ph_new)
        keep = np.abs(beats - (ph + k * p)) < 0.07 * p
    on = np.abs(beats - (ph + k * p)) < 0.07 * p
    return p, ph, float(on.mean())


def fade_point(level: np.ndarray, body: float, end: float, window: float) -> float:
    """Секунда, после которой A только затихает: конец последнего окна (длиной
    window), где громкость не ниже обычной на FADE_DB, без четверти окна.
    Не раньше чем за FADE_KEEP_MAX до конца звука."""
    import numpy as np

    loud = np.nonzero(level >= body - FADE_DB)[0]
    at = loud[-1] * HOP + 0.75 * window if len(loud) else end
    return float(min(end, max(at, end - FADE_KEEP_MAX)))


def rise_point(level: np.ndarray, body: float, window: float) -> float:
    """Секунда, с которой B звучит в полную силу (громкость тела − RISE_DB):
    середина первого такого окна длиной window."""
    import numpy as np

    up = np.nonzero(level >= body - RISE_DB)[0]
    return float(up[0] * HOP + 0.5 * window) if len(up) else 0.0


# ---------------- Звук ----------------


def decode(path: Path, start: float, length: float) -> np.ndarray:
    """Кусок трека с точностью до отсчёта: перемотка после -i (ffmpeg читает
    с начала). Перемотка до -i внутри AAC ложится мимо на 10–20 мс — края
    связки не совпали бы с треком, прочитанным с начала (как его играет плеер)."""
    import numpy as np

    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
            "-ss", f"{max(0.0, start):.6f}", "-t", f"{length:.6f}",
            "-f", "f32le", "-ac", "2", "-ar", str(SR), "-",
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


def _kweight(x: np.ndarray) -> np.ndarray:
    """Взвешивание K (ITU-R BS.1770) при 48 кГц — как у LUFS и ReplayGain 2."""
    from scipy.signal import lfilter

    b1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    a1 = [1.0, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621]
    return lfilter(b2, a2, lfilter(b1, a1, x, axis=0), axis=0)


def power_curve(x: np.ndarray, window: float) -> np.ndarray:
    """Мощность (по K) в окнах window с шагом HOP; кадр i — [i·HOP, i·HOP + window)."""
    import numpy as np

    if len(x) == 0:
        return np.zeros(0)
    p = (_kweight(x.astype(np.float64)) ** 2).sum(axis=1)
    c = np.concatenate([[0.0], np.cumsum(p)])
    w, h = max(1, int(window * SR)), int(HOP * SR)
    starts = np.arange(0, max(1, len(p) - w + 1), h)
    ends = np.minimum(starts + w, len(p))
    return (c[ends] - c[starts]) / np.maximum(1, ends - starts)


def lufs(power: np.ndarray | float) -> np.ndarray:
    import numpy as np

    return -0.691 + 10 * np.log10(np.asarray(power) + 1e-12)


def body_level(level: np.ndarray) -> float:
    """Обычная громкость куска: 80-й процентиль (тишина не в счёт)."""
    import numpy as np

    loud = level[level > -60]
    return float(np.percentile(loud, 80)) if len(loud) else -60.0


def find_beats(x: np.ndarray, prior: float | None) -> tuple[float, np.ndarray]:
    """Удары куска. Темп всего трека — подсказка: на коротком куске librosa
    без неё часто берёт 2/3 или половину темпа.

    Без обрезки слабых ударов по краям (trim=False): обрезка выбрасывала тихие
    удары вступления B, и мягкое вступление с барабаном считалось вступлением
    без барабана — A играл поверх ударов B вразнобой. Где удары настоящие,
    решает real_beats()."""
    import librosa
    import numpy as np

    mono = x.mean(axis=1)

    def fixed(bpm: float) -> np.ndarray:
        _, frames = librosa.beat.beat_track(
            y=mono, sr=SR, units="time", bpm=bpm, tightness=400, trim=False
        )
        return np.asarray(frames, dtype=float)

    if prior:
        _, frames = librosa.beat.beat_track(
            y=mono, sr=SR, units="time", start_bpm=prior, tightness=400, trim=False
        )
        frames = np.asarray(frames, dtype=float)
        if len(frames) > 8:
            local = 60.0 / float(np.median(np.diff(frames)))
            for k in (1.0, 2.0, 0.5):
                if abs(local * k / prior - 1) <= 0.06:
                    return local * k, frames if k == 1.0 else fixed(local * k)
        return float(prior), fixed(prior)
    tempo, frames = librosa.beat.beat_track(y=mono, sr=SR, units="time", trim=False)
    return float(np.atleast_1d(tempo)[0]), np.asarray(frames, dtype=float)


def real_beats(x: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """Удары с начала куска, где есть атака: трекер без обрезки ставит удары и
    в тишину, и в ровный аккорд вступления. Первый настоящий — с атакой не
    слабее 30 % обычной (медиана по второй половине ударов)."""
    import librosa
    import numpy as np

    if len(beats) < 4:
        return beats
    hop = 256
    env = librosa.onset.onset_strength(y=x.mean(axis=1), sr=SR, hop_length=hop)
    frames = np.round(np.asarray(beats) * SR / hop).astype(int)
    strength = np.array([env[max(0, f - 3) : f + 4].max() if f < len(env) else 0.0 for f in frames])
    ref = float(np.median(strength[len(strength) // 2 :]))
    if ref <= 0:
        return beats
    real = np.nonzero(strength >= 0.3 * ref)[0]
    return beats[real[0] :] if len(real) else beats[:0]


def _envelopes(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Огибающие атак всего звука и басов (бочка, 808) — шаг 256 отсчётов."""
    import librosa

    mono = x.mean(axis=1)
    full = librosa.onset.onset_strength(y=mono, sr=SR, hop_length=256)
    low = librosa.onset.onset_strength(y=low_band(mono[:, None])[:, 0], sr=SR, hop_length=256)
    return full, low


def beat_lag(a: np.ndarray, b: np.ndarray, window: float) -> tuple[float, float]:
    """На сколько секунд атаки B раньше атак A и насколько уверенно (0…1).

    Пик суммы корреляций огибающих всего звука и басов: по одним тарелкам
    трэп легко совпадает «на полдоли», бочка держит удар на ударе."""
    import numpy as np

    (fa, la), (fb, lb) = _envelopes(a), _envelopes(b)
    n = min(len(fa), len(fb))
    step = 256 / SR
    m = max(1, int(window / step))
    lags = range(-m, m + 1)
    total = np.zeros(len(lags))
    quality = []
    for ea, eb in ((fa[:n], fb[:n]), (la[:n], lb[:n])):
        ea, eb = ea - ea.mean(), eb - eb.mean()
        norm = float(np.linalg.norm(ea) * np.linalg.norm(eb))
        if norm < 1e-9:
            continue
        score = np.array(
            [
                float(np.dot(ea[max(0, k) : n + min(0, k)], eb[max(0, -k) : n - max(0, k)]))
                for k in lags
            ]
        )
        total += score / norm
        quality.append(float(score.max() / norm))
    best = int(np.argmax(total))
    return lags[best] * step, (max(quality) if quality else 0.0)


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


def low_band(x: np.ndarray) -> np.ndarray:
    """Басы без сдвига фазы: x − low_band(x) — ровно всё остальное."""
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, CROSSOVER, btype="lowpass", fs=SR, output="sos")
    return sosfiltfilt(sos, x, axis=0).astype("float32")


def ramp(n: int) -> np.ndarray:
    import numpy as np

    return np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]


def step_at(n: int, at: int, width: int) -> np.ndarray:
    """0 до (at − width), плавно (синус) до 1 к отсчёту at, дальше 1."""
    import numpy as np

    i = np.arange(n, dtype=np.float32)
    x = np.clip((i - (at - width)) / max(1, width), 0, 1)
    return (0.5 - 0.5 * np.cos(np.pi * x))[:, None].astype(np.float32)


def limit(x: np.ndarray, start: int, stop: int, alone: np.ndarray | None = None) -> np.ndarray:
    """Прижать пики выше CEILING в [start, stop) плавным усилением; вне отрезка
    звук не меняется. Минимум по окну 100 мс, затем среднее по 50 мс: среднее
    лежит внутри окна минимума, поэтому кривая нигде не выше нужной.

    alone — пик каждого трека порознь по отсчётам: где один трек сам у
    предела (громкий трек без поправки), прижимать нечего — так звучит и сам
    трек. Иначе ограничитель срабатывал у края перехода, где звучит один A, и
    усиление скакало на отсчёте — щелчок (аудит 23.11, пара 49)."""
    import numpy as np
    from scipy.ndimage import maximum_filter1d, uniform_filter1d

    seg = x[start:stop]
    if len(seg) == 0:
        return x
    peak = np.abs(seg).max(axis=1)
    ceiling = CEILING if alone is None else np.maximum(CEILING, alone[: len(seg)])
    if (peak <= ceiling).all():
        return x
    need = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    floor = -maximum_filter1d(-need, size=int(0.1 * SR) + 1)
    gain = np.minimum(uniform_filter1d(floor, size=int(0.05 * SR)), need)
    out = x.copy()
    out[start:stop] = seg * gain[:, None].astype(np.float32)
    return out


def level_match(mixed: np.ndarray, before: float, after: float) -> np.ndarray:
    """Сумма двух треков не громче прямой (в LU) между громкостью A до
    перехода и B после: только вниз, плавно, у краёв — без поправки (по ним
    связка стыкуется с самими треками). Возвращает усиление по отсчётам (n, 1)."""
    import numpy as np
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    n = len(mixed)
    if n < int(1.0 * SR):
        return np.ones((n, 1), dtype=np.float32)
    level = lufs(power_curve(mixed, 1.0))  # кадр i — окно с i·HOP длиной 1 с
    t = (np.arange(len(level)) * HOP + 0.5) / (n / SR)  # середина окна, доля перехода
    target = np.minimum(before + (after - before) * np.clip(t, 0, 1), max(before, after))
    cut = np.clip(level - target - LEVEL_TOLERANCE, 0, LEVEL_CUT_MAX)
    if cut.max() <= 0:
        return np.ones((n, 1), dtype=np.float32)
    # Кадры → отсчёты: поправка по окну держится всё окно (минимум усиления), потом сглаживается.
    frames = np.repeat(cut, int(HOP * SR))
    frames = np.concatenate(
        [frames, np.full(max(0, n - len(frames)), frames[-1] if len(frames) else 0)]
    )[:n]
    frames = -minimum_filter1d(-frames, size=int(1.0 * SR))  # максимум поправки по окну 1 с
    frames = uniform_filter1d(frames, size=int(0.5 * SR))
    edge = int(0.4 * SR)
    win = np.ones(n, dtype=np.float64)
    if n > 2 * edge:
        r = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, edge))
        win[:edge], win[-edge:] = r, r[::-1]
    return (10 ** (-frames * win / 20))[:, None].astype(np.float32)


def hold_a(
    a_full: np.ndarray,
    part_b: np.ndarray,
    planned: np.ndarray,
    before: float,
    after: float,
    end_n: int,
) -> np.ndarray:
    """Усиление верха A: не ниже плана и не ниже того, что нужно, чтобы с B
    вместе держать прямую громкости от A к B. Раз начав уходить, A не
    возвращается; за последние end_n отсчётов уходит до нуля в любом случае."""
    import numpy as np
    from scipy.ndimage import uniform_filter1d

    n = len(a_full)
    if n < SR // 2:
        return planned
    pa, pb = power_curve(a_full, 0.4), power_curve(part_b, 0.4)
    m = min(len(pa), len(pb))
    t = (np.arange(m) * HOP + 0.2) / (n / SR)
    target = 10 ** ((before + (after - before) * np.clip(t, 0, 1) + 0.691) / 10)
    need = np.sqrt(np.clip((target - pb[:m]) / np.maximum(pa[:m], 1e-12), 0, 1))
    need = np.minimum.accumulate(need)
    g = np.repeat(need, int(HOP * SR))
    g = np.concatenate([np.full(int(0.2 * SR), g[0] if len(g) else 1.0), g])  # окно — по середине
    g = np.concatenate([g, np.full(max(0, n - len(g)), g[-1] if len(g) else 0.0)])[:n]
    g = uniform_filter1d(g, size=int(0.2 * SR))
    env = np.ones(n)
    end_n = max(1, min(end_n, n))
    env[n - end_n :] = 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, end_n))
    return np.maximum(planned, (g * env)[:, None]).astype(np.float32)


def render(job: dict, beats_hook=None, parts: dict | None = None) -> tuple[np.ndarray, dict]:
    """Свести связку. beats_hook(x, prior) -> (tempo, beats) — для тестов;
    в parts (если дан) кладутся части A и B перехода порознь — для проверок."""
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
    b_body = b[b_start_i:]

    # Где A начинает затихать сам и где B входит в силу (кривые по 2 с: в
    # трэпе громкость дышит по тактам, и пауза в такт — ещё не затухание).
    level_a = lufs(power_curve(a[:a_end_i], 2.0))
    a_stop = fade_point(level_a, body_level(level_a), a_end, 2.0) if len(level_a) else a_end
    a_stop_i = int(round(a_stop * SR))
    level_b = lufs(power_curve(b_body, 2.0))
    rise_b = rise_point(level_b, body_level(level_b), 2.0) if len(level_b) else 0.0

    look_a = max(0, a_stop_i - int(30 * SR))
    tempo_a, beats_a = beat_finder(a[look_a:a_stop_i], job.get("tempo_a"))
    beats_a = np.asarray(beats_a, dtype=float) + look_a / SR  # секунды окна A
    tempo_b, beats_b = beat_finder(b_body[: int(30 * SR)], job.get("tempo_b"))
    beats_b = np.asarray(beats_b, dtype=float)
    if beats_hook is None:
        beats_b = real_beats(b_body[: int(30 * SR)], beats_b)
    # Без ударов librosa даёт темп 0. Подгонять тогда нечего (ударов мало —
    # растяжки не будет), а длины в «ударах» считаются от условных 120.
    tempo_a = tempo_a if tempo_a > 0 else 120.0
    tempo_b = tempo_b if tempo_b > 0 else 120.0
    period_a, phase_a, on_a = fit_grid(beats_a, tempo_a)
    period_b, phase_b, on_b = fit_grid(beats_b, tempo_b)
    grid_a = len(beats_a) > 20 and on_a >= GRID_OK
    grid_b = len(beats_b) > 4 and on_b >= GRID_OK
    # Растягивать можно, когда удары вообще есть (как до версии 3); ровной
    # сетке верится только для точного темпа — фазу ставит сверка по звуку.
    if grid_a:
        tempo_a = 60.0 / period_a
    if grid_b:
        tempo_b = 60.0 / period_b
    if len(beats_b):
        first_beat_b = float(beats_b[0])
        if grid_b:  # первый удар — на сетке (найденный дрожит)
            k = math.ceil((first_beat_b - 0.07 * period_b - phase_b) / period_b)
            first_beat_b = max(0.0, phase_b + k * period_b)
    else:
        first_beat_b = 0.0
    decision = choose(
        tempo_a,
        tempo_b,
        len(beats_a) > 20,
        len(beats_b) > 4,
        first_beat_b,
        voice_b,
        rise_b,
        steady=grid_a and grid_b,
    )
    beat = decision["beat"]
    lead_i = int(round(LEAD * SR))

    def a_beat(t: float) -> float:
        """Удар A не позже t (секунды окна A): на сетке, если ей верить."""
        if grid_a:
            return phase_a + math.floor((t - phase_a) / period_a + 1e-9) * period_a
        k = int(np.searchsorted(beats_a, t, side="right")) - 1
        return float(beats_a[k]) if k >= 0 else t

    # Части по полосам: низ без сдвига фазы, верх — остаток (вместе — сам звук).
    a_low = low_band(a)

    case = decision["case"]
    seam = None
    b_stretched = False
    if case == "intro":
        fade = decision["fade"]
        end = a_beat(a_stop) if grid_a else a_stop
        if end - fade < LEAD + 1.0:
            end = a_stop
        n = int(round(fade * SR))
        mix_i = max(lead_i, int(round(end * SR)) - n)
        # Вступление длиннее наложения — B начинается не с начала, а так, чтобы
        # наложение кончилось к его барабану (или голосу): иначе после ухода A
        # долго звучало бы одно тихое вступление.
        until = min(first_beat_b, voice_b) if voice_b is not None else first_beat_b
        skip = max(0, int(round((until - 0.25 - fade) * SR)))
        a_mix, a_mix_low = a[mix_i : mix_i + n], a_low[mix_i : mix_i + n]
        b_mix = b_body[skip : skip + n]
        n = min(len(a_mix), len(b_mix))
        a_hi, a_lo = a_mix[:n] - a_mix_low[:n], a_mix_low[:n]
        t = ramp(n)
        gain_a = np.cos(t * np.pi / 2)
        gain_b = np.sin(np.clip(t * 4, 0, 1) * np.pi / 2)
        part_b = b_mix[:n] * gain_b
        # Басы A — с его верхом (под вступлением B своих басов нет), но уходят
        # за последнюю четверть: барабан B входит уже на свои.
        low_shape = 1 - step_at(n, n, n // 4)
        gain_a_low = None
        end_n = int(round(beat * SR / 2))
        b_used = skip + n  # отсчётов B (с начала звука) уже в переходе
    elif case == "beatmatch":
        factor = decision["factor"]
        nb = decision["beats"]
        while True:
            L = nb * beat
            end_a = a_beat(a_stop)
            start_a = end_a - L
            if start_a * SR - lead_i >= 0 or nb <= 4:
                break
            nb //= 2
        start_a = max(start_a, LEAD + 0.5)
        sa = int(round(start_a * SR))
        nL = int(round(L * SR))
        head = b_body[: int((first_beat_b + L / factor + 3.0) * SR)]

        def place(f: float) -> tuple[np.ndarray, float, float, tuple]:
            """Растянуть начало B и найти его первый удар на ударе A."""
            bh = stretch(head, f)
            fbt = first_beat_b * f
            fi = int(fbt * SR)
            lag, quality = beat_lag(a[sa : sa + nL], bh[fi : fi + nL], window=30.0 / tempo_a)
            fbt = max(0.0, fbt - lag)
            # Уплывает ли B к концу: та же сверка по первой и второй половине.
            fi = int(round(fbt * SR))
            half = nL // 2
            l1, q1 = beat_lag(a[sa : sa + half], bh[fi : fi + half], window=15.0 / tempo_a)
            l2, q2 = beat_lag(
                a[sa + half : sa + nL], bh[fi + half : fi + nL], window=15.0 / tempo_a
            )
            return bh, fbt, quality, (l1, q1, l2, q2)

        b_head, first_b, quality, (l1, q1, l2, q2) = place(factor)
        # l2 > l1: атаки B к концу всё раньше — B быстрее A, растянуть сильнее.
        drift = l2 - l1
        if nb >= 8 and min(q1, q2) > 0.25 and abs(drift) > 0.004:
            better = factor * (1 + drift / (nL / 2 / SR))
            if abs(better * tempo_a / decision["tempo_b"] - 1) <= MAX_STRETCH:
                b_head2, first_b2, quality2, (m1, r1, m2, r2) = place(better)
                if abs(m2 - m1) < abs(drift):
                    factor, b_head, first_b = better, b_head2, first_b2
        fb_i = int(round(first_b * SR))
        pre = min(fb_i, sa - lead_i)  # начало B до его первого удара
        mix_i = sa - pre
        a_mix, a_mix_low = a[sa - pre : sa + nL], a_low[sa - pre : sa + nL]
        b_mix = b_head[fb_i - pre : fb_i + nL]
        n = min(len(a_mix), len(b_mix))
        a_mix, a_mix_low, b_mix = a_mix[:n], a_mix_low[:n], b_mix[:n]
        b_mix_low = low_band(b_head[: fb_i + nL + int(0.5 * SR)])[fb_i - pre : fb_i - pre + n]
        t = ramp(n)
        # B поднимается за первую половину, A уходит за вторую.
        gain_b = np.sin(np.clip(t * 2, 0, 1) * np.pi / 2)
        gain_a = np.cos(np.clip(t * 2 - 1, 0, 1) * np.pi / 2)
        # Басы — разом на ударе (четверть удара на смену): там, где B входит в
        # силу, но не раньше середины и не позже последнего удара наложения.
        rise_beat = round((rise_b - first_beat_b) * factor / beat)
        swap_beat = min(nb - 1, max(nb // 2, rise_beat))
        swap_at = pre + int(round(swap_beat * beat * SR))
        bass_b = step_at(n, swap_at, int(beat * SR / 4))
        a_hi, a_lo = a_mix - a_mix_low, a_mix_low
        gain_a_low = gain_a * (1 - bass_b)
        part_b = (b_mix - b_mix_low) * gain_b + b_mix_low * gain_b * bass_b
        end_n = n // 4
        # Сколько исходника B сыграно растянутым: дальше он идёт в своём темпе.
        b_used = int(round((fb_i - pre + n) / factor))
        b_stretched = True
        decision["factor"] = factor
        decision["beats"] = nb
        decision["lock"] = round(quality, 3)
        # Растянутый и исходный звук отсчёт в отсчёт не совпадают: шов в 20 мс.
        seam = b_head[fb_i - pre + n : fb_i - pre + n + int(0.02 * SR)]
    else:
        together = decision["together"]
        # Первый удар B — на удар A: звук B до удара (затакт) идёт раньше.
        # Длиннее CUT_EXTRA_MAX ударов (долгое начало без барабана при раннем
        # голосе) — B входит первым звуком: срезать начало B значило бы
        # срезать первые слова.
        factor = decision["factor"]
        lead_beat = first_beat_b if len(beats_b) else 0.0
        if lead_beat > (CUT_EXTRA_MAX + 4 - together) * beat:
            lead_beat = 0.0
        end_a = a_beat(a_stop)  # дальше A не звучит
        entry = end_a - together * beat  # первый удар B (секунды окна A)
        # Начало B — растянутое под темп A, если темпы близко (factor ≠ 1).
        span = lead_beat + (together + 1) * beat + 2.0
        if factor != 1.0:
            src = stretch(b_body[: int((span / factor + 2.0) * SR)], factor)
            lead_beat *= factor
        else:
            src = b_body
        # Первый удар B — на удар A по самому звуку: найденные удары дрожат.
        w = int(round(min(2, together + 1) * beat * SR))
        ei, fi = int(round(entry * SR)), int(round(lead_beat * SR))
        # Только в одном темпе: при разных темпах удары и не должны совпадать,
        # и сверка цеплялась бы за случайное (тест с затактом при 150/120).
        if factor != 1.0 and lead_beat > 0 and ei + w <= len(a) and fi + w <= len(src):
            lag, quality = beat_lag(a[ei : ei + w], src[fi : fi + w], window=15.0 / tempo_a)
            if quality > 0.2 and lead_beat - lag >= 0:
                lead_beat -= lag
            decision["lock"] = round(quality, 3)
        b_first = entry - lead_beat  # первый звук B
        # Хотя бы удар чистого A до первого удара B: смена басов (четверть удара)
        # иначе начиналась бы раньше перехода — басы A пропадали бы на первом
        # же отсчёте (щелчок, пара 49).
        start_a = a_beat(min(b_first, entry - beat, end_a - 4 * beat) + 1e-6)
        if start_a * SR < lead_i + 1:
            raise ValueError("A слишком короткий для связки")
        mix_i = int(round(start_a * SR))
        n = int(round(end_a * SR)) - mix_i
        a_mix, a_mix_low = a[mix_i : mix_i + n], a_low[mix_i : mix_i + n]
        n = len(a_mix)
        entry_i = int(round(entry * SR)) - mix_i
        b_at = int(round(b_first * SR)) - mix_i
        skip = max(0, -b_at)
        b_at = max(0, b_at)
        b_mix = src[skip : skip + n - b_at]
        b_mix_low = low_band(src[: skip + n - b_at + int(0.5 * SR)])[skip : skip + len(b_mix)]
        # Верх A: до первого звука B — как есть, потом уходит. Темпы разные —
        # за полудоли после первого удара B (дальше удары разошлись бы); в
        # одном темпе — к концу A, но не дольше двух ударов после удара B.
        f0 = min(b_at, max(0, entry_i - int(round(beat * SR))))
        stop = entry_i + int(round(beat * SR * (0.5 if factor == 1.0 else 2.0)))
        stop = min(n, max(stop, f0 + int(0.05 * SR)))
        curve = np.clip((np.arange(n, dtype=np.float32)[:, None] - f0) / max(1, stop - f0), 0, 1)
        gain_a = np.cos(curve * np.pi / 2)
        # Басы меняются на первом ударе B: у A уходят, у B входят — за четверть удара.
        bass_b = step_at(n, entry_i, int(beat * SR / 4))
        a_hi, a_lo = a_mix - a_mix_low, a_mix_low
        gain_a_low = gain_a * (1 - bass_b)
        end_n = int(round(beat * SR / 4))
        # B входит: при голосе сразу — на ударе (миллисекунды); в одном темпе —
        # за треть стыка; темпы разные — за четверть удара (A уже уходит, ждать
        # нечего: иначе между ними провал).
        if together == 1:
            rise_n = max(1, len(b_mix) // 40)
        elif factor != 1.0:
            rise_n = max(1, len(b_mix) // 3)
        else:
            rise_n = max(1, int(beat * SR / 4))
        rise = np.sin(
            np.clip(np.arange(len(b_mix), dtype=np.float32)[:, None] / rise_n, 0, 1) * np.pi / 2
        )
        bass_in = bass_b[b_at : b_at + len(b_mix)]
        part_b = np.zeros_like(a_hi)
        part_b[b_at : b_at + len(b_mix)] = ((b_mix - b_mix_low) + b_mix_low * bass_in) * rise
        used = skip + len(b_mix)  # отсчётов src
        if factor != 1.0:
            b_used = int(round(used / factor))
            b_stretched = True
            seam = src[used : used + int(0.02 * SR)]
        else:
            b_used = used

    if mix_i < lead_i:
        raise ValueError("A слишком короткий для связки")
    # Громкость суммы: к прямой между A до перехода и B после.
    before = float(lufs(power_curve(a[mix_i - int(3 * SR) : mix_i], 3.0))[0])
    after_b = b_body[b_used : b_used + int(3 * SR)]
    after = float(lufs(power_curve(after_b, 3.0))[0]) if len(after_b) > SR else before
    # Верх A уходит не быстрее, чем B набирает громкость: тихое начало B
    # само не держит громкость, и переход по плану провалился бы.
    gain_a = hold_a(a_hi + a_lo, part_b, gain_a, before, after, end_n)
    if gain_a_low is None:  # «intro»: басы A — с его верхом
        gain_a_low = gain_a * low_shape
    part_a = a_hi * gain_a + a_lo * gain_a_low
    level = level_match((part_a + part_b).astype(np.float32), before, after)
    part_a, part_b = (part_a * level).astype(np.float32), (part_b * level).astype(np.float32)
    mixed = part_a + part_b
    level_cut = float(-20 * np.log10(level.min())) if len(level) else 0.0
    if parts is not None:
        parts["a"], parts["b"] = part_a, part_b

    lead = a[mix_i - lead_i : mix_i]
    tail = b_body[b_used : b_used + int(round(TAIL * SR))].copy()
    if len(tail) < int(TAIL * SR * 0.5):
        raise ValueError("B слишком короткий для связки")
    if seam is not None and len(seam):
        r = ramp(len(seam))
        tail[: len(seam)] = seam * (1 - r) + tail[: len(seam)] * r
    bridge = np.concatenate([lead, mixed, tail]).astype(np.float32)
    alone = np.maximum(np.abs(part_a).max(axis=1), np.abs(part_b).max(axis=1))
    bridge = limit(bridge, lead_i, lead_i + len(mixed), alone)
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
        "a_dropped": round(a_end - a_stop, 2),
        "rise_b": round(rise_b, 2),
        "level_cut": round(level_cut, 2),
        # Почему так: доля ударов на сетке у A и B (верить ей — от GRID_OK).
        "grid": [round(on_a, 2), round(on_b, 2)],
        "reason": decision.get("reason"),
    }
    if "lock" in decision:
        plan["lock"] = decision["lock"]
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
