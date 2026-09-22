"""Test-wide setup that has to happen before any test module is imported.

adder.config refuses to import without an API_TOKEN -- deliberately, since the
API is reachable from the LAN and an empty token must fail closed. That means
test modules cannot import adder.* at the top level unless the variable is
already set, which is why this lives in conftest rather than in a fixture.
"""

import os
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
    monkeypatch.setattr(outside, "candidates", lambda *args, **kwargs: [])
    # Очередь скачиваний — состояние модуля; каждому тесту своя.
    monkeypatch.setattr(outside, "_jobs", queue.Queue())
    monkeypatch.setattr(outside, "_pending", set())
