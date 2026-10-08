"""Playlist covers: what counts as an image, and where the original lives.
Track covers: thumbnails, their cache and how many are made at once."""

import hashlib
import shutil
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from adder import config, covers, library, navidrome, playlists, runtime

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 32


@pytest.fixture()
def temp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [])
    return tmp_path


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_type_comes_from_the_bytes(temp_env, payload, expected):
    assert covers.store("mix", payload) == expected
    assert covers.media_type("mix") == expected


def test_a_file_that_is_not_an_image_is_refused(temp_env):
    """The name and the client's Content-Type are both supplied by the caller."""
    with pytest.raises(HTTPException) as excinfo:
        covers.store("mix", b"#!/bin/sh\nrm -rf /\n")
    assert excinfo.value.status_code == 415


def test_an_oversized_cover_is_refused(temp_env, monkeypatch):
    monkeypatch.setattr(config, "MAX_COVER_BYTES", 64)
    with pytest.raises(HTTPException) as excinfo:
        covers.store("mix", PNG + b"\x00" * 200)
    assert excinfo.value.status_code == 413


def test_storing_again_replaces_the_previous_cover(temp_env):
    covers.store("mix", PNG)
    covers.store("mix", JPEG)

    files = sorted(covers.COVERS_DIR.glob("mix.*"))
    assert len(files) == 1
    assert files[0].suffix == ".jpg"


def test_rename_carries_the_cover_across(temp_env):
    """Navidrome loses the cover on a rename; the local copy is what restores it."""
    covers.store("old", PNG)
    covers.rename("old", "new")

    assert covers.cover_file("old") is None
    assert covers.read_cover("new")[0] == PNG


def test_read_cover_of_an_unknown_playlist_is_none(temp_env):
    assert covers.read_cover("never-had-one") is None


