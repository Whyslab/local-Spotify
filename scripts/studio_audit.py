#!/usr/bin/env python3
"""Find the library's tracks that came from a clip or a censored upload, and
put the studio recording in their place.

The library's sound comes from YouTube, and a song there has many videos: the
clip, a premiere on the artist's channel, other people's uploads, reviews. A
clip has scenes and sound effects on top of the track, and is often bleeped;
the studio recording on YouTube Music (``adder/studio.py``) has neither and is
marked explicit when it keeps the swearing. A sample of 40 tracks (05.11.2026)
had only 18 from the studio recording, and seven "tracks" of 8 to 39 minutes
were whole albums, reaction videos and a review of a banking app.

Each track is looked up on YouTube Music; unless it already is the studio
recording (and not the clean twin of an explicit one), the studio recording is
downloaded and compared with the file, aligned, a quarter of a second at a
time, in two bands: the voice (300-3400 Hz) and the bass (below 150 Hz).

* A word muted in the file: the voice drops there while the bass stays, or the
  whole mix drops. Measured on «Export» (Baby Melo): the clean release is
  7-12 dB quieter in the voice at exactly three places.
* A word muted in the studio recording: the same, the other way round -- the
  file is the better copy and is kept.
* A clip: extra seconds before or after the song, or the sound differs in many
  places for no muted word («DEVO4KA», 19 seconds: the clip's own mix).
* The wrong video: the file is far longer or shorter than the song.

What happens to each verdict is the user's rule (05.11.2026): clips go,
censored files go, the wrong videos go; other people's uploads -- which the
user adds on purpose, often for an uncensored version -- stay, and where they
differ from the studio recording they are only listed, to be listened to.

Nothing is replaced without ``--apply``; that is the user's other rule: a
report first. The swap is upgrade_audio's: every tag and the cover are carried
over, ReplayGain is measured again, the "added" date is kept, the path stays
(playlists, plays and lyrics need nothing), and the old file is backed up
under BACKUP.

Usage:

    .venv/bin/python scripts/studio_audit.py --limit 20         # a first taste
    .venv/bin/python scripts/studio_audit.py                    # the whole library
    .venv/bin/python scripts/studio_audit.py --report           # report.html again
    .venv/bin/python scripts/studio_audit.py --apply            # swap every "replace"
    .venv/bin/python scripts/studio_audit.py --apply --skip "A/Singles/x.m4a"

``--apply`` leaves other people's uploads (UGC) alone even in a "replace"
group; ``--uploads-too`` swaps them as well.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import upgrade_audio as ua  # noqa: E402  (the swap and the YouTube client)

from adder import censor, config, library, studio  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("studio_audit")

DATA = Path.home() / ".local" / "share" / "local-Spotify" / "studio-audit"
STATE = DATA / "state.json"
BACKUP = DATA / "backup"
WORK = DATA / "work"
KEEP = DATA / "keep"  # studio recordings waiting for --apply
REPORT = DATA / "report.html"
LOCK = DATA / "lock"
RESCAN_PENDING = DATA / "rescan-pending"

RATE = censor.RATE  # the comparison itself lives in adder.censor
SAME_OVERALL = 0.97
EDGE_SECONDS = 1.5  # more than this before or after the song is the clip's
MANY_OTHER_SECONDS = 3.0
MOST_MUTES = 25
SAME_SONG = 0.9  # overall correlation under which it is another mix
WRONG_RATIO = 1.6
# Между скачиваниями. С 3 с YouTube закрыл загрузки на час после ~370
# роликов за 50 минут (06.11.2026) — и службе тоже: адрес у них один.
PAUSE_SECONDS = 15.0

# Words that make a video something other than the song.
_JUNK = re.compile(
    r"реакци|разбор|обзор|review|reaction|интервью|interview|подкаст|podcast|стрим|stream"
    r"|альбом|full\s+album|album\b|ep\b|mixtape|микстейп|сборник|compilation",
    re.IGNORECASE,
)

# What each verdict means, in the order the report shows them.
GROUPS = {
    "censored": ("replace", "Заменить: похоже, в вашем файле заглушены слова"),
    "clip": ("replace", "Заменить: звук из клипа"),
    "wrong": ("replace", "Заменить: не тот ролик (альбом, обзор, другая длина)"),
    "edges": ("replace", "Заменить: лишнее в начале или в конце"),
    "yours-uncensored": ("keep", "Оставить: ваш файл без цензуры, студийная запикана"),
    "listen": ("keep", "Послушать: чужая загрузка отличается от студийной"),
    "not-found": ("keep", "Студийной версии не нашлось"),
    "same": ("keep", "Совпадает со студийной"),
    "studio": ("keep", "Уже студийная запись"),
    "upload": ("keep", "Загружен файлом — не трогается"),
    "failed": ("keep", "Не проверен (сбой)"),
}


# ---------------------------------------------------------------------------
# Comparing the file with the studio recording
# ---------------------------------------------------------------------------


def _decode(path: Path) -> np.ndarray:
    return censor.decode_mono(path, RATE)


def compare(old: Path, new: Path) -> dict:
    """See adder.censor.compare (moved there 19.11.2026: the service uses it too)."""
    return censor.compare(old, new, decode=_decode)


def _seconds(spots: list, kind: str) -> float:
    return sum(end - start for start, end, k in spots if k == kind)


def verdict(clip: bool, compared: dict, explicit: bool = True) -> tuple[str, str]:
    """(group, why) for a track compared with its studio recording.

    Which kind a difference is, is a guess: a clip's own mix also drops the
    voice here and there («DEVO4KA»: 1:23 and 2:18, where the lyrics have no
    swearing). So a clip that differs at all is a clip, and a quiet voice in
    it is only "maybe muted". Someone's upload is replaced only when it looks
    muted against the studio recording, and kept when the studio recording
    looks muted against it.
    """
    spots = compared["spots"]
    muted_file = [s for s in spots if s[2] == "file"]
    muted_studio = [s for s in spots if s[2] == "studio"]
    other = _seconds(spots, "other")
    file_len, studio_len = compared["file_seconds"], compared["studio_seconds"]
    if studio_len and not (1 / WRONG_RATIO <= file_len / studio_len <= WRONG_RATIO):
        return "wrong", f"файл {clock(file_len)}, песня {clock(studio_len)}"
    edges = abs(compared["head"]) > EDGE_SECONDS or abs(compared["tail"]) > EDGE_SECONDS
    edges_why = f"лишнее: в начале {compared['head']:+.1f} с, в конце {compared['tail']:+.1f} с"
    # Another mix all the way through («Манхэттен», 0.52): its quiet places
    # are not muted words.
    different = compared["overall"] < SAME_SONG or len(muted_file) + len(muted_studio) > MOST_MUTES
    if clip and explicit and muted_studio:
        # A clip and an «18+» studio recording quieter somewhere. It used to
        # count as the clip's own ad-lib and the clip was replaced — but since
        # autumn 2025 the «18+» is muted too (drug words, 19.11.2026), and the
        # two cannot be told apart without the lyrics: not replaced
        # automatically, left to listen. Whatever else differs.
        return "listen", "в студийной (18+) тише: " + places(
            muted_studio
        ) + " — может быть заглушено"
    if muted_studio and not muted_file and not different:
        # The studio recording is the muted one and the file keeps the words:
        # even a clip is the better copy then.
        return "yours-uncensored", "в студийной заглушено: " + places(muted_studio)
    if clip:
        if different:
            return "clip", "другой звук по всей песне"
        if muted_file or muted_studio or other >= MANY_OTHER_SECONDS:
            why = f"отличается от студийной в {len(spots)} местах"
            if muted_file:
                why += "; возможно, заглушено: " + places(muted_file)
            return "clip", why
        return ("clip", edges_why) if edges else ("same", "")
    if different:
        return "listen", "другой звук по всей песне"
    if muted_file and not muted_studio:
        return "censored", "похоже, заглушено: " + places(muted_file)
    if muted_studio and not muted_file:
        return "yours-uncensored", "в студийной заглушено: " + places(muted_studio)
    if muted_file and muted_studio:
        return "listen", "голос отличается: у вас тише в " + places(muted_file)
    if other >= MANY_OTHER_SECONDS:
        return "listen", f"отличается в {len(spots)} местах ({clock(other)})"
    return ("edges", edges_why) if edges else ("same", "")


def clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def places(spots: list, limit: int = 6) -> str:
    shown = ", ".join(clock(s[0]) for s in spots[:limit])
    return shown + (f" и ещё {len(spots) - limit}" if len(spots) > limit else "")


# ---------------------------------------------------------------------------
# One track
# ---------------------------------------------------------------------------


def look_up(tags: dict, video_id: str | None, length: float | None = None) -> dict:
    """What YouTube Music says: the file's video type and the studio recording."""
    artist, title = tags["artist"], tags["title"]
    described = studio.describe(video_id) if video_id else None
    video_type = described["type"] if described else None
    songs = studio.search(artist, title)
    me = next((s for s in songs if s.video_id == video_id), None)
    found: dict = {
        "video_type": video_type,
        "video_title": described["title"] if described else "",
    }
    if video_type == studio.STUDIO and me is not None:
        twin = studio.explicit_twin(me, songs)
        if twin is None:
            return {**found, "group": "studio"}
        found.update(clean_studio=True, song=twin)
        return found
    if video_type == studio.STUDIO and me is None:
        return {**found, "group": "studio"}
    # Не намного длиннее файла: «Intro» того же артиста с другого альбома —
    # не эта песня. Цена: файл, обрезанный больше чем в 1,6 раза, выходит
    # «не нашлось», а не «не тот ролик».
    longest = (
        max(length * WRONG_RATIO, length + studio.LONGER_THAN_CLIP_SECONDS) if length else None
    )
    song = studio.pick(songs, artist, title, longest=longest)
    if song is None:
        return {**found, "group": "not-found"}
    found["song"] = song
    return found


