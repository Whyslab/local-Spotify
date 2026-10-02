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
    assert got == {"median": 300.0, "p90": 1000.0, "n": 5}
    assert measure_ui.stats([]) == {"median": None, "p90": None, "n": 0}


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


def test_search_queries_are_short_substrings_of_titles():
    qs = measure_ui.search_queries(["Биг бой", "Always", "17"], 20)
    assert len(qs) == 20
    assert all(1 <= len(q) <= 6 for q in qs)
    assert all(any(q in w.lower() for w in ["биг", "бой", "always"]) for q in qs)


def test_smoke_counts_a_missing_cover_as_fine_and_a_missing_page_as_not():
    assert not measure_ui.bad_response(404, "/api/cover")
    assert not measure_ui.bad_response(404, "/api/playlists/%E2%98%85%20X/cover")
    assert measure_ui.bad_response(404, "/static/app.js")
    assert measure_ui.bad_response(500, "/api/cover")
    assert measure_ui.bad_response(401, "/api/library")
    assert not measure_ui.bad_response(200, "/api/home")