@pytest.fixture()
def client(temp_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(navidrome, "configured", lambda: False)

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_upload_and_fetch_a_cover(client):
    client.post("/api/playlists", json={"name": "Ночь", "paths": []})

    uploaded = client.post(
        "/api/playlists/Ночь/cover",
        files={"image": ("photo.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["media_type"] == "image/png"

    fetched = client.get("/api/playlists/Ночь/cover")
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert fetched.headers["content-type"] == "image/png"


def test_upload_to_a_missing_playlist_is_404(client):
    response = client.post(
        "/api/playlists/nope/cover", files={"image": ("photo.png", PNG, "image/png")}
    )
    assert response.status_code == 404


def test_a_disguised_file_is_refused(client):
    """A .png name and an image/png header on something that is not an image."""
    client.post("/api/playlists", json={"name": "mix", "paths": []})
    response = client.post(
        "/api/playlists/mix/cover",
        files={"image": ("innocent.png", b"not an image at all", "image/png")},
    )
    assert response.status_code == 415


def test_missing_cover_is_404(client):
    client.post("/api/playlists", json={"name": "bare", "paths": []})
    assert client.get("/api/playlists/bare/cover").status_code == 404


def test_cover_survives_a_playlist_rewrite(client):
    """Reordering rewrites the .m3u; the cover must not be collateral damage."""
    client.post("/api/playlists", json={"name": "mix", "paths": ["A.m4a", "B.m4a"]})
    client.post("/api/playlists/mix/cover", files={"image": ("c.png", PNG, "image/png")})

    revision = client.get("/api/playlists/mix/tracks").json()["revision"]
    client.put(
        "/api/playlists/mix/tracks", json={"paths": ["B.m4a", "A.m4a"], "revision": revision}
    )

    assert client.get("/api/playlists/mix/cover").content == PNG
    assert client.get("/api/playlists/mix/tracks").json()["cover"] is True


def test_deleting_a_playlist_removes_its_cover(client):
    client.post("/api/playlists", json={"name": "gone", "paths": []})
    client.post("/api/playlists/gone/cover", files={"image": ("c.png", PNG, "image/png")})

    client.delete("/api/playlists/gone")

    assert covers.cover_file("gone") is None


@pytest.mark.parametrize("name", ["Rap vol. 2", "Mix [2024]", "R*", "a?b"])
def test_names_with_dots_and_glob_characters_keep_their_own_cover(name, tmp_path, monkeypatch):
    """«Rap vol. 2» ложилась в «Rap vol.jpg», «[2024]» не находилась, «R*» стирала чужие."""
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    covers.store("Rap vol", PNG)
    covers.store("Rock", PNG)
    covers.store(name, JPEG)

    assert covers.read_cover(name)[0] == JPEG
    assert covers.read_cover("Rap vol")[0] == PNG
    assert covers.read_cover("Rock")[0] == PNG

    covers.rename(name, name + " x")
    assert covers.read_cover(name + " x")[0] == JPEG
    assert covers.read_cover("Rap vol")[0] == PNG


def test_a_track_cover_can_be_asked_for_small(tmp_path, monkeypatch):
    """Списку нужна миниатюра, а не полноразмерная обложка в мегабайты."""
    import subprocess

    from adder import thumbs

    big = tmp_path / "big.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=1200x1200", "-frames:v", "1", str(big)],
        check=True,
    )  # fmt: skip
    art = (big.read_bytes(), "image/jpeg")

    small, kind = thumbs.shrink(art, 96)
    assert kind == "image/jpeg"
    assert len(small) < len(art[0]) / 5
    # Второй раз — из кэша, тот же результат.
    assert thumbs.shrink(art, 96)[0] == small
    # Не картинка — отдаётся как есть, без ошибки.
    assert thumbs.shrink((b"not an image", "image/png"), 96) == (
        b"not an image",
        "image/png",
    )


def _jpeg(color: str) -> bytes:
    import subprocess

    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=64x64", "-frames:v", "1",
         "-f", "mjpeg", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip


def test_the_same_thumbnail_asked_for_at_once_is_not_an_error(tmp_path, monkeypatch):
    """Два запроса писали один «<ключ>.part», и второй replace падал."""
    import shutil
    import subprocess
    import threading

    from adder import thumbs

    art = (_jpeg("red"), "image/jpeg")
    errors = []
    for _ in range(10):
        shutil.rmtree(runtime.THUMB_DIR, ignore_errors=True)
        ffmpeg_done = threading.Barrier(2)

        # Двое (столько пускает ограничение) выходят из ffmpeg разом и с
        # большим ответом: запись «.part» длится, и окно гонки открыто каждый
        # раз. Остальные находят готовую миниатюру — или встают парой следом.
        def ffmpeg(*_a, _done=ffmpeg_done, **_k):
            with suppress(threading.BrokenBarrierError):
                _done.wait(timeout=1)
            return subprocess.CompletedProcess([], 0, stdout=b"\xff" * 4_000_000)

        monkeypatch.setattr(thumbs.subprocess, "run", ffmpeg)

        def one():
            try:
                thumbs.shrink(art, 96)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert not errors, errors[:3]
    assert not list(runtime.THUMB_DIR.glob("*.part*"))


def _track_with_cover(path: Path, art: bytes) -> Path:
    from mutagen.mp4 import MP4, MP4Cover

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "tone.m4a", path)
    audio = MP4(path)
    audio["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    return path


def test_thumbnail_generation_is_bounded(tmp_path, monkeypatch):
    """A cold list asked for forty thumbnails and started forty ffmpeg at once."""
    from adder import thumbs

    running, peak, lock = [0], [0], threading.Lock()

    def ffmpeg(*_a, **_k):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        with lock:
            running[0] -= 1
        return subprocess.CompletedProcess([], 0, stdout=b"\xff\xd8small")

    monkeypatch.setattr(thumbs.subprocess, "run", ffmpeg)
    threads = [
        threading.Thread(target=thumbs.shrink, args=((f"art {i}".encode(), "image/jpeg"), 96))
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 2


def test_cached_thumbnail_skips_tag_parse(tmp_path, monkeypatch):
    """The second time the same file is asked for, its tags are not read:
    the file's stamp says it is the same, the thumbnail is on disk."""
    from adder import thumbs

    track = _track_with_cover(tmp_path / "A" / "Singles" / "B.m4a", _jpeg("red"))
    first = thumbs.cover(track, 96)
    assert first and first[1] == "image/jpeg"

    def parse(_path):
        raise AssertionError("tags read again")

    monkeypatch.setattr(library, "embedded_cover", parse)
    assert thumbs.cover(track, 96) == first
    assert thumbs.cover(track, 90) == first  # the nearest size, the same file


def test_a_track_gone_mid_request_has_no_cover(tmp_path):
    """Deleted between the library lookup and the stamp: "no cover" (404),
    as before the stamp, not an error (500)."""
    from adder import thumbs

    assert thumbs.cover(tmp_path / "A" / "Singles" / "Gone.m4a", 96) is None


def test_retagged_cover_gets_new_thumbnail(tmp_path):
    """A new cover keeps the file's mtime (it is the "added" date), but not
    its ctime: the stamp changes and the new picture is shrunk."""
    import os

    from mutagen.mp4 import MP4, MP4Cover

    from adder import thumbs

    track = _track_with_cover(tmp_path / "A" / "Singles" / "B.m4a", _jpeg("red"))
    before = thumbs.cover(track, 96)
    stat = track.stat()
    audio = MP4(track)
    audio["covr"] = [MP4Cover(_jpeg("blue"), imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    os.utime(track, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert thumbs.cover(track, 96) != before


def test_album_tracks_share_thumbnail(tmp_path, monkeypatch):
    """Twelve tracks of one album carry one picture: one thumbnail on disk."""
    from adder import thumbs

    art = _jpeg("green")
    calls = []
    real = subprocess.run
    monkeypatch.setattr(thumbs.subprocess, "run", lambda *a, **k: calls.append(1) or real(*a, **k))
    for n in range(3):
        thumbs.cover(_track_with_cover(tmp_path / "A" / "Album" / f"{n}.m4a", art), 96)
    assert len(calls) == 1
    assert len(list(runtime.THUMB_DIR.glob("*.jpg"))) == 1
    key = hashlib.sha1(art + b"|96").hexdigest()  # the name scripts/stress.py looks for
    assert (runtime.THUMB_DIR / f"{key}.jpg").is_file()


def test_the_nightly_run_prepares_every_track(tmp_path):
    """scripts/export_shelves.py runs this each night: tracks added past the
    service, or a cleared cache, get their thumbnails before anyone scrolls."""
    from adder import thumbs

    root = tmp_path / "library"
    arts = [_jpeg("red"), _jpeg("blue")]
    for n, art in enumerate(arts):
        _track_with_cover(root / f"A{n}" / "Singles" / "B.m4a", art)
    (root / "notes.txt").write_text("not a track")

    assert thumbs.prepare_library(root) == 2
    for art in arts:
        for size in thumbs.PREPARED:
            assert (runtime.THUMB_DIR / f"{thumbs._key(art, size)}.jpg").is_file()


# ---------------------------------------------------------------------------
# A screen's covers in one answer
# ---------------------------------------------------------------------------


def _unpack(body: bytes) -> list:
    """/api/covers: 4 bytes of header length, the JSON header, the pictures."""
    import json

    size = int.from_bytes(body[:4], "big")
    at = 4 + size
    out: list = []
    for item in json.loads(body[4:at])["items"]:
        if "length" in item:
            out.append((item["type"], body[at : at + item["length"]]))
            at += item["length"]
        else:
            out.append("later" if item.get("later") else "missing")
    assert at == len(body)
    return out


def _library_tracks():
    one = _track_with_cover(config.LIBRARY / "A" / "Singles" / "One.m4a", _jpeg("red"))
    two = _track_with_cover(config.LIBRARY / "A" / "Singles" / "Two.m4a", _jpeg("blue"))
    bare = config.LIBRARY / "A" / "Singles" / "Bare.m4a"
    shutil.copy(Path(__file__).parent / "fixtures" / "tone.m4a", bare)
    return [str(p.relative_to(config.LIBRARY)) for p in (one, two, bare)]


def test_screen_covers_come_in_one_answer(client):
    """On the phone each cover was a trip of its own, four at a time: ten rows
    on screen were three round trips. Now the screen's covers come in one
    answer, the same pictures /api/cover gives one by one."""
    one, two, bare = _library_tracks()
    singles = [client.get("/api/cover", params={"path": p, "size": 96}).content for p in (one, two)]
    asked = [one, "../outside.m4a", two, bare, "A/Singles/Gone.m4a"]

    response = client.post(
        "/api/covers",
        json={"items": [{"path": p, "size": 90 if p == two else 96} for p in asked]},
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "content-encoding" not in response.headers  # pictures are compressed already
    assert _unpack(response.content) == [
        ("image/jpeg", singles[0]),
        "missing",
        ("image/jpeg", singles[1]),
        "missing",
        "missing",
    ]


def test_the_batch_leaves_ffmpeg_to_single_requests(client, monkeypatch):
    """A thumbnail still to be made holds the whole answer behind ffmpeg; the
    batch says "later" for it, and that cover goes the old way, alone."""
    from adder import thumbs

    one, _two, _bare = _library_tracks()
    real = thumbs.subprocess.run
    made = []

    def ffmpeg(*args, **kwargs):
        made.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(thumbs.subprocess, "run", ffmpeg)
    ask = {"items": [{"path": one, "size": 96}]}

    assert _unpack(client.post("/api/covers", json=ask).content) == ["later"]
    assert made == []
    single = client.get("/api/cover", params={"path": one, "size": 96}).content
    assert made == [1]
    assert _unpack(client.post("/api/covers", json=ask).content) == [("image/jpeg", single)]


def test_the_batch_is_guarded(client):
    from adder import app as app_module

    one, _two, _bare = _library_tracks()
    fresh = TestClient(client.app)
    assert fresh.post("/api/covers", json={"items": []}).status_code == 401
    for bad in (
        {"items": [{"path": one, "size": 0}]},
        {"items": [{"path": one, "size": 96}] * (app_module.COVERS_MAX + 1)},
    ):
        assert client.post("/api/covers", json=bad).status_code == 422


def test_one_bad_path_does_not_fail_the_batch(client):
    """A NUL byte made Path.resolve() raise ValueError, and the whole screen's
    batch answered 500. It is one missing cover, like any path not found."""
    one, _two, _bare = _library_tracks()
    client.get("/api/cover", params={"path": one, "size": 96})
    response = client.post(
        "/api/covers",
        json={"items": [{"path": "a\u0000b.m4a", "size": 96}, {"path": one, "size": 96}]},
    )
    assert response.status_code == 200
    answers = _unpack(response.content)
    assert answers[0] == "missing" and answers[1][0] == "image/jpeg"
    long_path = {"items": [{"path": "a" * 5000, "size": 96}]}
    assert client.post("/api/covers", json=long_path).status_code == 422
