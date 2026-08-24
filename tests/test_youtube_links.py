import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_parser():
    spec = importlib.util.spec_from_file_location(
        "youtube_links",
        ROOT / "scripts" / "youtube_links.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_tracks_supports_named_columns(tmp_path):
    parser = load_parser()

    path = tmp_path / "tracks.csv"
    path.write_text(
        "position,track name,artists\n1,Track One,Artist One\n2,Track Two,Artist Two\n",
        encoding="utf-8",
    )

    tracks = parser.load_tracks(path)

    assert tracks == [
        {"position": 1, "name": "Track One", "artists": "Artist One"},
        {"position": 2, "name": "Track Two", "artists": "Artist Two"},
    ]


def test_load_tracks_supports_bom(tmp_path):
    parser = load_parser()

    path = tmp_path / "tracks.csv"
    path.write_text(
        "\ufeffposition,name,artists\n1,Track,Artist\n",
        encoding="utf-8",
    )

    tracks = parser.load_tracks(path)

    assert tracks[0]["name"] == "Track"
    assert tracks[0]["artists"] == "Artist"


def test_similarity_normalizes_common_youtube_words():
    parser = load_parser()

    assert (
        parser.similarity(
            "Artist - Track (Official Video)",
            "Artist - Track",
        )
        == 1.0
    )


def test_score_candidate_penalizes_cover():
    parser = load_parser()

    normal = parser.score_candidate(
        {"title": "Track", "uploader": "Artist"},
        "Artist",
        "Track",
    )

    cover = parser.score_candidate(
        {"title": "Track Cover", "uploader": "Artist"},
        "Artist",
        "Track",
    )

    assert normal > cover


def test_search_retries_after_failure(monkeypatch):
    parser = load_parser()

    calls = {"count": 0}

    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "url": "abc123",
                "title": "Track",
                "uploader": "Artist",
            }
        )

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temporary failure")
        return Result()

    monkeypatch.setattr(parser.subprocess, "run", fake_run)
    monkeypatch.setattr(parser.time, "sleep", lambda _: None)

    result = parser.yt_search_candidates("Artist - Track")

    assert calls["count"] == 2
    assert result[0]["title"] == "Track"


def test_parser_paths_are_absolute():
    parser = load_parser()

    assert parser.CSV_IN.is_absolute()
    assert parser.CSV_OUT.is_absolute()
    assert parser.DB_PATH.is_absolute()