def audit_one(
    client, path: Path, tags: dict, video_id: str | None, previous: dict | None = None
) -> dict:
    # До сравнения: правка файла во время него не должна попасть в отметку.
    stamp = list(ua._stamp(path.stat()))
    found = look_up(tags, video_id, tags.get("duration"))
    if "group" in found:
        return {"group": found["group"], "video_type": found["video_type"]}
    song: studio.Song = found.pop("song")
    # --again: та же студийная запись и тот же файл — сравнение уже есть, и
    # качать её снова незачем (лимит YouTube).
    # Но запись к замене должна лежать в KEEP: её могла забрать замена дубля.
    if (
        previous
        and previous.get("video") == song.video_id
        and previous.get("stamp") == stamp
        and (
            GROUPS.get(previous.get("group", ""), ("keep",))[0] != "replace"
            or (KEEP / f"{song.video_id}.m4a").is_file()
        )
    ):
        # Возвращённое вручную — решение владельца; «replaced» — уже сделанная
        # замена (её compared — про старый файл, пересчёт стёр бы историю).
        if (
            "restored" in previous
            or "compared" not in previous
            or previous.get("group") not in GROUPS
        ):
            return {**previous, "rechecked": True}
        # Правила вердикта меняются (19.11.2026): старое «заменить» не должно
        # пережить их — сравнение то же, пересчитать дёшево.
        group, why = verdict(previous.get("clip", False), previous["compared"],
                             previous.get("explicit", False))  # fmt: skip
        return {**previous, "group": group, "why": why, "rechecked": True}
    got = ua.download(client, song.video_id)
    if got is None:
        return {"group": "failed", "why": "студийная запись не скачалась", "video": song.video_id}
    new, video_title = got
    try:
        compared = compare(path, new)
        clip = found["video_type"] == studio.OFFICIAL_VIDEO or studio.looks_like_clip(
            found["video_title"]
        )
        group, why = verdict(clip, compared, song.explicit)
        if GROUPS[group][0] == "replace":
            KEEP.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), KEEP / f"{song.video_id}.m4a")
    finally:
        new.unlink(missing_ok=True)
    return {
        "group": group,
        "why": why,
        "video_type": found["video_type"],
        "video_title": found["video_title"],
        "clean_studio": bool(found.get("clean_studio")),
        "clip": clip,
        "video": song.video_id,
        "studio_title": f"{song.artist} - {song.title}",
        "explicit": song.explicit,
        "compared": compared,
        "stamp": stamp,
    }


