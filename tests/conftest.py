"""Test-wide setup that has to happen before any test module is imported.

adder.config refuses to import without an API_TOKEN -- deliberately, since the
API is reachable from the LAN and an empty token must fail closed. That means
test modules cannot import adder.* at the top level unless the variable is
already set, which is why this lives in conftest rather than in a fixture.
"""

import os
import socket
import sys
from pathlib import Path

os.environ.setdefault("API_TOKEN", "test-secret")

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _outside_stays_offline(monkeypatch, tmp_path):
    """Умное перемешивание спрашивает Deezer о новых треках — в тестах нет.

    Иначе любой тест, дёргающий /api/shuffle, ходил бы в сеть и писал в
    настоящий кэш рядом с кодом. Тесты самих треков со стороны возвращают
    настоящий подбор у себя (см. test_outside.py).
    """
    import queue

    from adder import outside, runtime

    monkeypatch.setattr(runtime, "OUTSIDE_DIR", tmp_path / "outside-cache")
    monkeypatch.setattr(runtime, "THUMB_DIR", tmp_path / "thumb-cache")
    monkeypatch.setattr(runtime, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(outside, "candidates", lambda *args, **kwargs: [])
    # Добавление трека измеряет его отдельным процессом и спрашивает Deezer
    # об альбоме — в тестах ни того, ни другого.
    from adder import analysis, enrich, ingest

    monkeypatch.setattr(analysis, "analyse_track", lambda path: None)
    monkeypatch.setattr(enrich, "lookup", lambda artist, title: None)
    monkeypatch.setattr(enrich, "musicbrainz_lookup", lambda artist, title: None)
    # И обход текстов в фоне — тоже сеть.
    from adder import lyrics

    monkeypatch.setattr(lyrics, "start_backfill", lambda: None)
    # Добавление трека сразу спрашивает текст — в тестах каталог «не знает»
    # ничего, а кэш свой у каждого теста.
    monkeypatch.setattr(lyrics, "CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(lyrics, "_ask", lambda *args, **kwargs: {})
    monkeypatch.setattr(lyrics, "_search", lambda *args, **kwargs: {})
    # /health не должен зависеть от того, стоят ли на машине с тестами
    # ffmpeg и Deno (настоящая проверка — в test_dependencies.py).
    monkeypatch.setattr(ingest, "check_dependencies", lambda: {"ffmpeg": "ok", "js_runtime": "ok"})
    # Всплывающие уведомления на рабочем столе — не во время тестов.
    from adder import config

    monkeypatch.setattr(config, "DESKTOP_NOTIFICATIONS", False)
    # И ListenBrainz — только в своих тестах, с подменённым requests.
    monkeypatch.setattr(config, "LISTENBRAINZ_TOKEN", "")
    # Пауза после ограничения YouTube — состояние процесса; каждому тесту своя.
    monkeypatch.setattr(runtime, "_yt_pause_until", 0.0)
    # Очередь скачиваний — состояние модуля; каждому тесту своя.
    monkeypatch.setattr(outside, "_jobs", queue.Queue())
    monkeypatch.setattr(outside, "_pending", set())


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never reach YouTube, Deezer, iTunes, LRCLIB or Navidrome.

    The service swallows network errors on purpose (a failed cover lookup must
    not fail a download), so simply blocking sockets would hide a test that
    forgot to mock something. Every attempted connection is recorded instead,
    and the test that made it fails at teardown, naming the address. Loopback
    stays open for tests that start a local server on purpose.
    """
    attempts = []

    def allowed(sock, address):
        if sock.family == socket.AF_UNIX:
            return True
        host = address[0] if isinstance(address, tuple) else address
        return host in ("127.0.0.1", "::1", "localhost") and not _is_proxy(address)

    def refuse(self, address):
        if allowed(self, address):
            return _real_connect(self, address)
        attempts.append(address)
        raise ConnectionRefusedError(f"network access in tests is blocked: {address}")

    def refuse_ex(self, address):
        if allowed(self, address):
            return _real_connect_ex(self, address)
        attempts.append(address)
        return 111  # ECONNREFUSED

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_ex)

    yield

    assert not attempts, (
        f"test tried to open network connections {attempts}; "
        "mock the call (yt-dlp, enrich, requests, httpx) instead"
    )


def _is_proxy(address) -> bool:
    """An HTTP(S) proxy on loopback still leads to the internet."""
    from urllib.parse import urlparse

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        proxy = urlparse(os.environ.get(name, ""))
        if proxy.port and proxy.port == address[1]:
            return True
    return False
