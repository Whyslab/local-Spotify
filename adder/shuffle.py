"""Building a queue that has a shape, instead of one that is merely unpredictable.

Ordinary shuffle has two complaints against it and they are different problems.

The jarring transitions are a question of *order*: a slow track next to a fast
one is bad wherever it lands. That is solved by walking the library rather than
sampling it -- each next track is chosen from those close to the current one in
tempo and energy.

Playing the same hundred tracks forever is a question of *weight*: uniform
random over a thousand tracks really does keep returning to the same ones, and
the fix is to make a track less likely the more recently it was heard.

The time of day is the third thing, and it only works once there is a journal
to learn from. Until then this module is smoothness and coverage, which is most
of the value and needs no history at all.
"""

import logging
import math
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How far apart two adjacent tracks may be in tempo. Twelve BPM is about the
# width of one "feel" -- 90 and 100 sit together, 90 and 140 do not.
TEMPO_TOLERANCE = 12.0

# Half time and double time are the same groove. An 80 BPM track next to a 160
# BPM one is a smooth transition, not a jump, and refusing it would split the
# library in two along an arbitrary line.
TEMPO_FOLDS = (0.5, 1.0, 2.0)

# How many recent plays make a track unlikely to come back.
RECENCY_WINDOW = 200

# What an unmeasured track is worth as a transition.
#
# Not 1.0, which is what treating its tempo distance as zero amounted to: that
# says "a perfectly smooth transition" when the truth is "we have no idea". A
# smooth transition is the thing being asked for, so a candidate that cannot be
# shown to provide one should lose to a candidate that can -- while remaining
# reachable, because it can still serve coverage and the hour.
#
# This is also what makes the feature work before the whole library has been
# through the analyser, and it fades to nothing on its own once it has.
#
# Deliberately not lower. At 0.12 a measured track appears in the queue at
# roughly four times its share of the library -- enough to steer by, while
# leaving the unmeasured majority reachable. Pushing it to 0.02 would make the
# queue almost entirely measured, and would also mean nothing new gets heard
# until the analyser has been through it: covering the shelf is the other half
# of what was being asked for, not a detail.
UNKNOWN_TRANSITION = 0.12

# Какую долю очереди отдавать трекам не из подборки, когда перемешивают
# подборку, а не всю фонотеку.
#
# Треть выбрана не случайно: половина — это уже не «твоя подборка с добавками»,
# а другая подборка, и узнать её не выйдет. Меньше четверти — и за пятьдесят
# треков новых наберётся десяток, то есть незаметно.
OUTSIDE_SHARE = 0.3

# Вкус — что из журнала видно про сам трек, а не про его место в очереди.
#
# Пропуск и дослушивание весят тем меньше, чем дальше в журнале: за пятьдесят
# прослушиваний их вес падает вдвое. Считаем прослушиваниями, а не днями: месяц
# без музыки не делает вчерашний пропуск старым — после него ничего и не слушали.
#
# Числа ниже подобраны бэктестом (scripts/backtest_shuffle.py --fit) на первой
# половине журнала 14.10.2026 и проверены на второй: новая оценка опускает
# пропущенное и поднимает дослушанное в среднем на 0.015 перцентиля, 95 % —
# от 0.007 до 0.025. Эффект слабый, но не шум. Сетку шире подбор тянет к краям
# (штраф артисту 6, прибавка дослушанному 4), а интервал там задевает ноль:
# 542 прослушиваний на четыре числа мало, и умеренные значения честнее.
TASTE_HALF_LIFE = 50
# Дослушанным считается трек, сыгранный хотя бы на столько от длины.
HEARD_SHARE = 0.9
# Насколько свежий пропуск прячет трек: exp(-SKIP_PENALTY) после одного.
SKIP_PENALTY = 0.5
# Насколько свежее дослушивание поднимает трек: до 1 + HEARD_BONUS.
HEARD_BONUS = 1.0
# Артист: доля пропусков у него против доли по всему журналу, сглаженная
# ARTIST_PRIOR воображаемыми прослушиваниями со средней долей, — один пропуск
# у нового артиста ещё ничего не говорит.
ARTIST_PENALTY = 3.0
ARTIST_PRIOR = 3.0


@dataclass
class Track:
    path: str
    artist: str
    tempo: float | None = None
    energy: float | None = None
    brightness: float | None = None
    music_key: str = ""
    plays: int = 0
    # Position in the play journal counted back from now, or None if unheard.
    last_play_rank: int | None = None
    # How often this track was played in the current hour of the day, over the
    # whole journal. The "context of the moment", once there is one.
    hour_affinity: float = 0.0
    # Пропуски и дослушивания этого трека, свежие весят больше (TASTE_HALF_LIFE).
    skip_score: float = 0.0
    heard_score: float = 0.0
    # Насколько артиста пропускают чаще (> 0) или реже (< 0), чем всех.
    artist_skip: float = 0.0
    extra: dict = field(default_factory=dict)


