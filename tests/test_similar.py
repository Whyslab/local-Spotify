"""Разбор ответов Deezer: сбой — это сбой, а не «ничего нет».

Сеть не нужна: httpx.MockTransport отвечает тем, что задано в тесте.
"""

import httpx
import pytest

from adder import similar


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(similar, "MIN_INTERVAL", 0)


def test_a_quota_error_inside_a_200_is_not_an_empty_answer():
    """Превышение квоты Deezer отдаёт с кодом 200 — его нельзя кэшировать как промах."""
    quota = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
    with (
        client(lambda request: httpx.Response(200, json=quota)) as c,
        pytest.raises(similar._Unreachable),
    ):
        similar._find_artist(c, "Markul")


def test_quota_errors_are_not_remembered(monkeypatch, tmp_path):
    quota = {"error": {"code": 4}}
    real = httpx.Client
    monkeypatch.setattr(
        similar.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=quota))),
    )
    assert similar.top_tracks("Markul", tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_artist_is_an_empty_answer():
    with client(lambda request: httpx.Response(200, json={"data": []})) as c:
        assert similar._find_artist(c, "Никто") is None


def test_of_two_namesakes_the_one_people_listen_to_wins():
    found = {
        "data": [
            {"id": 1, "name": "Pharaoh", "nb_fan": 15},
            {"id": 2, "name": "Pharaoh", "nb_fan": 89452},
            {"id": 3, "name": "Pharaoh Sanders", "nb_fan": 999999},
        ]
    }
    with client(lambda request: httpx.Response(200, json=found)) as c:
        assert similar._find_artist(c, "Pharaoh")["id"] == 2
