"""Картинки из Deezer на диске: обложки «Нового для вас» и фото артистов.

Страница их сама не качает: на айфоне она открыта через Tailscale, и каждый
показ главной тянул бы десяток картинок из чужой сети заново. Служба берёт
картинку один раз и дальше отдаёт с диска — как обложки треков.

Кэш временный: больше `MAX_FILES` картинок не держим, первыми уходят те, что
дольше всех не показывались. Пропавшая картинка просто скачается снова.

Ходим только на CDN Deezer (`*.dzcdn.net`) и только по https, без переходов
по перенаправлениям: адрес приходит со страницы, и иначе служба стала бы
качать что угодно, куда её попросят.
"""

from __future__ import annotations

import hashlib
import logging
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from . import covers, ingest, runtime

logger = logging.getLogger(__name__)

TIMEOUT = 8
# Обложка 500×500 весит 50–100 КБ, фото артиста 1000×1000 — до 300 КБ.
MAX_BYTES = 2 * 1024 * 1024
# Около 60 МБ на диске: главная, все артисты фонотеки и запас.
MAX_FILES = 600


def allowed(url: str) -> bool:
    """Адрес с CDN Deezer — иначе не качаем."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "dzcdn.net" or host.endswith(".dzcdn.net"))


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def _cached(key: str) -> tuple[bytes, str] | None:
    for extension, media in (("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")):
        path = runtime.WEB_COVERS_DIR / f"{key}.{extension}"
        if path.is_file():
            try:
                # Время изменения — «когда показывали последний раз»: по нему
                # чистка выбирает, что выбросить.
                os.utime(path)
                return path.read_bytes(), media
            except OSError:
                return None
    return None


def _trim() -> None:
    try:
        files = [p for p in runtime.WEB_COVERS_DIR.iterdir() if p.is_file()]
    except OSError:
        return
    if len(files) <= MAX_FILES:
        return

    def shown_at(path):
        try:
            return path.stat().st_mtime
        except OSError:  # соседний запрос уже убрал
            return 0.0

    files.sort(key=shown_at)
    # Срезаем с запасом, чтобы не чистить на каждой следующей картинке.
    for path in files[: len(files) - MAX_FILES * 9 // 10]:
        path.unlink(missing_ok=True)


def fetch(url: str) -> tuple[bytes, str] | None:
    """Картинка по адресу Deezer: (байты, тип) или None, если её не достать."""
    if not url or not allowed(url):
        return None
    key = _key(url)
    cached = _cached(key)
    if cached is not None:
        return cached

    try:
        response = httpx.get(url, timeout=TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        logger.info("картинка Deezer %s: %s", url, exc)
        return None
    if not response.is_success:
        return None
    data = response.content
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        extension, media = covers.detect(data)
    except HTTPException:
        return None

    try:
        runtime.WEB_COVERS_DIR.mkdir(parents=True, exist_ok=True)
        ingest.write_atomic(runtime.WEB_COVERS_DIR / f"{key}.{extension}", data)
        _trim()
    except OSError as exc:
        # Не записалось — покажем и так, просто в следующий раз скачаем снова.
        logger.warning("картинка Deezer не легла на диск: %s", exc)
    return data, media