def tempo_distance(first: float | None, second: float | None) -> float:
    """Distance in BPM, counting half and double time as the same tempo."""
    if not first or not second:
        # An unmeasured track is not evidence of a jump, so it always passes the
        # tempo window. That is all zero means here: the weighting still
        # demotes it through UNKNOWN_TRANSITION, which is where "we have no
        # idea" is priced in.
        return 0.0
    # В обе стороны: иначе 85→160 было 10, а 160→85 — 5, и одна и та же пара
    # треков стыковалась по-разному в зависимости от порядка.
    return min(min(abs(first * fold - second), abs(second * fold - first)) for fold in TEMPO_FOLDS)


def _coverage_weight(track: Track) -> float:
    """Higher for tracks that have not been heard lately.

    This is the part that answers "the same hundred tracks keep coming back".
    Something played twenty tracks ago should be rare; something never played
    should be common.
    """
    if track.last_play_rank is None:
        return 1.0
    if track.last_play_rank >= RECENCY_WINDOW:
        return 1.0
    # Linear from 0.05 (just played) up to 1.0 (fell out of the window).
    return 0.05 + 0.95 * (track.last_play_rank / RECENCY_WINDOW)


def taste_weight(track: Track) -> float:
    """Ниже для того, что недавно пропускали, выше для дослушанного.

    Это множитель, а не запрет: пропущенный однажды трек становится реже, но
    не исчезает — пропускают и под настроение.
    """
    weight = math.exp(-SKIP_PENALTY * track.skip_score)
    weight *= 1.0 + HEARD_BONUS * min(track.heard_score, 1.0)
    weight *= math.exp(-ARTIST_PENALTY * track.artist_skip)
    return weight


def candidate_weight(
    current: Track | None,
    candidate: Track,
    use_moment: bool = True,
    use_taste: bool = True,
    taste: float | None = None,
) -> float:
    """Вес кандидата за ``current`` (``None`` — первый трек очереди).

    ``taste`` — уже посчитанный ``taste_weight(candidate)``: очередь считает
    его раз на трек, а не на каждом шаге.

    Тот же, что в ``build_queue``, без поправки на повтор артиста: бэктест
    (``scripts/backtest_shuffle.py``) сравнивает им старую и новую оценку.
    """
    weight = _coverage_weight(candidate)
    if current is None:
        # The first track has nothing to follow, so start where the queue can
        # be steered from.
        if not candidate.tempo:
            weight *= UNKNOWN_TRANSITION
    else:
        weight *= _transition_weight(current, candidate)
    if use_moment:
        weight *= 1.0 + candidate.hour_affinity
    if use_taste:
        weight *= taste_weight(candidate) if taste is None else taste
    return weight


def _transition_weight(current: Track, candidate: Track) -> float:
    """Higher the better the candidate follows the current track."""
    if not candidate.tempo or not current.tempo:
        return UNKNOWN_TRANSITION

    distance = tempo_distance(current.tempo, candidate.tempo)
    tempo_score = math.exp(-((distance / TEMPO_TOLERANCE) ** 2))

    if current.energy is not None and candidate.energy is not None:
        energy_score = math.exp(-((abs(current.energy - candidate.energy) / 0.15) ** 2))
    else:
        energy_score = 1.0

    return tempo_score * energy_score


