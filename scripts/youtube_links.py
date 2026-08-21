import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
from difflib import SequenceMatcher

CSV_IN = "Monday.txt"
CSV_OUT = "spotify_tracks_youtube.csv"
DB_PATH = "links_state.db"

NAME_KEYS = ["name", "track name", "title", "track", "song", "название"]
ARTIST_KEYS = ["artists", "artist", "artist name(s)", "artist(s)", "artist name", "исполнитель"]
POS_KEYS = ["position", "#", "index", "n", "no", "№"]

SEARCH_COUNT = 5
MIN_MATCH_SCORE = 0.55


def pick(row: dict, keys: list[str]) -> str:
    lowered = {(k or "").strip().lower(): k for k in row.keys()}
    for key in keys:
        actual = lowered.get(key)
        if actual and row.get(actual):
            return str(row[actual]).strip()
    return ""


def load_tracks(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(f, dialect=dialect)
        print("Колонки входа:", reader.fieldnames)
        raw_rows = list(reader)

    tracks = []
    for idx, row in enumerate(raw_rows, start=1):
        name = pick(row, NAME_KEYS)
        artists = pick(row, ARTIST_KEYS)
        pos_raw = pick(row, POS_KEYS)

        if not name:
            values = [str(v).strip() for v in row.values() if v and str(v).strip()]
            line = " ".join(values)
            if " - " in line:
                artists, name = (p.strip() for p in line.split(" - ", 1))
            else:
                name = line

        try:
            position = int(pos_raw)
        except (ValueError, TypeError):
            position = idx

        tracks.append({"position": position, "name": name, "artists": artists})

    return tracks


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            position INTEGER PRIMARY KEY,
            youtube_url TEXT,
            status TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\b(official|audio|video|music|lyrics?|visualizer|hd|4k)\b", " ", text)
    text = re.sub(r"\b(feat\.?|ft\.?)\b", " feat ", text)
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def similarity(left: str, right: str) -> float:
    left_n = normalize(left)
    right_n = normalize(right)
    if not left_n or not right_n:
        return 0.0

    sequence = SequenceMatcher(None, left_n, right_n).ratio()
    left_words = set(left_n.split())
    right_words = set(right_n.split())
    overlap = len(left_words & right_words) / max(len(left_words), 1)
    return max(sequence, overlap)


def score_candidate(candidate: dict, artist: str, title: str) -> float:
    candidate_title = candidate.get("title") or ""
    candidate_artist = candidate.get("uploader") or candidate.get("channel") or ""

    title_score = similarity(title, candidate_title)
    artist_score = similarity(artist, candidate_artist)
    score = title_score * 0.65 + artist_score * 0.35

    lowered = candidate_title.lower()
    query_lower = title.lower()
    penalty_words = ("karaoke", "cover", "slowed", "sped up", "8d", "nightcore")
    if any(word in lowered for word in penalty_words):
        score *= 0.70

    for version in ("remix", "live", "acoustic"):
        if version in lowered and version not in query_lower:
            score *= 0.80

    return min(score, 1.0)


def yt_search_candidates(query: str) -> list[dict]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--flat-playlist",
        "--no-download",
        "--no-warnings",
        "-j",
        f"ytsearch{SEARCH_COUNT}:{query}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        candidates = []
        for line in result.stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("url") or data.get("webpage_url"):
                candidates.append(data)
        return candidates
    except (OSError, subprocess.SubprocessError):
        return []


def yt_search_url(artist: str, title: str):
    query = f"{artist} - {title}".strip(" -")
    candidates = yt_search_candidates(query)
    if not candidates:
        return None, 0.0

    ranked = sorted(
        ((score_candidate(candidate, artist, title), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    score, best = ranked[0]
    if score < MIN_MATCH_SCORE:
        return None, score

    return best.get("url") or best.get("webpage_url"), score


def main():
    tracks = load_tracks(CSV_IN)

    if not tracks or not any(t["name"] for t in tracks):
        raise SystemExit("Не удалось распознать треки во входном файле")

    print("Первые запросы:")
    for t in tracks[:3]:
        print("  ", f"{t['artists']} - {t['name']}".strip(" -"))

    conn = init_db()
    out_rows = []
    total = len(tracks)

    for idx, t in enumerate(tracks, start=1):
        position = t["position"]
        query = f"{t['artists']} - {t['name']}".strip(" -")

        existing = conn.execute(
            "SELECT youtube_url FROM links WHERE position = ?", (position,)
        ).fetchone()

        if existing and existing[0]:
            yt_url = existing[0]
            score = 1.0
        else:
            yt_url, score = yt_search_url(t["artists"], t["name"])
            yt_url = yt_url or ""
            conn.execute(
                "INSERT OR REPLACE INTO links (position, youtube_url, status, updated_at) VALUES (?, ?, ?, ?)",
                (position, yt_url, "found" if yt_url else "not_found",
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            time.sleep(1)

        out_rows.append({
            "position": position,
            "name": t["name"],
            "artists": t["artists"],
            "youtube_url": yt_url,
        })

        if yt_url:
            print(f"[{idx}/{total}] OK   score={score:.2f} {query}")
        else:
            print(f"[{idx}/{total}] MISS score={score:.2f} {query}")

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_rows[0].keys())
        writer.writeheader()
        writer.writerows(out_rows)

    found = sum(1 for r in out_rows if r["youtube_url"])
    print(f"\nГотово: {CSV_OUT} | найдено {found} из {total}")


if __name__ == "__main__":
    main()
