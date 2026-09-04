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
    extra: dict = field(default_factory=dict)


def tempo_distance(first: float | None, second: float | None) -> float:
    """Distance in BPM, counting half and double time as the same tempo."""
    if not first or not second:
        # An unmeasured track is not evidence of a jump; treat it as neutral so
        # a half-analysed library still shuffles sensibly.
        return 0.0
    return min(abs(first * fold - second) for fold in TEMPO_FOLDS)


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
) -> list[Track]:
    """Order tracks so that each one follows plausibly from the one before.

    Greedy with weighted randomness rather than a strict nearest neighbour: a
    strict one produces the same queue every time from the same starting track,
    which is a playlist, not a shuffle.
    """
    if not tracks:
        return []

    rng = random.Random(seed)
    pool = list(tracks)
    queue: list[Track] = []

    # The first track is chosen on standing alone -- coverage, and the hour if
    # there is a journal -- since there is nothing yet for it to follow.
    def opening_weight(track: Track) -> float:
        weight = _coverage_weight(track)
        if use_moment:
            weight *= 1.0 + track.hour_affinity
        # The first track has nothing to follow, so the same reasoning applies
        # to it directly: start where the queue can be steered from.
        if not track.tempo:
            weight *= UNKNOWN_TRANSITION
        return max(weight, 1e-6)

    current = rng.choices(pool, weights=[opening_weight(t) for t in pool], k=1)[0]
    pool.remove(current)
    queue.append(current)

    while pool and len(queue) < size:
        recent_artists = {t.artist for t in queue[-2:] if t.artist}

        # Criterion 24 is a limit, not a preference: neighbours must be within
        # the tolerance. Weighting alone let a 20 BPM jump through -- unlikely
        # is not the same as excluded. Candidates outside the window are cut
        # first, and only if that leaves nothing does the window widen, so a
        # library with a gap in it still produces a queue instead of stopping.
        candidates = [c for c in pool if tempo_distance(current.tempo, c.tempo) <= tempo_tolerance]
        if not candidates:
            nearest = min(tempo_distance(current.tempo, c.tempo) for c in pool)
            candidates = [
                c for c in pool if tempo_distance(current.tempo, c.tempo) <= nearest + 1e-9
            ]
            logger.debug(
                "No track within %.0f BPM of %.0f; stretching to %.0f",
                tempo_tolerance,
                current.tempo or 0,
                nearest,
            )

        weights = []
        for candidate in candidates:
            weight = _transition_weight(current, candidate) * _coverage_weight(candidate)
            if use_moment:
                weight *= 1.0 + candidate.hour_affinity
            if candidate.artist and candidate.artist in recent_artists:
                # Not forbidden outright -- in a library where one artist holds
                # a tenth of the tracks, a hard ban distorts everything after
                # it -- but pushed far enough down to be rare.
                weight *= 0.02
            weights.append(max(weight, 1e-9))

        chosen = rng.choices(candidates, weights=weights, k=1)[0]
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
    "recently" means here.
    """
    features = {row["path"]: row for row in feature_rows}

    last_rank: dict[str, int] = {}
    hour_counts: dict[str, int] = {}
    total_at_hour = 0
    for rank, play in enumerate(play_rows):
        path = play["path"]
        last_rank.setdefault(path, rank)
        played_hour = play.get("hour")
        if played_hour is not None and int(played_hour) == current_hour:
            hour_counts[path] = hour_counts.get(path, 0) + 1
            total_at_hour += 1

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
            )
        )
    return tracks


def plain_shuffle(tracks: list[Track], size: int = 50, seed: int | None = None) -> list[Track]:
    """Uniform random, for the comparison to have something to be compared against."""
    rng = random.Random(seed)
    pool = list(tracks)
    rng.shuffle(pool)
    return pool[:size]
