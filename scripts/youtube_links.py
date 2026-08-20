import csv
import json
import time
import sqlite3
import subprocess

CSV_IN = "Monday.txt"
CSV_OUT = "spotify_tracks_youtube.csv"
DB_PATH = "links_state.db"

NAME_KEYS = ["name", "track name", "title", "track", "song", "название"]
ARTIST_KEYS = ["artists", "artist", "artist name(s)", "artist(s)", "artist name", "исполнитель"]
POS_KEYS = ["position", "#", "index", "n", "no", "№"]


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


def yt_search_url(query: str):
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--no-download",
        "--no-warnings",
        "-j",
        "ytsearch1:" + query,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout.strip().splitlines()[0])
        return data.get("url") or data.get("webpage_url")
    except Exception:
        return None


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
        else:
            yt_url = yt_search_url(query) or ""
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

        print(f"[{idx}/{total}] {'OK  ' if yt_url else 'MISS'} {query}")

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_rows[0].keys())
        writer.writeheader()
        writer.writerows(out_rows)

    found = sum(1 for r in out_rows if r["youtube_url"])
    print(f"\nГотово: {CSV_OUT} | найдено {found} из {total}")


if __name__ == "__main__":
    main()