def junk_title(path: Path, tags: dict) -> bool:
    return bool(_JUNK.search(tags["title"]) or _JUNK.search(path.stem))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def take_lock():
    import fcntl

    DATA.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def audit(root: Path, state: dict, args: argparse.Namespace) -> int:
    links, uploads = ua.known_links(args.db)
    client = ua.make_client()
    done = stopped = 0
    for path in sorted(root.rglob("*.m4a")):
        relative = str(path.relative_to(root))
        if args.only and relative != args.only:
            continue
        previous = state.get(relative, {}).get("group")
        if previous and previous != "failed" and not args.again:
            continue
        # --again --groups clip,listen: проверить заново только эти группы.
        if args.again and args.groups and previous not in args.groups.split(","):
            continue
        try:
            tags = library.read_tags(path)
        except Exception as exc:
            logger.warning("%s: unreadable for now (%s)", relative, exc)
            continue
        source = tags.get("source") or links.get(relative, "")
        if ua.is_upload(tags, uploads) or source.startswith("file:"):
            state[relative] = {"group": "upload"}
            continue
        if args.limit is not None and done >= args.limit:
            break
        done += 1
        logger.info("%s", relative)
        entry: dict
        try:
            entry = audit_one(client, path, tags, ua.youtube_id(source), state.get(relative))
        except ua.Blocked as exc:
            logger.error("YouTube is refusing requests, stopping here: %s", exc)
            stopped = 2
            break
        except Exception as exc:
            logger.warning("  failed: %s", exc)
            entry = {"group": "failed", "why": str(exc)[:200]}
        entry["junk_title"] = junk_title(path, tags)
        entry["artist"], entry["title"] = tags["artist"], tags["title"]
        state[relative] = entry
        logger.info("  -> %s %s", entry["group"], entry.get("why", ""))
        ua.save_state(state, STATE)
        if "video" in entry and not entry.pop("rechecked", False):
            time.sleep(PAUSE_SECONDS)
    ua.save_state(state, STATE)
    return stopped


