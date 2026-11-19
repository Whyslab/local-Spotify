"""«Клипы» — очередь на починку (adder/repair.py).

Сети нет: выдачу YouTube Music, скачивание, сравнение и текст подставляет
каждый тест. Звук — настоящий файл (tests/fixtures/tone.m4a), чтобы замена
на месте проверялась по байтам и тегам.
"""

import json
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from adder import censor, config, db, library, playlists, repair, runtime, studio

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
TRACK = "PHARAOH Boulevard Depo/Singles/Champagne Squirt.m4a"
LINES = [
    {"at": 10.0, "line": "Всё хорошо"},
    {"at": 29.5, "line": "Курим дурь до утра"},
    {"at": 33.0, "line": "Конец"},
]


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "guard_real_library", lambda *a, **k: None)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    db.db_init()
    target = config.LIBRARY / TRACK
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    audio = MP4(target)
    audio["\xa9nam"] = ["Champagne Squirt"]
    audio["\xa9ART"] = ["PHARAOH, Boulevard Depo"]
    audio["\xa9cmt"] = ["мои теги"]
    audio.save()
    library.invalidate_library_index()
    return target


def found(video_id, kind, duration, explicit=False, title="Champagne Squirt"):
    return studio.Found(video_id, title, ("PHARAOH",), duration, kind, explicit)


def compared(overall=0.95, lag=0.0, spots=(), head=0.0, tail=0.0, file_s=233.0, cand_s=234.0):
    return {"overall": overall, "lag": lag, "spots": [list(s) for s in spots], "head": head,
            "tail": tail, "file_seconds": file_s, "studio_seconds": cand_s}  # fmt: skip


# ---------------------------------------------------------------------------
# Что качать
# ---------------------------------------------------------------------------


def test_candidates_are_the_explicit_studio_a_clip_and_uploads_of_its_length():
    chosen = repair.choose(
        [
            found("clean", "studio", 234),
            found("explicit", "studio", 234, explicit=True),
            found("mine", "upload", 234),  # из него скачан файл
            found("clip", "clip", 250),
            found("far", "upload", 228),  # на 6 с длиннее — другая версия
            found("near", "upload", 236),
            found("near2", "upload", 233),
        ],
        current_id="mine",
        reference=234,
    )
    assert [c.video_id for c in chosen] == ["explicit", "clip", "near"]


def test_without_a_studio_two_uploads_are_taken():
    chosen = repair.choose(
        [found("u1", "upload", 230), found("u2", "upload", 231), found("c", "clip", 300)],
        current_id="",
        reference=231,
    )
    assert [c.video_id for c in chosen] == ["c", "u1", "u2"]


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------


def test_words_muted_in_the_file_make_an_upload_the_best():
    """Champagne Squirt: файл — подцензурная студийная, оригинал — старая загрузка."""
    result = repair.assess(
        233.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 234,
                "compared": compared(0.99, spots=[]),
            },
            {
                "video_id": "u",
                "kind": "upload",
                "duration": 234,
                "compared": compared(0.93, spots=[(30.0, 30.5, "file")]),
            },
        ],  # fmt: skip
        LINES,
    )
    assert [p["kind"] for p in result["problems"]] == ["words"]
    assert "курим" in result["problems"][0]["text"].lower()
    assert result["best"] == "u" and result["better"]
    assert result["place"] == {"at": 29.0, "candidate_at": 29.0}


def test_a_candidate_muted_on_a_word_is_never_the_best():
    result = repair.assess(
        233.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 234,
                "compared": compared(0.97, spots=[(30.0, 30.5, "studio")]),
            },
            {"video_id": "c", "kind": "clip", "duration": 236, "compared": compared(0.9)},
        ],  # fmt: skip
        LINES,
    )
    studio_row = next(c for c in result["candidates"] if c["video_id"] == "s")
    assert studio_row["muted"] == [{"at": 30.0, "word": "Курим"}]
    assert result["best"] == "c"


