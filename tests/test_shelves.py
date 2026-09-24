"""Полки главной, выгруженные в файлы: что на телефоне окажется ровно то же.

Фонотека здесь временная, и наружу тесты не ходят: `discover` спрашивает у
Deezer похожих артистов, поэтому она подменяется. Настоящая фонотека и
настоящая сеть в тестах означали бы, что прогон зависит от того, что сегодня
ответил Deezer и что лежит в ~/Music.
"""

import pytest

from adder import config, db, library, playlists, shelves

# Сорока измеренных треков хватает, чтобы у moods.collections появились все
# четыре полки: границы там — четверти самой фонотеки, а каждая полка
# показывается только от восьми треков. Темп, энергия и яркость растут вместе,
# поэтому «В машину» (быстрее и громче половины) непуст.
TOTAL = 40


def _features() -> list[dict]:
    return [
        {
            "path": f"Artist {i:02d}/Singles/Track {i:02d}.m4a",
            "tempo": 80.0 + i,
            "energy": 0.10 + i * 0.01,
            "brightness": 1000.0 + i * 50,
        }
        for i in range(TOTAL)
    ]


def _index_rows() -> list[dict]:
    return [
        {
            "path": row["path"],
            "artist": f"Artist {i:02d}",
            "title": f"Track {i:02d}",
            "album": "",
            "duration": 180.0,
            "albumartist": "",
            "track": None,
            "haystack": "",
        }
        for i, row in enumerate(_features())
    ]


def _discover_returning(count: int):
    """Подмена `discover`: та же форма ответа, но без похода в сеть."""
    paths = [row["path"] for row in _features()[-count:]] if count else []
    return lambda rows, **kwargs: {"based_on": [], "tracks": [{"path": p} for p in paths]}


@pytest.fixture()
def shelf_library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    config.LIBRARY.mkdir(parents=True)

    rows = _index_rows()
    monkeypatch.setattr(library, "library_index", lambda: rows)
    monkeypatch.setattr(db, "db_query", lambda sql, params=(): _features())
    monkeypatch.setattr(shelves, "discover", _discover_returning(10))
    return config.LIBRARY


ALL_SHELVES = {
    "★ В машину",
    "★ Разогнаться",
    "★ На работу",
    "★ Поярче",
    "★ Может понравиться",
}


def test_every_shelf_becomes_a_playlist_with_the_star(shelf_library):
    """Приставка — то, по чему полку видно в Amperfy и отличают от своей подборки."""
    report = shelves.export()

    assert {item["name"] for item in report} == ALL_SHELVES
    assert all(item["written"] for item in report)
    assert {path.name for path in shelf_library.glob("*.m3u")} == {
        f"{name}.m3u" for name in ALL_SHELVES
    }


def test_written_file_is_an_m3u_of_relative_paths(shelf_library):
    """Navidrome читает пути от корня фонотеки; абсолютный путь он не найдёт."""
    report = {item["name"]: item for item in shelves.export()}
    text = (shelf_library / "★ Разогнаться.m3u").read_text(encoding="utf-8")

    assert text.startswith("#EXTM3U")
    paths = playlists.parse(text)
    # Число строк — то же, что скрипт назовёт в отчёте: иначе отчёт врёт.
    assert len(paths) == report["★ Разогнаться"]["count"]
    assert all(not path.startswith("/") for path in paths)


def test_a_short_shelf_is_skipped_and_leaves_yesterdays_file_alone(shelf_library, monkeypatch):
    """Вчерашняя подборка лучше пустой: полка из трёх треков файл не трогает."""
    monkeypatch.setattr(shelves, "discover", _discover_returning(3))
    name = f"{shelves.PREFIX}{shelves.DISCOVER_NAME}"
    playlists.create(name, ["Artist 00/Singles/Track 00.m4a"])
    before = playlists.playlist_path(name).read_text(encoding="utf-8")

    report = {item["name"]: item for item in shelves.export()}

    assert report[name]["written"] is False
    assert report[name]["count"] == 3
    assert playlists.playlist_path(name).read_text(encoding="utf-8") == before
    # И пропуск не должен выглядеть как перезапись: копии в истории нет.
    assert not (playlists.HISTORY_DIR / name).exists()


def test_a_second_run_overwrites_a_hand_edit(shelf_library):
    """Обещание из README: правка внутри `★ ...` не живёт, но и не теряется."""
    shelves.export()
    path = shelf_library / "★ На работу.m3u"
    original = path.read_text(encoding="utf-8")
    path.write_text("#EXTM3U\nПравка руками.m4a\n", encoding="utf-8")

    shelves.export()

    assert path.read_text(encoding="utf-8") == original
    archived = list((playlists.HISTORY_DIR / "★ На работу").glob("*.m3u"))
    assert any("Правка руками.m4a" in copy.read_text(encoding="utf-8") for copy in archived)


def test_a_measured_track_that_left_the_library_is_not_written(shelf_library, monkeypatch):
    """Строка в audio_features живёт дольше файла, а битая строка в подборке — нет."""
    ghost = "Удалённый/Singles/Нет его.m4a"
    measured = _features() + [
        # Самый быстрый, громкий и звонкий: без отбора по индексу он попал бы
        # сразу в три полки.
        {"path": ghost, "tempo": 200.0, "energy": 0.99, "brightness": 9000.0}
    ]
    monkeypatch.setattr(db, "db_query", lambda sql, params=(): measured)

    shelves.export()

    for file in shelf_library.glob("*.m3u"):
        assert ghost not in playlists.parse(file.read_text(encoding="utf-8"))
