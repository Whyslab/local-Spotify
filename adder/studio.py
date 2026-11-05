"""Студийная версия трека: YouTube Music вместо клипа.

Звук фонотеки берётся с YouTube, а там у одной песни много роликов: клип,
премьера на канале артиста, чужие загрузки, обзоры. Замер 05.11.2026 на
выборке из 40 треков фонотеки: только 18 скачаны со студийной записи, у
остальных источник — клип или чужая загрузка; ещё семь «треков» по 8–39 минут
оказались целыми альбомами, «реакциями и разборами» и даже обзором банковского
приложения «Rocket Money». У клипа бывают вставки, звуки сцены и заглушённые
слова; у студийной записи их нет.

YouTube Music сам делит ролики на типы, и студийная запись там — это тип
``MUSIC_VIDEO_TYPE_ATV`` («песня»): звук, который выпустил дистрибьютор, с
пометкой «18+» (``isExplicit``), если в нём есть мат. У одной песни бывают две
такие записи — с матом и чистая (запиканная); тогда берётся первая. Это
проверено звуком: у «Export» (Baby Melo) чистая версия расходится с версией
«18+» ровно в трёх местах, и в них голос тише на 7–12 дБ.

Решения пользователя 05.11.2026:

* ссылка на клип → качается студийная версия, в карточке задачи сказано, что
  её подменили; студийной нет — качается клип с пометкой;
* чужая загрузка (не клип) остаётся как прислана: такие пользователь
  добавляет сам, и там часто как раз версия без цензуры;
* у студийной записи есть версия «18+» — берётся она.

Любой сбой YouTube Music здесь — «не знаю» (None или пустой список), а не
ошибка: вызывающий тогда делает то же, что делал до этого модуля.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from . import outside, similar

logger = logging.getLogger(__name__)

STUDIO = "MUSIC_VIDEO_TYPE_ATV"
OFFICIAL_VIDEO = "MUSIC_VIDEO_TYPE_OMV"

SEARCH_LIMIT = 10
# Версии «18+» и чистая — один мастер с заглушёнными словами: длина у них
# совпадает до секунды («Export»: 104 и 104 с).
TWIN_SECONDS = 3
# Клип бывает длиннее трека на целую сцену («TOKYO»: клип 465 с, трек 161 с),
# но короче него почти не бывает. Студийная запись намного длиннее клипа —
# скорее другой трек с тем же названием, чем тот же.
LONGER_THAN_CLIP_SECONDS = 30
# Все запросы к YouTube Music из всех потоков — не чаще этого.
MIN_INTERVAL = 0.4

# Пометки клипа в названии ролика. Клип артиста на его же канале YouTube Music
# часто числит обычным роликом (UGC), а не «официальным видео» — «DEVO4KA
# (Official Video, 2024)», «ДА/НЕТ (Official Glamour Video)». «Lyric video»,
# «visualizer», «премьера трека» и «audio» — не клип: там звук трека.
_CLIP = re.compile(
    r"official\b[^()\[\]|]{0,25}\bvideo|music\s+video|\bmv\b|\bm/v\b|видеоклип|клип"
    r"|[(\[]\s*video\s*[)\]]",
    re.IGNORECASE,
)

# Слова, которые делают из песни другую запись. Подходят, только если они есть
# и в названии самой песни: «Live Forever» — не концерт.
_OTHER_VERSION = re.compile(
    r"\b(live|concert|концерт\w*|karaoke|караоке|cover|кавер|remix|ремикс|rmx|sped\s+up"
    r"|speed\s+up|slowed|reverb|nightcore|8d|instrumental|минус|acoustic\w*|акустик\w*"
    r"|bass\s+boosted|mashup|мэшап|radio\s+edit|clean|censored|version|версия|demo|демо"
    r"|snippet|сниппет|extended|ремастер)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Song:
    """Студийная запись на YouTube Music."""

    video_id: str
    title: str
    artists: tuple[str, ...]
    duration: float | None
    explicit: bool
    album: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


@dataclass(frozen=True)
class Choice:
    """Что качать вместо присланной ссылки. ``song`` None — качать ссылку как
    есть; ``note`` — что сказать в карточке задачи (может быть и без замены)."""

    song: Song | None
    note: str
    # Что прислано: «клип» или «чистая студийная» — для заметки, если замена
    # не скачается.
    sent: str = "клип"


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()
_pace_lock = threading.Lock()
_last_call = 0.0


def _ytmusic():
    global _client
    with _client_lock:
        if _client is None:
            from ytmusicapi import YTMusic

            # Своя сессия ytmusicapi ждёт ответа до 30 с — этого хватает.
            _client = YTMusic(language="en")
        return _client


def _paced(call):
    global _last_call
    with _pace_lock:
        wait = _last_call + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    return call(_ytmusic())


def _song(item: dict) -> Song | None:
    video_id = item.get("videoId")
    if not video_id or not isinstance(video_id, str):
        return None
    if item.get("videoType") not in (None, STUDIO):
        return None
    if item.get("isAvailable") is False:
        return None
    duration = item.get("duration_seconds")
    album = item.get("album") or {}
    return Song(
        video_id=video_id,
        title=str(item.get("title") or ""),
        artists=tuple(
            str(a.get("name") or "") for a in item.get("artists") or [] if isinstance(a, dict)
        ),
        duration=float(duration) if isinstance(duration, int | float) else None,
        explicit=bool(item.get("isExplicit")),
        album=str(album.get("name") or "") if isinstance(album, dict) else "",
    )


def search(artist: str, title: str, limit: int = SEARCH_LIMIT) -> list[Song]:
    """Студийные записи по артисту и названию, в порядке выдачи YouTube Music
    (пусто — нет или YouTube Music не ответил)."""
    return _search(f"{similar.primary(artist)} {title}".strip(), limit) or []


def search_query(query: str, limit: int = 3) -> list[Song]:
    """Студийные записи по запросу человека — те, чьи название и артист
    целиком есть в запросе: на «Монеточка» чужие песни первыми не встанут, на
    «Adele Love» — чужая «Love». Версия «18+» раньше чистой."""
    words = outside._latin_words(query)
    fitting = [
        song
        for song in _search(query, SEARCH_LIMIT) or []
        if (key := _title_key(song.title))
        and key <= words
        and same_version(song.title, query)
        and any((a := outside._latin_words(name)) and a <= words for name in song.artists)
    ]
    fitting.sort(key=lambda song: not song.explicit)
    return fitting[:limit]


def _search(query: str, limit: int) -> list[Song] | None:
    """Выдача YouTube Music; None — не ответил (а не «ничего нет»)."""
    if not query:
        return []
    try:
        found = _paced(lambda yt: yt.search(query, filter="songs", limit=limit))
    except Exception as exc:  # noqa: BLE001 — сбой = «не знаю»
        logger.info("YouTube Music: поиск %r не удался: %s", query, exc)
        return None
    songs = []
    for item in found or []:
        song = _song(item) if isinstance(item, dict) else None
        if song is not None:
            songs.append(song)
    return songs


def describe(video_id: str) -> dict | None:
    """Тип ролика по YouTube Music (STUDIO, OFFICIAL_VIDEO, …) и его название;
    None — не узнали."""
    try:
        details = _paced(lambda yt: yt.get_song(video_id)).get("videoDetails") or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("YouTube Music: ролик %s не описан: %s", video_id, exc)
        return None
    video_type = details.get("musicVideoType")
    return {
        "type": video_type if isinstance(video_type, str) else None,
        "title": str(details.get("title") or ""),
    }


def kind(video_id: str) -> str | None:
    """Тип ролика по YouTube Music, None — не узнали."""
    described = describe(video_id)
    return described["type"] if described else None


# ---------------------------------------------------------------------------
# Сравнение названий
# ---------------------------------------------------------------------------


def looks_like_clip(video_title: str) -> bool:
    title = video_title or ""
    return bool(_CLIP.search(title)) and not _NOT_A_CLIP.search(title)


# «Official Lyric Video», «Official Visualizer» — под звук самого трека.
_NOT_A_CLIP = re.compile(
    r"\blyrics?\s+video|lyric\s+clip|visuali[sz]er|текст\s+песни", re.IGNORECASE
)


def _title_key(title: str) -> frozenset[str]:
    """Слова названия без скобок, гостей и хвоста после « - »: «Gulyayu -
    Slowed» и «Сон (Acoustic Version)» сравниваются по «Gulyayu» и «Сон», а
    другая версия отсекается отдельно (_OTHER_VERSION)."""
    bare = outside.core_title(title or "")
    bare = re.sub(r"[(\[][^)\]]*[)\]]", " ", bare)
    bare = re.split(r"\s[-–—|]\s", bare, maxsplit=1)[0]
    return frozenset(outside._latin_words(bare))


def _other_versions(text: str) -> set[str]:
    return {
        re.sub(r"\s+", " ", m.group(0).lower()).replace("speed up", "sped up")
        for m in _OTHER_VERSION.finditer(text or "")
    }


# Слова, без которых запись остаётся той же: «Acoustic Version» и «Acoustic» —
# одна версия, а «(Clean)» в названии файла — то, от чего и уходим.
_NOT_REQUIRED = {"version", "версия", "clean", "censored"}
_SAME_RECORDING = re.compile(
    r"\b(?:album|single|explicit|original|lp|main)\s+version\b", re.IGNORECASE
)


def same_version(found: str, wanted: str) -> bool:
    """Та же версия песни: у найденной нет лишних пометок («Live», «Remix»),
    и все пометки нужной у неё есть — ремикс по ссылке на клип не меняется на
    оригинал (рецензия 05.11.2026)."""
    # «(Album Version)» у найденной — та же запись; «(Metal Version)» — нет.
    have = _other_versions(_SAME_RECORDING.sub(" ", found or ""))
    need = _other_versions(wanted)
    return not (have - need) and not ((need - _NOT_REQUIRED) - have)


def same_artist(song: Song, artist: str) -> bool:
    """Хоть один артист записи назван в ``artist`` целиком.

    Латиницей: YouTube Music пишет «Eldzhey», «ЕГОР КРИД», а теги — «Элджей»,
    «Егор Крид». По именам, а не по словам: «Drake» — не «Drake Bell», «Lil» —
    не «Lil Peep». В ``artist`` может быть несколько имён («A feat. B», «A, B»).
    """
    # Целиком тоже: «Simon & Garfunkel» — одно имя, а не два. «feat.» и
    # запятая делят всегда, «&», «/», «and» — только если у записи несколько
    # артистов: иначе «Florence» сошёлся бы с «Florence and the Machine».
    splitter = _ARTIST_SPLIT if len(song.artists) > 1 else _FEAT_SPLIT
    parts = [artist or "", *splitter.split(artist or "")]
    names = [outside._latin_words(n) for n in parts]
    names = [n for n in names if n]
    if not names:
        return False
    for name in song.artists:
        words = outside._latin_words(name)
        if words and words in names:
            return True
    return False


_FEAT_SPLIT = re.compile(r"\s*(?:,|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|\+|/|;|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\band\b|\bи\b)\s*",
    re.IGNORECASE,
)


def same_song(song: Song, artist: str, title: str) -> bool:
    """Та же песня: тот же артист, то же название, не другая версия."""
    wanted = _title_key(title)
    if not wanted or _title_key(song.title) != wanted:
        return False
    if not same_version(song.title, title):
        return False
    return same_artist(song, artist)


def explicit_twin(song: Song, songs: list[Song]) -> Song | None:
    """Версия «18+» той же записи, если ``song`` — чистая, а она есть."""
    if song.explicit:
        return None
    key = _title_key(song.title)
    for other in songs:
        if other.video_id == song.video_id or not other.explicit:
            continue
        if _title_key(other.title) != key or not (set(other.artists) & set(song.artists)):
            continue
        if _other_versions(other.title) != _other_versions(song.title):
            continue
        # Длина — единственное, что отличает ту же запись от другой с тем же
        # названием, поэтому без неё близнеца нет.
        if not song.duration or not other.duration:
            continue
        if abs(song.duration - other.duration) > TWIN_SECONDS:
            continue
        return other
    return None


# ---------------------------------------------------------------------------
# Выбор
# ---------------------------------------------------------------------------


def pick(
    songs: list[Song],
    artist: str,
    title: str,
    duration: float | None = None,
    tolerance: float | None = None,
    longest: float | None = None,
) -> Song | None:
    """Лучшая студийная запись из выдачи, или None.

    ``duration`` с ``tolerance`` — длина из каталога (Deezer): она надёжна, и
    запись другой длины не берётся. ``longest`` — длина клипа: он сам может
    быть длиннее трека на сцену, поэтому от него — только верхняя граница.
    Из подходящих — версия «18+», затем ближайшая по длине, затем первая в
    выдаче.
    """
    fitting = []
    for rank, song in enumerate(songs):
        if not same_song(song, artist, title):
            continue
        if (
            duration
            and tolerance is not None
            and (not song.duration or abs(song.duration - duration) > tolerance)
        ):
            continue
        if longest and song.duration and song.duration > longest:
            continue
        off = abs(song.duration - duration) if duration and song.duration else 0.0
        fitting.append((not song.explicit, off, rank, song))
    if not fitting:
        return None
    return min(fitting, key=lambda row: row[:3])[3]


def find(
    artist: str,
    title: str,
    duration: float | None = None,
    tolerance: float | None = None,
    longest: float | None = None,
) -> Song | None:
    """Студийная запись песни на YouTube Music, или None (нет или не ответил)."""
    return pick(search(artist, title), artist, title, duration, tolerance, longest)


def as_search_result(song: Song) -> dict:
    """Студийная запись в виде строки выдачи поиска (sources.search_youtube)."""
    label = "YouTube Music · студийная" + (" · 18+" if song.explicit else "")
    return {
        "url": song.url,
        "title": f"{song.artist} - {song.title}" if song.artists else song.title,
        "channel": label,
        "duration": song.duration,
        "studio": True,
        "explicit": song.explicit,
    }


def for_link(
    video_id: str,
    video_title: str,
    artist: str,
    title: str,
    length: float | None,
) -> Choice | None:
    """Что качать вместо присланного ролика; None — качать его самого.

    Клип (тип «официальное видео» или пометка клипа в названии) меняется на
    студийную запись той же песни. Студийная чистая запись — на свою версию
    «18+», если она есть. Остальное — чужие загрузки, премьеры трека, lyric
    video — остаётся как прислано.
    """
    video_type = kind(video_id)
    if video_type == STUDIO:
        songs = search(artist, title)
        me = next((s for s in songs if s.video_id == video_id), None)
        twin = explicit_twin(me, songs) if me else None
        if twin is None:
            return None
        return Choice(twin, "Взята версия 18+ (без цензуры) вместо чистой", sent="чистая")
    if video_type != OFFICIAL_VIDEO and not looks_like_clip(video_title):
        return None
    longest = length + LONGER_THAN_CLIP_SECONDS if length else None
    found = _search(f"{similar.primary(artist)} {title}".strip(), SEARCH_LIMIT)
    if found is None:
        return None  # YouTube Music не ответил — молча качаем присланное
    song = pick(found, artist, title, longest=longest)
    if song is None:
        return Choice(None, "Студийной версии не нашлось — взят звук клипа")
    return Choice(song, "Взята студийная версия вместо клипа")
