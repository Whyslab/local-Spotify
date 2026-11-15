"""Deciding between two copies of the same song.

When a download looks like a track already in the library it is stored anyway
and its task carries a warning (see ingest.find_similar_library_track): it may
be the better recording, and which one to keep is the listener's call. This is
where that call is made, with both files side by side.

Keeping one copy moves the other to the trash the way every deletion does, and
first puts the kept copy in its place in every playlist, so a playlist never
ends up pointing at a file that is gone.

The same question is asked of the whole library too (15.11.2026): tracks
already in it that are one song under two names — «Кайен» and «Кайен (1)»,
«Smart» and «Смарт», a clip and a fan-clip — see library_groups().
"""

from __future__ import annotations

import json
import logging
import re
import threading

import mutagen

from . import config, db, enrich, library, lyrics, playlists, runtime

logger = logging.getLogger(__name__)

WARNING_PREFIX = "Похоже на уже имеющийся трек: "


def _similar_path(task: dict) -> str | None:
    # Older tasks have only the warning text; newer ones the column as well.
    if task.get("similar_to"):
        return task["similar_to"]
    warning = task.get("warning") or ""
    return warning[len(WARNING_PREFIX) :] if warning.startswith(WARNING_PREFIX) else None


def _clear(tid: int) -> None:
    db.task_update(tid, warning=None, similar_to=None)


def describe(path: str) -> dict | None:
    """What helps to choose: tags, length, bitrate, size, where it came from."""
    absolute = config.LIBRARY / path
    if not absolute.is_file():
        return None
    row = next((r for r in library.library_index() if r["path"] == path), None) or {}
    bitrate = codec = None
    try:
        info = mutagen.File(absolute).info
        bitrate = round(getattr(info, "bitrate", 0) / 1000) or None
        length = getattr(info, "length", 0) or 0
        if bitrate is None and length > 0:
            # mutagen reports 0 for Opus in MP4; the average is what matters here.
            bitrate = round(absolute.stat().st_size * 8 / length / 1000) or None
        codec = getattr(info, "codec", None) or absolute.suffix.lstrip(".")
    except Exception:
        codec = absolute.suffix.lstrip(".")
    return {
        "path": path,
        "title": row.get("title") or absolute.stem,
        "artist": row.get("artist") or "",
        "album": row.get("album") or "",
        "duration": row.get("duration"),
        "added": row.get("added"),
        "source": row.get("source") or "",
        "bitrate": bitrate,
        "codec": codec,
        "size": absolute.stat().st_size,
    }


def pairs() -> list[dict]:
    """Every unresolved pair whose two files are both still in the library.

    A pair where one side has since been deleted is not a question any more;
    its warning is cleared on the way.
    """
    found = []
    tasks = db.db_query(
        "SELECT id, result_path, warning, similar_to FROM tasks "
        "WHERE status = 'done' AND warning IS NOT NULL AND warning != '' ORDER BY id DESC"
    )
    for task in tasks:
        existing_path = _similar_path(task)
        # Not every warning is about a twin («Исполнитель — название канала…»,
        # «Старая версия оставлена…»): those are not pairs, and clearing them
        # here wiped them before anyone read them.
        if not existing_path:
            continue
        new = describe(task["result_path"]) if task["result_path"] else None
        existing = describe(existing_path) if existing_path else None
        if new is None or existing is None:
            _clear(task["id"])
            continue
        found.append({"task": task["id"], "new": new, "existing": existing})
    return found


def resolve(tid: int, keep: str) -> dict:
    """keep = "new", "existing" or "both". Returns what was done."""
    if keep not in ("new", "existing", "both"):
        raise ValueError("keep must be 'new', 'existing' or 'both'")
    pair = next((p for p in pairs() if p["task"] == tid), None)
    if pair is None:
        raise LookupError("No such duplicate pair")
    if keep == "both":
        _clear(tid)
        return {"kept": [pair["new"]["path"], pair["existing"]["path"]], "removed": None}

    kept = pair[keep]["path"]
    removed = pair["existing" if keep == "new" else "new"]["path"]
    moved = playlists.swap_everywhere(removed, kept)
    left = playlists.still_pointing_at(removed)
    if left:
        # Не удалять то, на что ещё ссылается подборка (см. ingest._apply_replacement).
        raise RuntimeError(f"Подборки {', '.join(left)} не обновились — дубликат оставлен")
    library.delete_track(removed)
    library.carry_history(removed, kept)
    if keep == "existing":
        db.task_update(tid, result_path=kept)
    _clear(tid)
    logger.info(
        "Duplicate resolved: kept %s, removed %s, playlists updated %d",
        kept,
        removed,
        moved,
        extra={"task_id": tid},
    )
    return {"kept": [kept], "removed": removed, "playlists": moved}