def test_a_clip_with_scenes_is_named_and_a_studio_preferred():
    result = repair.assess(
        290.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 234,
                "compared": compared(0.9, lag=40.0, head=40.0, tail=16.0, file_s=290),
            }
        ],  # fmt: skip
        LINES,
    )
    assert [p["kind"] for p in result["problems"]] == ["scenes"]
    assert result["best"] == "s" and result["better"]
    assert result["place"]["at"] == 36.0  # за 3 с до начала песни, ещё 1 с запаса
    assert result["place"]["candidate_at"] == 0.0


def test_a_shorter_file_is_named():
    result = repair.assess(
        200.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 234,
                "compared": compared(0.95, file_s=200, cand_s=234),
            }
        ],  # fmt: skip
        LINES,
    )
    assert [p["kind"] for p in result["problems"]] == ["shorter"]


def test_nothing_wrong_means_no_better_version():
    result = repair.assess(
        233.0,
        [{"video_id": "s", "kind": "studio", "duration": 234, "compared": compared(0.99)}],
        LINES,
    )
    assert result["problems"] == [] and not result["better"]
    assert result["best"] == "s"  # предложить всё равно можно — решает человек


def test_a_recording_matching_no_version_is_another_recording():
    result = repair.assess(
        240.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 234,
                "compared": compared(0.2, file_s=240),
            }
        ],  # fmt: skip
        LINES,
    )
    assert [p["kind"] for p in result["problems"]] == ["other"]


def test_a_quiet_place_without_a_word_is_no_muted_word():
    """Урок DEVO4KA: тихое место само ничего не доказывает."""
    result = repair.assess(
        233.0,
        [
            {
                "video_id": "u",
                "kind": "upload",
                "duration": 234,
                "compared": compared(0.95, spots=[(11.0, 11.5, "file")]),
            }
        ],  # fmt: skip
        LINES,
    )
    assert result["problems"] == []


# ---------------------------------------------------------------------------
# Очередь и подборка
# ---------------------------------------------------------------------------


def _klipy():
    path = playlists.playlist_path(repair.PLAYLIST)
    return [e.path for e in playlists.read(repair.PLAYLIST).entries] if path.exists() else []


def _row(path=TRACK):
    rows = db.db_query("SELECT * FROM repairs WHERE path = ?", (path,))
    return dict(rows[0]) if rows else None


def test_a_track_put_in_klipy_is_queued_and_forgotten_when_taken_out(lib):
    playlists.create(repair.PLAYLIST, [TRACK])
    repair.refresh()
    assert _row()["status"] == "queued" and _row()["auto"] == 0

    current = playlists.read(repair.PLAYLIST)
    playlists.write(repair.PLAYLIST, [], current.revision)
    repair.refresh()
    assert _row() is None


@pytest.fixture()
def examined(lib, monkeypatch):
    """«Клипы» с одним треком; YouTube Music находит студийную и загрузку,
    загрузка — без цензуры, файл — заглушён на «дурь»."""
    playlists.create(repair.PLAYLIST, [TRACK])
    monkeypatch.setattr(
        studio, "candidates",
        lambda artist, title: [found("stud", "studio", 1, explicit=True),
                               found("upl", "upload", 1)],
    )  # fmt: skip
    downloads = []

    def download(cand, into):
        downloads.append(cand.video_id)
        into.mkdir(parents=True, exist_ok=True)
        target = into / f"{cand.video_id}.m4a"
        shutil.copy(FIXTURE, target)
        return target

    monkeypatch.setattr(repair, "_download", download)
    monkeypatch.setattr(censor, "available", lambda: True)

    def compare(file, cand):
        if cand.stem == "upl":
            return compared(0.93, spots=[(30.0, 30.5, "file")], file_s=1, cand_s=1)
        return compared(0.99, file_s=1, cand_s=1)

    monkeypatch.setattr(censor, "compare", compare)
    monkeypatch.setattr(censor, "synced_lyrics", lambda artist, title, duration: LINES)
    monkeypatch.setattr(
        repair, "_cut", lambda source, at, target: (target.write_bytes(b"mp3"), True)[1]
    )
    repair.refresh()
    assert repair.process_one()
    return downloads


