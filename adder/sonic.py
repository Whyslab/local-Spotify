"""Умное перемешивание по звучанию: «якорь + облака».

Старое умное (``shuffle.build_queue``) шло жадно от трека к треку по темпу и
громкости. По журналу темп и громкость на пропуски не влияли, а жадная
прогулка через десять шагов уходила далеко от того, с чего начали.

Здесь у каждого трека есть звучание — эмбеддинг нейросети Essentia
Discogs-EffNet и оценки настроения (``scripts/embed_audio.py``, таблица
``sonic``). Из них:

1. **Облака.** Треки фонотеки делятся на ~20 групп похожих по звуку
   (сферический k-means по эмбеддингу), отдельно для русских и
   нерусских песен — так решил владелец 19.11: русское и иностранное в одном
   облаке не смешиваются, язык меняется только на стыке блоков.
2. **Блоки.** Очередь идёт блоками по 4–6 треков из одного облака. Первый
   трек блока — якорь; остальные выбираются по близости к якорю, а не к
   предыдущему треку: так нет дрейфа.
3. **Переходы.** Следующий блок — в облако рядом с якорем сессии (оно само
   или ближайшие центры), с поправкой на то, как такие переходы пропускали
   раньше. Раз в 3–4 блока якорем сессии становится текущее облако.
4. **Ограничения.** Артист не чаще раза в 6 треков, альбом — раз в 9;
   совместные треки («A • B») сторожат обоих.
5. **Волна энергии.** Мягкая синусоида возбуждения с периодом около
   14 треков (~45 минут): подъём, плато, спад — не случайные скачки.
6. **Пропуски учат облака и переходы**, а не отдельные треки (тех
   слишком мало): ранний пропуск (< 30 с) — неудача перехода из облака
   предыдущего трека и самого облака. Выбор — сэмплированием Томпсона из
   Beta(дослушано + 1, пропущено + 1): то, о чём мало знаем, тоже пробуется.
7. **Темп** — слабая поправка в расстоянии, без жёсткого окна.

Расстояние между треком и якорем:
``0,55·звук + 0,20·граф артистов + 0,10·|Δ энергии| + 0,10·[язык] + 0,05·темп``
(веса из исследования 19.11; звук — косинусное расстояние, делённое на его
медиану по фонотеке, остальное — в долях от 0 до 1).

Модуль чистый: ``Space`` строится из строк, очередь — из ``shuffle.Track``.
База и сеть — в ``load_space`` и ``artist_graph``.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np

from . import shuffle

logger = logging.getLogger(__name__)

CLOUDS = 20
# Облако меньше этого не выделяется: k для группы — по её доле в фонотеке,
# но не больше чем по одному облаку на столько треков.
MIN_CLOUD = 25
KMEANS_ITERATIONS = 30

BLOCK_MIN, BLOCK_MAX = 4, 6
# Сколько ближайших к якорю кандидатов разыгрывается по весам. Не один —
# иначе от того же якоря всегда та же очередь.
# 16, не 8: с 8 за 30 очередей играло 730 разных треков из 1513, с 16 — 773
# (у старого умного ~950); шаг по звуку почти тот же (медиана 0,199 против 0,186).
TOP_CANDIDATES = 16
# Соседние облака: столько ближайших центров рассматривается для перехода.
NEIGHBOUR_CLOUDS = 4
# Только что пройденные облака: их вес при выборе следующего умножается на это.
# Якорь сессии: 3–4 блока следующее облако берётся рядом с ним, а не рядом с
# последним — иначе очередь блуждает и через 5 блоков где угодно (ТЗ:
# «раз в 3–4 блока — новый якорь»). Потом якорем становится текущее облако.
SESSION_BLOCKS = (3, 4)
RECENT_CLOUD_PENALTY = 0.15
RECENT_CLOUDS = 3
# Смена языка на стыке блоков разрешена, но реже, чем шаг внутри языка.
LANGUAGE_SWITCH = 0.5

ARTIST_GAP = 6  # артист — не чаще раза в 6 треков (5 других между)
ALBUM_GAP = 9  # альбом — не чаще раза в 9

WEIGHTS = {"sound": 0.55, "artist": 0.20, "energy": 0.10, "language": 0.10, "tempo": 0.05}
# Насколько резко падает вес с расстоянием: exp(-d / TEMPERATURE).
TEMPERATURE = 0.12

WAVE_PERIOD = 14  # треков на полный круг волны
WAVE_AMPLITUDE = 0.8  # в стандартных отклонениях энергии
WAVE_WIDTH = 1.2  # насколько мягко: exp(-((e - цель) / WIDTH)²)

EARLY_SKIP = 30.0  # секунд — ранний пропуск
SESSION_GAP = 30 * 60  # дольше этой паузы — новая сессия

# Меньше этой доли треков набора со звучанием — новое умное не строится,
# работает старое (пока ночной расчёт не прошёл фонотеку).
MIN_COVERAGE = 0.8

RU = "ru"
OTHER = "other"


# Буквы кириллического блока U+0400–U+04FF; U+0482–U+0489 — знак тысяч и
# диакритика, не буквы (isalpha() у них ложно).
_CYRILLIC_LETTER = re.compile("[\u0400-\u0481\u048a-\u04ff]")


def is_cyrillic_text(text: str) -> float:
    """Доля кириллицы среди букв строки (0, если букв нет).

    Счёт — встроенными map и re, а не перебором символов в Python: тексты всей
    фонотеки — 2,4 млн знаков, и перебор стоил 2 с на каждую пересборку
    (профиль 24.11.2026)."""
    letters = sum(map(str.isalpha, text))
    if not letters:
        return 0.0
    return len(_CYRILLIC_LETTER.findall(text)) / letters


def text_verdict(text: str) -> str | None:
    """Язык по тексту песни; None — текста мало (меньше 40 букв) или нет."""
    if text and sum(map(str.isalpha, text)) >= 40:
        return RU if is_cyrillic_text(text) >= 0.3 else OTHER
    return None


_BRACKETS = re.compile(r"[(\[][^)\]]*[)\]]")


def _bare_title(title: str) -> str:
    """Название без скобок: «Benz Truck (гелик)» — «Benz Truck». В скобках —
    чужие пометки (перевод, «prod. by», версия), а не язык песни."""
    return _BRACKETS.sub(" ", title).strip() or title


def languages(
    rows: Iterable[dict],
    lyrics: Callable[[str], str] | None = None,
    verdict_of: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """Язык каждого трека: ``ru`` или ``other``.

    ``verdict_of`` — готовый вывод по тексту для пути (``text_verdict`` от
    текста песни), вместо ``lyrics``: так служба не считает буквы заново.

    По порядку доверия: текст песни (кэш LRCLIB), кириллица в названии,
    большинство треков того же артиста. Латинские названия у русского рэпа —
    обычное дело (PHARAOH — Champagne Squirt), поэтому артист решает за
    трек без текста и без кириллицы в названии.
    """
    rows = list(rows)
    own: dict[str, str | None] = {}
    by_artist: dict[str, list[str]] = {}
    for row in rows:
        path = row["path"]
        if verdict_of is not None:
            verdict = verdict_of(path)
        else:
            verdict = text_verdict(lyrics(path) if lyrics else "")
        if verdict is None and is_cyrillic_text(_bare_title(row.get("title") or "")) >= 0.3:
            verdict = RU
        own[path] = verdict
        for artist in _artists(row):
            by_artist.setdefault(artist, []).append(path)

    def artist_language(row: dict) -> str | None:
        votes = [own[p] for a in _artists(row) for p in by_artist.get(a, []) if own[p]]
        if votes:
            return RU if votes.count(RU) >= len(votes) / 3 else OTHER
        name = row.get("artist") or ""
        if is_cyrillic_text(name) >= 0.3:
            return RU
        return None

    out = {}
    for row in rows:
        out[row["path"]] = own[row["path"]] or artist_language(row) or OTHER
    return out


# «feat»/«feat.»/«featuring»/«ft.» — только отдельным словом с пробелом после:
# «Feather Falls» — один артист (как artist_graph.parts).
_FEAT = re.compile(r"\s*•\s*|\s+(?:feat(?:uring)?\.?|ft\.)\s+", re.IGNORECASE)


def _artists(row: dict) -> list[str]:
    """Участники трека: «A • B», «A feat. B», «A ft. B» — как artist_graph.parts."""
    raw = row.get("artist") or row.get("albumartist") or ""
    names = [" ".join(part.lower().split()) for part in _FEAT.split(raw)]
    return [name for name in names if name]


def _album(row: dict) -> str:
    album = " ".join((row.get("album") or "").lower().split())
    title = " ".join((row.get("title") or "").lower().split())
    # Сингл часто подписан альбомом с тем же именем, что у трека, или
    # «Singles»: это не альбом, и разносить такие треки незачем.
    if not album or album == title or album in {"singles", "single"}:
        return ""
    who = " ".join((row.get("albumartist") or row.get("artist") or "").lower().split())
    return f"{who}\x00{album}"


def spherical_kmeans(
    vectors: np.ndarray, k: int, seed: int = 0, iterations: int = KMEANS_ITERATIONS
) -> tuple[np.ndarray, np.ndarray]:
    """Метки и центры (единичной длины). Строки ``vectors`` — единичной длины.

    k-means++ на косинусной близости, с зерном: одна и та же фонотека
    делится одинаково от запуска к запуску.
    """
    n = len(vectors)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    centers = np.empty((k, vectors.shape[1]), dtype=np.float32)
    centers[0] = vectors[rng.integers(n)]
    closest = 1.0 - vectors @ centers[0]
    for i in range(1, k):
        weights = np.maximum(closest, 0) ** 2
        total = float(weights.sum())
        pick = rng.choice(n, p=weights / total) if total > 0 else rng.integers(n)
        centers[i] = vectors[pick]
        closest = np.minimum(closest, 1.0 - vectors @ centers[i])
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iterations):
        new = np.argmax(vectors @ centers.T, axis=1)
        if _ and np.array_equal(new, labels):
            break
        labels = new
        for i in range(k):
            members = vectors[labels == i]
            if len(members):
                mean = members.sum(axis=0)
                centers[i] = mean / max(float(np.linalg.norm(mean)), 1e-12)
    # Пустые облака — когда одинаковых векторов больше, чем различимых групп
    # (дубли одной песни): выбросить и уплотнить номера.
    used = np.unique(labels)
    remap = np.full(k, -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return remap[labels], centers[used]


@dataclass
class Space:
    """Звучание фонотеки: эмбеддинги, энергия, язык и облака."""

    paths: list[str]
    matrix: np.ndarray  # N × D, строки единичной длины
    energy: np.ndarray  # N, в стандартных отклонениях
    language: list[str]
    cloud: np.ndarray  # N, номер облака
    centers: np.ndarray  # C × D
    cloud_language: list[str]
    neighbours: list[list[int]]  # для каждого облака — ближайшие по центру
    sound_scale: float  # медиана косинусного расстояния между треками
    index: dict[str, int] = field(default_factory=dict)
    mood: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index = {path: i for i, path in enumerate(self.paths)}

    def __contains__(self, path: str) -> bool:
        return path in self.index

    def cloud_of(self, path: str) -> int | None:
        i = self.index.get(path)
        return int(self.cloud[i]) if i is not None else None


def energy_score(rows: list[dict]) -> np.ndarray:
    """Энергия трека: возбуждение (DEAM) и головы EffNet, в z-оценках.

    Возбуждение по восьми–шестнадцати патчам шумит на ±0,1–0,2, головы
    добавляют своё: агрессивное и танцевальное — вверх, спокойное — вниз.
    """

    def z(key: str) -> np.ndarray:
        values = np.array([float(row.get(key) or 0.0) for row in rows], dtype=np.float64)
        spread = float(values.std())
        return (values - values.mean()) / spread if spread > 1e-9 else np.zeros(len(rows))

    raw = z("arousal") + 0.5 * z("aggressive") + 0.3 * z("danceable") - 0.5 * z("relaxed")
    spread = float(raw.std())
    return raw / spread if spread > 1e-9 else raw


def build_space(
    rows: list[dict],
    language_of: dict[str, str],
    clouds: int = CLOUDS,
    seed: int = 0,
) -> Space | None:
    """``rows`` — строки таблицы ``sonic`` (``embedding`` — float32 байты)."""
    rows = [row for row in rows if row.get("embedding")]
    if len(rows) < 2:
        return None
    matrix = np.stack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = (matrix / np.maximum(norms, 1e-12)).astype(np.float32)
    paths = [row["path"] for row in rows]
    lang = [language_of.get(path, OTHER) for path in paths]

    labels = np.zeros(len(rows), dtype=np.int64)
    centers_list: list[np.ndarray] = []
    cloud_language: list[str] = []
    groups = sorted({*lang})
    for group in groups:
        members = np.array([i for i, value in enumerate(lang) if value == group])
        share = len(members) / len(rows)
        k = max(1, min(round(clouds * share), len(members) // MIN_CLOUD or 1))
        sub_labels, sub_centers = spherical_kmeans(matrix[members], k, seed=seed)
        offset = len(centers_list)
        labels[members] = sub_labels + offset
        centers_list.extend(sub_centers)
        cloud_language.extend([group] * len(sub_centers))
    centers = np.stack(centers_list).astype(np.float32)

    similarity = centers @ centers.T
    neighbours: list[list[int]] = []
    for c in range(len(centers)):
        order = [int(i) for i in np.argsort(-similarity[c]) if int(i) != c]
        neighbours.append(order[:NEIGHBOUR_CLOUDS])
    # Соседство — в обе стороны: облако, которое ни у кого не в ближайших,
    # иначе недостижимо (19.11.2026 облако в 106 треков не попало ни в одну
    # из 30 очередей).
    for c, near in enumerate([list(n) for n in neighbours]):
        for other in near:
            if c not in neighbours[other]:
                neighbours[other].append(c)

    # Медиана расстояния по случайным парам: делит «звук», чтобы его доля в
    # расстоянии была сравнима с остальными (у EffNet косинусы сидят у 0,8–1).
    rng = np.random.default_rng(seed)
    a = rng.integers(len(rows), size=min(4000, len(rows) * 4))
    b = rng.integers(len(rows), size=len(a))
    keep = a != b
    pair = 1.0 - np.einsum("ij,ij->i", matrix[a[keep]], matrix[b[keep]])
    scale = float(np.median(pair)) if pair.size else 1.0

    mood = {
        key: np.array([float(row.get(key) or 0.0) for row in rows])
        for key in ("happy", "sad", "aggressive", "relaxed", "party", "danceable", "voice")
    }
    return Space(
        paths=paths,
        matrix=matrix,
        energy=energy_score(rows),
        language=lang,
        cloud=labels,
        centers=centers,
        cloud_language=cloud_language,
        neighbours=neighbours,
        sound_scale=max(scale, 1e-6),
        mood=mood,
    )


# ---------------------------------------------------------------------------
# Чему учат пропуски


@dataclass
class Lessons:
    """Счётчики дослушиваний и ранних пропусков по облакам и переходам."""

    cloud: dict[int, list[float]] = field(default_factory=dict)  # [дослушано, пропущено]
    move: dict[tuple[int, int], list[float]] = field(default_factory=dict)

    def sample_cloud(self, cloud: int, rng: random.Random) -> float:
        good, bad = self.cloud.get(cloud, (0.0, 0.0))
        return rng.betavariate(good + 1, bad + 1)

    def sample_move(self, src: int, dst: int, rng: random.Random) -> float:
        good, bad = self.move.get((src, dst), (0.0, 0.0))
        return rng.betavariate(good + 1, bad + 1)


def learn(plays: list[dict], space: Space) -> Lessons:
    """Уроки из журнала. ``plays`` — от старых к новым: path, at (секунды),
    skipped, played_seconds, ordered (играли по порядку — не учит)."""
    lessons = Lessons()
    previous: dict | None = None
    for play in plays:
        cloud = space.cloud_of(play["path"])
        if previous is not None and play["at"] - previous["at"] > SESSION_GAP:
            previous = None
        if cloud is not None and not play.get("ordered"):
            early = (
                bool(play.get("skipped")) and float(play.get("played_seconds") or 0) < EARLY_SKIP
            )
            slot = lessons.cloud.setdefault(cloud, [0.0, 0.0])
            slot[1 if early else 0] += 1
            src = space.cloud_of(previous["path"]) if previous else None
            if src is not None:
                move = lessons.move.setdefault((src, cloud), [0.0, 0.0])
                move[1 if early else 0] += 1
        previous = play
    return lessons


# ---------------------------------------------------------------------------
# Очередь


ArtistSimilarity = Callable[[list[str], list[str]], float]


def _no_graph(first: list[str], second: list[str]) -> float:
    return 1.0 if set(first) & set(second) else 0.0


@dataclass
class _Ctx:
    space: Space
    meta: dict[str, dict]
    tracks: dict[str, shuffle.Track]
    taste: dict[str, float]
    artist_sim: ArtistSimilarity
    rng: random.Random
    focus: list[str]
    # Артист → {облако: сколько его треков там}; для треков без звучания.
    artist_clouds: dict[str, dict[int, int]] = field(default_factory=dict)
    # Разобранное один раз на очередь: участники, альбом и расстояние до якоря
    # спрашиваются тысячи раз за сборку (профиль 24.11.2026: половина времени).
    _artist_memo: dict[str, list[str]] = field(default_factory=dict)
    _artist_set_memo: dict[str, frozenset[str]] = field(default_factory=dict)
    _album_memo: dict[str, str] = field(default_factory=dict)
    _distance_memo: dict[tuple[str, str], float] = field(default_factory=dict)

    def artists_of(self, path: str) -> list[str]:
        found = self._artist_memo.get(path)
        if found is None:
            found = self._artist_memo[path] = _artists(self.meta.get(path, {}))
        return found

    def artist_set(self, path: str) -> frozenset[str]:
        found = self._artist_set_memo.get(path)
        if found is None:
            found = self._artist_set_memo[path] = frozenset(self.artists_of(path))
        return found

    def album_of(self, path: str) -> str:
        found = self._album_memo.get(path)
        if found is None:
            found = self._album_memo[path] = _album(self.meta.get(path, {}))
        return found


def distance(ctx: _Ctx, path: str, anchor: str) -> float:
    """Расстояние трека до якоря блока (см. WEIGHTS)."""
    known = ctx._distance_memo.get((path, anchor))
    if known is None:
        known = ctx._distance_memo[(path, anchor)] = _distance(ctx, path, anchor)
    return known


def _distance(ctx: _Ctx, path: str, anchor: str) -> float:
    space = ctx.space
    i, j = space.index.get(path), space.index.get(anchor)
    if i is not None and j is not None:
        sound = (1.0 - float(space.matrix[i] @ space.matrix[j])) / space.sound_scale
        energy = min(abs(float(space.energy[i] - space.energy[j])) / 2.0, 1.0)
        language = 0.0 if space.language[i] == space.language[j] else 1.0
    else:
        # Без звучания (новый, ещё не посчитанный трек) — «не знаем»: середина.
        sound, energy, language = 1.0, 0.5, 0.5
    artists_a = ctx.artists_of(path)
    artists_b = ctx.artists_of(anchor)
    artist = 1.0 - ctx.artist_sim(artists_a, artists_b)
    first, second = ctx.tracks.get(path), ctx.tracks.get(anchor)
    tempo_gap = shuffle.tempo_distance(
        first.tempo if first else None, second.tempo if second else None
    )
    tempo = min(tempo_gap / (2 * shuffle.TEMPO_TOLERANCE), 1.0)
    w = WEIGHTS
    return (
        w["sound"] * sound
        + w["artist"] * artist
        + w["energy"] * energy
        + w["language"] * language
        + w["tempo"] * tempo
    )


def _standing(ctx: _Ctx, path: str) -> float:
    """Вес трека самого по себе: давность, час, вкус (как в старом умном)."""
    track = ctx.tracks[path]
    weight = shuffle._coverage_weight(track) * (1.0 + track.hour_affinity)
    weight *= ctx.taste.get(path, 1.0)
    if ctx.focus:
        # Радио: похожесть на артиста станции.
        weight *= 0.3 + ctx.artist_sim(ctx.artists_of(path), ctx.focus)
    return weight


def _wave(ctx: _Ctx, path: str, position: int, phase: float) -> float:
    i = ctx.space.index.get(path)
    if i is None:
        return 1.0
    target = WAVE_AMPLITUDE * math.sin(2 * math.pi * position / WAVE_PERIOD + phase)
    return math.exp(-(((float(ctx.space.energy[i]) - target) / WAVE_WIDTH) ** 2))


def _blocked(ctx: _Ctx, path: str, queue: list[str], strict: int) -> bool:
    """Нарушает ли ``path`` разнос артиста (strict ≥ 1) и альбома (strict ≥ 2)."""
    if strict >= 1:
        mine = ctx.artist_set(path)
        if mine:
            for other in queue[-(ARTIST_GAP - 1) :]:
                if mine & ctx.artist_set(other):
                    return True
    if strict >= 2:
        album = ctx.album_of(path)
        if album:
            for other in queue[-(ALBUM_GAP - 1) :]:
                if ctx.album_of(other) == album:
                    return True
    return False


def _allowed(ctx: _Ctx, pool: list[str], queue: list[str]) -> list[str]:
    """Кандидаты по разносу: сначала оба правила, потом только артист,
    потом без правил — маленькая подборка одного артиста не обрывается."""
    for strict in (2, 1, 0):
        allowed = [p for p in pool if not _blocked(ctx, p, queue, strict)]
        if allowed:
            return allowed
    return pool


STUCK_LIMIT = 30


def _clean(ctx: _Ctx, pool: list[str], queue: list[str]) -> bool:
    return any(not _blocked(ctx, p, queue, 2) for p in pool)


def _clean_elsewhere(
    ctx: _Ctx, members: dict[int, list[str]], current: int, queue: list[str]
) -> bool:
    return any(_clean(ctx, paths, queue) for c, paths in members.items() if c != current)


def _cloud_members(ctx: _Ctx, remaining: set[str]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    # По порядку, не как лежит в множестве: порядок строк в set меняется от
    # запуска к запуску, и очередь с тем же зерном выходила бы другой.
    for path in sorted(remaining):
        cloud = _home_cloud(ctx, path)
        if cloud is not None:
            out.setdefault(cloud, []).append(path)
    return out


def _home_cloud(ctx: _Ctx, path: str) -> int | None:
    cloud = ctx.space.cloud_of(path)
    if cloud is not None:
        return cloud
    # Без звучания — облако, где больше всего треков того же артиста.
    votes: dict[int, int] = {}
    for artist in ctx.artists_of(path):
        for c, n in ctx.artist_clouds.get(artist, {}).items():
            votes[c] = votes.get(c, 0) + n
    return max(sorted(votes), key=lambda c: votes[c]) if votes else None


def _artist_clouds(space: Space, meta: dict[str, dict]) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for path, i in space.index.items():
        for artist in _artists(meta.get(path, {})):
            slot = out.setdefault(artist, {})
            c = int(space.cloud[i])
            slot[c] = slot.get(c, 0) + 1
    return out


def _next_cloud(
    ctx: _Ctx,
    current: int | None,
    members: dict[int, list[str]],
    visited: list[int],
    lessons: Lessons,
    leaving: int | None = None,
) -> int:
    """Облако рядом с ``current`` (якорем сессии): оно само или соседнее, но не
    ``leaving`` — то, где только что был блок; если все пусты — ближайшее
    непустое."""
    available = [c for c, paths in members.items() if paths]
    if leaving is not None and len(available) > 1:
        available = [c for c in available if c != leaving]
    space = ctx.space
    if current is None:
        candidates = available
    else:
        near = [
            c
            for c in [current, *space.neighbours[current]]
            if c in members and members[c] and c in available
        ]
        if not near:
            # Соседи кончились (маленькая подборка): ближайшие из оставшихся.
            order = np.argsort(-(space.centers[[*available]] @ space.centers[current]))
            near = [
                available[int(i)] for i in order[:NEIGHBOUR_CLOUDS] if available[int(i)] != current
            ]
        candidates = near or available
    # Стык считается от облака, откуда уходим: его близость, урок перехода и
    # смена языка. Близость к якорю сессии — ещё множитель, против дрейфа.
    source = leaving if leaving is not None else current
    weights = []
    for c in candidates:
        weight = lessons.sample_cloud(c, ctx.rng) * math.sqrt(len(members[c]))
        if source is not None:
            closeness = float(space.centers[c] @ space.centers[source])
            weight *= max(closeness, 0.05) ** 4
            weight *= lessons.sample_move(source, c, ctx.rng)
            if space.cloud_language[c] != space.cloud_language[source]:
                weight *= LANGUAGE_SWITCH
        if current is not None and current != source:
            weight *= max(float(space.centers[c] @ space.centers[current]), 0.05) ** 2
        if c in visited[-RECENT_CLOUDS:]:
            weight *= RECENT_CLOUD_PENALTY
        weights.append(max(weight, 1e-9))
    return ctx.rng.choices(candidates, weights=weights, k=1)[0]


def _pick(
    ctx: _Ctx,
    pool: list[str],
    queue: list[str],
    anchor: str | None,
    position: int,
    phase: float,
) -> str:
    allowed = _allowed(ctx, pool, queue)
    if anchor is None:
        weights = [_standing(ctx, p) * _wave(ctx, p, position, phase) for p in allowed]
        return ctx.rng.choices(allowed, weights=_finite(weights, 1e-9), k=1)[0]
    gaps = sorted((distance(ctx, p, anchor), p) for p in allowed)[:TOP_CANDIDATES]
    weights = [
        math.exp(-gap / TEMPERATURE) * _standing(ctx, p) * _wave(ctx, p, position, phase)
        for gap, p in gaps
    ]
    return ctx.rng.choices([p for _, p in gaps], weights=_finite(weights, 1e-12), k=1)[0]


def _finite(weights: list[float], floor: float) -> list[float]:
    """NaN или бесконечность в весах (битая строка звучания) — как самый малый вес:
    random.choices на них падает, а очередь должна собраться."""
    return [w if math.isfinite(w) and w > floor else floor for w in weights]


def build_session(
    tracks: list[shuffle.Track],
    space: Space,
    meta: dict[str, dict],
    size: int = 50,
    seed: int | None = None,
    previous: shuffle.Track | None = None,
    lessons: Lessons | None = None,
    artist_sim: ArtistSimilarity | None = None,
    focus_artist: str = "",
    use_taste: bool = True,
) -> list[shuffle.Track]:
    """Очередь блоками вокруг якорей (см. описание модуля).

    ``tracks`` — что перемешивается (подборка, фонотека, круг радио).
    ``meta`` — теги из индекса фонотеки по пути (artist, album, title).
    ``previous`` — уже играющий трек: первый блок продолжает его облако.
    ``focus_artist`` — радио: вес похожести на этого артиста.
    """
    if not tracks:
        return []
    rng = random.Random(seed)
    by_path = {t.path: t for t in tracks}
    ctx = _Ctx(
        space=space,
        meta=meta,
        tracks={**by_path, **({previous.path: previous} if previous else {})},
        taste={t.path: shuffle.taste_weight(t) for t in tracks} if use_taste else {},
        artist_sim=artist_sim or _no_graph,
        rng=rng,
        focus=_artists({"artist": focus_artist}) if focus_artist else [],
    )
    if any(p not in space for p in by_path):
        ctx.artist_clouds = _artist_clouds(space, meta)
    lessons = lessons or Lessons()
    remaining = set(by_path)
    if previous is not None:
        remaining.discard(previous.path)
    members = _cloud_members(ctx, remaining)
    # Совсем без облака (ни звучания, ни артиста со звучанием) — в общий хвост:
    # такие треки встают по одному между блоками, чтобы не пропасть.
    homeless = [p for p in sorted(remaining) if _home_cloud(ctx, p) is None]

    queue: list[str] = [previous.path] if previous else []
    start = len(queue)
    visited: list[int] = []
    current = _home_cloud(ctx, previous.path) if previous else None
    anchor = previous.path if previous else None
    phase = rng.uniform(0, 2 * math.pi)
    if anchor is not None and anchor in space:
        # Волна начинается с энергии того, что уже играет.
        e = float(space.energy[space.index[anchor]]) / WAVE_AMPLITUDE
        phase = math.asin(max(-1.0, min(1.0, e)))

    def homeless_one() -> None:
        # Без облака — тоже с правилом «артист раз в 6, альбом раз в 9».
        path = _pick(ctx, homeless, queue, None, len(queue) - start, phase)
        homeless.remove(path)
        queue.append(path)

    first_block = previous is not None and current is not None and bool(members.get(current))
    around = current  # облако якоря сессии
    stuck = 0  # блоков подряд без единого трека (страховка от зацикливания)
    blocks_left = rng.randint(*SESSION_BLOCKS)
    while len(queue) - start < size and (any(members.values()) or homeless):
        if not any(members.values()):
            homeless_one()
            continue
        if not first_block:
            if blocks_left <= 0:
                around = current
                blocks_left = rng.randint(*SESSION_BLOCKS)
            current = _next_cloud(ctx, around, members, visited, lessons, leaving=current)
            if around is None:
                around = current
            anchor = None
        first_block = False
        blocks_left -= 1
        assert current is not None
        visited.append(current)
        block = rng.randint(BLOCK_MIN, BLOCK_MAX)
        before = len(queue)
        for _ in range(block):
            pool = members.get(current) or []
            if not pool or len(queue) - start >= size:
                break
            if (
                stuck < STUCK_LIMIT
                and not _clean(ctx, pool, queue)
                and _clean_elsewhere(ctx, members, current, queue)
            ):
                # Здесь остался только повтор артиста или альбома, а в других
                # облаках есть чистые треки: блок кончается раньше, правило не
                # ослабляется (ослабление — только когда чистых нет нигде).
                break
            path = _pick(ctx, pool, queue, anchor, len(queue) - start, phase)
            pool.remove(path)
            queue.append(path)
            if anchor is None:
                anchor = path
        stuck = 0 if len(queue) > before else stuck + 1
        if homeless and rng.random() < 0.5 and len(queue) - start < size:
            homeless_one()
    return [ctx.tracks[p] for p in queue[start:]]


def coverage(tracks: list[shuffle.Track], space: Space | None) -> float:
    if not tracks or space is None:
        return 0.0
    return sum(1 for t in tracks if t.path in space) / len(tracks)


def report(queue: list[shuffle.Track], space: Space, meta: dict[str, dict]) -> dict:
    """Числа для проверки очереди: разнос артистов и альбомов, облака."""
    paths = [t.path for t in queue]
    artist_close = album_close = 0
    for i, path in enumerate(paths):
        mine = set(_artists(meta.get(path, {})))
        for other in paths[max(0, i - (ARTIST_GAP - 1)) : i]:
            if mine & set(_artists(meta.get(other, {}))):
                artist_close += 1
                break
        album = _album(meta.get(path, {}))
        if album and any(
            _album(meta.get(o, {})) == album for o in paths[max(0, i - (ALBUM_GAP - 1)) : i]
        ):
            album_close += 1
    clouds = [space.cloud_of(p) for p in paths]
    switches = sum(1 for a, b in zip(clouds, clouds[1:], strict=False) if a != b)
    return {
        "tracks": len(paths),
        "artist_too_close": artist_close,
        "album_too_close": album_close,
        "cloud_switches": switches,
        "clouds": len({c for c in clouds if c is not None}),
    }


# ---------------------------------------------------------------------------
# Загрузка из базы (кэш на процесс)

_CACHE: dict = {"key": None, "space": None}
_CACHE_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()


def load_space(index_rows: list[dict]) -> Space | None:
    """Пространство звучания текущей фонотеки; пересчёт — когда меняется
    таблица ``sonic`` или набор путей фонотеки."""
    from . import db

    signature = db.db_query("SELECT COUNT(*) AS n, MAX(analyzed_at) AS at FROM sonic")
    present = {row["path"] for row in index_rows}
    # Артист — в ключе: правка тегов меняет язык и облака артиста.
    tags = frozenset((row["path"], row.get("artist") or "") for row in index_rows)
    key = (signature[0]["n"], signature[0]["at"], len(present), hash(tags))
    with _CACHE_LOCK:
        if _CACHE["key"] == key:
            return _CACHE["space"]
    # Пересборку делает один запрос: остальные ждут его и берут готовое, а не
    # считают k-means каждый сам (главная строит шесть подборок разом).
    with _BUILD_LOCK:
        with _CACHE_LOCK:
            if _CACHE["key"] == key:
                return _CACHE["space"]
        space = _build_from_db(index_rows, present)
        with _CACHE_LOCK:
            _CACHE["key"] = key
            _CACHE["space"] = space
    return space


def _build_from_db(index_rows: list[dict], present: set[str]) -> Space | None:
    from . import db

    rows = [
        row
        for row in db.db_query(
            "SELECT path, embedding, happy, sad, aggressive, relaxed, party, danceable, "
            "voice, arousal, valence FROM sonic"
        )
        if row["path"] in present
    ]
    language_of = languages(index_rows, verdict_of=_lyrics_verdict)
    return build_space(rows, language_of)


# Язык по тексту песни — по отпечатку файла кэша текстов: пересборка после
# каждого нового трека читала все ~1650 файлов и считала в них буквы заново
# (2,2 с из 2,6 с сборки, замер 24.11.2026). Файл не менялся — вывод тот же.
_TEXTS: dict[str, tuple[int, str | None]] = {}
_TEXTS_LOCK = threading.Lock()


def _lyrics_verdict(rel_path: str) -> str | None:
    from . import lyrics

    try:
        stamp = lyrics._key(rel_path).stat().st_mtime_ns
    except OSError:
        stamp = -1
    with _TEXTS_LOCK:
        known = _TEXTS.get(rel_path)
    if known is not None and known[0] == stamp:
        return known[1]
    verdict = text_verdict(lyrics.plain_text(rel_path)) if stamp != -1 else None
    with _TEXTS_LOCK:
        _TEXTS[rel_path] = (stamp, verdict)
    return verdict


def forget() -> None:
    with _CACHE_LOCK:
        _CACHE["key"] = None
        _CACHE["space"] = None
    with _TEXTS_LOCK:
        _TEXTS.clear()


# ---------------------------------------------------------------------------
# Какое умное играет: новое (по звучанию) или старое (по темпу)

ENGINE_KEY = "smart_engine"
ENGINES = ("sonic", "classic")
# Решение владельца 19.11.2026: «Умное» сразу новое, старое — одной
# настройкой и стороной слепого сравнения.
ENGINE_DEFAULT = "sonic"


def engine_level() -> str:
    from . import db

    try:
        rows = db.db_query("SELECT value FROM settings WHERE key = ?", (ENGINE_KEY,))
        value = rows[0]["value"] if rows else ENGINE_DEFAULT
    except Exception:  # noqa: BLE001 — нет базы или она не та: как по умолчанию
        return ENGINE_DEFAULT
    return value if value in ENGINES else ENGINE_DEFAULT


def set_engine_level(level: str) -> None:
    from . import db

    if level not in ENGINES:
        raise ValueError(level)
    db.db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (ENGINE_KEY, level))
