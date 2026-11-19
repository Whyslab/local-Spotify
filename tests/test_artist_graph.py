"""Граф артистов (adder/artist_graph.py): источники, свои сессии, ночной проход."""

import json

import httpx
import pytest

from adder import artist_graph


def test_names_compare_without_case_and_punctuation():
    assert artist_graph.key("  PHARAOH ") == "pharaoh"
    assert artist_graph.key("Ёлка") == "елка"
    assert artist_graph.parts("Скриптонит • 104 feat. Truwer") == ["скриптонит", "104", "truwer"]


def test_edges_are_both_ways_and_scaled():
    data = {
        "artists": {
            "pharaoh": {
                "lb": [{"name": "Boulevard Depo", "score": 200}, {"name": "ЛСП", "score": 100}],
                "deezer": ["Kizaru", "OG Buda", "SALUKI"],
            }
        }
    }
    edges = artist_graph.edges_from(data)
    assert edges["pharaoh"]["boulevard depo"] == 1.0
    assert edges["лсп"]["pharaoh"] == 0.5
    assert edges["kizaru"]["pharaoh"] == 1.0
    assert edges["pharaoh"]["saluki"] == 0.5


def test_own_sessions_count_only_heard_neighbours():
    artist_of = {"a": "A", "b": "B", "c": "C", "d": "D"}
    plays = [
        {"path": "a", "at": 0, "skipped": 0},
        {"path": "b", "at": 200, "skipped": 0},  # a—b: пара
        {"path": "c", "at": 400, "skipped": 1},  # пропуск: ни b—c, ни c—d
        {"path": "d", "at": 600, "skipped": 0},
        {"path": "a", "at": 10_000, "skipped": 0},  # пауза: d—a не пара
        {"path": "b", "at": 10_200, "skipped": 0, "ordered": 1},  # по порядку — не вкус
    ]
    edges = artist_graph.own_edges(plays, artist_of)
    assert edges == {
        "a": {"b": 1 / (1 + artist_graph.OWN_HALF)},
        "b": {"a": 1 / (1 + artist_graph.OWN_HALF)},
    }


def test_similarity_reads_the_file_and_the_journal(monkeypatch, tmp_path):
    artist_graph.GRAPH_FILE.write_text(
        json.dumps({"artists": {"pharaoh": {"lb": [{"name": "Boulevard Depo", "score": 1}]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(artist_graph, "_own", lambda: {"x": {"y": 0.4}})
    sim = artist_graph.similarity_function()
    assert sim(["PHARAOH"], ["boulevard depo"]) == 1.0
    assert sim(["X"], ["Y"]) == 0.4
    assert sim(["X • PHARAOH"], ["pharaoh"]) == 1.0
    assert sim(["nobody"], ["else"]) == 0.0


ROWS = [
    {"path": "p/1.m4a", "artist": "PHARAOH", "title": "Champagne Squirt"},
    {"path": "n/1.m4a", "artist": "Nobody", "title": "Track"},
]


def _transport(calls, fail_mb=0):
    state = {"mb_fail": fail_mb}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host = request.url.host
        if host == "musicbrainz.org":
            if state["mb_fail"]:
                state["mb_fail"] -= 1
                return httpx.Response(503, json={"error": "busy"})
            query = request.url.params["query"]
            if "PHARAOH" in query:
                return httpx.Response(
                    200,
                    json={
                        "recordings": [
                            {"artist-credit": [{"artist": {"id": "mbid-ph", "name": "PHARAOH"}}]}
                        ]
                    },
                )
            return httpx.Response(200, json={"recordings": []})
        if host == "labs.api.listenbrainz.org":
            return httpx.Response(200, json=[{"name": "Boulevard Depo", "score": 226}])
        if request.url.path == "/search":
            q = request.url.params["q"]
            if q.startswith("PHARAOH"):
                return httpx.Response(
                    200, json={"data": [{"artist": {"id": 10216612, "name": "Pharaoh"}}]}
                )
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/related"):
            return httpx.Response(200, json={"data": [{"name": "Kizaru"}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(artist_graph, "MB_PACE", 0.0)
    monkeypatch.setattr(artist_graph, "OTHER_PACE", 0.0)
    monkeypatch.setattr(artist_graph, "RETRY_PAUSES", [0.0, 0.0])


def test_refresh_fills_the_graph_and_resumes(monkeypatch):
    calls = []
    result = artist_graph.refresh(ROWS, now=1000.0, transport=_transport(calls))
    assert result["asked"] == 2
    data = json.loads(artist_graph.GRAPH_FILE.read_text(encoding="utf-8"))
    ph = data["artists"]["pharaoh"]
    assert ph["mbid"] == "mbid-ph" and ph["deezer_id"] == 10216612
    assert ph["lb"][0]["name"] == "Boulevard Depo" and ph["deezer"] == ["Kizaru"]
    assert data["artists"]["nobody"]["missed"] is True
    # Через день — ни одного запроса: всё свежее, промах ждёт неделю.
    again = []
    artist_graph.refresh(ROWS, now=1000.0 + 86400, transport=_transport(again))
    assert again == []


def test_a_busy_musicbrainz_is_retried_not_recorded_as_unknown():
    calls = []
    artist_graph.refresh(ROWS[:1], now=1.0, transport=_transport(calls, fail_mb=2))
    data = json.loads(artist_graph.GRAPH_FILE.read_text(encoding="utf-8"))
    assert data["artists"]["pharaoh"]["mbid"] == "mbid-ph"


def test_a_network_failure_writes_nothing_for_that_artist():
    calls = []
    artist_graph.refresh(ROWS[:1], now=1.0, transport=_transport(calls, fail_mb=10))
    data = json.loads(artist_graph.GRAPH_FILE.read_text(encoding="utf-8"))
    entry = data["artists"].get("pharaoh", {})
    assert not entry.get("at") and not entry.get("missed")