def test_a_queued_track_is_examined_and_shown(examined):
    assert examined == ["stud", "upl"]
    assert _row()["status"] == "ready"
    listing = repair.listing()
    item = listing["items"][0]
    assert listing["ready"] == 1
    assert item["best"] == "upl" and item["better"]
    assert item["problems"][0]["kind"] == "words"
    assert item["listen"] == {"current": True, "candidate": True}
    assert repair.excerpt(TRACK, "candidate").read_bytes() == b"mp3"
    # Не лучший больше не нужен: «Заменить» берёт лучший.
    assert not (repair._work_dir(TRACK) / "stud.m4a").exists()


def test_replace_puts_the_best_on_the_same_path_with_the_old_tags(examined, lib):
    before = lib.stat()
    original = lib.read_bytes()

    result = repair.replace(TRACK, "upl")

    assert result["replaced"]
    assert lib.exists() and lib.read_bytes() != original
    tags = MP4(lib).tags
    assert tags["\xa9cmt"] == ["мои теги"]
    assert tags["----:com.apple.iTunes:SOURCE_URL"][0].decode().endswith("v=upl")
    assert lib.stat().st_mtime_ns == before.st_mtime_ns  # дата добавления та же
    assert (repair.BACKUP_DIR / TRACK).read_bytes() == original
    assert _klipy() == []
    assert _row()["status"] == "done"
    assert not repair._work_dir(TRACK).exists()
    assert [r["path"] for r in repair.replaced_since(0)] == [TRACK]
    # Сделанное не возвращается в очередь.
    repair.refresh()
    assert _row()["status"] == "done"


def test_a_file_changed_after_the_examination_is_not_overwritten(examined, lib):
    audio = MP4(lib)
    audio["\xa9cmt"] = ["поправлено в панели"]
    audio.save()
    edited = lib.read_bytes()

    with pytest.raises(repair.Refused, match="изменился"):
        repair.replace(TRACK, "upl")

    assert lib.read_bytes() == edited
    assert _row()["status"] == "queued"
    assert _klipy() == [TRACK]


def test_keep_takes_it_out_of_klipy_for_good(examined):
    repair.keep(TRACK)
    assert _klipy() == []
    repair.refresh()
    assert _row()["status"] == "kept"
    assert not repair._work_dir(TRACK).exists()


def test_put_back_after_keep_it_is_examined_again(examined):
    repair.keep(TRACK)
    current = playlists.read(repair.PLAYLIST)
    playlists.write(repair.PLAYLIST, [TRACK], current.revision)
    repair.refresh()
    assert _row()["status"] == "queued"


def test_a_replacement_by_link_takes_it_out_of_klipy(examined):
    repair.after_replacement(TRACK, "Other/Singles/New.m4a")
    assert _klipy() == []
    assert _row()["status"] == "done"


def test_an_auto_queued_track_found_fine_leaves_klipy_by_itself(lib, monkeypatch):
    monkeypatch.setattr(studio, "candidates", lambda a, t: [found("stud", "studio", 1)])
    monkeypatch.setattr(repair, "_download", lambda cand, into: (
        into.mkdir(parents=True, exist_ok=True) or shutil.copy(FIXTURE, into / "stud.m4a")
        and into / "stud.m4a"))  # fmt: skip
    monkeypatch.setattr(censor, "available", lambda: True)
    monkeypatch.setattr(censor, "compare", lambda f, c: compared(0.99, file_s=1, cand_s=1))
    monkeypatch.setattr(censor, "synced_lyrics", lambda a, t, d: LINES)
    monkeypatch.setattr(repair, "_cut", lambda *a: True)

    repair.queue_auto(TRACK, "проверь")
    assert _klipy() == [TRACK]
    repair.process_one()

    assert _klipy() == []
    assert _row()["status"] == "kept"