# ---------------------------------------------------------------------------
# Одинаковые песни во всей фонотеке
# ---------------------------------------------------------------------------

# Хвосты названий от YouTube, а не от автора. «Speed up», «slowed», «remix»,
# «live» здесь нет нарочно: это другие версии, а не та же запись.
_JUNK = re.compile(
    r"\s*[(\[][^)\]]*\b(lyrics?|audio|official|video|clip|клип|текст|visuali[sz]er"
    r"|prod|feat|ft|remaster(?:ed)?|explicit|clean|премьера|premiere|mv|hd|hq)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_TAIL = re.compile(
    r"\s+[-–—|]\s+(official|lyrics?|audio|video|клип|премьера|prod)\b.*$", re.IGNORECASE
)
# Слова другой версии. Скобка или хвост с ними остаётся в названии, даже если
# рядом мусорное слово: «(Live Video)», «(Sped Up Audio)», «(Remix ft. X)» —
# не та же запись (замечание рецензента 15.11).
_VERSION = re.compile(
    r"\b(remix|rmx|live|sped|speed|slowed|nightcore|acoustic|instrumental|karaoke"
    r"|cover|version|edit|mix|reverb|bass ?boost(?:ed)?|remake|demo|extended|mashup"
    r"|flip|bootleg|vip|rework|ремикс|лайв|акустик\w*|верси\w*|кавер|минус"
    r"|ускорен\w*|замедлен\w*)\b",
    re.IGNORECASE,
)


def _drop_junk(match: re.Match) -> str:
    return match.group(0) if _VERSION.search(match.group(0)) else ""


def _fold(text: str) -> str:
    """Одна запись для «Смарт» и «Smart», «ГЛАМ!» и «glam»: латиницей, без знаков."""
    return enrich.normalize(text).translate(lyrics._LATIN).replace(" ", "")


def _artists(row: dict) -> list[str]:
    names = []
    for joined in (row.get("artist") or "", row.get("albumartist") or ""):
        for part in joined.split("•"):
            names += enrich.split_artists(part.strip())
    return names


def _artist_keys(row: dict) -> set[str]:
    return {_fold(name) for name in _artists(row)} - {""}


def _title_key(row: dict) -> str:
    # Не lyrics.clean_title: тот режет всё после «(prod», и «Song (prod. X)
    # (Remix)» стал бы просто «Song».
    title = _TAIL.sub(_drop_junk, _JUNK.sub(_drop_junk, row.get("title") or ""))
    # «GONE.Fludd, ЛСП — Ути-Пути»: артист, попавший в название из имени ролика.
    for dash in (" — ", " – ", " - "):
        if dash in title:
            head, tail = title.split(dash, 1)
            heads = {_fold(head)} | {_fold(h) for h in re.split(r"\s*[,&•]\s*", head)}
            if tail.strip() and heads & _artist_keys(row):
                title = tail
            break
    return _fold(title)


def _signature(paths) -> str:
    return "\n".join(sorted(paths))


def _dismissed() -> set[str]:
    try:
        data = json.loads(runtime.DUPLICATES_DISMISSED_FILE.read_text(encoding="utf-8"))
        return {str(item) for item in data}
    except (OSError, ValueError, TypeError):
        return set()


