"""Listens from the web player go to ListenBrainz, queued so none are lost."""

import pytest

from adder import config, db, library, listenbrainz, runtime


class Response:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


@pytest.fixture()
def lb(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(config, "LISTENBRAINZ_TOKEN", "lb-token")
    monkeypatch.setattr(listenbrainz, "_rejected", False)
    db.db_init()
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {
                "path": "A/Singles/B.m4a",
                "artist": "Daft Punk • Pharrell Williams",
                "title": "Get Lucky",
                "album": "Random Access Memories",
                "source": "https://www.youtube.com/watch?v=abc",
            }
        ],
    )
    posts = []

    def post(url, json, headers, timeout):
        posts.append({"url": url, "json": json, "headers": headers})
        return lb.answer

    lb = type("LB", (), {"answer": Response(200), "posts": posts})
    monkeypatch.setattr(listenbrainz.requests, "post", post)
    return lb


@pytest.mark.parametrize(
    ("played", "duration", "counts"),
    [
        (120, 240, True),  # half
        (119, 240, False),
        (240, 3600, True),  # four minutes of a long mix
        (239, 3600, False),
        (20, 25, False),  # too short to be a track
        (None, 200, False),
    ],
)
def test_the_listenbrainz_rule(played, duration, counts):
    assert listenbrainz.counts_as_listen(played, duration) is counts


def test_a_listen_is_queued_then_sent(lb):
    assert listenbrainz.queue_listen("A/Singles/B.m4a", 200, 248)
    assert listenbrainz.status() == "1 waiting"

    assert listenbrainz.send_pending() == 1

    (post,) = lb.posts
    assert post["url"] == "https://api.listenbrainz.org/1/submit-listens"
    assert post["headers"] == {"Authorization": "Token lb-token"}
    assert post["json"]["listen_type"] == "single"
    (listen,) = post["json"]["payload"]
    meta = listen["track_metadata"]
    assert meta["artist_name"] == "Daft Punk, Pharrell Williams"
    assert meta["track_name"] == "Get Lucky"
    assert meta["release_name"] == "Random Access Memories"
    assert meta["additional_info"]["duration_ms"] == 248000
    assert meta["additional_info"]["origin_url"] == "https://www.youtube.com/watch?v=abc"
    assert listenbrainz.status() == "ok"


def test_a_skip_is_not_a_listen(lb):
    assert not listenbrainz.queue_listen("A/Singles/B.m4a", 10, 248)
    assert listenbrainz.send_pending() == 0
    assert lb.posts == []


def test_an_outage_keeps_the_listen_for_later(lb):
    listenbrainz.queue_listen("A/Singles/B.m4a", 200, 248)
    lb.answer = Response(503, "maintenance")

    assert listenbrainz.send_pending() == 0
    assert listenbrainz.pending_count() == 1

    lb.answer = Response(200)
    assert listenbrainz.send_pending() == 1
    assert listenbrainz.pending_count() == 0


def test_no_network_keeps_the_listen_for_later(lb, monkeypatch):
    listenbrainz.queue_listen("A/Singles/B.m4a", 200, 248)

    def down(*args, **kwargs):
        raise listenbrainz.requests.ConnectionError("no route")

    monkeypatch.setattr(listenbrainz.requests, "post", down)

    assert listenbrainz.send_pending() == 0
    assert listenbrainz.pending_count() == 1


def test_a_rejected_token_stops_sending_and_says_so(lb):
    listenbrainz.queue_listen("A/Singles/B.m4a", 200, 248)
    lb.answer = Response(401, "invalid token")

    assert listenbrainz.send_pending() == 0
    assert listenbrainz.status() == "token rejected"
    lb.answer = Response(200)
    assert listenbrainz.send_pending() == 0  # not until a restart
    assert len(lb.posts) == 1


def test_switched_off_without_a_token(lb, monkeypatch):
    monkeypatch.setattr(config, "LISTENBRAINZ_TOKEN", "")

    assert not listenbrainz.queue_listen("A/Singles/B.m4a", 200, 248)
    assert listenbrainz.status() == "off"


def test_the_play_journal_feeds_the_queue(lb, monkeypatch):
    from fastapi.testclient import TestClient

    from adder import app as app_module

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "LIBRARY", runtime.DB_PATH.parent / "library")
    monkeypatch.setattr(listenbrainz, "start", lambda: None)
    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/plays",
            json={
                "path": "A/Singles/B.m4a",
                "played_seconds": 200,
                "duration": 248,
                "skipped": False,
            },
            headers={"Authorization": "Bearer test-secret"},
        )
    assert response.status_code == 200
    assert listenbrainz.pending_count() == 1