def test_an_auto_queued_track_with_nothing_to_compare_stays(lib, monkeypatch):
    monkeypatch.setattr(studio, "candidates", lambda a, t: [])
    repair.queue_auto(TRACK, "проверь")
    repair.process_one()
    assert _klipy() == [TRACK]
    assert _row()["status"] == "ready"


def test_a_probe_with_drug_words_goes_to_klipy(lib, monkeypatch):
    monkeypatch.setattr(censor, "synced_lyrics", lambda a, t, d: LINES)
    repair.probe_later(TRACK, "студийная без сверки")
    assert _klipy() == []

    repair.process_one()

    assert _klipy() == [TRACK]
    assert _row()["status"] == "queued" and _row()["auto"] == 1


def test_a_probe_without_such_words_is_forgotten(lib, monkeypatch):
    monkeypatch.setattr(censor, "synced_lyrics", lambda a, t, d: [{"at": 1.0, "line": "Привет"}])
    repair.probe_later(TRACK, "студийная без сверки")
    repair.process_one()
    assert _klipy() == [] and _row() is None


def test_a_probe_waits_when_the_lyrics_catalogue_is_down(lib, monkeypatch):
    monkeypatch.setattr(censor, "synced_lyrics", lambda a, t, d: None)
    repair.probe_later(TRACK, "студийная без сверки")
    repair.process_one()
    row = _row()
    assert row["status"] == "probe" and row["next_at"] > time.time()
    assert not repair.process_one()  # до next_at — нечего делать


def test_youtube_music_down_retries_later(lib, monkeypatch):
    monkeypatch.setattr(studio, "candidates", lambda a, t: None)
    playlists.create(repair.PLAYLIST, [TRACK])
    repair.refresh()
    repair.process_one()
    row = _row()
    assert row["status"] == "queued" and row["next_at"] > time.time()
    assert "не ответил" in row["error"]


# ---------------------------------------------------------------------------
# Пределы YouTube
# ---------------------------------------------------------------------------


def test_downloads_wait_for_the_users_queue_and_the_pause(monkeypatch):
    waits = []
    monkeypatch.setattr(runtime, "PROCESSING_URLS", {"https://youtu.be/mine"})
    repair._last_download = time.monotonic()

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            runtime.PROCESSING_URLS.clear()
        else:
            repair._last_download = 0.0
        return False

    event = threading.Event()  # другой тест мог оставить общий взведённым
    monkeypatch.setattr(event, "wait", wait)
    monkeypatch.setattr(runtime, "shutdown_event", event)
    repair._wait_for_turn()
    assert len(waits) == 2
    assert 1.0 <= waits[0] <= repair.PAUSE_SECONDS


def test_the_hourly_limit_holds_downloads(monkeypatch):
    now = time.monotonic()
    repair._downloads.extend([now - 10] * repair.DOWNLOADS_PER_HOUR)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        repair._downloads.clear()
        return False

    event = threading.Event()  # другой тест мог оставить общий взведённым
    monkeypatch.setattr(event, "wait", wait)
    monkeypatch.setattr(runtime, "shutdown_event", event)
    repair._wait_for_turn()
    assert waits == [30.0]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _client(monkeypatch):
    from adder.app import app

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer test-secret"})
    return client


def test_the_api_lists_plays_and_replaces(examined, lib, monkeypatch):
    client = _client(monkeypatch)
    listing = client.get("/api/repairs").json()
    assert listing["items"][0]["path"] == TRACK
    sound = client.get("/api/repairs/listen", params={"path": TRACK, "which": "current"})
    assert sound.status_code == 200 and sound.headers["content-type"] == "audio/mpeg"
    assert client.get("/api/repairs/listen",
                      params={"path": TRACK, "which": "../x"}).status_code == 409  # fmt: skip

    done = client.post("/api/repairs/replace", json={"path": TRACK, "video_id": "upl"})
    assert done.status_code == 200, done.text
    again = client.post("/api/repairs/replace", json={"path": TRACK, "video_id": "upl"})
    assert again.status_code == 409
    replaced = client.get("/api/library/replaced", params={"since": 0}).json()
    assert [r["path"] for r in replaced["replaced"]] == [TRACK]