def _write_dismissed(signatures: set[str]) -> None:
    target = runtime.DUPLICATES_DISMISSED_FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(signatures), ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _raw_groups(rows: list[dict]) -> list[list[dict]]:
    """Треки с одним названием и хотя бы одним общим артистом — группой.

    Длина не в счёт: клип длиннее студийной записи, а это та же песня.
    """
    by_title: dict[str, list[dict]] = {}
    for row in rows:
        key = _title_key(row)
        if key:
            by_title.setdefault(key, []).append(row)
    groups = []
    for same_title in by_title.values():
        left = list(same_title)
        while len(left) > 1:
            group = [left.pop(0)]
            keys = _artist_keys(group[0])
            grown = True
            while grown:
                grown = False
                for row in list(left):
                    if _artist_keys(row) & keys:
                        group.append(row)
                        keys |= _artist_keys(row)
                        left.remove(row)
                        grown = True
            if len(group) > 1:
                groups.append(group)
    return groups


def _open_groups(rows: list[dict]) -> list[list[dict]]:
    """Группы, о которых ещё не решено.

    Пара, уже показанная как «похожий трек» после скачивания (pairs()), здесь
    не повторяется; «оставить все» убирает группу, пока в ней те же файлы, —
    новый третий экземпляр покажет её снова.
    """
    dismissed = _dismissed()
    task_pairs = _task_pair_signatures()
    return [
        group
        for group in _raw_groups(rows)
        if _signature(row["path"] for row in group) not in dismissed | task_pairs
    ]


def _task_pair_signatures() -> set[str]:
    """Пары «похожий трек» из задач — по одной базе, без чтения файлов и без
    правок в ней (pairs() читает теги и стирает устаревшие предупреждения)."""
    tasks = db.db_query(
        "SELECT result_path, warning, similar_to FROM tasks "
        "WHERE status = 'done' AND warning IS NOT NULL AND warning != ''"
    )
    found = set()
    for task in tasks:
        other = _similar_path(task)
        if other and task["result_path"]:
            found.add(_signature([task["result_path"], other]))
    return found


def open_count(rows: list[dict] | None = None) -> int:
    """Сколько групп ждёт решения — для главной, без чтения файлов."""
    return len(_open_groups(library.library_index() if rows is None else rows))


def library_groups() -> list[dict]:
    """Одинаковые песни, о которых ещё не решено, — с тем, по чему выбирать."""
    found = []
    for group in _open_groups(library.library_index()):
        described = [d for d in (describe(row["path"]) for row in group) if d]
        if len(described) > 1:
            found.append({"tracks": described})
    return found


def _current_group(paths: list[str]) -> list[str]:
    """Группа из этих путей, если она и сейчас такая; иначе LookupError.

    Удалять можно только то, что сам поиск назвал одинаковым: иначе этот
    запрос стирал бы любой трек фонотеки.
    """
    wanted = _signature(paths)
    for group in _raw_groups(library.library_index()):
        if _signature(row["path"] for row in group) == wanted:
            return sorted(paths)
    raise LookupError("Такой группы одинаковых треков нет")


_resolve_lock = threading.Lock()


def keep_one(paths: list[str], keep: str) -> dict:
    """Оставить ``keep``, остальные из группы — в корзину; подборки и история
    прослушиваний переходят к оставленному.

    Сначала подборки всех лишних, потом удаление: если подборка одного не
    обновилась, не удалено ничего — а не половина группы под ответом
    «дубликат оставлен».
    """
    with _resolve_lock:
        group = _current_group(paths)
        if keep not in group:
            raise ValueError("Оставить можно только один из этой группы")
        extra = [path for path in group if path != keep]
        moved = sum(playlists.swap_everywhere(path, keep) for path in extra)
        left = sorted({name for path in extra for name in playlists.still_pointing_at(path)})
        if left:
            raise RuntimeError(f"Подборки {', '.join(left)} не обновились — ничего не удалено")
        for path in extra:
            library.delete_track(path)
            library.carry_history(path, keep)
    logger.info(
        "Library duplicate resolved: kept %s, removed %s, playlists updated %d",
        keep,
        extra,
        moved,
        extra={"task_id": "system"},
    )
    return {"kept": [keep], "removed": extra, "playlists": moved}


def keep_all(paths: list[str]) -> dict:
    """«Оставить все»: больше эту группу не показывать."""
    with _resolve_lock:
        group = _current_group(paths)
        signatures = _dismissed()
        signatures.add(_signature(group))
        _write_dismissed(signatures)
    return {"kept": group, "removed": []}
