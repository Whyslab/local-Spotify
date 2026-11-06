"""Студийная версия вместо клипа (adder/studio.py) и замена трека, которая
раньше молча не срабатывала.

Сети нет: выдачу YouTube Music подставляет каждый тест (conftest по
умолчанию делает вид, что YouTube Music не отвечает). Названия и длины —
из настоящих ответов 05.11.2026.
"""

import shutil
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from adder import config, db, enrich, ingest, library, outside, runtime, sources, studio
from adder import queue as adder_queue

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
# Настоящие, до заглушек conftest (см. его «Tests must stay offline»).
REAL_SEARCH = studio._search
REAL_KIND = studio.kind


def song(video_id, title, artists, duration, explicit=False, video_type=studio.STUDIO):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a} for a in artists],
        "duration_seconds": duration,
        "isExplicit": explicit,
        "videoType": video_type,
        "album": {"name": title},
    }


def music(monkeypatch, results, kinds=None):
    """YouTube Music отвечает ``results`` на любой поиск; ``kinds`` — типы роликов."""
    asked = []

    def fake_search(query, limit):
        asked.append(query)
        return [s for s in (studio._song(item) for item in results) if s is not None]

    monkeypatch.setattr(studio, "_search", fake_search)
    monkeypatch.setattr(studio, "kind", lambda video_id: (kinds or {}).get(video_id))
    return asked


EXPORT = [
    song("eFERfkG_2JQ", "Export", ["Baby Melo"], 104, explicit=True),
    song("NenxQ-qXwF0", "Export", ["Baby Melo"], 104, explicit=False),
    song("04x3RshrpG8", "ЭКСПОНАТ", ["MIA BOYKA"], 99),
]


# ---------------------------------------------------------------------------
# Выбор записи
# ---------------------------------------------------------------------------


def test_the_explicit_version_wins_over_the_clean_one():
    songs = [studio._song(item) for item in reversed(EXPORT)]
    chosen = studio.pick(songs, "Baby Melo", "Export")
    assert chosen.video_id == "eFERfkG_2JQ"


def test_the_clean_version_is_taken_when_it_is_the_only_one():
    songs = [studio._song(EXPORT[1])]
    assert studio.pick(songs, "Baby Melo", "Export").video_id == "NenxQ-qXwF0"


@pytest.mark.parametrize(
    "title",
    [
        "Gulyayu - Slowed",
        "Сон (Acoustic Version)",
        "DON'T PLAY BAE (Metal Version by MIRASCREAM)",
        "Export (Live)",
        "Export (Clean)",
        "Export (Sped Up)",
    ],
)
def test_another_version_of_the_song_is_not_taken(title):
    other = studio._song(song("x1", title, ["Baby Melo"], 104, explicit=True))
    wanted = title.split(" (")[0].split(" - ")[0]
    assert studio.pick([other], "Baby Melo", wanted) is None


def test_the_version_is_taken_when_the_song_itself_names_it():
    acoustic = studio._song(
        song("pyNvaGTnsrU", "КАК ДЕЛИШКИ (Anton Bender Acoustic Guitar)", ["GONE.Fludd"], 200)
    )
    chosen = studio.pick(
        [acoustic], "GONE.Fludd", "КАК ДЕЛИШКИ (Acoustic) [Guitar by Anton Bender]"
    )
    assert chosen is acoustic


def test_names_match_across_alphabets_and_case():
    """YouTube Music: «Eldzhey», «ЕГОР КРИД», «Malo 2.0»; теги: «Элджей», «Егор Крид»."""
    dom = studio._song(song("1", "Дом Периньон", ["Пошлая Молли", "Eldzhey"], 175, True))
    malo = studio._song(song("2", "Malo 2.0", ["ЕГОР КРИД", "OG Buda"], 327, True))
    assert studio.pick([dom], "Элджей, Пошлая молли", "Дом Периньон") is dom
    assert studio.pick([malo], "Егор Крид", "Мало 2.0 (Премьера трека 2026)") is malo


def test_another_artist_with_the_same_title_is_not_taken():
    cover = studio._song(song("x", "Revenge", ["Gxrxushe"], 170, True))
    assert studio.pick([cover], "XXXTENTACION", "Revenge") is None