def apply(root: Path, state: dict, args: argparse.Namespace) -> int:
    """Swap every "replace" verdict, except the ones named with --skip."""
    # upgrade_audio's swap backs up and marks the rescan in its own folder;
    # this run keeps its own, so its originals are never mixed with those.
    ua.BACKUP, ua.RESCAN_PENDING = BACKUP, RESCAN_PENDING
    skip = set(args.skip or [])
    groups = set(args.groups.split(",")) if args.groups else None
    changed = 0
    # Одна студийная запись на два файла-дубля: удалять её после всех замен.
    spent: set[Path] = set()
    for relative, entry in sorted(state.items()):
        if GROUPS.get(entry.get("group", ""), ("keep",))[0] != "replace" or relative in skip:
            continue
        if groups is not None and entry.get("group") not in groups:
            continue
        if args.only and relative != args.only:
            continue
        if "compared" in entry and "clip" in entry:
            # Вердикт по нынешним правилам, а не по тем, что были при аудите.
            group, why = verdict(entry["clip"], entry["compared"], entry.get("explicit", False))
            if GROUPS.get(group, ("keep",))[0] != "replace":
                logger.warning("%s: now %s (%s), not swapped", relative, group, why)
                continue
        # Чужая загрузка — выбор пользователя, даже если звук из клипа (06.11.2026).
        if entry.get("video_type") == studio.UPLOAD and not args.uploads_too:
            continue
        path, kept = root / relative, KEEP / f"{entry['video']}.m4a"
        if not path.is_file() or not kept.is_file():
            logger.warning("%s: file or studio recording is gone, skipped", relative)
            continue
        if list(ua._stamp(path.stat())) != entry.get("stamp"):
            logger.warning("%s: changed since the audit, skipped (run it again)", relative)
            continue
        staged = WORK / kept.name
        WORK.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kept, staged)
        try:
            ua.swap(
                path,
                staged,
                f"https://www.youtube.com/watch?v={entry['video']}",
                root,
                args.db,
                same_recording=False,
            )
        except Exception as exc:
            logger.warning("%s: not swapped (%s)", relative, exc)
            staged.unlink(missing_ok=True)
            continue
        spent.add(kept)
        entry["group_before"], entry["group"] = entry["group"], "replaced"
        entry["stamp"] = list(ua._stamp(path.stat()))
        state[relative] = entry
        ua.save_state(state, STATE)
        changed += 1
        logger.info("%s: swapped (%s)", relative, entry.get("why", ""))
    for kept in spent:
        kept.unlink(missing_ok=True)
    logger.info("Swapped %d", changed)
    if changed:
        ua.rescan_navidrome()
    return 0


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def report(state: dict) -> str:
    groups: dict[str, list[tuple[str, dict]]] = {}
    for relative, entry in sorted(state.items(), key=lambda kv: kv[0].lower()):
        groups.setdefault(entry.get("group", "failed"), []).append((relative, entry))
    order = [*GROUPS, "replaced"]
    rows = []
    for group in order:
        items = groups.get(group, [])
        if not items:
            continue
        action, title = GROUPS.get(group, ("done", "Уже заменено"))
        rows.append(f'<h2 class="{action}">{html.escape(title)} — {len(items)}</h2>')
        if group in ("same", "studio", "upload"):
            continue
        rows.append("<table><tr><th>Трек</th><th>Что не так</th><th>Студийная</th></tr>")
        for relative, entry in items:
            studio_cell = ""
            if entry.get("video"):
                url = f"https://music.youtube.com/watch?v={entry['video']}"
                mark = " · 18+" if entry.get("explicit") else ""
                studio_cell = (
                    f'<a href="{html.escape(url)}">{html.escape(entry.get("studio_title", ""))}</a>'
                    f"{mark}"
                )
            why = entry.get("why", "") + (
                " · в названии «обзор/альбом»" if entry.get("junk_title") else ""
            )
            rows.append(
                f"<tr><td>{html.escape(relative)}</td><td>{html.escape(why)}</td>"
                f"<td>{studio_cell}</td></tr>"
            )
        rows.append("</table>")
    return "\n".join(rows)


