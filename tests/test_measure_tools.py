"""scripts/measure_ui.py: the arithmetic and the budget gate, without a browser.

The browser half is what it drives; what decides "the speed targets are met"
is here, and that must not quietly pass a miss.
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_ui.py"
spec = importlib.util.spec_from_file_location("measure_ui", SCRIPT)
assert spec and spec.loader
measure_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure_ui)


def test_measure_parsers():
    got = measure_ui.stats([300.0, 100.0, 200.0, 400.0, 1000.0])
    assert got == {"median": 300.0, "p90": 1000.0, "n": 5, "failed": 0}
    assert measure_ui.stats([]) == {"median": None, "p90": None, "n": 0, "failed": 0}
    assert measure_ui.stats([100.0, None]) == {"median": 100.0, "p90": 100.0, "n": 1, "failed": 1}


def test_budget_gate_fails_on_miss():
    good = {
        "laptop": {"ready_cold": measure_ui.stats([900.0]), "search": measure_ui.stats([100.0])}
    }
    assert measure_ui.misses(good) == []

    slow = {
        "phone": {"ready_cold": measure_ui.stats([1600.0]), "search": measure_ui.stats([100.0])}
    }
    assert measure_ui.misses(slow) == ["phone/ready_cold: 1600 > 1500"]

    # Not measured is a miss too: "no number" must not read as "fast".
    empty = {"laptop": {"covers": measure_ui.stats([])}}
    assert measure_ui.misses(empty) == ["laptop/covers: нет замера"]

    # A run that never saw its result is a miss, not a quietly dropped sample.
    timed_out = {"laptop": {"search": measure_ui.stats([100.0, None])}}
    assert measure_ui.misses(timed_out) == ["laptop/search: 1 прогон(ов) не дождались результата"]

    # One long task while scrolling already breaks "none".
    janky = {"laptop": {"long_tasks": measure_ui.stats([1.0])}}
    assert measure_ui.misses(janky) == ["laptop/long_tasks: 1 > 0"]


def test_budget_report_names_the_miss():
    results = {"laptop": {"search": measure_ui.stats([400.0])}}
    assert measure_ui.misses(results)
    assert measure_ui.table(results).splitlines()[1].split()[:3] == ["laptop", "search", "400"]


def test_token_is_read_like_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# c\nexport API_TOKEN="se cret"\nOTHER=1\n', encoding="utf-8")
    assert measure_ui.read_token(env) == "se cret"
    env.write_text("API_TOKEN=abc  # comment\n", encoding="utf-8")
    assert measure_ui.read_token(env) == "abc"


def test_search_queries_are_short_substrings_that_change_the_result():
    rows = [
        {"title": "Биг бой", "artist": "Платина", "album": "x"},
        {"title": "Always", "artist": "madk1d", "album": "Always"},
        {"title": "Бентли", "artist": "PHARAOH", "album": "y"},
    ]
    qs = measure_ui.search_queries(rows, 20)
    assert len(qs) == 20
    assert all(1 <= len(q) <= 6 for q in qs)
    # The last letter must change what is found, or nothing is redrawn to time.
    assert all(measure_ui._hits(rows, q) != measure_ui._hits(rows, q[:-1]) for q in qs)


def test_smoke_counts_a_missing_cover_as_fine_and_a_missing_page_as_not():
    assert not measure_ui.bad_response(404, "/api/cover")
    assert not measure_ui.bad_response(404, "/api/playlists/%E2%98%85%20X/cover")
    assert not measure_ui.bad_response(404, "/api/artist-photo")
    assert measure_ui.bad_response(404, "/static/app.js")
    assert measure_ui.bad_response(500, "/api/cover")
    assert measure_ui.bad_response(401, "/api/library")
    assert not measure_ui.bad_response(200, "/api/home")
    # Any other refusal is a broken click: a malformed body, a wrong method, a
    # conflict, the rate limit -- the sweep of every control relies on this.
    for status in (400, 405, 409, 422, 429):
        assert measure_ui.bad_response(status, "/api/plays"), status
    assert measure_ui.bad_response(400, "/api/cover")
    # Only reading a picture may find none; uploading or removing one may not.
    assert measure_ui.bad_response(404, "/api/artist-photo", "DELETE")
    assert measure_ui.bad_response(404, "/api/playlists/X/cover", "POST")


def test_window_perf_lines_are_parsed_and_other_output_ignored():
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    import measure_window

    lines = [
        "MPRIS unavailable",
        'PERF {"event": "ready", "ready_ms": 310.5, "wall_ms": 1000.0}',
        "PERF {broken",
        'PERF {"event": "error", "message": "boom"}',
    ]
    events = measure_window.parse_perf(lines)
    assert [e["event"] for e in events] == ["ready", "error"]
    assert measure_window.errors_of(events) == ["boom"]


def test_window_ready_counts_from_page_start():
    """The window's start was measured from the process start, and bare GTK
    with WebKit and an empty page already took 0.6 s of the 0.5 s goal. The
    goal now counts from the page's own start (decision 08.10.2026); the
    process start is reported beside it, without a goal."""
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    import measure_ui
    import measure_window

    events = [{"event": "ready", "ready_ms": 371.0, "wall_ms": 5000.0}]
    assert measure_window.split_start(994.0, events) == (371.0, 623.0)
    results = {
        "window": {
            "ready_warm": measure_ui.stats([371.0]),
            "start_warm": measure_ui.stats([623.0]),
        }
    }
    assert measure_ui.misses(results) == []


def test_phone_latency_reaches_what_the_service_worker_fetches():
    """CDP's latency covers only the page's own requests; the covers the
    service worker fetched for the page came in 7 ms instead of 66 (measured
    08.10.2026). The phone profile now reaches the service through a delay
    proxy, which every request crosses, the worker's too."""
    import socket
    import sys
    import threading
    import time

    sys.path.insert(0, str(SCRIPT.parent))
    import measure_ui

    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def echo():
        conn, _ = server.accept()
        with conn:
            while data := conn.recv(1024):
                conn.sendall(data.upper())

    threading.Thread(target=echo, daemon=True).start()
    proxy = measure_ui.DelayProxy("127.0.0.1", port, one_way_ms=40)
    try:
        assert urlsplit_port(proxy.url) != port
        with socket.create_connection(("127.0.0.1", urlsplit_port(proxy.url))) as conn:
            for word in (b"ping", b"pong"):
                started = time.monotonic()
                conn.sendall(word)
                assert conn.recv(1024) == word.upper()
                assert time.monotonic() - started >= 0.08
    finally:
        proxy.close()
        server.close()


def urlsplit_port(url):
    from urllib.parse import urlsplit

    return urlsplit(url).port