def test_a_catalogue_length_is_held_to_its_tolerance():
    songs = [studio._song(EXPORT[0])]
    assert studio.pick(songs, "Baby Melo", "Export", duration=110, tolerance=3) is None
    assert studio.pick(songs, "Baby Melo", "Export", duration=106, tolerance=3) is songs[0]


def test_a_studio_track_much_longer_than_the_clip_is_not_the_same_song():
    long = studio._song(song("x", "Export", ["Baby Melo"], 400, True))
    assert studio.pick([long], "Baby Melo", "Export", longest=104 + 30) is None


def test_a_music_video_in_the_results_is_not_a_studio_track():
    assert (
        studio._song(song("v", "TOKYO", ["SQWOZ BAB"], 465, video_type=studio.OFFICIAL_VIDEO))
        is None
    )


@pytest.mark.parametrize(
    ("video_title", "clip"),
    [
        ("9mice feat. glaive - DEVO4KA (Official Video, 2024)", True),
        ("nkeeei, uniqe — ДА/НЕТ (Official Glamour Video)", True),
        ("SQWOZ BAB - TOKYO (Премьера клипа 2025)", True),
        ("Daft Punk - Get Lucky (Official Music Video)", True),
        ("Элджей, Пошлая молли - Дом Периньон (премьера трека)", False),
        ("9MICE & KAI ANGEL - I WANNA BE YOUR DOG (Lyrics Video)", False),
        ("XXXTENTACION - Revenge (Audio)", False),
        ("Some Song (Official Visualizer)", False),
        ("Some Song (Official Lyric Video)", False),
        ("Some Song (Official Visualizer Video)", False),
    ],
)
def test_a_clip_is_told_by_its_title(video_title, clip):
    assert studio.looks_like_clip(video_title) is clip


# ---------------------------------------------------------------------------
# Ссылка, которую прислал человек
# ---------------------------------------------------------------------------


def test_a_music_video_link_turns_into_the_studio_track(monkeypatch):
    music(
        monkeypatch,
        [song("PLm8E_wyKZM", "TOKYO", ["SQWOZ BAB"], 161, True)],
        {"j1O0qKwqQEo": studio.OFFICIAL_VIDEO},
    )
    choice = studio.for_link("j1O0qKwqQEo", "SQWOZ BAB - TOKYO", "SQWOZ BAB", "TOKYO", 465)
    assert choice.song.video_id == "PLm8E_wyKZM"
    assert "студийная" in choice.note


def test_someone_elses_upload_is_left_as_sent(monkeypatch):
    """Решение пользователя: чужие загрузки он добавляет сам — там бывает версия без цензуры."""
    asked = music(monkeypatch, EXPORT, {"mj_bCXsurws": "MUSIC_VIDEO_TYPE_UGC"})
    choice = studio.for_link(
        "mj_bCXsurws", "Элджей, Пошлая молли - Дом Периньон (премьера трека)", "Э", "Дом", 176
    )
    assert choice is None
    assert asked == []


def test_a_clip_uploaded_as_an_ordinary_video_is_still_a_clip(monkeypatch):
    music(
        monkeypatch,
        [song("VsodgjI3DUw", "DEVO4KA (feat. glaive)", ["9mice"], 185, True)],
        {"clipid": "MUSIC_VIDEO_TYPE_UGC"},
    )
    choice = studio.for_link(
        "clipid", "9mice feat. glaive - DEVO4KA (Official Video, 2024)", "9mice feat. glaive",
        "DEVO4KA (Official Video, 2024)", 184,
    )  # fmt: skip
    assert choice.song.video_id == "VsodgjI3DUw"


def test_a_clean_studio_link_turns_into_its_explicit_twin(monkeypatch):
    music(monkeypatch, EXPORT, {"NenxQ-qXwF0": studio.STUDIO})
    choice = studio.for_link("NenxQ-qXwF0", "Export", "Baby Melo", "Export", 104)
    assert choice.song.video_id == "eFERfkG_2JQ"
    assert "18+" in choice.note


def test_an_explicit_studio_link_is_left_alone(monkeypatch):
    music(monkeypatch, EXPORT, {"eFERfkG_2JQ": studio.STUDIO})
    assert studio.for_link("eFERfkG_2JQ", "Export", "Baby Melo", "Export", 104) is None


