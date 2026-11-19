"""Сверка двух записей одной песни: где у одной заглушены слова.

Студийные записи русского рэпа на YouTube Music с осени 2025 приглушают слова
про наркотики (мат оставлен), а пометка «18+» и videoId те же (разбор
19.11.2026). Тихое место само ничего не доказывает — у клипа своё сведение тоже
где-то тише, — поэтому ``judge`` верит ему, только когда на том же месте строка
синхронного текста со словом из ``censor_words``.

Раньше это жило в scripts/studio_audit.py; теперь им пользуется и служба
(подмена клипа студийной при добавлении), а скрипты берут отсюда.
numpy/scipy — ленивые импорты: без них ``available()`` = False, и служба
качает как раньше, с пометкой «не сверено».
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from .censor_words import drug_hits, hits

if TYPE_CHECKING:
    import numpy as np

RATE = 16000
STEP = RATE // 4  # a quarter of a second
LAG_RATE = 8000  # find_lag works on every second sample
LAG_EXCERPT_SECONDS = 120
DIFFERENT = 0.6  # correlation under which a window does not match
AUDIBLE = 0.1  # of the track's own RMS: quieter windows are not judged
# Padding before or after the song: about -40 dB under it. A clip's quiet
# outro (wind, a room, «КТО ТЕБЕ ЭТО СКАЗАЛ»: 5 s at some -20 dB) is louder.
SILENCE = 0.01
MUTED_DB = 6.0
WHOLE_MUTE_DB = 10.0
SPOT_GAP = 0.5
EDGE_ZONE = 3.0  # a dip this close to the start or end of the song is not a muted word
LONGEST_MUTE = 3.0
LAG_LIMIT_SECONDS = 300

# Правило владельца (промт 19.11.2026): общее сходство ниже — другое сведение,
# автоматика не решает. Строки LRC точны до ~1 с, и слово бывает на стыке строк.
SAME_MIX = 0.75
LINE_SLACK = 1.0
LAST_LINE_SECONDS = 8.0
LONGER_CLIP = 1.6  # как WRONG_RATIO аудита: длиннее — у клипа сцены


def available() -> bool:
    return find_spec("numpy") is not None and find_spec("scipy") is not None


def decode_mono(path: Path, rate: int = RATE) -> np.ndarray:
    import numpy as np

    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
         "-f", "f32le", "-"],
        capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
    ).stdout  # fmt: skip
    return np.frombuffer(out, np.float32)


def find_lag(a: np.ndarray, b: np.ndarray, limit: int, rate: int = LAG_RATE) -> int:
    """Offset of b against a, in samples, within +-limit, from the openings only.

    A full-length FFT of a 40-minute mix needs gigabytes; the offset between two
    copies of one recording is a matter of seconds and shows in the first two
    minutes as well as anywhere.
    """
    import numpy as np

    n_excerpt = LAG_EXCERPT_SECONDS * rate
    x = a[: n_excerpt + limit].astype(np.float64)
    y = b[: n_excerpt + limit].astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(y, n)), n)
    lags = np.concatenate([np.arange(0, limit + 1), np.arange(-limit, 0)])
    values = np.concatenate([c[: limit + 1], c[n - limit :]])
    return int(lags[int(np.argmax(values))])


def compare(old: Path, new: Path, decode=None) -> dict:
    """How the file differs from the studio recording, aligned.

    ``head``/``tail``: seconds the file has before and after the song;
    ``spots``: [start, end, kind] in the file's own time, kind being "file"
    (muted in the file), "studio" (muted in the studio recording) or "other".
    """
    import numpy as np
    from scipy.signal import butter, sosfiltfilt

    decode = decode or (lambda path: decode_mono(path, RATE))
    a, b = decode(old), decode(new)
    result: dict = {
        "file_seconds": round(len(a) / RATE, 1),
        "studio_seconds": round(len(b) / RATE, 1),
    }
    if not len(a) or not len(b):
        return {**result, "overall": 0.0, "head": 0.0, "tail": 0.0, "lag": 0.0, "spots": []}
    half = 2  # find_lag works at 8 kHz
    limit = int(min(LAG_LIMIT_SECONDS, abs(len(a) - len(b)) / RATE + 5) * LAG_RATE)
    lag = find_lag(a[::half], b[::half], limit) * half
    x, y = (a[lag:], b) if lag >= 0 else (a, b[-lag:])
    m = min(len(x), len(y))
    x, y = x[:m].astype(np.float64), y[:m].astype(np.float64)
    voice = butter(4, [300, 3400], btype="band", fs=RATE, output="sos")
    bass = butter(4, 150, btype="low", fs=RATE, output="sos")
    xv, yv = sosfiltfilt(voice, x), sosfiltfilt(voice, y)
    xb, yb = sosfiltfilt(bass, x), sosfiltfilt(bass, y)

    def rms(s: np.ndarray) -> float:
        return float(np.sqrt(np.mean(s * s))) + 1e-9

    def db(p: np.ndarray, q: np.ndarray) -> float:
        return 20 * float(np.log10(rms(p) / rms(q)))

    def corr(p: np.ndarray, q: np.ndarray) -> float:
        d = float(np.sqrt(np.dot(p, p) * np.dot(q, q)))
        return float(np.dot(p, q)) / d if d else 0.0

    level = max(rms(x), rms(y))
    voice_level = max(rms(xv), rms(yv))
    windows = []
    for start in range(0, m - STEP + 1, STEP):
        cut = slice(start, start + STEP)
        p, q = x[cut], y[cut]
        if max(rms(p), rms(q)) < AUDIBLE * level:
            continue
        voiced = max(rms(xv[cut]), rms(yv[cut])) >= AUDIBLE * voice_level
        windows.append(
            (
                start,
                corr(p, q),
                corr(xv[cut], yv[cut]) if voiced else 1.0,
                db(p, q),
                db(xv[cut], yv[cut]) if voiced else None,
                db(xb[cut], yb[cut]),
            )
        )
    # Two copies of one song are rarely equally loud: the usual difference is
    # taken out, so that only a place that drops against it counts.
    usual = float(np.median([w[3] for w in windows])) if windows else 0.0
    voiced_db = [w[4] for w in windows if w[4] is not None]
    usual_voice = float(np.median(voiced_db)) if voiced_db else 0.0
    marks = []
    for start, whole_corr, voice_corr, whole, in_voice, in_bass in windows:
        whole -= usual
        in_bass -= usual
        in_voice = in_voice - usual_voice if in_voice is not None else 0.0
        # Only where the sound itself differs: a level alone moves by several
        # dB between two masters of one song (a premiere against the release,
        # «Дом Периньон»). Under a loud beat a muted word leaves the whole mix
        # almost as it was, so the voice band is asked on its own too: the
        # instruments left in it change its shape.
        if whole_corr >= DIFFERENT and voice_corr >= DIFFERENT:
            continue
        if whole <= -WHOLE_MUTE_DB or (in_voice <= -MUTED_DB and in_voice - in_bass <= -4):
            kind = "file"
        elif whole >= WHOLE_MUTE_DB or (in_voice >= MUTED_DB and in_voice - in_bass >= 4):
            kind = "studio"
        else:
            kind = "other"
        marks.append((start / RATE, kind))

    spots: list[list] = []
    for at, kind in marks:
        file_at = at + max(lag, 0) / RATE
        if spots and file_at - spots[-1][1] <= SPOT_GAP and spots[-1][2] == kind:
            spots[-1][1] = round(file_at + STEP / RATE, 2)
        else:
            spots.append([round(file_at, 2), round(file_at + STEP / RATE, 2), kind])
    # A muted word is a short dip inside the song. At the very start or end it
    # is another intro or fade («Альтуха»: the upload's first two seconds), and
    # a long one is another mix, not a bleep.
    song_end = max(lag, 0) / RATE + m / RATE
    for spot in spots:
        near_edge = spot[0] < max(lag, 0) / RATE + EDGE_ZONE or spot[1] > song_end - EDGE_ZONE
        if spot[2] != "other" and (near_edge or spot[1] - spot[0] > LONGEST_MUTE):
            spot[2] = "other"
    head = max(lag, 0) / RATE
    tail = (len(a) - max(lag, 0)) / RATE - len(b) / RATE + max(-lag, 0) / RATE
    # Silence before or after the song is padding, not the clip's own sound.
    if head > 0 and rms(a[: max(lag, 0)].astype(np.float64)) < SILENCE * level:
        head = 0.0
    end = max(lag, 0) + len(b) - max(-lag, 0)
    if tail > 0 and rms(a[end:].astype(np.float64)) < SILENCE * level:
        tail = 0.0
    return {
        **result,
        "overall": round(corr(x, y), 3),
        "head": round(head, 1),
        "tail": round(tail, 1),
        "spots": spots,
        # Сдвиг студийной против файла, с: место файла ``t`` — это ``t - lag``
        # студийной (по ней размечен синхронный текст LRCLIB).
        "lag": round(lag / RATE, 3),
    }


@dataclass(frozen=True)
class Verdict:
    take_studio: bool
    why: str  # для карточки задачи, по-русски


def drug_lines(synced: list[dict]) -> list[dict]:
    """Строки синхронного текста со словами про наркотики."""
    return [line for line in synced if drug_hits(line.get("line") or "")]


def judge(compared: dict, synced: list[dict]) -> Verdict:
    """Брать ли студийную вместо присланной (``compare(file=присланная, new=студийная)``).

    Правило владельца: студийная тише на месте строки со словом, которое
    глушат (наркотики; мат тоже бывает — разбор 19.11.2026), при общем сходстве
    не ниже SAME_MIX — она подцензурная, берётся присланная. Сходство ниже —
    другое сведение, автоматика не решает: присланная, «проверь». Длинное или
    краевое расхождение («other»: фраза целиком, эффект вместо тишины) на такой
    строке — тоже «проверь». Тихое место без такого слова — мало: студийная.
    """
    if compared.get("overall", 0.0) < SAME_MIX:
        file_len, studio_len = compared.get("file_seconds", 0), compared.get("studio_seconds", 0)
        if studio_len and file_len / studio_len > LONGER_CLIP:
            # Клип со сценами (7:45 против 2:41) — его звук не песня; сдвиг
            # мог не найтись из-за длинного вступления. Как раньше: студийная.
            return Verdict(
                True, "Взята студийная версия вместо клипа (не сверена: клип намного длиннее)"
            )
        return Verdict(
            False, "Оставлен звук клипа: другое сведение, сверить со студийной нельзя — проверь"
        )
    lag = compared.get("lag", 0.0)
    # Конец последней строки — не «до конца песни»: иначе любое тихое место в
    # концовке легло бы на её слово.
    following = synced[1:] + [{"at": synced[-1]["at"] + LAST_LINE_SECONDS}] if synced else []
    ends = [(line["at"], nxt["at"]) for line, nxt in zip(synced, following, strict=True)]

    studio_len = compared.get("studio_seconds") or 0.0

    def word_at(start: float, end: float, find) -> str | None:
        a, b = start - lag - LINE_SLACK, end - lag + LINE_SLACK
        for line, (at, until) in zip(synced, ends, strict=True):
            words = find(line.get("line") or "")
            if words and at < b and until > a:
                return words[0]
        return None

    for start, end, kind in compared.get("spots", []):
        if kind == "studio":
            word = word_at(start, end, hits)
        elif kind == "other":
            # «other» бывает и своим сведением клипа, и краем песни, а мат в
            # рэпе почти в каждой строке: здесь — только слова про наркотики
            # и не у краёв.
            at_edge = start - lag < EDGE_ZONE or (studio_len and end - lag > studio_len - EDGE_ZONE)
            word = None if at_edge else word_at(start, end, drug_hits)
        else:
            continue
        if word is None:
            continue
        minute, second = divmod(int(max(start - lag, 0)), 60)
        where = f"«{word}» ({minute}:{second:02d})"
        if kind == "studio":
            return Verdict(False, f"Оставлен звук клипа: в студийной заглушено {where}")
        return Verdict(False, f"Оставлен звук клипа: студийная отличается на {where} — проверь")
    return Verdict(
        True, "Взята студийная версия вместо клипа — сверена: тихих мест на строках со словами нет"
    )


def synced_lyrics(artist: str, title: str, duration: float | None) -> list[dict] | None:
    """Синхронный текст песни из LRCLIB по длине *студийной* записи (по ней он
    размечен). None — каталог не ответил; [] — синхронного текста нет."""
    from . import lyrics

    data = lyrics._ask(artist, title, "", duration)
    if data == {}:
        data = lyrics._search(artist, title, duration)
    if data is None:
        return None
    return lyrics.parse_synced(data.get("syncedLyrics") or "")
