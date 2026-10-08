"""Миниатюры обложек треков: уменьшение, кэш на диске и в памяти, заготовка.

Миниатюра лежит в ``runtime.THUMB_DIR`` под хешем самой обложки и размера —
не пути и времени изменения файла: edit_track возвращает mtime на место, и
заново найденная обложка иначе навсегда показывалась старой миниатюрой.
Заодно треки одного альбома делят одну.

Чтобы тёплый запрос не читал теги, в памяти держится соответствие «отпечаток
файла (mtime, размер, ctime) → имена его миниатюр». ctime меняет любая запись
в файл, даже та, что вернула mtime, — новая обложка даёт новый отпечаток.
"""

import hashlib
import logging
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

from . import ingest, library, runtime

logger = logging.getLogger("adder")

SIZES = (96, 300, 600)
# Строка списка и плитка: их заготавливаем заранее. 600 — большой плеер,
# он один на экране.
PREPARED = (96, 300)
# Холодный список просил сорок миниатюр разом и запускал сорок ffmpeg.
_FFMPEG = threading.BoundedSemaphore(2)
_KNOWN: dict[str, tuple[tuple[int, int, int], dict[int, str] | None]] = {}
_KNOWN_MAX = 50_000
_LOCK = threading.Lock()


def nearest(size: int) -> int:
    """Ближайший из трёх размеров: иначе кэш рос бы на каждый пиксель."""
    return next((s for s in SIZES if s >= size), SIZES[-1])


def _key(art: bytes, size: int) -> str:
    digest = hashlib.sha1(art)
    digest.update(f"|{size}".encode())
    return digest.hexdigest()


def shrink(art: tuple[bytes, str], size: int) -> tuple[bytes, str]:
    """Обложка, уменьшенная до ``size`` по большей стороне, в JPEG.

    ffmpeg — не новая зависимость: без него служба не принимает ни одного
    файла. Не вышло уменьшить — отдаём как есть.
    """
    cached = runtime.THUMB_DIR / f"{_key(art[0], size)}.jpg"
    if cached.is_file():
        return cached.read_bytes(), "image/jpeg"
    with _FFMPEG:
        # Пока ждали очереди, ту же миниатюру мог сделать соседний запрос.
        if cached.is_file():
            return cached.read_bytes(), "image/jpeg"
        try:
            done = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "image2pipe", "-i", "pipe:0",
                    "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                    "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "pipe:1",
                ],
                input=art[0],
                capture_output=True,
                timeout=15,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return art
    if done.returncode != 0 or not done.stdout:
        return art
    runtime.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    ingest.write_atomic(cached, done.stdout)
    return done.stdout, "image/jpeg"


def _stamp(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size, st.st_ctime_ns


def cover(path: Path, size: int) -> tuple[bytes, str] | None:
    """Обложка трека: ``size`` > 0 — миниатюра, 0 — как в файле; None — её нет."""
    if size <= 0:
        return library.embedded_cover(path)
    size = nearest(size)
    stamp = _stamp(path)
    with _LOCK:
        known = _KNOWN.get(str(path))
    if known and known[0] == stamp:
        if known[1] is None:
            return None
        with suppress(OSError):
            return (runtime.THUMB_DIR / f"{known[1][size]}.jpg").read_bytes(), "image/jpeg"
    # Отпечаток снят до чтения: если файл перепишут, пока его читаем, сохранённый
    # окажется старым, и следующий запрос прочтёт файл заново.
    art = library.embedded_cover(path)
    names = {s: _key(art[0], s) for s in SIZES} if art else None
    with _LOCK:
        if len(_KNOWN) >= _KNOWN_MAX:
            _KNOWN.clear()
        _KNOWN[str(path)] = (stamp, names)
    if art is None:
        return None
    return shrink(art, size)


def prepare(path: Path) -> None:
    """Миниатюры строки и плитки — заранее, чтобы первый показ их не ждал."""
    for size in PREPARED:
        cover(path, size)


def prepare_library(root: Path) -> int:
    """Заготовить для всей фонотеки; возвращает, сколько файлов обошли."""
    count = 0
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in library.AUDIO_SUFFIXES or not path.is_file():
            continue
        try:
            prepare(path)
        except OSError as exc:
            logger.warning("Thumbnails for %s: %s", path, exc, extra={"task_id": "system"})
        count += 1
    return count
