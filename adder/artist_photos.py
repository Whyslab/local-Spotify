"""Своя шапка артиста — картинка, которую человек поставил вместо фото из Deezer.

Фото из Deezer бывает старым, чужим (тёзка) или его нет вовсе. Тогда шапку
ставят руками, и она главнее: пока своя лежит здесь, Deezer не спрашивается.

Файл называется хешем имени, а не самим именем: в именах артистов бывают «/»,
«*», «..» и всё, что угодно, и чистить их ради имени файла незачем. Регистр не
важен — «PHARAOH» и «Pharaoh» в тегах один и тот же артист.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from fastapi import HTTPException

from . import config, covers, ingest, runtime

logger = logging.getLogger(__name__)

_MEDIA = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _stem(name: str) -> str:
    return hashlib.sha1(name.strip().lower().encode()).hexdigest()


def _files(name: str) -> list[Path]:
    stem = _stem(name)
    return [runtime.ARTIST_PHOTOS_DIR / f"{stem}.{extension}" for extension in _MEDIA]


def read(name: str) -> tuple[bytes, str] | None:
    """Своя шапка: (байты, тип) или None."""
    for path in _files(name):
        if path.is_file():
            return path.read_bytes(), _MEDIA[path.suffix.lstrip(".")]
    return None


def store(name: str, data: bytes) -> str:
    """Положить свою шапку. Возвращает тип картинки."""
    if not name.strip():
        raise HTTPException(status_code=400, detail="Нет имени артиста")
    if len(data) > config.MAX_COVER_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Картинка {len(data) // 1024} КБ, а можно до {config.MAX_COVER_BYTES // 1024} КБ"
            ),
        )
    extension, media = covers.detect(data)
    runtime.ARTIST_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    # Старая могла быть другого формата — иначе read() нашёл бы её первой.
    for path in _files(name):
        path.unlink(missing_ok=True)
    ingest.write_atomic(runtime.ARTIST_PHOTOS_DIR / f"{_stem(name)}.{extension}", data)
    logger.info(
        "Своя шапка артиста %r (%s, %d байт)", name, media, len(data), extra={"task_id": "system"}
    )
    return media


def delete(name: str) -> None:
    for path in _files(name):
        path.unlink(missing_ok=True)
