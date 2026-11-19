"""Умное перемешивание по звучанию (adder/sonic.py): облака, блоки, правила."""

import random

import numpy as np
import pytest

from adder import shuffle, sonic

DIM = 16


def _vector(center: int, rng: np.random.Generator, spread: float = 0.15) -> bytes:
    base = np.zeros(DIM, dtype=np.float32)
    base[center] = 1.0
    v = base + rng.normal(0, spread, DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.astype(np.float32).tobytes()


def _library(groups: int = 4, artists_per_group: int = 6, per_artist: int = 5, seed: int = 0):
    """Фонотека из ``groups`` звуковых групп; у каждой — свои артисты и альбомы.

    Половина групп русская (кириллица в названиях), половина — нет.
    """
    rng = np.random.default_rng(seed)
    index, rows = [], []
    for g in range(groups):
        russian = g % 2 == 0
        for a in range(artists_per_group):
            artist = f"Артист{g}_{a}" if russian else f"Artist{g}_{a}"
            for t in range(per_artist):
                path = f"{artist}/Album/{t}.m4a"
                title = f"Песня {t}" if russian else f"Song {t}"
                index.append(
                    {"path": path, "artist": artist, "title": title, "album": f"Альбом {g}{a}"}
                )
                rows.append(
                    {
                        "path": path,
                        "embedding": _vector(g, rng),
                        "arousal": 5 + rng.normal(0, 0.5),
                        "aggressive": float(rng.random()),
                        "relaxed": float(rng.random()),
                        "danceable": float(rng.random()),
                        "happy": float(rng.random()),
                        "sad": float(rng.random()),
                        "party": float(rng.random()),
                        "voice": 0.9,
                    }
                )
    return index, rows


def _space(index, rows, clouds: int = 4):
    return sonic.build_space(rows, sonic.languages(index), clouds=clouds)


def _tracks(index) -> list[shuffle.Track]:
    return [shuffle.Track(path=r["path"], artist=r["artist"]) for r in index]


def _meta(index) -> dict:
    return {r["path"]: r for r in index}


# ---------------------------------------------------------------------------
# Язык


def test_language_comes_from_lyrics_first():
    rows = [{"path": "a.m4a", "artist": "PHARAOH", "title": "Champagne Squirt"}]
    lyrics = {"a.m4a": "Я однажды догнал, что не ошибается тот, кто ничего не делает " * 2}
    assert sonic.languages(rows, lambda p: lyrics.get(p, ""))["a.m4a"] == sonic.RU


def test_latin_title_follows_the_artist_majority():
    # У русского рэпа латинские названия — обычное дело: решает артист.
    rows = [
        {"path": "1", "artist": "PHARAOH", "title": "Champagne Squirt"},
        {"path": "2", "artist": "PHARAOH", "title": "Дико, например"},
        {"path": "3", "artist": "PHARAOH", "title": "Есть"},
        {"path": "4", "artist": "Billie Eilish", "title": "bad guy"},
    ]
    lang = sonic.languages(rows)
    assert lang["1"] == sonic.RU
    assert lang["4"] == sonic.OTHER


def test_a_feature_counts_for_both_artists():
    rows = [
        {"path": "1", "artist": "Скриптонит", "title": "Положение"},
        {"path": "2", "artist": "Скриптонит • 104", "title": "Lost"},
    ]
    assert sonic.languages(rows)["2"] == sonic.RU


# ---------------------------------------------------------------------------
# Облака


def test_kmeans_is_repeatable_and_finds_obvious_groups():
    index, rows = _library(groups=3, artists_per_group=4, per_artist=5)
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    first, _ = sonic.spherical_kmeans(matrix, 3, seed=7)
    second, _ = sonic.spherical_kmeans(matrix, 3, seed=7)
    assert np.array_equal(first, second)
    # Каждая звуковая группа — целиком в одном облаке.
    for g in range(3):
        assert len(set(first[g * 20 : (g + 1) * 20])) == 1


def test_russian_and_foreign_never_share_a_cloud():
    index, rows = _library(groups=4)
    # Сделаем русскую и нерусскую группы одинаковыми по звуку: облака всё
    # равно не смешиваются — решение владельца 19.11.
    same = rows[0]["embedding"]
    for r in rows:
        r["embedding"] = same
    space = _space(index, rows)
    for c in range(len(space.centers)):
        langs = {space.language[i] for i in np.where(space.cloud == c)[0]}
        assert len(langs) == 1, c


# ---------------------------------------------------------------------------
# Очередь


def test_artist_keeps_six_apart_and_album_nine():
    index, rows = _library(groups=4, artists_per_group=6, per_artist=5)
    space = _space(index, rows)
    meta = _meta(index)
    for seed in range(10):
        queue = sonic.build_session(_tracks(index), space, meta, size=60, seed=seed)
        assert len(queue) == 60
        assert len({t.path for t in queue}) == 60
        report = sonic.report(queue, space, meta)
        assert report["artist_too_close"] == 0, seed
        assert report["album_too_close"] == 0, seed


def test_queue_goes_in_blocks_of_one_cloud():
    index, rows = _library(groups=4, artists_per_group=8, per_artist=5)
    space = _space(index, rows)
    queue = sonic.build_session(_tracks(index), space, _meta(index), size=60, seed=3)
    clouds = [space.cloud_of(t.path) for t in queue]
    runs, current = [], 1
    for a, b in zip(clouds, clouds[1:], strict=False):
        if a == b:
            current += 1
        else:
            runs.append(current)
            current = 1
    # Блоки — по 4–6 (последний может быть короче: очередь кончилась).
    assert all(sonic.BLOCK_MIN <= r <= sonic.BLOCK_MAX for r in runs), runs
    # Не прыжки с трека на трек: смен облака — раз в блок, не чаще.
    assert len(runs) <= 60 / sonic.BLOCK_MIN


def test_a_block_stays_near_its_anchor_not_the_last_track():
    # Внутри облака — «подоблака»: якорь блока держит своё, а жадная прогулка
    # уползала бы. Сравниваем среднее расстояние до якоря с расстоянием до
    # случайного трека того же облака.
    index, rows = _library(groups=2, artists_per_group=12, per_artist=5, seed=4)
    space = _space(index, rows, clouds=2)
    meta = _meta(index)
    near, rand = [], []
    rng = random.Random(0)
    for seed in range(8):
        queue = sonic.build_session(_tracks(index), space, meta, size=30, seed=seed)
        clouds = [space.cloud_of(t.path) for t in queue]
        start = 0
        for i in range(1, len(queue) + 1):
            if i == len(queue) or clouds[i] != clouds[start]:
                anchor = space.index[queue[start].path]
                for t in queue[start + 1 : i]:
                    near.append(1 - float(space.matrix[space.index[t.path]] @ space.matrix[anchor]))
                    same = [p for p in space.paths if space.cloud_of(p) == clouds[start]]
                    other = space.index[rng.choice(same)]
                    rand.append(1 - float(space.matrix[other] @ space.matrix[anchor]))
                start = i
    assert np.mean(near) < np.mean(rand)


def test_previous_track_continues_its_own_cloud():
    index, rows = _library(groups=4)
    space = _space(index, rows)
    tracks = _tracks(index)
    previous = tracks[0]
    queue = sonic.build_session(tracks, space, _meta(index), size=10, seed=1, previous=previous)
    assert previous.path not in {t.path for t in queue}
    assert space.cloud_of(queue[0].path) == space.cloud_of(previous.path)


def test_a_small_one_artist_playlist_still_plays_whole():
    index, rows = _library(groups=1, artists_per_group=1, per_artist=7)
    space = _space(index, rows, clouds=1)
    queue = sonic.build_session(_tracks(index), space, _meta(index), size=50, seed=0)
    assert sorted(t.path for t in queue) == sorted(r["path"] for r in index)


def test_tracks_without_sound_follow_their_artist():
    index, rows = _library(groups=4)
    missing = rows.pop(0)  # первый трек ещё не посчитан
    space = _space(index, rows)
    tracks = _tracks(index)
    queue = sonic.build_session(tracks, space, _meta(index), size=len(tracks), seed=2)
    assert missing["path"] in {t.path for t in queue}
    assert sonic.coverage(tracks, space) < 1.0


def test_energy_wave_prefers_the_target():
    index, rows = _library(groups=1, per_artist=4)
    space = _space(index, rows, clouds=1)
    ctx = sonic._Ctx(
        space=space,
        meta=_meta(index),
        tracks={t.path: t for t in _tracks(index)},
        taste={},
        artist_sim=sonic._no_graph,
        rng=random.Random(0),
        focus=[],
    )
    low = space.paths[int(np.argmin(space.energy))]
    high = space.paths[int(np.argmax(space.energy))]
    # Фаза π/2: цель на гребне волны — громкое выигрывает у тихого.
    peak = sonic.WAVE_PERIOD // 4
    assert sonic._wave(ctx, high, peak, 0.0) > sonic._wave(ctx, low, peak, 0.0)


# ---------------------------------------------------------------------------
# Пропуски учат облака и переходы


def test_learn_counts_early_skips_per_cloud_and_move():
    index, rows = _library(groups=2, artists_per_group=3, per_artist=4)
    space = _space(index, rows, clouds=2)
    a = space.paths[0]
    b = next(p for p in space.paths if space.cloud_of(p) != space.cloud_of(a))
    plays = [
        {"path": a, "at": 0, "skipped": 0, "played_seconds": 200},
        {"path": b, "at": 200, "skipped": 1, "played_seconds": 5},  # ранний пропуск
        {"path": a, "at": 210, "skipped": 1, "played_seconds": 120},  # поздний — не ранний
        {"path": b, "at": 10_000, "skipped": 1, "played_seconds": 3},  # новая сессия
        {"path": a, "at": 10_100, "skipped": 1, "played_seconds": 3, "ordered": 1},
    ]
    lessons = sonic.learn(plays, space)
    ca, cb = space.cloud_of(a), space.cloud_of(b)
    assert lessons.move[(ca, cb)] == [0.0, 1.0]
    assert lessons.move[(cb, ca)] == [1.0, 0.0]
    # Игра по порядку не учит, пауза рвёт сессию: перехода b→a из неё нет.
    assert lessons.cloud[cb] == [0.0, 2.0]
    assert lessons.cloud[ca] == [2.0, 0.0]


def test_a_skipped_move_is_taken_less_often():
    index, rows = _library(groups=3, artists_per_group=6, per_artist=5)
    space = _space(index, rows, clouds=3)
    meta = _meta(index)
    start = space.paths[0]
    home = space.cloud_of(start)
    others = sorted({int(c) for c in space.cloud} - {home})
    bad, good = others[0], others[1]
    lessons = sonic.Lessons(move={(home, bad): [0.0, 40.0], (home, good): [40.0, 0.0]})
    tracks = _tracks(index)
    chosen = {bad: 0, good: 0}
    for seed in range(60):
        queue = sonic.build_session(
            tracks,
            space,
            meta,
            size=8,
            seed=seed,
            previous=next(t for t in tracks if t.path == start),
            lessons=lessons,
        )
        clouds = [space.cloud_of(t.path) for t in queue]
        nxt = next((c for c in clouds if c != home), None)
        if nxt in chosen:
            chosen[nxt] += 1
    assert chosen[good] > 3 * chosen[bad]


def test_radio_focus_pulls_towards_the_station_artist():
    index, rows = _library(groups=1, artists_per_group=8, per_artist=5)
    space = _space(index, rows, clouds=1)
    meta = _meta(index)
    fan = "Артист0_1"

    def sim(first, second):
        if set(first) & set(second):
            return 1.0
        return 0.9 if "артист0_1" in second and "артист0_2" in first else 0.0

    hits = 0
    for seed in range(20):
        queue = sonic.build_session(
            _tracks(index), space, meta, size=12, seed=seed, artist_sim=sim, focus_artist=fan
        )
        hits += sum(1 for t in queue if t.artist in {fan, "Артист0_2"})
    # Без фокуса — 2 артиста из 8, около четверти; с фокусом — заметно больше.
    assert hits / (20 * 12) > 0.3


def test_report_counts_close_repeats():
    index, _ = _library(groups=1, artists_per_group=1, per_artist=3)
    meta = _meta(index)
    space_rows = [
        {"path": r["path"], "embedding": np.ones(DIM, np.float32).tobytes()} for r in index
    ]
    space = sonic.build_space(space_rows, sonic.languages(index), clouds=1)
    queue = _tracks(index)
    assert sonic.report(queue, space, meta)["artist_too_close"] == 2


def test_empty_or_tiny_inputs():
    assert sonic.build_space([], {}) is None
    index, rows = _library(groups=1, artists_per_group=1, per_artist=2)
    space = _space(index, rows, clouds=1)
    assert sonic.build_session([], space, {}) == []


@pytest.mark.parametrize("text,expected", [("", 0.0), ("abc", 0.0), ("абв", 1.0), ("аб cd", 0.5)])
def test_cyrillic_share(text, expected):
    assert sonic.is_cyrillic_text(text) == expected
