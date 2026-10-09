#!/usr/bin/env python3
"""Is there a better *legal, free* copy of the library's music? A one-off check.

Plan 7.6 (stage 4b.3): take 50 tracks -- 30 Russian-language (mostly rap), 10
foreign, 10 old or rare -- and look each one up in sources that allow
downloading for free and legally:

* Internet Archive -- only items that carry a licence (``licenseurl``, i.e.
  Creative Commons or public domain). Unlicensed uploads there are rips, not
  a legal source, and do not count.
* Free Music Archive -- everything there is under a CC licence.
* Bandcamp -- only free or name-your-price downloads. Its search page is
  behind a bot wall; the search box's own suggestion API is not, and track
  pages say whether the download is free. Remixers and cover bands put the
  original artist into ``band_name``, so a Bandcamp hit is only a candidate.
* Jamendo -- its API wants a registered key, so it is looked up by hand (a web
  search per artist) and written into the ``jamendo`` column of the results
  file before ``--verdict``.

A hit counts only when it is *better*: lossless, or AAC >= 256 / MP3 320 with
energy above 19 kHz, and the same performance. That is decided by hand for each
hit (the ``better`` column: yes/no). Go -- worth building a fetcher (4b.4) --
when more than 20 % of the sample has one (>= 11 of 50).

Usage:

    scripts/source_recon.py --pick 50 > sample.tsv      # from the library's tags
    scripts/source_recon.py --check sample.tsv > results.tsv   # network
    scripts/source_recon.py --verdict results.tsv
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from adder.config import LIBRARY
except Exception:  # no API_TOKEN / no .env: the default layout still works
    LIBRARY = Path.home() / "Music" / "Normalized Library"

GROUPS = {"ru": 30, "foreign": 10, "old": 10}
OLD_BEFORE = 2012  # release year; plus RARE below
# Foreign artists in this library (2026-10-09); everyone else after 2012 is Russian-language.
FOREIGN = {
    "21 Savage", "Aitch", "Cavetown", "Central Cee", "Chief Keef", "Destroy Lonely", "Diplo",
    "Dominic Fike", "Drake", "Flipp Dinero", "FloyyMenor", "Ghostemane", "Icona Pop", "Jaxomy",
    "JELEEL!", "Juice WRLD", "Kanye West", "Key Glock", "King Von", "Lil Peep", "Lil Tecca",
    "Lil Texas", "Lil Uzi Vert", "Marshmello", "Niko B", "Playboi Carti", "Rae Sremmurd",
    "Rich Amiri", "Riton", "Sueco", "The Weeknd", "Tion Wayne", "Travis Scott",
    "Twenty One Pilots", "Tyla", "XXXTENTACION", "Yeat", "YNW Melly",
}  # fmt: skip
RARE = {"N1NT3ND0", "Yasuha", "ICE MC"}
GOAL = 0.2  # share of the sample that must have a better copy
FIELDS = ["group", "artist", "title", "path", "archive", "fma", "bandcamp", "jamendo", "better"]
ARCHIVE = "https://archive.org/advancedsearch.php"
FMA = "https://freemusicarchive.org/search"
BANDCAMP = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
FMA_TRACK = r'href="(https://freemusicarchive\.org/music/[^"]+/[^"]+/[^"]+/)"'
UA = {"User-Agent": "local-Spotify source recon (one-off, ~100 requests)"}


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", text.casefold().replace("ё", "е"))


def verdict(rows: list[dict]) -> tuple[str, int, int]:
    """("go" | "no-go", tracks with a better copy, sample size)."""
    better = sum(1 for r in rows if r.get("better") in (True, "yes"))
    total = len(rows)
    return ("go" if better > GOAL * total else "no-go"), better, total


def library_rows(root: Path) -> list[dict]:
    from mutagen import File

    rows = []
    for path in sorted(root.rglob("*.m4a")):
        try:
            tags = File(path, easy=True) or {}
        except Exception:
            continue
        first = {k: str((tags.get(k) or [""])[0]) for k in ("artist", "title", "date")}
        year = first["date"][:4]
        rows.append({
            "artist": first["artist"], "title": first["title"],
            "year": int(year) if year.isdigit() else None,
            "path": str(path.relative_to(root)),
        })  # fmt: skip
    return rows


def group_of(row: dict) -> str:
    if row["artist"] in RARE or (row["year"] and row["year"] < OLD_BEFORE):
        return "old"
    return "foreign" if row["artist"] in FOREIGN else "ru"


def pick(rows: list[dict], seed: int = 409) -> list[dict]:
    """GROUPS[g] tracks per group, at most one per artist where the pool allows."""
    rng = random.Random(seed)
    sample = []
    for group, n in GROUPS.items():
        pool = [r for r in rows if r["artist"] and r["title"] and group_of(r) == group]
        rng.shuffle(pool)
        seen: set[str] = set()
        chosen = []
        for r in pool:
            if r["artist"] not in seen:
                seen.add(r["artist"])
                chosen.append(r)
        chosen += [r for r in pool if r not in chosen]
        sample += [{"group": group, **r} for r in chosen[:n]]
    return sample


def _creators(doc: dict) -> list[str]:
    creator = doc.get("creator") or []
    return [creator] if isinstance(creator, str) else list(creator)


def archive_matches(docs: list[dict], artist: str, title: str) -> list[str]:
    """Archive items by this artist, with this title, under a licence."""
    return [
        d["identifier"]
        for d in docs
        if d.get("licenseurl")
        and any(norm(c) == norm(artist) for c in _creators(d))
        and norm(title) in norm(str(d.get("title", "")))
    ]


def bandcamp_matches(results: list[dict], artist: str, title: str) -> list[str]:
    """Track pages by this artist with this title, from the search suggestions."""
    return [
        r["item_url_path"]
        for r in results
        if r.get("type") == "t"
        and norm(r.get("band_name") or "") == norm(artist)
        and norm(title) in norm(r.get("name") or "")
    ]


def bandcamp_free(html: str) -> bool:
    """A free download, or name-your-price from zero."""
    data = html.replace("&quot;", '"')
    if re.search(r'"freeDownloadPage":"[^"]+"', data):
        return True
    price = re.search(r'"minimum_price":([0-9.]+)', data)
    return price is not None and float(price.group(1)) == 0.0


def bandcamp_search(artist: str, title: str) -> list[str]:
    import requests

    main_artist = re.split(r",| feat\.| & ", artist)[0].strip()
    body = {"search_text": f"{main_artist} {title}", "search_filter": "t",
            "full_page": False, "fan_id": None}  # fmt: skip
    resp = requests.post(BANDCAMP, json=body, headers=UA, timeout=30)
    resp.raise_for_status()
    pages = bandcamp_matches(resp.json()["auto"]["results"], main_artist, title)
    return [url for url in pages if bandcamp_free(_get(url, {}).text)]


def _get(url: str, params: dict):
    import requests

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=UA, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5)
    raise AssertionError("unreachable")


def archive_search(artist: str, title: str) -> list[str]:
    main_artist = re.split(r",| feat\.| & ", artist)[0].strip()
    q = f'creator:("{main_artist}") AND mediatype:audio'
    params = {"q": q, "fl[]": ["identifier", "creator", "title", "licenseurl"],
              "rows": 200, "output": "json"}  # fmt: skip
    docs = _get(ARCHIVE, params).json()["response"]["docs"]
    return archive_matches(docs, main_artist, title)


def fma_matches(html: str, artist: str, title: str) -> list[str]:
    """Track links on an FMA search page whose artist and title match."""
    found = []
    for link in set(re.findall(FMA_TRACK, html)):
        parts = urllib.parse.unquote(link).rstrip("/").split("/")
        if norm(parts[-3]) == norm(artist) and norm(title) in norm(parts[-1]):
            found.append(link)
    return sorted(found)


def fma_search(artist: str, title: str) -> list[str]:
    main_artist = re.split(r",| feat\.| & ", artist)[0].strip()
    html = _get(FMA, {"adv": 1, "quicksearch": f"{main_artist} {title}"}).text
    return fma_matches(html, main_artist, title)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Look for better legal free copies (plan 7.6).")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--pick", type=int, metavar="N", help="draw the sample (N = 50)")
    what.add_argument("--check", type=Path, metavar="SAMPLE", help="search Archive, FMA, Bandcamp")
    what.add_argument("--verdict", type=Path, metavar="RESULTS", help="go / no-go")
    parser.add_argument("--library", type=Path, default=LIBRARY)
    parser.add_argument("--seed", type=int, default=409)
    args = parser.parse_args(argv)
    out = csv.DictWriter(sys.stdout, FIELDS, delimiter="\t", extrasaction="ignore")

    if args.pick:
        sample = pick(library_rows(args.library), args.seed)
        if len(sample) != args.pick:
            print(f"warning: {len(sample)} tracks, not {args.pick}", file=sys.stderr)
        out.writeheader()
        out.writerows(sample)
        return 0

    if args.check:
        with args.check.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        out.writeheader()
        for i, row in enumerate(rows, 1):
            row["archive"] = " ".join(archive_search(row["artist"], row["title"]))
            row["fma"] = " ".join(fma_search(row["artist"], row["title"]))
            row["bandcamp"] = " ".join(bandcamp_search(row["artist"], row["title"]))
            out.writerow(row)
            sys.stdout.flush()
            print(f"{i}/{len(rows)}", file=sys.stderr, flush=True)
            time.sleep(1)  # be gentle with two free services
        return 0

    with args.verdict.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    found = [r for r in rows if any(r.get(k) for k in ("archive", "fma", "bandcamp", "jamendo"))]
    decision, better, total = verdict(rows)
    print(f"found anywhere: {len(found)} of {total}; better: {better} of {total} -> {decision}")
    for r in found:
        print(f"  [{r['group']}] {r['artist']} - {r['title']}: better={r.get('better') or '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
