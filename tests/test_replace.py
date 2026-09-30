"""Замена трека: новая версия встаёт на место старой, старая уезжает в корзину.

Сеть здесь не нужна: проверяется не загрузка, а то, что происходит после неё —
подмена пути в подборках. Именно она опасна: ошибись в ней, и в корзину уедет
не тот файл, а подборка останется со ссылкой в никуда.
"""

import pytest

from adder import config, playlists, runtime


@pytest.fixture
def library(monkeypatch, tmp_path):
    """Фонотека и подборки на временных путях — настоящие не трогаем."""
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(config, "LIBRARY", root)
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    for name in ("old.m4a", "new.m4a", "other.m4a"):
        (root / name).write_bytes(b"audio")
    return root


def _make(name: str, paths: list[str]) -> None:
    playlists.create(name, paths)


def _paths(name: str) -> list[str]:
    """Подборка отдаёт строки со своими номерами и тегами — нам нужны пути."""
    return [entry.path for entry in playlists.read(name).entries]


def test_swap_keeps_the_place_in_the_list(library):
    _make("Вечер", ["other.m4a", "old.m4a", "other.m4a"])

    changed = playlists.swap_everywhere("old.m4a", "new.m4a")

    assert changed == 1
    assert _paths("Вечер") == ["other.m4a", "new.m4a", "other.m4a"]


def test_swap_touches_every_playlist_that_has_the_track(library):
    _make("Первая", ["old.m4a"])
    _make("Вторая", ["other.m4a", "old.m4a"])
    _make("Третья", ["other.m4a"])

    changed = playlists.swap_everywhere("old.m4a", "new.m4a")

    assert changed == 2
    assert _paths("Третья") == ["other.m4a"]


def test_swap_replaces_every_copy_in_one_playlist(library):
    """Один трек может стоять в подборке дважды — заменить надо обе строки."""
    _make("Дубли", ["old.m4a", "other.m4a", "old.m4a"])

    playlists.swap_everywhere("old.m4a", "new.m4a")

    assert _paths("Дубли") == ["new.m4a", "other.m4a", "new.m4a"]


def test_swap_does_nothing_without_the_track(library):
    _make("Чужая", ["other.m4a"])

    assert playlists.swap_everywhere("old.m4a", "new.m4a") == 0
    assert _paths("Чужая") == ["other.m4a"]


@pytest.mark.parametrize("odd", [".sync-conflict", "x" * 130, "a\x01b"])
def test_an_odd_playlist_file_is_left_out_and_does_not_stop_the_swap(library, odd):
    """Файл, чьё имя не прошло бы safe_name, валил каждую замену ошибкой 400.

    Новый трек к тому времени уже лежал в фонотеке, а старый оставался во
    всех подборках.
    """
    (library / f"{odd}.m3u").write_text("#EXTM3U\nold.m4a\n", encoding="utf-8")
    _make("Вечер", ["old.m4a"])

    assert [p["name"] for p in playlists.listing()] == ["Вечер"]
    assert playlists.swap_everywhere("old.m4a", "new.m4a") == 1
    assert _paths("Вечер") == ["new.m4a"]


def test_one_playlist_that_cannot_be_written_does_not_stop_the_others(library, monkeypatch):
    _make("Первая", ["old.m4a"])
    _make("Вторая", ["old.m4a"])
    real = playlists._atomic_write

    def flaky(path, text):
        if path.stem == "Вторая":
            raise OSError("disk says no")
        real(path, text)

    monkeypatch.setattr(playlists, "_atomic_write", flaky)

    assert playlists.swap_everywhere("old.m4a", "new.m4a") == 1
    assert _paths("Первая") == ["new.m4a"]


def test_still_pointing_at_sees_every_m3u_including_hidden_ones(library):
    _make("Mix", ["old.m4a"])
    (library / ".conflict.m3u").write_text("#EXTM3U\nold.m4a\n")
    (library / "Other.m3u").write_text("#EXTM3U\nother.m4a\n")

    assert playlists.still_pointing_at("old.m4a") == [".conflict.m3u", "Mix.m3u"]
    playlists.swap_everywhere("old.m4a", "new.m4a")
    assert playlists.still_pointing_at("old.m4a") == [".conflict.m3u"]
