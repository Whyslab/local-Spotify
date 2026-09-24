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