def test_a_clip_without_a_studio_track_is_kept_and_said_so(monkeypatch):
    music(monkeypatch, [], {"clip": studio.OFFICIAL_VIDEO})
    choice = studio.for_link("clip", "A - B (Official Video)", "A", "B", 200)
    assert choice.song is None
    assert "не нашлось" in choice.note


def test_silence_from_youtube_music_downloads_the_link_without_a_note(monkeypatch):
    monkeypatch.setattr(studio, "kind", lambda video_id: studio.OFFICIAL_VIDEO)
    monkeypatch.setattr(studio, "_search", lambda query, limit: None)
    assert studio.for_link("clip", "A - B (Official Video)", "A", "B", 200) is None


def test_a_failing_client_is_silence_not_an_error(monkeypatch):
    class Broken:
        def search(self, *a, **k):
            raise ConnectionError("down")

        def get_song(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(studio, "_ytmusic", lambda: Broken())
    monkeypatch.setattr(studio, "MIN_INTERVAL", 0)
    monkeypatch.setattr(studio, "_search", REAL_SEARCH)
    assert REAL_SEARCH("q", 5) is None
    assert studio.search("A", "B") == []
    assert REAL_KIND("abc") is None


# ---------------------------------------------------------------------------
# Автоматический поиск: импорт подборок, артист целиком, радио
# ---------------------------------------------------------------------------


def test_a_playlist_import_takes_the_studio_track(monkeypatch):
    music(monkeypatch, EXPORT)

    def no_youtube(*a, **k):
        raise AssertionError("YouTube search must not be needed")

    monkeypatch.setattr(sources, "search_youtube", no_youtube)
    match = sources.best_youtube_match(sources.Candidate("Baby Melo", "Export", 105))
    assert match["url"].endswith("eFERfkG_2JQ")


def test_a_playlist_import_falls_back_to_youtube_search(monkeypatch):
    music(monkeypatch, [])
    monkeypatch.setattr(
        sources,
        "search_youtube",
        lambda q, limit=8: [
            {"url": "https://www.youtube.com/watch?v=zzz", "title": "A - B", "channel": "A",
             "duration": 200},
        ],
    )  # fmt: skip
    match = sources.best_youtube_match(sources.Candidate("A", "B", 200))
    assert match["url"].endswith("zzz")


def test_outside_tracks_take_the_studio_track_of_the_right_length(monkeypatch):
    music(monkeypatch, EXPORT)
    monkeypatch.setattr(outside, "_search", lambda q: [])
    assert outside.find_video("Baby Melo", "Export", 104)["id"] == "eFERfkG_2JQ"
    # Длина из Deezer другая — студийной записи не верим, ищем как раньше.
    assert outside.find_video("Baby Melo", "Export", 150) is None


def test_manual_search_shows_studio_tracks_first_and_marks_them(monkeypatch):
    music(monkeypatch, EXPORT)

    class Done:
        returncode = 0
        stdout = (
            '{"entries": [{"id": "eFERfkG_2JQ", "title": "Export", "uploader": "x"},'
            ' {"id": "clip1", "title": "Baby Melo - Export (Official Video)", "uploader": "x"}]}'
        )
        stderr = ""

    monkeypatch.setattr(ingest, "run_yt_dlp", lambda cmd, timeout: Done())
    found = sources.search_youtube("Baby Melo Export")
    assert [row["url"].rsplit("=", 1)[-1] for row in found] == [
        "eFERfkG_2JQ",
        "NenxQ-qXwF0",
        "clip1",
    ]
    assert found[0]["channel"] == "YouTube Music · студийная · 18+"
    assert found[1]["channel"] == "YouTube Music · студийная"


# ---------------------------------------------------------------------------
# Конвейер: ссылка на клип и замена
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from adder import playlists

    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
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
    return tmp_path


def youtube(monkeypatch, metas, audio=FIXTURE):
    """yt-dlp: ``metas`` — метаданные по id ролика; качается копия ``audio``."""
    downloaded = []

    def meta(url):
        vid = url.rsplit("=", 1)[-1]
        return {"id": vid, **metas[vid]}

    def download(url, vid):
        downloaded.append(url)
        target = runtime.TMP_DIR / f"{vid}.m4a"
        shutil.copy(audio, target)
        return target

    monkeypatch.setattr(ingest, "yt_meta", meta)
    monkeypatch.setattr(ingest, "yt_download", download)
    return downloaded


def run(url, replace_of=None):
    tid = db.db_exec(
        "INSERT INTO tasks(url, status, replace_of) VALUES(?, 'queued', ?)", (url, replace_of)
    ).lastrowid
    adder_queue.process(tid, url)
    return db.db_query("SELECT * FROM tasks WHERE id = ?", (tid,))[0]


def watch(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def test_a_shared_clip_is_filed_as_the_studio_track(app, monkeypatch):
    music(
        monkeypatch,
        [song("PLm8E_wyKZM", "TOKYO", ["SQWOZ BAB"], 161, True)],
        {"j1O0qKwqQEo": studio.OFFICIAL_VIDEO},
    )
    downloaded = youtube(
        monkeypatch,
        {
            "j1O0qKwqQEo": {"title": "SQWOZ BAB - TOKYO (Премьера клипа 2025)", "duration": 465},
            "PLm8E_wyKZM": {"title": "TOKYO", "artist": "SQWOZ BAB", "track": "TOKYO",
                            "duration": 161},
        },
    )  # fmt: skip

    task = run(watch("j1O0qKwqQEo"))

    assert task["status"] == "done", task["error"]
    assert downloaded == [watch("PLm8E_wyKZM")]
    assert task["result_path"] == "SQWOZ BAB/Singles/TOKYO.m4a"
    assert task["warning"] == "Взята студийная версия вместо клипа"
    tags = MP4(config.LIBRARY / task["result_path"]).tags
    assert tags["----:com.apple.iTunes:SOURCE_URL"][0].decode() == watch("PLm8E_wyKZM")


def test_a_clip_whose_studio_track_is_already_here_is_not_downloaded_again(app, monkeypatch):
    music(
        monkeypatch,
        [song("PLm8E_wyKZM", "TOKYO", ["SQWOZ BAB"], 161, True)],
        {"j1O0qKwqQEo": studio.OFFICIAL_VIDEO},
    )
    metas = {
        "j1O0qKwqQEo": {"title": "SQWOZ BAB - TOKYO (Official Video)", "duration": 465},
        "PLm8E_wyKZM": {"title": "TOKYO", "artist": "SQWOZ BAB", "track": "TOKYO"},
    }
    downloaded = youtube(monkeypatch, metas)
    first = run(watch("PLm8E_wyKZM"))
    library.invalidate_library_index()

    second = run(watch("j1O0qKwqQEo"))

    assert second["status"] == "done", second["error"]
    assert second["result_path"] == first["result_path"]
    assert downloaded == [watch("PLm8E_wyKZM")]


def _two_tracks(monkeypatch):
    """«Old» и «New» в фонотеке, «Old» — в подборке «Вечер»."""
    from adder import playlists

    youtube(
        monkeypatch,
        {"old": {"title": "A - Old"}, "new": {"title": "A - New"}},
    )
    run(watch("old"))
    other = config.LIBRARY.parent / "other.m4a"
    shutil.copy(FIXTURE, other)
    audio = MP4(other)
    audio["\xa9cmt"] = ["different bytes"]
    audio.save()
    youtube(monkeypatch, {"old": {"title": "A - Old"}, "new": {"title": "A - New"}}, audio=other)
    run(watch("new"))
    playlists.create("Вечер", ["A/Singles/Old.m4a"])
    library.invalidate_library_index()


def test_replacing_with_a_link_already_in_the_library_uses_that_track(app, monkeypatch):
    """«Как делишки», 05.11.2026: замена ссылкой на трек, который уже есть, отвечала 409."""
    from fastapi.testclient import TestClient

    from adder import app as app_module
    from adder import playlists

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    _two_tracks(monkeypatch)
    with TestClient(app_module.app) as client:
        client.headers.update({"Authorization": "Bearer test-secret"})
        response = client.post(
            "/api/replace", json={"path": "A/Singles/Old.m4a", "url": watch("new")}
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"task": None, "replaced_with": "A/Singles/New.m4a"}
    assert playlists.read("Вечер").entries[0].path == "A/Singles/New.m4a"
    assert not (config.LIBRARY / "A/Singles/Old.m4a").exists()
    assert (runtime.TRASH_DIR / "A/Singles/Old.m4a").exists()


def test_replacing_a_track_with_its_own_link_is_refused(app, monkeypatch):
    from fastapi.testclient import TestClient

    from adder import app as app_module

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    _two_tracks(monkeypatch)
    with TestClient(app_module.app) as client:
        client.headers.update({"Authorization": "Bearer test-secret"})
        response = client.post(
            "/api/replace", json={"path": "A/Singles/Old.m4a", "url": watch("old")}
        )
    assert response.status_code == 409
    assert (config.LIBRARY / "A/Singles/Old.m4a").exists()


def test_a_replacement_whose_sound_is_already_here_still_replaces(app, monkeypatch):
    """Скачанный звук совпал с файлом фонотеки: раньше задача кончалась
    «готово», а старый трек оставался в подборках."""
    from adder import playlists

    _two_tracks(monkeypatch)
    new_bytes = config.LIBRARY / "A/Singles/New.m4a"
    youtube(monkeypatch, {"again": {"title": "A - New (copy)"}}, audio=new_bytes)
    monkeypatch.setattr(ingest, "find_duplicate_library_file", lambda path: new_bytes)

    task = run(watch("again"), replace_of="A/Singles/Old.m4a")

    assert task["status"] == "done", task["error"]
    assert task["result_path"] == "A/Singles/New.m4a"
    assert playlists.read("Вечер").entries[0].path == "A/Singles/New.m4a"
    assert not (config.LIBRARY / "A/Singles/Old.m4a").exists()
    assert "встал на место старого" in task["warning"]


def test_a_replacement_by_a_link_known_from_the_tags_still_replaces(app, monkeypatch):
    """Задачи нет (база потеряна), но файл помнит ссылку в теге SOURCE_URL."""
    from adder import playlists

    _two_tracks(monkeypatch)
    db.db_exec("DELETE FROM tasks WHERE url = ?", (watch("new"),))
    library.invalidate_library_index()

    task = run(watch("new"), replace_of="A/Singles/Old.m4a")

    assert task["status"] == "done", task["error"]
    assert playlists.read("Вечер").entries[0].path == "A/Singles/New.m4a"
    assert not (config.LIBRARY / "A/Singles/Old.m4a").exists()


# ---------------------------------------------------------------------------
# Рецензия 05.11.2026
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["Remix", "Live", "Slowed", "Sped Up"])
def test_a_remix_clip_is_not_turned_into_the_original(monkeypatch, variant):
    """Клип ремикса или концертника — не оригинал: подмена дала бы другую песню."""
    music(monkeypatch, [song("orig", "Song", ["Artist"], 200, True)])
    choice = studio.for_link(
        "clip", f"Artist - Song ({variant}) (Official Video)", "Artist", f"Song ({variant})", 210
    )
    assert choice.song is None


def test_a_remix_clip_turns_into_the_studio_remix(monkeypatch):
    music(
        monkeypatch,
        [song("orig", "Song", ["Artist"], 200, True), song("rmx", "Song (Remix)", ["Artist"], 190)],
    )
    choice = studio.for_link(
        "clip", "Artist - Song (Remix) (Official Video)", "Artist", "Song (Remix)", 210
    )
    assert choice.song.video_id == "rmx"


def test_the_explicit_twin_is_the_same_version_with_a_known_length():
    clean = studio._song(song("c", "Song", ["A"], 200))
    sped = studio._song(song("s", "Song (Sped Up)", ["A"], 200, True))
    unknown = studio._song(song("u", "Song", ["A"], None, True))
    twin = studio._song(song("t", "Song", ["A"], 201, True))
    assert studio.explicit_twin(clean, [sped, unknown]) is None
    assert studio.explicit_twin(clean, [sped, unknown, twin]) is twin


@pytest.mark.parametrize(
    ("their", "ours", "same"),
    [
        ("Drake Bell", "Drake", False),
        ("Lil", "Lil Peep", False),
        ("Future", "Drake feat. Future", True),
        ("Simon & Garfunkel", "Simon & Garfunkel", True),
        ("Eldzhey", "Элджей, Пошлая молли", True),
        # Теги фонотеки склеивают артистов через «•».
        ("Кравц", "Элджей • Кравц", True),
        ("Travis Scott", "Playboi Carti • Travis Scott", True),
        ("Drake Bell", "Drake • Future", False),
    ],
)
def test_an_artist_matches_by_whole_name(their, ours, same):
    assert studio.same_artist(studio._song(song("x", "S", [their], 100)), ours) is same


def test_manual_search_does_not_put_another_artists_song_first(monkeypatch):
    music(
        monkeypatch,
        [song("o", "Love", ["Someone"], 200), song("a", "Love", ["Adele"], 210),
         song("l", "Love (Live)", ["Adele"], 260)],
    )  # fmt: skip
    assert [s.video_id for s in studio.search_query("Adele Love")] == ["a"]


def _replacing_clip(monkeypatch, studio_meta=None):
    music(
        monkeypatch,
        [song("PLm8E_wyKZM", "TOKYO", ["SQWOZ BAB"], 161, True)],
        {"j1O0qKwqQEo": studio.OFFICIAL_VIDEO},
    )
    return youtube(
        monkeypatch,
        {
            "j1O0qKwqQEo": {"title": "SQWOZ BAB - TOKYO (Official Video)", "duration": 465},
            "PLm8E_wyKZM": studio_meta
            or {"title": "TOKYO", "artist": "SQWOZ BAB", "track": "TOKYO", "duration": 161},
        },
    )


def test_a_studio_track_that_will_not_download_falls_back_to_the_clip(app, monkeypatch):
    """Сбой подмены = качать присланное, а не ошибка задачи."""
    downloaded = _replacing_clip(monkeypatch)
    real = ingest.yt_download

    def refuse_studio(url, vid):
        if vid == "PLm8E_wyKZM":
            raise RuntimeError("Video unavailable in your country")
        return real(url, vid)

    monkeypatch.setattr(ingest, "yt_download", refuse_studio)

    task = run(watch("j1O0qKwqQEo"))

    assert task["status"] == "done", task["error"]
    assert downloaded == [watch("j1O0qKwqQEo")]
    assert "звук клипа" in task["warning"]
    tags = MP4(config.LIBRARY / task["result_path"]).tags
    assert tags["----:com.apple.iTunes:SOURCE_URL"][0].decode() == watch("j1O0qKwqQEo")


def test_a_clip_whose_studio_track_is_the_replaced_one_says_nothing_was_replaced(app, monkeypatch):
    """Меняли студийную её же клипом: ничего не произошло — и так и сказано."""
    _replacing_clip(monkeypatch)
    first = run(watch("PLm8E_wyKZM"))
    library.invalidate_library_index()

    task = run(watch("j1O0qKwqQEo"), replace_of=first["result_path"])

    assert task["status"] == "done", task["error"]
    assert task["replace_outcome"] == 0
    assert "менять нечего" in task["warning"]
    assert (config.LIBRARY / first["result_path"]).exists()


def test_a_finished_replacement_says_it_replaced(app, monkeypatch):
    _two_tracks(monkeypatch)
    youtube(monkeypatch, {"third": {"title": "A - Third"}})
    task = run(watch("third"), replace_of="A/Singles/Old.m4a")
    assert task["status"] == "done", task["error"]
    assert task["replace_outcome"] == 1


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from adder import app as app_module

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    client = TestClient(app_module.app)
    client.headers.update({"Authorization": "Bearer test-secret"})
    return client


def test_a_replacement_by_a_track_already_here_leaves_a_trail(app, monkeypatch):
    """Сохранённая на телефоне очередь со старым путём находит новый трек."""
    _two_tracks(monkeypatch)
    with _client(monkeypatch) as client:
        client.post("/api/replace", json={"path": "A/Singles/Old.m4a", "url": watch("new")})
        assert library.replaced_by("A/Singles/Old.m4a") == "A/Singles/New.m4a"


def test_a_playlist_holding_both_tracks_keeps_one_line(app, monkeypatch):
    from adder import playlists

    _two_tracks(monkeypatch)
    playlists.create("Оба", ["A/Singles/Old.m4a", "A/Singles/New.m4a"])
    assert ingest.swap_into("A/Singles/Old.m4a", "A/Singles/New.m4a")
    assert [e.path for e in playlists.read("Оба").entries] == ["A/Singles/New.m4a"]


def test_a_missing_new_track_never_trashes_the_old_one(app, monkeypatch):
    _two_tracks(monkeypatch)
    (config.LIBRARY / "A/Singles/New.m4a").unlink()
    assert not ingest.swap_into("A/Singles/Old.m4a", "A/Singles/New.m4a")
    assert (config.LIBRARY / "A/Singles/Old.m4a").exists()


def test_a_task_path_holding_another_recording_is_not_trusted(app, monkeypatch):
    """Замена с тем же именем кладёт под путь задачи другую запись."""
    from adder import app as app_module

    _two_tracks(monkeypatch)
    db.db_exec(
        "UPDATE tasks SET result_path = ? WHERE url = ?", ("A/Singles/New.m4a", watch("old"))
    )
    db.db_exec("DELETE FROM tasks WHERE url = ?", (watch("new"),))
    library.invalidate_library_index()
    # New.m4a помнит ссылку «new», а не «old».
    assert app_module._already_have(watch("old")) == "A/Singles/Old.m4a"
    (config.LIBRARY / "A/Singles/Old.m4a").unlink()
    library.invalidate_library_index()
    assert app_module._already_have(watch("old")) is None


def test_one_task_can_be_asked_for_by_id(app, monkeypatch):
    _two_tracks(monkeypatch)
    tid = db.db_query("SELECT id FROM tasks WHERE url = ?", (watch("new"),))[0]["id"]
    with _client(monkeypatch) as client:
        assert client.get(f"/api/tasks/{tid}").json()["result_path"] == "A/Singles/New.m4a"
        assert client.get("/api/tasks/999999").status_code == 404


@pytest.mark.parametrize(
    ("found", "wanted", "same"),
    [
        ("Song (Album Version)", "Song", True),
        ("Song (Explicit Version)", "Song", True),
        ("Song (Clean)", "Song", False),
        ("Song (Remix)", "Song", False),
        ("Song", "Song (Remix)", False),
        ("Song (Speed Up)", "Song (Sped Up)", True),
        ("Song (Metal Version)", "Song", False),
    ],
)
def test_versions_compare_both_ways(found, wanted, same):
    assert studio.same_version(found, wanted) is same


@pytest.mark.parametrize(
    ("their", "ours"),
    [
        ("Fire", "Earth, Wind & Fire"),
        ("Florence", "Florence and the Machine"),
        ("DC", "AC/DC"),
        ("Garfunkel", "Simon & Garfunkel"),
    ],
)
def test_a_piece_of_a_band_name_is_not_the_band(their, ours):
    assert not studio.same_artist(studio._song(song("x", "S", [their], 100)), ours)


@pytest.mark.parametrize(
    "video_title",
    ["Song (Official Video) [HQ Audio]", "Song (Official Music Video) (Lyrics)"],
)
def test_a_clip_with_audio_or_lyrics_words_is_still_a_clip(video_title):
    assert studio.looks_like_clip(video_title)


def test_a_rate_limit_on_the_studio_track_is_not_swallowed(app, monkeypatch):
    _replacing_clip(monkeypatch)
    real = ingest.yt_download

    def limited(url, vid):
        if vid == "PLm8E_wyKZM":
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return real(url, vid)

    monkeypatch.setattr(ingest, "yt_download", limited)
    tid = db.db_exec(
        "INSERT INTO tasks(url, status) VALUES(?, 'queued')", (watch("j1O0qKwqQEo"),)
    ).lastrowid
    with pytest.raises(RuntimeError, match="429"):
        ingest.download_to_temp(tid, watch("j1O0qKwqQEo"))


def test_a_song_of_several_tagged_artists_is_found():
    songs = [studio._song(song("c", "CRUSH", ["Playboi Carti", "Travis Scott"], 174, True))]
    found = studio.pick(songs, "Playboi Carti • Travis Scott", "CRUSH (with Travis Scott)")
    assert found is songs[0]
