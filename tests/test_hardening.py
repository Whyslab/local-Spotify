"""Мелочи из полного разбора проекта 23.09.2026 — каждая когда-то была ошибкой.

Служба слушает сеть общежития, поэтому половина здесь — про то, что видно
без ключа и что происходит с кривым вводом.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, enrich, ingest, library, navidrome, playlists, runtime, signing


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


AUTH = {"Authorization": "Bearer test-secret"}


def test_health_without_a_token_says_only_whether_it_is_alive(client):
    short = client.get("/health").json()
    assert short == {"status": "healthy"}
    full = client.get("/health", headers=AUTH).json()
    assert "library_path" in full and "active_tasks" in full


def test_the_api_map_is_not_published(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_a_non_ascii_token_is_refused_not_a_crash(client):
    response = client.get("/api/playlists", headers={"Authorization": "Bearer ключ".encode()})
    assert response.status_code == 401


def test_a_non_ascii_signature_is_refused_not_a_crash():
    assert signing.verify("a.m4a", 9999999999, "подпись") is False


@pytest.mark.parametrize("bad", ["a.m4a\nb.m4a", "#EXTINF:1,x", "   ", "a\rb"])
def test_a_path_that_would_break_the_m3u_is_refused(client, bad):
    playlists.create("p", ["a.m4a"])
    response = client.put("/api/playlists/p/tracks", headers=AUTH, json={"paths": ["a.m4a", bad]})
    assert response.status_code == 400
    assert [line.path for line in playlists.read("p").entries] == ["a.m4a"]


def test_rename_never_overwrites_an_existing_playlist(client):
    playlists.create("a", ["1.m4a"])
    playlists.create("b", ["2.m4a"])
    with pytest.raises(Exception):  # noqa: B017 — HTTPException 409
        playlists.rename("a", "b")
    assert [line.path for line in playlists.read("b").entries] == ["2.m4a"]
    assert [line.path for line in playlists.read("a").entries] == ["1.m4a"]


def test_a_playlist_file_not_in_utf8_does_not_break_the_list(client):
    playlists.create("ok", ["a.m4a"])
    (config.LIBRARY / "старая.m3u").write_bytes("#EXTM3U\nПесня.m4a\n".encode("cp1251"))
    names = [row["name"] for row in client.get("/api/playlists", headers=AUTH).json()]
    assert names == ["ok"]
    assert client.get("/health", headers=AUTH).status_code == 200


def test_yo_and_ye_are_the_same_letter_when_matching():
    assert enrich.normalize("Ёлка") == enrich.normalize("Елка") == "елка"
    assert enrich.normalize("Мой") == "мои"  # й теряет кратку с обеих сторон одинаково


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("..", "Unknown"),
        ("???", "Unknown"),
        (".hidden", "hidden"),
        ("a\x00b\x1fc", "abc"),
        ("", "Unknown"),
    ],
)
def test_file_names_from_tags_are_safe(raw, expected):
    assert ingest.sanitize_filename(raw) == expected


def test_an_mp3_is_streamed_as_mp3(client, tmp_path):
    track = config.LIBRARY / "A" / "Singles" / "t.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"ID3" + b"\x00" * 100)
    expires = 9999999999
    response = client.get(
        "/api/stream",
        params={
            "path": "A/Singles/t.mp3",
            "exp": expires,
            "sig": signing.sign("A/Singles/t.mp3", expires),
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")


def test_shuffle_size_is_clamped(client, monkeypatch):
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {"path": f"t{i}.m4a", "artist": f"A{i}", "title": "T", "duration": 1} for i in range(10)
        ],
    )
    body = client.get("/api/shuffle", headers=AUTH, params={"size": -1, "mode": "plain"}).json()
    assert len(body["queue"]) == 1


def test_every_route_but_the_public_few_needs_the_token(client):
    """Новый адрес без verify_token — это дыра в сети общежития. Список тех,
    кому можно без ключа, короткий и явный."""
    from fastapi.routing import APIRoute

    from adder import app as app_module

    # /sw.js is the offline worker: page code like /static/, no data in it.
    public = {"/api/stream", "/health", "/", "/sw.js"}
    open_routes = []
    for route in app_module.app.routes:
        if not isinstance(route, APIRoute) or route.path in public:
            continue
        names = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call}
        if "verify_token" not in names:
            open_routes.append(route.path)
    assert open_routes == []


def test_an_oversized_upload_is_refused_before_it_is_read(client, monkeypatch):
    from adder import app as app_module

    monkeypatch.setitem(app_module.BODY_LIMITS, "/api/import", 1000)
    response = client.post("/api/import", files={"files": ("a.mp3", b"x" * 5000, "audio/mpeg")})
    assert response.status_code == 413


def test_a_playlist_cannot_point_outside_the_library():
    from fastapi import HTTPException

    from adder import playlists

    for bad in ("/etc/shadow", "../../../../etc/passwd", "A/../../x.m4a", "A\\..\\x.m4a"):
        with pytest.raises(HTTPException):
            playlists._clean_paths([bad])
    fine = ["A/Singles/Wait....m4a", "B/Singles/..and more.m4a", "outside:0123456789abcdef"]
    assert playlists._clean_paths(fine) == fine


def test_static_files_never_serve_dotfiles(monkeypatch, tmp_path):
    """web/.omc/… (служебные файлы инструментов) отдавались по /static с 200."""
    from adder import app as app_module

    web = tmp_path / "web"
    (web / ".omc").mkdir(parents=True)
    (web / ".omc" / "state.json").write_text("{}")
    (web / ".env").write_text("SECRET=1")
    (web / "app.js").write_text("// ok")
    (web / "sub").mkdir()
    (web / "sub" / ".hidden").write_text("x")
    static = next(r for r in app_module.app.routes if getattr(r, "path", None) == "/static")
    monkeypatch.setattr(static.app, "all_directories", [web])
    client = TestClient(app_module.app)

    assert client.get("/static/app.js").status_code == 200
    for path in (".env", ".omc/state.json", "sub/.hidden", "sub/../.env", "%2eenv"):
        assert client.get(f"/static/{path}").status_code == 404, path


def test_player_events_go_to_the_journal_and_need_the_token(client, caplog):
    """Страница сообщает о паузе, ошибке и застревании: 30.09.2026 музыка
    молча остановилась, и причину было не найти."""
    import logging

    body = {"event": "stall-reload", "path": "A/B.m4a", "at": 12.5, "detail": "попытка 1"}
    assert client.post("/api/player-event", json=body).status_code in (401, 403)
    with caplog.at_level(logging.INFO, logger="adder.app"):
        r = client.post("/api/player-event", json=body, headers=AUTH)
    assert r.status_code == 200
    assert "Player: stall-reload at 12.5s A/B.m4a (попытка 1)" in caplog.text
    bad = client.post("/api/player-event", json={"event": "rm -rf"}, headers=AUTH)
    assert bad.status_code == 422


def test_backup_rotation_removes_wal_and_shm(tmp_path):
    """Old snapshots went, their -wal and -shm stayed behind for good."""
    import shutil
    import sqlite3
    import subprocess

    project = tmp_path / "project"
    (project / "deploy").mkdir(parents=True)
    (project / "adder").mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "deploy" / "backup.sh", project / "deploy")
    con = sqlite3.connect(project / "adder" / "adder.db")
    con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()

    backups = tmp_path / "backups"
    backups.mkdir()
    # Ten older snapshots: the run adds an eleventh, so the oldest one goes.
    for day in range(10, 20):
        (backups / f"adder_202609{day}_030000.db").write_bytes(b"db")
    for side in ("-wal", "-shm"):
        (backups / f"adder_20260910_030000.db{side}").write_bytes(b"")  # goes with its db
        (backups / f"adder_20260901_030000.db{side}").write_bytes(b"")  # db long gone
    (backups / "adder_20260919_030000.db-shm").write_bytes(b"")  # db stays: kept

    subprocess.run(
        ["bash", str(project / "deploy" / "backup.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BACKUP_DIR": str(backups)},
        check=True,
        capture_output=True,
    )

    names = sorted(p.name for p in backups.iterdir())
    assert len([n for n in names if n.endswith(".db")]) == 10
    assert "adder_20260910_030000.db" not in names
    sides = [n for n in names if n.endswith(("-wal", "-shm"))]
    assert sides == ["adder_20260919_030000.db-shm"]


def test_install_env_value_matches_dotenv(tmp_path):
    """install.sh writes LIBRARY_PATH into the unit; it must read .env as the service does.

    It used to strip quotes only: "export X=…" and a trailing "# comment" reached
    the unit and Navidrome's config as part of the path.
    """
    import subprocess
    import sys

    from dotenv import dotenv_values

    install = (Path(__file__).resolve().parent.parent / "deploy" / "install.sh").read_text()
    start = install.index("env_value() {")
    function = install[start : install.index("\n}\n", start) + 3]
    repo = tmp_path / "repo"
    (repo / "adder").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    env = repo / "adder" / ".env"
    env.write_text(
        "export LIBRARY_PATH=/srv/music  # the big disk\n"
        "PORT = '8787'\n"
        'SHUTDOWN_TIMEOUT="30"\n'
        "API_TOKEN=abc#def\n",
        encoding="utf-8",
    )
    expected = dotenv_values(env)
    for key in ("LIBRARY_PATH", "PORT", "SHUTDOWN_TIMEOUT", "API_TOKEN", "MISSING"):
        got = subprocess.run(
            ["bash", "-c", f'REPO="$1"\n{function}\nenv_value {key}', "_", str(repo)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert got == (expected.get(key) or ""), key


@pytest.mark.parametrize(
    "value",
    [
        r'"/srv/a\\b"',  # a backslash: an escape in Navidrome's TOML and the unit
        r'"/srv/a\"b"',
        "/srv/100%",
        r'"/srv/music\n"',  # dotenv turns it into a newline; $(...) would eat it
        r'"/srv/a\nb"',
        # Accepted as they are: a check that refused everything would pass the rest.
        "ok:/srv/Моя музыка & x",
        "ok:/srv/[it];`x` y",
    ],
)
def test_install_refuses_a_library_path_that_breaks_the_configs(tmp_path, value):
    import subprocess
    import sys

    install = (Path(__file__).resolve().parent.parent / "deploy" / "install.sh").read_text()
    start = install.index("env_value() {")
    function = install[start : install.index("\n}\n", start) + 3]
    check_from = install.index('LIBRARY_PATH_VALUE="$(env_value LIBRARY_PATH')
    check = install[check_from : install.index("\nfi\n", check_from) + 4]
    repo = tmp_path / "repo"
    (repo / "adder").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    # Single quotes: dotenv takes the text as it is, with no ${...} expansion.
    raw = f"'{value[3:]}'" if value.startswith("ok:") else value
    (repo / "adder" / ".env").write_text(f"LIBRARY_PATH={raw}\n", encoding="utf-8")
    script = (
        f'set -euo pipefail\nREPO="$1"\n{function}\n{check}\necho "accepted: [$LIBRARY_PATH_VALUE]"'
    )

    run = subprocess.run(["bash", "-c", script, "_", str(repo)], capture_output=True, text=True)

    if value.startswith("ok:"):
        assert run.returncode == 0, run.stderr
        assert run.stdout == f"accepted: [{value[3:]}]\n"
    else:
        assert run.returncode == 1, run.stdout
        assert "LIBRARY_PATH in adder/.env contains" in run.stderr


def test_backup_rotation_ignores_foreign_and_odd_names(tmp_path):
    """Rotation parsed ls: a name with a space could make it delete elsewhere."""
    import os
    import shutil
    import sqlite3
    import subprocess

    project = tmp_path / "project"
    (project / "deploy").mkdir(parents=True)
    (project / "adder").mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "deploy" / "backup.sh", project / "deploy")
    con = sqlite3.connect(project / "adder" / "adder.db")
    con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    backups = tmp_path / "backups"
    backups.mkdir()
    victim = backups / "keep.txt"
    victim.write_text("keep me")
    for i in range(12):
        (backups / f"env_2026090{i % 10}_0{i}0000").write_text("x")
    # Old: ls | xargs split this into "env_x" and "keep.txt" and deleted the latter.
    (backups / "env_x keep.txt").write_text("odd")
    (backups / "env_notes").write_text("mine")
    # Oldest of all, so it is in the tail the rotation deletes.
    for odd in ("env_x keep.txt", "env_notes"):
        os.utime(backups / odd, (1, 1))
    # The old script stopped early without any playlists_* under pipefail.
    (backups / "playlists_20260901_000000.tar.gz").write_bytes(b"")

    run = subprocess.run(
        ["bash", str(project / "deploy" / "backup.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BACKUP_DIR": str(backups)},
        capture_output=True,
    )

    assert victim.exists(), "a file that is not a backup was deleted"
    assert run.returncode == 0, run.stderr
    assert victim.read_text() == "keep me"
    assert (backups / "env_notes").exists()
    assert (backups / "env_x keep.txt").exists()
    assert len(list(backups.glob("env_[0-9]*"))) == 10


def test_a_json_body_is_capped_before_it_is_read(client, monkeypatch):
    """FastAPI reads and parses a JSON body before verify_token runs: without
    a token, a 500 MB body to any POST was held in memory and parsed (security
    review, 08.10.2026). Every body now has a cap: by Content-Length at once,
    and by the bytes as they come for a body sent in chunks."""
    from fastapi.testclient import TestClient

    from adder import app as app_module

    monkeypatch.setattr(app_module, "DEFAULT_BODY_LIMIT", 1000)
    stranger = TestClient(client.app)
    big = b'{"items": [' + b"0," * 1000 + b"0]}"

    def chunks():
        for start in range(0, len(big), 100):
            yield big[start : start + 100]

    assert stranger.post("/api/covers", content=big).status_code == 413
    assert stranger.post("/api/covers", content=chunks()).status_code == 413
    assert client.put("/api/playlists/x/tracks", content=big).status_code == 413
    # Under the cap nothing changes: no token — 401.
    assert stranger.post("/api/covers", json={"items": []}).status_code == 401
