"""«Заменить по ссылке»: что будет скачано — до замены, глава альбомного ролика
по отметке времени, и своя ссылка без молчаливой подмены студийной
(разбор 19.11.2026, случай Cristoforo Colombo 18.11).

Сети нет: yt-dlp и YouTube Music подставляет каждый тест.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adder import config, db, enrich, ingest, library, runtime, studio
from adder import queue as adder_queue

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"

# Ролик 18.11: альбом FRIENDLY THUG 52 NGG целиком, 31:28, с главами.
ALBUM_ID = "XWDNhc0lAj0"
ALBUM_META = {
    "id": ALBUM_ID,
    "title": "FRIENDLY THUG 52 NGG - Cristoforo Colombo (Full Album)",
    "uploader": "FRIENDLY THUG 52 NGG",
    "duration": 1888,
    "chapters": [
        {"title": "Intro", "start_time": 0, "end_time": 95},
        {"title": "Cristoforo Colombo", "start_time": 95, "end_time": 190},
        {"title": "Bugatti", "start_time": 190, "end_time": 400},
        {"title": "Outro", "start_time": 400, "end_time": 1888},
    ],
}
OLD = "FRIENDLY THUG 52 NGG/Singles/Cristoforo Colombo.m4a"


# ---------------------------------------------------------------------------
# Отметка времени, главы, ссылка задачи
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "seconds"),
    [
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&t=95", 95.0),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&t=95s", 95.0),
        (f"https://youtu.be/{ALBUM_ID}?t=1m35s", 95.0),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&t=1h2m3s", 3723.0),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&start=100", 100.0),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}#t=120", 120.0),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}", None),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&t=soon", None),
        (f"https://www.youtube.com/watch?v={ALBUM_ID}&t=1m35sx", None),
    ],
)
def test_the_time_mark_of_a_link_is_read(url, seconds):
    assert ingest.link_time(url) == seconds


def test_a_chapter_is_chosen_by_time_or_by_the_track_title():
    found = ingest.chapters(ALBUM_META)
    assert [c["title"] for c in found] == ["Intro", "Cristoforo Colombo", "Bugatti", "Outro"]
    assert ingest.pick_chapter(found, 100.0)["title"] == "Cristoforo Colombo"
    assert ingest.pick_chapter(found, 95.0)["title"] == "Cristoforo Colombo"  # начало главы
    assert ingest.pick_chapter(found, 5000.0) is None  # мимо всех глав
    assert ingest.pick_chapter(found, None, "cristoforo colombo")["start"] == 95
    numbered = [{"title": "02. Bugatti", "start": 0.0, "end": 10.0}]
    assert ingest.pick_chapter(numbered, None, "Bugatti") is numbered[0]
    assert ingest.pick_chapter(found, None, "Нет такой") is None


def test_the_choice_travels_in_the_task_link_and_off_the_tag():
    link = f"https://www.youtube.com/watch?v={ALBUM_ID}"
    key = ingest.task_link(link, start=95, end=190, title="Cristoforo Colombo & Co", keep=True)
    assert key != link
    assert ingest.split_task_link(key) == (
        link,
        {"keep": True, "start": 95.0, "end": 190.0, "title": "Cristoforo Colombo & Co"},
    )
    assert ingest.task_source(key) == f"{link}&t=95"
    assert ingest.task_link(link) == link
    assert ingest.split_task_link(link) == (link, {})
    assert ingest.task_source(ingest.task_link(link, keep=True)) == link


# ---------------------------------------------------------------------------
# Через службу
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from adder import playlists

    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(runtime, "guard_real_library", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "check_disk_space", lambda *a, **k: (True, 10_000))
    monkeypatch.setattr(enrich, "lookup", lambda artist, title: None)
    monkeypatch.setattr(ingest, "fetch_cover_url", lambda url: (None, None))
    monkeypatch.setattr(ingest, "fetch_cover", lambda a, t, thumb: (None, None))
    runtime.TMP_DIR.mkdir()
    config.LIBRARY.mkdir()
    runtime.PROCESSING_URLS.clear()
    runtime.shutdown_event.clear()
    library.invalidate_library_index()
    db.db_init()
    # Старый трек: весь альбом одним файлом (так он скачался в первый раз).
    target = config.LIBRARY / OLD
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    ingest.write_tags(
        target,
        enrich.TrackInfo(album="Cristoforo Colombo", artists=["FRIENDLY THUG 52 NGG"]),
        "Cristoforo Colombo",
        None,
        None,
    )
    library.invalidate_library_index()
    return tmp_path


@pytest.fixture()
def client(app):
    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client
    # Задачи ставятся в общую очередь процесса, а работают их тесты сами.
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
    runtime.PROCESSING_URLS.clear()


def album_video(monkeypatch, seconds=30):
    """yt-dlp: метаданные альбомного ролика; «скачивается» тон на ``seconds`` с."""
    downloaded = []
    monkeypatch.setattr(ingest, "yt_meta", lambda url: dict(ALBUM_META))

    def download(url, vid):
        downloaded.append(url)
        target = runtime.TMP_DIR / f"{vid}.m4a"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-b:a", "64k",
             "-f", "mp4", str(target)],
            check=True,
        )  # fmt: skip
        return target

    monkeypatch.setattr(ingest, "yt_download", download)
    return downloaded


def test_preview_of_the_album_link_from_18_11_names_its_chapter(client, monkeypatch):
    """Ссылка на альбомный ролик с отметкой времени: видно, что это альбом на
    31 минуту, какая глава будет взята, и что есть студийная — как вариант."""
    album_video(monkeypatch)
    monkeypatch.setattr(studio, "describe", lambda vid: {"type": None, "title": ""})
    asked = []

    def find(artist, title, duration=None, tolerance=None, longest=None):
        asked.append((artist, title, duration))
        return studio.Song("studio95", "Cristoforo Colombo", ("FRIENDLY THUG 52 NGG",), 96, True)

    monkeypatch.setattr(studio, "find", find)
    body = client.get(
        "/api/replace/preview",
        params={"path": OLD, "url": f"https://www.youtube.com/watch?v={ALBUM_ID}&t=100"},
    ).json()

    assert body["video"]["kind"] == "album"
    assert body["video"]["duration"] == 1888
    assert body["video"]["chapters"] == 4
    assert body["at"] == 100
    assert body["chapter"] == {"title": "Cristoforo Colombo", "start": 95.0, "end": 190.0}
    assert body["seconds"] == 95
    assert body["long"] is False
    assert body["track"]["title"] == "Cristoforo Colombo"
    assert body["studio"]["url"] == "https://www.youtube.com/watch?v=studio95"
    assert asked == [("FRIENDLY THUG 52 NGG", "Cristoforo Colombo", 95)]

    # Без отметки времени и без главы с названием трека — весь ролик, и он длинный.
    monkeypatch.setattr(
        ingest,
        "yt_meta",
        lambda url: {**ALBUM_META, "chapters": ALBUM_META["chapters"][2:]},
    )
    body = client.get(
        "/api/replace/preview",
        params={"path": OLD, "url": f"https://www.youtube.com/watch?v={ALBUM_ID}"},
    ).json()
    assert body["chapter"] is None
    assert body["long"] is True
    assert body["seconds"] == 1888


def test_replacing_with_a_chapter_takes_only_that_chapter_and_no_studio(client, monkeypatch):
    """Выбрана «моя ссылка» с главой: качается ролик, остаётся одна глава, на
    том же пути, и студийную не ищут."""
    downloaded = album_video(monkeypatch, seconds=30)

    def no_studio(*args, **kwargs):
        raise AssertionError("своя ссылка не подменяется студийной")

    monkeypatch.setattr(studio, "for_link", no_studio)
    response = client.post(
        "/api/replace",
        json={
            "path": OLD,
            "url": f"https://www.youtube.com/watch?v={ALBUM_ID}&t=12",
            "choice": "link",
            "start": 10,
            "end": 20,
            "title": "Cristoforo Colombo",
        },
    )
    assert response.status_code == 200, response.text
    tid = response.json()["task"]
    url = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]
    runtime.PROCESSING_URLS.discard(url)
    adder_queue.process(tid, url)
    task = db.db_query("SELECT * FROM tasks WHERE id = ?", (tid,))[0]

    assert task["status"] == "done", task["error"]
    assert task["result_path"] == OLD  # тот же путь: звёзды и скачанное на месте
    assert downloaded == [f"https://www.youtube.com/watch?v={ALBUM_ID}"]
    library.invalidate_library_index()
    row = next(r for r in library.library_index() if r["path"] == OLD)
    assert 9 <= row["duration"] <= 11  # глава 10 с, а не весь ролик
    assert row["source"] == f"https://www.youtube.com/watch?v={ALBUM_ID}&t=10"


def test_my_own_clip_link_is_not_swapped_for_the_studio_one(client, monkeypatch):
    """«Взять мою ссылку» на клип: качается клип, а не студийная."""
    downloaded = []
    monkeypatch.setattr(
        ingest,
        "yt_meta",
        lambda url: {
            "id": url.rsplit("=", 1)[-1],
            "title": "A - Song (Official Video)",
            "duration": 1,
        },
    )

    def download(url, vid):
        downloaded.append(url)
        target = runtime.TMP_DIR / f"{vid}.m4a"
        shutil.copy(FIXTURE, target)
        return target

    monkeypatch.setattr(ingest, "yt_download", download)
    monkeypatch.setattr(
        studio,
        "for_link",
        lambda *a: studio.Choice(studio.Song("studio1", "Song", ("A",), 1, True), "студийная"),
    )
    response = client.post(
        "/api/replace",
        json={"path": OLD, "url": "https://youtu.be/clip123", "choice": "link"},
    )
    tid = response.json()["task"]
    url = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]
    runtime.PROCESSING_URLS.discard(url)
    adder_queue.process(tid, url)
    assert downloaded == ["https://www.youtube.com/watch?v=clip123"]
    now = db.db_query("SELECT result_path FROM tasks WHERE id = ?", (tid,))[0]["result_path"]

    # А выбранная студийная — качается она, без новой подмены.
    response = client.post(
        "/api/replace",
        json={
            "path": now,
            "url": "https://youtu.be/clip456",
            "choice": "studio",
            "studio_url": "https://www.youtube.com/watch?v=studio2",
        },
    )
    tid = response.json()["task"]
    url = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]
    runtime.PROCESSING_URLS.discard(url)
    adder_queue.process(tid, url)
    assert downloaded[-1] == "https://www.youtube.com/watch?v=studio2"


def test_a_chapter_must_have_both_ends_in_order(client):
    base = {"path": OLD, "url": f"https://www.youtube.com/watch?v={ALBUM_ID}", "choice": "link"}
    assert client.post("/api/replace", json={**base, "start": 10}).status_code == 400
    assert client.post("/api/replace", json={**base, "start": 20, "end": 10}).status_code == 400
    assert (
        client.post("/api/replace", json={**base, "start": 0, "end": 4 * 3600}).status_code == 400
    )


def _replace_chapter(client, start, end, title):
    response = client.post(
        "/api/replace",
        json={
            "path": OLD,
            "url": f"https://www.youtube.com/watch?v={ALBUM_ID}&t={start}",
            "choice": "link",
            "start": start,
            "end": end,
            "title": title,
        },
    )
    assert response.status_code == 200, response.text
    tid = response.json()["task"]
    return tid, db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]


def test_two_chapters_of_one_album_do_not_share_a_temp_file(client, monkeypatch):
    """Две замены из одного альбомного ролика идут разом (два работника): у
    каждой свои временные файлы, иначе вторая резала бы уже отрезанное."""
    names = []
    monkeypatch.setattr(ingest, "yt_meta", lambda url: dict(ALBUM_META))

    def download(url, vid):
        names.append(vid)
        target = runtime.TMP_DIR / f"{vid}.m4a"
        shutil.copy(FIXTURE, target)
        return target

    monkeypatch.setattr(ingest, "yt_download", download)
    monkeypatch.setattr(ingest, "cut_section", lambda path, start, end: path)
    monkeypatch.setattr(ingest, "stage_into_temp", _stop)  # дальше скачивания незачем
    for start, title in ((0, "Cristoforo Colombo"), (1, "Bugatti")):
        tid, url = _replace_chapter(client, start, start + 1, title)
        with pytest.raises(_Stop):
            ingest.download_to_temp(tid, url)
    assert len(names) == 2 and names[0] != names[1], names
    assert all(n.startswith(ALBUM_ID) for n in names)


class _Stop(Exception):
    pass


def _stop(*args, **kwargs):
    raise _Stop


def test_a_numbered_chapter_from_a_topic_channel_lands_on_the_same_path(client, monkeypatch):
    """Глава «03. Cristoforo Colombo» с канала «… - Topic»: номер и канал не
    уводят замену на новый путь — она ложится на место старого трека."""
    album_video(monkeypatch, seconds=30)
    monkeypatch.setattr(
        ingest, "yt_meta", lambda url: {**ALBUM_META, "uploader": "Friendly Thug - Topic"}
    )
    tid, url = _replace_chapter(client, 10, 20, "03. Cristoforo Colombo")
    runtime.PROCESSING_URLS.discard(url)
    adder_queue.process(tid, url)
    task = db.db_query("SELECT * FROM tasks WHERE id = ?", (tid,))[0]
    assert task["status"] == "done", task["error"]
    assert task["result_path"] == OLD
    library.invalidate_library_index()
    row = next(r for r in library.library_index() if r["path"] == OLD)
    assert row["title"] == "Cristoforo Colombo"
    assert "Topic" not in (row["artist"] or "")


def test_a_failure_without_a_title_names_the_link_without_the_choice(app, monkeypatch):
    """Сбой до того, как YouTube назвал ролик: в уведомлении — ссылка, а не
    «…#replace:start=95.000&end=…» (служебное, для задачи)."""
    from adder import notify

    said = []
    monkeypatch.setattr(notify, "track_failed", lambda name, why: said.append(name))
    link = ingest.task_link(
        f"https://www.youtube.com/watch?v={ALBUM_ID}", start=95, end=190, title="C", keep=True
    )
    assert "#replace:" in link
    adder_queue._notify_failed(424242, link, None)  # задачи нет — названия тоже
    assert said == [
        f"https://www.youtube.com/watch?v={ALBUM_ID}&t=95"
    ]  # с отметкой — понятно, какая глава