def test_the_api_needs_the_token(lib):
    from adder.app import app

    assert TestClient(app).get("/api/repairs").status_code in (401, 403)


def test_a_result_is_json_with_the_file_stamp(examined, lib):
    result = json.loads(_row()["result"])
    assert result["stamp"] == repair._stamp(lib)


def test_a_version_that_does_not_match_the_file_is_not_the_best():
    """Champagne Squirt, живой разбор 19.11.2026: загрузка со сходством 0,06
    стала «лучшей», а клип со сходством 0,98, отличающийся от файла на словах
    про наркотики, и был оригиналом."""
    result = repair.assess(
        233.0,
        [
            {"video_id": "upl", "kind": "upload", "duration": 234, "compared": compared(0.057)},
            {
                "video_id": "clip",
                "kind": "clip",
                "duration": 234,
                "compared": compared(0.976, spots=[(30.0, 30.5, "file"), (31.0, 31.5, "other")]),
            },
        ],  # fmt: skip
        LINES,
    )
    assert result["best"] == "clip" and result["better"]
    assert [p["kind"] for p in result["problems"]] == ["words"]


def test_when_no_version_matches_none_is_called_better():
    """BLESSED: клип 2:35, все три версии 1:55, звук не совпал ни с одной."""
    result = repair.assess(
        155.0,
        [
            {
                "video_id": "s",
                "kind": "studio",
                "duration": 115,
                "compared": compared(0.022, file_s=155, cand_s=115),
            },
            {
                "video_id": "u",
                "kind": "upload",
                "duration": 115,
                "compared": compared(0.021, file_s=155, cand_s=115),
            },
        ],  # fmt: skip
        LINES,
    )
    assert not result["better"]
    assert "Длиннее песни (2:35 против 1:55)" in result["problems"][0]["text"]


# ---------------------------------------------------------------------------
# После review 19.11.2026 (critic, code-reviewer)
# ---------------------------------------------------------------------------


def test_a_reupload_of_the_same_censored_audio_is_not_the_best():
    """Перезалив того же подцензурного клипа отличий от файла не даёт — это не
    «чисто». Лучшая — версия, у которой слово на месте тишины слышно."""
    result = repair.assess(
        233.0,
        [
            {"video_id": "copy", "kind": "upload", "duration": 234, "compared": compared(0.97)},
            {
                "video_id": "orig",
                "kind": "upload",
                "duration": 234,
                "compared": compared(0.93, spots=[(30.0, 30.5, "file")]),
            },
        ],  # fmt: skip
        LINES,
    )
    assert result["best"] == "orig" and result["better"]


def test_without_a_studio_a_file_of_the_lyrics_length_reads_words_in_its_own_time():
    """Файл — студийная из YouTube Music (её видео в кандидаты не берётся),
    кандидат — клип с 20-секундным вступлением: слово ищется по времени файла,
    а не по сдвигу клипа."""
    result = repair.assess(
        213.0,
        [
            {
                "video_id": "clip",
                "kind": "clip",
                "duration": 234,
                "compared": compared(
                    0.95, lag=-20.0, spots=[(30.0, 30.5, "file")], file_s=213, cand_s=234
                ),
            }
        ],  # fmt: skip
        LINES,
        lyrics_seconds=213.0,
    )
    assert [p["kind"] for p in result["problems"]] == ["words"]
    assert "курим" in result["problems"][0]["text"].lower()