def counts(state: dict) -> dict[str, int]:
    found: dict[str, int] = {}
    for entry in state.values():
        found[entry.get("group", "?")] = found.get(entry.get("group", "?"), 0) + 1
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=ua.REPO / "adder" / "adder.db")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many tracks")
    parser.add_argument("--only", default=None, help="one track, by library-relative path")
    parser.add_argument("--again", action="store_true", help="audit already audited tracks again")
    parser.add_argument("--report", action="store_true", help="only write report.html")
    parser.add_argument("--apply", action="store_true", help="swap every 'replace' verdict")
    parser.add_argument("--skip", action="append", help="a path --apply leaves alone")
    parser.add_argument(
        "--uploads-too", action="store_true", help="--apply swaps other people's uploads too"
    )
    parser.add_argument(
        "--groups",
        default=None,
        help="only these groups, comma-separated (clip,edges): for --apply, or with --again",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    lock = take_lock()
    if lock is None:
        logger.error("Another run is going on (%s); this one stops.", LOCK)
        return 3
    root = args.library or config.LIBRARY
    ua.WORK = WORK  # upgrade_audio's downloads land in this run's folder
    WORK.mkdir(parents=True, exist_ok=True)
    for stale in WORK.glob("*"):
        stale.unlink(missing_ok=True)
    state = ua.load_state(STATE)
    code = 0
    if args.apply:
        code = apply(root, state, args)
    elif not args.report:
        code = audit(root, state, args)
    REPORT.write_text(
        '<!doctype html><meta charset="utf-8"><title>Студийные версии</title>\n' + report(state),
        encoding="utf-8",
    )
    logger.info("Totals: %s", ", ".join(f"{k} {v}" for k, v in sorted(counts(state).items())))
    logger.info("Report: %s", REPORT)
    return code


if __name__ == "__main__":
    sys.exit(main())
