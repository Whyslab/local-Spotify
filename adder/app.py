"""YouTube -> Navidrome: веб-интерфейс + фоновые воркеры."""
import os
import json
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from mutagen.mp4 import MP4, MP4Cover
from pydantic import BaseModel

PROJECT = Path(__file__).resolve().parent
TMP_DIR = PROJECT / "tmp"
LIBRARY = Path(os.environ.get("LIBRARY_PATH", str(Path.home() / "Music" / "Normalized Library")))
DB_PATH = PROJECT / "adder.db"
PORT = int(os.environ.get("PORT", "8787"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))


TASK_QUEUE: queue.Queue = queue.Queue()

# ---------------- SQLite ----------------
def db_exec(sql: str, params=()):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur
    finally:
        con.close()

def db_query(sql: str, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()

def db_init():
    db_exec("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, status TEXT, artist TEXT, title TEXT, error TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime')))""")

def task_update(tid: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    db_exec(f"UPDATE tasks SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            (*fields.values(), tid))

# ---------------- Текст / метаданные ----------------
def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    name = re.sub(r"[\[\]'\"]", "", str(name))
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

JUNK = [
    r"official\s+(music\s+)?(video|audio|lyric\s+video|clip)",
    r"official\s+(video|audio)",
    r"(lyric(s)?\s+video|visuali[sz]er|music\s+video)",
    r"премьера(\s+(трека|клипа))?",
    r"текст\s+песни",
    r"\b(hd|hq|4k|remastered)\b",
]

def clean_title(s: str) -> str:
    for p in JUNK:
        s = re.sub(p, " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—|_,:()")
    return s or "Unknown"

def split_artist_title(meta: dict):
    artist = meta.get("artist") or meta.get("creator") or ""
    title = meta.get("track") or meta.get("title") or "Unknown"
    if not artist and " - " in title:
        artist, title = title.split(" - ", 1)
    if not artist:
        artist = meta.get("uploader", "Unknown Artist")
    artist = artist.split(",")[0].split(" feat")[0].split(" ft")[0]
    return sanitize_filename(artist), sanitize_filename(clean_title(title))

# ---------------- Сеть / yt-dlp ----------------
def yt_meta(url: str) -> dict:
    p = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", url],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    return json.loads(p.stdout)

def yt_download(url: str, vid: str) -> Path:
    p = subprocess.run([sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "m4a",
                        "--audio-quality", "0", "--no-playlist",
                        "-o", str(TMP_DIR / f"{vid}.%(ext)s"), url],
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    target = TMP_DIR / f"{vid}.m4a"
    if not target.exists():
        found = list(TMP_DIR.glob(f"{vid}.*"))
        if not found:
            raise RuntimeError("Файл не найден после скачивания")
        target = found[0]
    return target

def get_hd_cover(artist: str, title: str):
    """Возвращает (bytes, fmt) или (None, None)."""
    try:
        q = f"{artist} {title}".replace(" ", "+")
        r = requests.get(f"https://itunes.apple.com/search?term={q}&limit=1&entity=song", timeout=10)
        if r.ok and r.json().get("resultCount", 0) > 0:
            art = r.json()["results"][0].get("artworkUrl100", "").replace("100x100bb", "3000x3000bb")
            img = requests.get(art, timeout=15)
            if img.ok:
                return img.content, "jpg"
    except Exception:
        pass
    return None, None

def fetch_cover(artist: str, title: str, thumb_url: str | None):
    data, fmt = get_hd_cover(artist, title)
    if data:
        return data, fmt
    if thumb_url:  # fallback: превью YouTube
        try:
            img = requests.get(thumb_url, timeout=15)
            if img.ok:
                return img.content, ("png" if img.content.startswith(b"\x89PNG") else "jpg")
        except Exception:
            pass
    return None, None

def unique_path(base: Path) -> Path:
    p, n = base, 1
    while p.exists():
        p = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return p

# ---------------- Воркер ----------------
def process(tid: int, url: str):
    tmp_file = None
    try:
        task_update(tid, status="downloading")
        meta = yt_meta(url)
        artist, title = split_artist_title(meta)
        tmp_file = yt_download(url, meta["id"])

        task_update(tid, status="tagging", artist=artist, title=title)
        cover, fmt = fetch_cover(artist, title, meta.get("thumbnail"))

        target = unique_path(LIBRARY / artist / "Singles" / f"{title}.m4a")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_file), str(target))
        tmp_file = None

        audio = MP4(target)
        audio["\xa9nam"] = title
        audio["\xa9ART"] = artist
        audio["aART"] = artist
        audio["\xa9alb"] = "Singles"
        if cover:
            fmt_const = MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover, imageformat=fmt_const)]
        audio.save()
        task_update(tid, status="done")
    except Exception as e:
        task_update(tid, status="error", error=str(e)[:300])
    finally:
        if tmp_file and tmp_file.exists():
            tmp_file.unlink()

def worker():
    while True:
        tid, url = TASK_QUEUE.get()
        process(tid, url)
        TASK_QUEUE.task_done()

# ---------------- Web ----------------
app = FastAPI()

class AddRequest(BaseModel):
    links: list[str]

@app.post("/api/add")
def add(req: AddRequest):
    ids = []
    for link in req.links:
        link = link.strip()
        if not link:
            continue
        cur = db_exec("INSERT INTO tasks(url, status) VALUES(?, 'queued')", (link,))
        TASK_QUEUE.put((cur.lastrowid, link))
        ids.append(cur.lastrowid)
    return {"added": ids}

@app.get("/api/tasks")
def tasks():
    return db_query("SELECT * FROM tasks ORDER BY id DESC LIMIT 50")

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Music Adder</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:0 auto;padding:16px}
textarea{width:100%;height:120px;background:#222;color:#eee;border:1px solid #444;border-radius:8px;padding:8px}
button{background:#2f7df6;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;margin-top:8px}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:14px}
td,th{padding:6px 4px;border-bottom:1px solid #333;text-align:left;vertical-align:top}
.done{color:#4caf50}.error{color:#f44336}.queued,.downloading,.tagging{color:#ffb300}
</style></head><body>
<h2>YouTube → Navidrome</h2>
<textarea id=links placeholder="https://www.youtube.com/watch?v=...&#10;https://youtu.be/..."></textarea>
<button onclick=add()>Добавить</button>
<table><thead><tr><th>Статус</th><th>Трек</th><th>URL</th></tr></thead><tbody id=tb></tbody></table>
<script>
async function add(){
  const links=document.getElementById('links').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!links.length)return;
  await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({links})});
  document.getElementById('links').value='';poll();
}
async function poll(){
  const r=await fetch('/api/tasks');const t=await r.json();
  document.getElementById('tb').innerHTML=t.map(x=>
   `<tr><td class=${x.status}>${x.status}</td><td>${x.artist?x.artist+' — ':''}${x.title||''}${x.error?'<br><small>'+x.error+'</small>':''}</td><td><small>${x.url}</small></td></tr>`).join('');
}
setInterval(poll,2000);poll();
</script></body></html>"""

if __name__ == "__main__":
    PROJECT.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    for _ in range(MAX_WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
