"""Ways of naming a track that is not in the library yet.

Four of them: a YouTube link (which the rest of the pipeline already handles),
a search phrase, a YouTube playlist, and a Spotify playlist. The last one is
the awkward case and most of this file is about it.
"""

import json
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import config, enrich, ingest, runtime, studio

logger = logging.getLogger(__name__)

SPOTIFY_EMBED = "https://open.spotify.com/embed/playlist/{playlist_id}"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
TIMEOUT = 20

# The embed endpoint stops at 100 entries and does not say how many there are
# in total -- checked against a playlist of 1085: no count field anywhere in
# the page. So a long playlist cannot even be reported as "100 of N"; the
# honest message is that this is as far as the source goes.
SPOTIFY_EMBED_LIMIT = 100

# How close two durations have to be to count as the same recording. Matching
# on title alone pulled in live versions and other people's covers back in
# August; the rule was tightened twice before it held. Three seconds allows for
# a trimmed intro, not for a different arrangement.
DURATION_TOLERANCE_SECONDS = 3

# Сколько студийных записей YouTube Music ставить первыми в выдачу поиска.
STUDIO_RESULTS = 3


@dataclass(frozen=True)
class Candidate:
    """One track named by a source, before it is known to exist on YouTube."""

    artist: str
    title: str
    duration: float | None = None

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces -- for comparison only."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (text or "").lower())
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


# Тот же запрос в ближайшие минуты — из памяти: человек листает выдачу,
# возвращается назад, поправляет одну букву и стирает её.
SEARCH_CACHE_SECONDS = 600
# Поиск внутри службы дольше этого не ждём: зависшее соединение (дома ломается
# IPv6) держало бы запрос минутами, а отдельный процесс снимается по своему
# сроку и при остановке службы.
IN_PROCESS_SECONDS = 12
# Между поисками внутри службы — не меньше секунды: импорт подборки ищет трек
# за треком, и без процесса (3 с на запуск) запросы шли бы к YouTube втрое
# чаще. Как --sleep-requests 1 в ~/.config/yt-dlp/config у отдельного процесса.
SEARCH_GAP_SECONDS = 1.0
_search_gap_lock = threading.Lock()
_last_search = [0.0]


class _RateLimited(Exception):
    """YouTube ответил 429: отдельным процессом спрашивать незачем."""


SEARCH_CACHE_MAX = 200
_search_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_search_cache_lock = threading.Lock()