def build_queue(
    tracks: list[Track],
    size: int = 50,
    seed: int | None = None,
    use_moment: bool = True,
    tempo_tolerance: float = TEMPO_TOLERANCE,
    core: set[str] | None = None,
    outside_share: float = OUTSIDE_SHARE,
    previous: Track | None = None,
    use_taste: bool = True,
) -> list[Track]:
    """Order tracks so that each one follows plausibly from the one before.

    Greedy with weighted randomness rather than a strict nearest neighbour: a
    strict one produces the same queue every time from the same starting track,
    which is a playlist, not a shuffle.

    ``core`` -- пути подборки, которую включили. Тогда ``tracks`` это вся
    фонотека, а подборка остаётся костяком: очередь начинается с неё, и
    примерно ``outside_share`` мест достаётся трекам со стороны, подобранным по
    тем же правилам смежности. Без ``core`` всё как было — перемешивается то,
    что передали.

    ``previous`` — трек, который уже играет (в очередь он не входит). Тогда
    первый трек выбирается как следующий за ним, а не сам по себе: так
    продолжается радио, и стык между порциями не слышен.

    ``use_taste`` — учитывать пропуски и дослушивания (``taste_weight``).
    """
    if not tracks:
        return []

    rng = random.Random(seed)
    pool = list(tracks)
    queue: list[Track] = []

    core_paths = set(core) if core else None
    # Сколько мест в очереди отдано стороне. Пересчёта по ходу нет: бюджет
    # тратится, и когда он кончился, очередь снова только из подборки.
    outside_budget = round(size * outside_share) if core_paths else 0

    # The first track is chosen on standing alone -- coverage, the hour and
    # taste if there is a journal -- since there is nothing yet for it to follow.
    taste = {t.path: taste_weight(t) for t in pool} if use_taste else {}

    def opening_weight(track: Track) -> float:
        return max(
            candidate_weight(None, track, use_moment, use_taste, taste.get(track.path)), 1e-6
        )

    # Начинать со своего. Включили подборку — первым должно заиграть то, что в
    # ней лежит, иначе это не «подборка с добавками», а чужая очередь.
    if previous is not None:
        pool = [t for t in pool if t.path != previous.path]
        current = previous
    else:
        opening = [t for t in pool if t.path in core_paths] if core_paths else pool
        if not opening:
            opening = pool
        current = rng.choices(opening, weights=[opening_weight(t) for t in opening], k=1)[0]
        pool.remove(current)
        queue.append(current)

    while pool and len(queue) < size:
        recent_artists = {
            t.artist for t in [*([previous] if previous else []), *queue][-2:] if t.artist
        }

        # Решаем сначала, чьё это место, и только потом ищем трек. Иначе всё
        # чужое встанет в начало: фонотека в сто раз больше подборки, и по
        # весам она бы просто её задавила, пока не кончится бюджет.
        side = pool
        if core_paths:
            slots_left = size - len(queue)
            outside_turn = outside_budget > 0 and rng.random() < outside_budget / slots_left
            side = [c for c in pool if (c.path not in core_paths) is outside_turn]
            if not side:
                # Подборка кончилась (или своих по темпу не осталось) — доиграем
                # из фонотеки, это лучше, чем оборвать очередь на десятом треке.
                side = pool

        # Criterion 24 is a limit, not a preference: neighbours must be within
        # the tolerance. Weighting alone let a 20 BPM jump through -- unlikely
        # is not the same as excluded. Candidates outside the window are cut
        # first, and only if that leaves nothing does the window widen, so a
        # library with a gap in it still produces a queue instead of stopping.
        candidates = [c for c in side if tempo_distance(current.tempo, c.tempo) <= tempo_tolerance]
        if not candidates and side is not pool:
            # По темпу рядом не осталось никого с той стороны, чья очередь.
            # Взять подходящий трек с другой лучше, чем рвать переход ради
            # очерёдности: гладкость — то, ради чего всё и затевалось.
            side = pool
            candidates = [
                c for c in side if tempo_distance(current.tempo, c.tempo) <= tempo_tolerance
            ]
        if not candidates:
            nearest = min(tempo_distance(current.tempo, c.tempo) for c in side)
            candidates = [
                c for c in side if tempo_distance(current.tempo, c.tempo) <= nearest + 1e-9
            ]
            logger.debug(
                "No track within %.0f BPM of %.0f; stretching to %.0f",
                tempo_tolerance,
                current.tempo or 0,
                nearest,
            )

        weights = []
        for candidate in candidates:
            weight = candidate_weight(
                current, candidate, use_moment, use_taste, taste.get(candidate.path)
            )
            if candidate.artist and candidate.artist in recent_artists:
                # Not forbidden outright -- in a library where one artist holds
                # a tenth of the tracks, a hard ban distorts everything after
                # it -- but pushed far enough down to be rare.
                weight *= 0.02
            weights.append(max(weight, 1e-9))

        chosen = rng.choices(candidates, weights=weights, k=1)[0]
        if core_paths and chosen.path not in core_paths:
            outside_budget = max(outside_budget - 1, 0)
        pool.remove(chosen)
        queue.append(chosen)
        current = chosen

    return queue


