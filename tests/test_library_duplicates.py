"""Одинаковые песни во всей фонотеке (15.11.2026): найти, показать, решить."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from adder import config, db, duplicates, library, playlists, runtime

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
AUTH = {"Authorization": "Bearer test-secret"}


def row(artist, title, path=None):
    return {
        "artist": artist,
        "albumartist": "",
        "title": title,
        "path": path or f"{artist}/{title}",
    }


def grouped(*rows):
    return sorted(sorted(r["path"] for r in g) for g in duplicates._raw_groups(list(rows)))


def test_one_song_under_two_names_is_one_group():
    assert grouped(row("Валентин Стрыкало", "Кайен", "a"), row("Валентин Стрыкало", "Кайен", "b"))
    assert grouped(row("PHARAOH", "Smart", "a"), row("PHARAOH", "Смарт", "b"))
    assert grouped(
        row("GONE.Fludd • CAKEBOY", "GLAM (Prod. by InfinityRize) [FlexStarMusic - Audio]", "a"),
        row("GONE.Fludd • Cakeboy", "GLAM!", "b"),
    )
    assert grouped(
        row("GONE.Fludd", "Агент Паранойи (фан-клип)", "a"),
        row("GONE.Fludd", "Агент Паранойи", "b"),
    )
    # Артист, попавший в название из имени ролика.
    assert grouped(row("MAYOT", "MAYOT – Не поменяло", "a"), row("MAYOT", "Не поменяло", "b"))
    # Соавтор: общий артист хоть один.
    assert grouped(row("Aarne • Liza Evans", "РЕВНУЮ", "a"), row("Liza Evans", "Ревную", "b"))


def test_other_versions_and_other_artists_are_not_the_same_song():
    assert not grouped(row("A", "Song", "a"), row("A", "Song (Speed Up)", "b"))
    assert not grouped(row("A", "Song", "a"), row("A", "Song (Remix)", "b"))
    assert not grouped(row("A", "Song", "a"), row("B", "Song", "b"))
    assert not grouped(row("A", "Song", "a"))
    # Мусорное слово рядом со словом версии не делает скобку мусором.
    for other in ("Song (Live Video)", "Song (Sped Up Audio)", "Song (Remix ft. B)",
                  "Song (Official Remix)", "Song (Nightcore Lyrics)", "Song (prod. X) (Remix)",
                  "Song - Official Remix", "Song (ремикс)"):  # fmt: skip
        assert not grouped(row("A", "Song", "a"), row("A", other, "b")), other
    # А просто мусор — по-прежнему та же песня.
    for same in ("Song (Official Video)", "Song [prod. by X]", "Song - Lyrics", "Song (feat. B)",
                 "Song (Remastered Version)", "Song (Explicit Version)", "Song (Clean Edit)",
                 "Song (feat. Live)", "Song (prod. X"):  # fmt: skip
        assert grouped(row("A", "Song", "a"), row("A", same, "b")), same


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(runtime, "DUPLICATES_DISMISSED_FILE", tmp_path / "dismissed.json")
    monkeypatch.setattr(runtime, "guard_real_library", lambda *a, **k: None)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    config.LIBRARY.mkdir()
    library.invalidate_library_index()
    db.db_init()
    return tmp_path


def put(rel, artist, title):
    target = config.LIBRARY / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, target)
    audio = MP4(target)
    audio["\xa9ART"] = [artist]
    audio["\xa9nam"] = [title]
    audio["\xa9cmt"] = [rel]  # разные байты
    audio.save()
    library.invalidate_library_index()
    return rel


@pytest.fixture()
def twins(env):
    return [put("Strykalo/Singles/Кайен.m4a", "Стрыкало", "Кайен"),
            put("Strykalo/Singles/Кайен (1).m4a", "Стрыкало", "Кайен")]  # fmt: skip


def test_twins_are_listed_with_what_to_choose_by(twins):
    (group,) = duplicates.library_groups()
    assert sorted(t["path"] for t in group["tracks"]) == sorted(twins)
    assert all(t["codec"] and t["size"] > 0 for t in group["tracks"])


def test_keeping_one_moves_the_rest_to_the_trash_and_playlists_follow(twins):
    keep, gone = twins
    playlists.create("Mix", [gone])

    result = duplicates.keep_one(twins, keep)

    assert result["removed"] == [gone]
    assert not (config.LIBRARY / gone).exists()
    assert (runtime.TRASH_DIR / gone).exists()
    assert [e.path for e in playlists.read("Mix").entries] == [keep]
    library.invalidate_library_index()
    assert duplicates.library_groups() == []


def test_a_playlist_that_does_not_follow_leaves_every_copy_in_place(twins, monkeypatch):
    """Из трёх экземпляров подборка третьего не обновилась — не удалён ни
    один, а не второй уже в корзине под ответом «оставлен» (рецензент 15.11)."""
    third = put("Strykalo/Singles/Кайен (2).m4a", "Стрыкало", "Кайен")
    group = [*twins, third]
    real = playlists.still_pointing_at
    monkeypatch.setattr(
        playlists, "still_pointing_at", lambda p: ["Mix"] if p == third else real(p)
    )
    with pytest.raises(RuntimeError, match="ничего не удалено"):
        duplicates.keep_one(group, twins[0])
    assert all((config.LIBRARY / p).exists() for p in group)
    library.invalidate_library_index()
    (left,) = duplicates.library_groups()
    assert len(left["tracks"]) == 3


def test_a_pair_already_asked_after_a_download_is_not_asked_twice(twins, monkeypatch):
    """Пару «похожий трек» спрашивает раздел загрузок; на главной она не
    повторяется — и подсчёт для главной не читает файлы (рецензент 15.11)."""
    tid = db.db_exec("INSERT INTO tasks(url, status) VALUES('u', 'done')", ()).lastrowid
    db.task_update(tid, result_path=twins[1], warning="похоже", similar_to=twins[0])
    monkeypatch.setattr(duplicates, "describe", lambda p: pytest.fail("прочитал файл"))
    assert duplicates.open_count() == 0


def test_keep_all_hides_the_group_until_a_third_copy_comes(twins):
    duplicates.keep_all(twins)
    assert duplicates.library_groups() == []
    assert (config.LIBRARY / twins[1]).exists()

    put("Strykalo/Singles/Кайен (2).m4a", "Стрыкало", "Кайен")
    (group,) = duplicates.library_groups()
    assert len(group["tracks"]) == 3


def test_the_api_removes_only_what_the_search_called_the_same(twins, env):
    from adder import app as app_module

    other = put("Other/Singles/Another.m4a", "Other", "Another")
    with TestClient(app_module.app) as client:
        assert client.get("/api/duplicates/library").status_code == 401
        assert len(client.get("/api/duplicates/library", headers=AUTH).json()) == 1

        # Не группа: так нельзя стереть произвольный трек.
        refused = client.post(
            "/api/duplicates/library/resolve",
            json={"paths": [twins[0], other], "keep": twins[0]},
            headers=AUTH,
        )
        assert refused.status_code == 404
        assert (config.LIBRARY / other).exists()

        bad = client.post(
            "/api/duplicates/library/resolve",
            json={"paths": twins, "keep": other},
            headers=AUTH,
        )
        assert bad.status_code == 400

        done = client.post(
            "/api/duplicates/library/resolve", json={"paths": twins, "keep": twins[0]}, headers=AUTH
        )
        assert done.status_code == 200
        assert not (config.LIBRARY / twins[1]).exists()
        assert client.get("/api/duplicates/library", headers=AUTH).json() == []