def search_youtube(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube and return what was found, without downloading anything.

    The point is to let a person choose. Two uploads of the same song differ in
    length, in channel, and in whether they are the studio version -- guessing
    between them is what produced a library full of live takes last time.
    """
    query = (query or "").strip()
    if not query:
        return []
    key = (query.lower(), limit)
    with _search_cache_lock:
        hit = _search_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < SEARCH_CACHE_SECONDS:
        return [dict(row) for row in hit[1]]

    # Студийные записи YouTube Music — первыми и с пометкой (канал «YouTube
    # Music · студийная · 18+»): выбирает человек, но лучшее у него перед
    # глазами. Только те, где в запросе есть и артист, и название: иначе на
    # «Монеточка» первыми встали бы чужие песни.
    # Оба поиска — разом: по очереди это было 0,8 + 3 с (замер 24.11.2026).
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-search") as pool:
        studio_future = pool.submit(studio.search_query, query, limit=STUDIO_RESULTS)
        entries, error = _youtube_entries(query, limit)
        try:
            studio_songs = studio_future.result()
            studio_ok = True
        except Exception as exc:  # noqa: BLE001 — студийные — дополнение к выдаче
            logger.info("Студийные для «%s» не нашлись: %s", query, exc)
            studio_songs, studio_ok = [], False
    studio_first = [studio.as_search_result(song) for song in studio_songs]
    seen = {row["url"].rsplit("=", 1)[-1] for row in studio_first}

    if entries is None:
        if studio_first:
            return studio_first
        raise RuntimeError(error)

    found = list(studio_first)
    for entry in entries:
        video_id = entry.get("id")
        if not video_id or video_id in seen:
            continue
        found.append(
            {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": entry.get("title") or "",
                "channel": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration"),
            }
        )
    # Урезанную выдачу (студийный поиск упал) и пустую не запоминаем: через
    # минуту человек спросит снова и должен получить полную.
    if studio_ok and found:
        with _search_cache_lock:
            if len(_search_cache) >= SEARCH_CACHE_MAX:
                _search_cache.clear()
            _search_cache[key] = (time.monotonic(), [dict(row) for row in found])
    return found


def _youtube_entries(query: str, limit: int) -> tuple[list[dict] | None, str]:
    """Строки выдачи YouTube или (None, причина сбоя)."""
    count = max(1, min(limit, 20))
    if not config.COOKIES_FROM_BROWSER:
        try:
            entries = _search_in_process(query, count)
        except _RateLimited as exc:
            # Пауза для всех вызовов yt-dlp, как у очереди загрузок: следующий
            # поиск пойдёт в run_yt_dlp, а тот её честно выждет.
            runtime.pause_youtube(config.RATE_LIMIT_BACKOFF)
            return None, str(exc)
        if entries is not None:
            return entries, ""
    result = ingest.run_yt_dlp(
        [*ingest.ytdlp_base(), "-J", "--flat-playlist", f"ytsearch{count}:{query}"],
        timeout=90,
    )
    if result.returncode != 0:
        return None, ingest.ytdlp_error(result.stderr)
    payload = json.loads(result.stdout or "{}")
    return list(payload.get("entries") or []), ""


def _search_in_process(query: str, count: int) -> list[dict] | None:
    """Поиск yt-dlp внутри службы, без нового процесса: 0,8–1,2 с вместо 3
    (запуск Python и импорт всех разборщиков каждый раз — замер 24.11.2026).

    None — «не вышло, спроси отдельным процессом»: при паузе YouTube (её
    честно выждет run_yt_dlp), при любой ошибке и при cookies из браузера
    (они настроены только для командной строки). Отдельный процесс остаётся
    и запасом на случай, когда загруженный в память yt-dlp устарел, а
    music-ytdlp-update уже поставил новый.
    """
    if runtime.youtube_pause_left():
        return None
    answer: dict = {}

    def ask() -> None:
        try:
            import yt_dlp

            runtime.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            options = {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "extract_flat": True,
                "skip_download": True,
                "socket_timeout": 10,
                "extractor_retries": 1,
                # ~/.config/yt-dlp/config сюда не читается: --force-ipv4 оттуда
                # (IPv6 дома ломается, и yt-dlp молча висит) — здесь.
                "source_address": "0.0.0.0",
                # Тот же кэш, что у отдельного процесса (ingest.ytdlp_env).
                "cachedir": str(runtime.CACHE_DIR / "yt-dlp"),
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                answer["info"] = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
        except Exception as exc:  # noqa: BLE001 — запасной путь в _youtube_entries
            answer["error"] = exc

    # Свой поток-демон, а не пул: зависший поиск не держит ни этот запрос
    # дольше срока, ни остановку службы (пул ждал бы его на выходе).
    with _search_gap_lock:
        wait = _last_search[0] + SEARCH_GAP_SECONDS - time.monotonic()
        _last_search[0] = time.monotonic() + max(0.0, wait)
    if wait > 0 and runtime.shutdown_event.wait(wait):
        return None
    worker = threading.Thread(target=ask, name="yt-search", daemon=True)
    worker.start()
    deadline = time.monotonic() + IN_PROCESS_SECONDS
    while worker.is_alive() and time.monotonic() < deadline:
        if runtime.shutdown_event.is_set():
            return None
        worker.join(0.2)
    if worker.is_alive():
        logger.info("Поиск YouTube в процессе завис — отдельным процессом")
        return None
    info = answer.get("info")
    if "error" in answer and ingest.classify_error(str(answer["error"])) == "rate_limited":
        raise _RateLimited(str(answer["error"]))
    if "error" in answer or not isinstance(info, dict):
        logger.info(
            "Поиск YouTube в процессе не вышел (%s) — отдельным процессом", answer.get("error")
        )
        return None
    return [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]


def youtube_playlist(url: str) -> list[str]:
    """Every video URL in a YouTube playlist."""
    result = ingest.run_yt_dlp(
        [*ingest.ytdlp_base(), "-J", "--flat-playlist", url],
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(ingest.ytdlp_error(result.stderr))

    payload = json.loads(result.stdout or "{}")
    urls = []
    for entry in payload.get("entries") or []:
        video_id = entry.get("id")
        if video_id:
            urls.append(f"https://www.youtube.com/watch?v={video_id}")
    return urls


def best_youtube_match(candidate: Candidate) -> dict | None:
    """Pick the YouTube result that is the same recording, or admit there is none.

    Three conditions, all required: the artist appears, the title appears, and
    the length is within a few seconds. Any two of them are not enough -- title
    and artist alone match a live version of the same song by the same person.
    """
    # Сначала студийная запись YouTube Music: без звуков клипа и с матом,
    # если есть версия «18+» (studio). Длина — по тем же правилам.
    song = studio.find(
        candidate.artist,
        candidate.title,
        candidate.duration,
        tolerance=DURATION_TOLERANCE_SECONDS if candidate.duration else None,
    )
    if song is not None:
        return studio.as_search_result(song)

    try:
        results = search_youtube(candidate.query, limit=8)
    except Exception as exc:
        logger.info("Search failed for %r: %s", candidate.query, exc)
        return None

    wanted_artist = _normalise(candidate.artist)
    wanted_title = _normalise(candidate.title)

    for result in results:
        haystack = _normalise(f"{result['title']} {result['channel']}")
        if wanted_title and wanted_title not in haystack:
            continue
        if wanted_artist and not any(word in haystack for word in wanted_artist.split()):
            continue
        if (
            candidate.duration
            and result.get("duration")
            and abs(float(result["duration"]) - float(candidate.duration))
            > DURATION_TOLERANCE_SECONDS
        ):
            continue
        return result
    return None


# ---------------------------------------------------------------------------
# Deezer albums: a whole album by link or by name
# ---------------------------------------------------------------------------
#
# The track list comes from Deezer, which knows track order and lengths; each
# track is then found on YouTube by best_youtube_match, the same strict rule the
# Spotify import uses. The album's id travels with every task (album_hint), so
# the tracks are tagged as this album rather than whichever release a per-track
# lookup happens to find first.


def deezer_album_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if (parsed.hostname or "").lower().rstrip(".") not in {"deezer.com", "www.deezer.com"}:
        return None
    match = re.fullmatch(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?album/(\d+)/?", parsed.path)
    return match.group(1) if match else None


def search_albums(query: str, limit: int = 8) -> list[dict]:
    """Albums on Deezer matching free text, for the listener to choose from."""
    query = query.strip()
    if not query:
        return []
    found = enrich._get(
        f"{enrich.DEEZER}/search/album?q={urllib.parse.quote(query)}&limit={max(1, min(limit, 25))}"
    )
    albums = []
    for album in (found or {}).get("data") or []:
        if not album.get("id"):
            continue
        albums.append(
            {
                "id": str(album["id"]),
                "title": album.get("title") or "",
                "artist": (album.get("artist") or {}).get("name") or "",
                "tracks": album.get("nb_tracks"),
                "cover": album.get("cover_medium") or "",
                "type": album.get("record_type") or "",
            }
        )
    return albums


def deezer_album(album_id: str) -> tuple[dict, list[Candidate]]:
    """The album's title and artist, and its tracks in order."""
    album = enrich._get(f"{enrich.DEEZER}/album/{album_id}")
    if not album or "id" not in album:
        raise RuntimeError("Deezer does not know this album")
    listing = enrich._get(f"{enrich.DEEZER}/album/{album_id}/tracks?limit=500")
    tracks = (listing or {}).get("data") or ((album.get("tracks") or {}).get("data") or [])
    candidates = [
        Candidate(
            artist=(track.get("artist") or {}).get("name") or "",
            title=track.get("title") or "",
            duration=track.get("duration"),
        )
        for track in tracks
        if track.get("title")
    ]
    about = {
        "id": str(album["id"]),
        "title": album.get("title") or "",
        "artist": (album.get("artist") or {}).get("name") or "",
    }
    return about, candidates


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


def spotify_playlist_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if (parsed.hostname or "").lower().rstrip(".") not in {"open.spotify.com", "spotify.com"}:
        return None
    match = re.search(r"/playlist/([A-Za-z0-9]+)", parsed.path)
    return match.group(1) if match else None


def spotify_playlist(url: str) -> tuple[list[Candidate], bool]:
    """Read a public Spotify playlist without an API key.

    The Web API is unusable here: every endpoint answers 403 without an active
    Premium subscription on the app owner's account, including reading a public
    playlist. The embed page carries the track list as JSON and needs no
    credentials at all -- but it stops at 100 entries.

    Returns the tracks and whether the list was cut short.
    """
    playlist_id = spotify_playlist_id(url)
    if not playlist_id:
        raise ValueError("Not a Spotify playlist link")

    response = requests.get(
        SPOTIFY_EMBED.format(playlist_id=playlist_id),
        headers={"User-Agent": BROWSER_UA},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise RuntimeError("Spotify changed the embed page; the track list is not where it was")

    def find_track_list(node):
        if isinstance(node, dict):
            if isinstance(node.get("trackList"), list):
                return node["trackList"]
            for value in node.values():
                found = find_track_list(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find_track_list(value)
                if found is not None:
                    return found
        return None

    track_list = find_track_list(json.loads(match.group(1))) or []
    candidates = [
        Candidate(
            artist=entry.get("subtitle") or "",
            title=entry.get("title") or "",
            duration=(entry.get("duration") or 0) / 1000 or None,
        )
        for entry in track_list
        if entry.get("title")
    ]
    return candidates, len(candidates) >= SPOTIFY_EMBED_LIMIT