def queue_report(queue: list[Track]) -> dict:
    """The numbers the acceptance criteria are stated in.

    Kept next to the builder rather than in the tests, because it is also what
    the blind comparison and the skip-rate measurement report on.
    """
    if len(queue) < 2:
        return {
            "tracks": len(queue),
            "max_tempo_jump": None,
            "mean_tempo_jump": None,
            "measured_transitions": 0,
            "artist_repeats": 0,
            "distinct_artists": len({t.artist for t in queue if t.artist}),
        }

    jumps = [
        tempo_distance(queue[i].tempo, queue[i + 1].tempo)
        for i in range(len(queue) - 1)
        if queue[i].tempo and queue[i + 1].tempo
    ]
    repeats = sum(
        1
        for i in range(len(queue) - 1)
        if queue[i].artist and queue[i].artist == queue[i + 1].artist
    )
    return {
        "tracks": len(queue),
        # None, not zero, when nothing could be compared. A queue of unmeasured
        # tracks has an unknown largest jump, and reporting 0.0 would read as
        # "perfectly smooth" when it means "no data".
        "max_tempo_jump": round(max(jumps), 2) if jumps else None,
        "mean_tempo_jump": round(sum(jumps) / len(jumps), 2) if jumps else None,
        "measured_transitions": len(jumps),
        "artist_repeats": repeats,
        "distinct_artists": len({t.artist for t in queue if t.artist}),
    }


def tracks_from_rows(
    index_rows: list[dict],
    feature_rows: list[dict],
    play_rows: list[dict],
    current_hour: int,
) -> list[Track]:
    """Assemble what the builder needs out of the three tables that hold it.

    A pure function on purpose: the interesting behaviour is in how recency and
    hour affinity are derived, and that should be testable without a database.

    ``play_rows`` must be newest first -- the position in that list is what
    "recently" means here. ``skipped``, ``played_seconds`` and ``duration`` в
    строках — для вкуса (``taste_weight``); без них вкус нейтрален.
    """
    features = {row["path"]: row for row in feature_rows}
    artist_of = {row["path"]: _artist_key(row.get("artist") or "") for row in index_rows}

    last_rank: dict[str, int] = {}
    hour_counts: dict[str, int] = {}
    total_at_hour = 0
    skip_score: dict[str, float] = {}
    heard_score: dict[str, float] = {}
    artist_plays: dict[str, float] = {}
    artist_skips: dict[str, float] = {}
    all_plays = all_skips = 0.0
    for rank, play in enumerate(play_rows):
        path = play["path"]
        last_rank.setdefault(path, rank)
        played_hour = play.get("hour")
        if played_hour is not None and int(played_hour) == current_hour:
            hour_counts[path] = hour_counts.get(path, 0) + 1
            total_at_hour += 1

        fade = 0.5 ** (rank / TASTE_HALF_LIFE)
        skipped = bool(play.get("skipped"))
        if skipped:
            skip_score[path] = skip_score.get(path, 0.0) + fade
        elif _heard_through(play):
            heard_score[path] = heard_score.get(path, 0.0) + fade
        all_plays += fade
        all_skips += fade if skipped else 0.0
        artist = artist_of.get(path)
        if artist:
            artist_plays[artist] = artist_plays.get(artist, 0.0) + fade
            artist_skips[artist] = artist_skips.get(artist, 0.0) + (fade if skipped else 0.0)

    overall = all_skips / all_plays if all_plays else 0.0

    def artist_skip(artist: str) -> float:
        if not artist or artist not in artist_plays:
            return 0.0
        rate = (artist_skips[artist] + ARTIST_PRIOR * overall) / (
            artist_plays[artist] + ARTIST_PRIOR
        )
        return rate - overall

    tracks = []
    for row in index_rows:
        path = row["path"]
        feature = features.get(path, {})
        # Share of this hour's listening that went to this track, scaled so a
        # track that dominates the hour roughly doubles its chances rather than
        # crowding everything else out.
        affinity = (hour_counts.get(path, 0) / total_at_hour) if total_at_hour else 0.0
        tracks.append(
            Track(
                path=path,
                artist=row.get("artist") or "",
                tempo=feature.get("tempo"),
                energy=feature.get("energy"),
                brightness=feature.get("brightness"),
                music_key=feature.get("music_key") or "",
                last_play_rank=last_rank.get(path),
                hour_affinity=min(affinity * 5.0, 1.0),
                skip_score=skip_score.get(path, 0.0),
                heard_score=heard_score.get(path, 0.0),
                artist_skip=artist_skip(artist_of[path]),
            )
        )
    return tracks


def _artist_key(artist: str) -> str:
    return " ".join(artist.lower().split())


def _heard_through(play: dict) -> bool:
    played, duration = play.get("played_seconds"), play.get("duration")
    return bool(played and duration and played >= HEARD_SHARE * duration)


def plain_shuffle(tracks: list[Track], size: int = 50, seed: int | None = None) -> list[Track]:
    """Uniform random, for the comparison to have something to be compared against."""
    rng = random.Random(seed)
    pool = list(tracks)
    rng.shuffle(pool)
    return pool[:size]
