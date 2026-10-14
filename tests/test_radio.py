"""Радио: бесконечная очередь от трека или артиста."""

import threading
import time

import pytest

from adder import outside, runtime

KEY = "0123456789abcdef"


# ---------------------------------------------------------------------------
# Скачивания со стороны: не больше K в час и не во время своих задач
# ---------------------------------------------------------------------------


@pytest.fixture
def running():
    """Как в test_outside.py: поток скачивания живёт, пока идёт тест, и
    останавливается в конце — иначе он доставался бы следующему тесту."""
    runtime.shutdown_event.clear()
    yield
    worker = outside._worker
    if worker is not None and worker.is_alive():
        runtime.shutdown_event.set()
        worker.join(timeout=5)
        assert not worker.is_alive(), "the outside fetch thread did not stop"
    outside._worker = None
    runtime.shutdown_event.clear()
    runtime.PROCESSING_URLS.clear()


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def remember(*keys):
    outside.remember(
        [{"key": key, "artist": "Markul", "title": key, "duration": 180} for key in keys]
    )


def fake_fetch(fetched):
    def fetch(key):
        fetched.append(key)
        outside._audio_path(key).write_bytes(b"audio")
        outside._update_meta(key, status="ready", fetched=time.time())

    return fetch


def test_no_more_than_the_hourly_limit_is_fetched(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "FETCHES_PER_HOUR", 3)
    keys = [f"{i:016x}" for i in range(5)]
    remember(*keys)

    outside.request(keys)
    assert wait_for(lambda: not outside._pending)

    assert len(fetched) == 3
    # Не скачанное сверх лимита — не неудача: его попросят снова, когда час
    # освободится, и оно скачается.
    assert sorted(outside.status(k) for k in keys) == ["ready"] * 3 + ["wanted"] * 2


def test_the_hour_frees_up_as_old_fetches_age(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "FETCHES_PER_HOUR", 1)
    outside._fetch_times.append(time.monotonic() - 3601)
    remember(KEY)

    outside.request([KEY])
    assert wait_for(lambda: outside.status(KEY) == "ready")


def test_nothing_is_fetched_while_the_users_own_downloads_run(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "USER_WAIT_STEP", 0.02)
    runtime.PROCESSING_URLS.add("https://www.youtube.com/watch?v=users_own")
    remember(KEY)

    outside.request([KEY])
    time.sleep(0.3)
    assert fetched == []
    assert outside.status(KEY) == "pending"

    runtime.PROCESSING_URLS.clear()
    assert wait_for(lambda: fetched == [KEY])


def test_one_fetch_at_a_time(monkeypatch, running):
    """Больше двух разом нельзя по плану; поток скачивания один — значит, один."""
    inside = 0
    most = 0
    lock = threading.Lock()

    def slow_fetch(key):
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
        time.sleep(0.05)
        with lock:
            inside -= 1

    monkeypatch.setattr(outside, "fetch", slow_fetch)
    keys = [f"{i:016x}" for i in range(4)]
    remember(*keys)
    outside.request(keys[:2])
    outside.request(keys[2:])
    assert wait_for(lambda: not outside._pending)
    assert most == 1