def test_an_auto_queued_track_without_lyrics_is_not_dropped(lib, monkeypatch):
    """Положен за слова про наркотики — без текста слова не сверены: остаётся
    карточкой, а не уходит из «Клипов» молча."""
    monkeypatch.setattr(studio, "candidates", lambda a, t: [found("stud", "studio", 1)])
    monkeypatch.setattr(repair, "_download", lambda cand, into: (
        into.mkdir(parents=True, exist_ok=True) or shutil.copy(FIXTURE, into / "stud.m4a")
        and into / "stud.m4a"))  # fmt: skip
    monkeypatch.setattr(censor, "available", lambda: True)
    monkeypatch.setattr(censor, "compare", lambda f, c: compared(0.99, file_s=1, cand_s=1))
    monkeypatch.setattr(censor, "synced_lyrics", lambda a, t, d: [])
    monkeypatch.setattr(repair, "_cut", lambda *a: True)

    repair.queue_auto(TRACK, "в тексте слова про наркотики — не сверена")
    repair.process_one()

    assert _klipy() == [TRACK]
    assert _row()["status"] == "ready"


def test_when_every_download_fails_it_is_a_failure_not_a_verdict(lib, monkeypatch):
    monkeypatch.setattr(studio, "candidates", lambda a, t: [found("stud", "studio", 1)])

    def broken(cand, into):
        raise RuntimeError("ERROR: Sign in to confirm your age")

    monkeypatch.setattr(repair, "_download", broken)
    monkeypatch.setattr(censor, "available", lambda: True)
    playlists.create(repair.PLAYLIST, [TRACK])
    repair.refresh()
    repair.process_one()
    row = _row()
    assert row["status"] == "failed" and "ни одну версию" in row["error"]


def test_a_rate_limit_silences_the_background_for_an_hour(lib, monkeypatch):
    monkeypatch.setattr(studio, "candidates", lambda a, t: [found("stud", "studio", 1)])

    def limited(cand, into):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(repair, "_download", limited)
    monkeypatch.setattr(censor, "available", lambda: True)
    monkeypatch.setattr(runtime, "pause_youtube", lambda seconds: None)
    playlists.create(repair.PLAYLIST, [TRACK])
    repair.refresh()
    repair.process_one()
    assert repair._banned_until - time.monotonic() > 3000
    assert repair._users_turn()


def test_a_job_cut_by_a_restart_is_queued_again(lib):
    db.db_exec(
        "INSERT INTO repairs(path, status, auto, attempts, next_at, updated_at)"
        " VALUES (?, 'working', 0, 0, 0, ?)",
        (TRACK, time.time()),
    )
    repair._reset_working()
    assert _row()["status"] == "queued"


def test_a_playlist_that_cannot_be_read_keeps_the_examinations(examined, monkeypatch):
    def broken(name):
        raise OSError("диск занят")

    monkeypatch.setattr(playlists, "read", broken)
    repair.refresh()
    assert _row()["status"] == "ready"
    assert repair._work_dir(TRACK).exists()


def test_a_probe_track_the_user_puts_in_klipy_is_theirs(lib):
    repair.probe_later(TRACK, "студийная без сверки")
    playlists.create(repair.PLAYLIST, [TRACK])
    repair.refresh()
    assert _row()["status"] == "queued" and _row()["auto"] == 0


def test_a_new_studio_on_a_path_repaired_before_is_probed(examined):
    repair.replace(TRACK, "upl")
    repair.probe_later(TRACK, "моя ссылка при замене — студийная")
    assert _row()["status"] == "probe"


def test_a_change_that_keeps_mtime_and_size_still_stops_the_replace(examined, lib):
    """Правка тегов на месте сохраняет mtime и часто размер; ctime — нет."""
    lib.chmod(0o640)
    with pytest.raises(repair.Refused, match="изменился"):
        repair.replace(TRACK, "upl")


def test_replace_drops_the_old_loudness_tags(examined, lib, monkeypatch):
    from adder import loudness

    audio = MP4(lib)
    audio["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [b"-9.00 dB"]
    audio.save()
    monkeypatch.setattr(loudness, "apply", lambda path: None)  # замер не вышел
    repair.retry(TRACK)
    repair.process_one()
    repair.replace(TRACK, "upl")
    assert "----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN" not in MP4(lib).tags
