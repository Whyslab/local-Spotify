"""Тесты скрипта разбора — того, что три недели молча не работало.

Оба свойства проверяются здесь потому, что оба однажды уже отсутствовали и
обошлись фонотеке в девять неизмеренных треков: кэш numba должен уезжать в
папку, доступную на запись из песочницы службы, а неизмеренный трек обязан
возвращаться вызывающей стороне как ошибка, а не как «всё хорошо».
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_script():
    """Скрипт лежит вне пакета, поэтому подгружаем его файлом."""
    path = REPO / "scripts" / "analyze_audio.py"
    spec = importlib.util.spec_from_file_location("analyze_audio_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_numba_cache_points_somewhere_writable():
    """Иначе numba пишет рядом с librosa — а там только чтение, и разбор падает."""
    _load_script()  # переменная не нужна: проверяется след импорта в окружении

    import os

    assert os.environ["NUMBA_CACHE_DIR"] == str(REPO / "adder" / "numba-cache")


def test_failed_track_is_reported_as_failure(tmp_path, monkeypatch, capsys):
    """Неизмеренный трек — ненулевой код выхода: только по нему служба и узнаёт о сбое."""
    module = _load_script()

    library = tmp_path / "library"
    library.mkdir()
    broken = library / "broken.m4a"
    broken.write_bytes(b"not audio at all")

    db = tmp_path / "test.db"

    monkeypatch.setattr(
        sys, "argv", ["analyze_audio.py", "--library", str(library), "--db", str(db)]
    )

    assert module.main() == 1
    assert "с ошибкой 1" in capsys.readouterr().out


def test_measured_track_is_stored_and_reported_ok(tmp_path, monkeypatch, capsys):
    """Обратная сторона того же контракта: измеренный трек даёт 0 и строку в базе."""
    module = _load_script()

    library = tmp_path / "library"
    library.mkdir()
    track = library / "ok.m4a"
    track.write_bytes(b"pretend audio")

    db = tmp_path / "test.db"

    # Сам разбор здесь не нужен: проверяется контракт вокруг него, а не librosa.
    monkeypatch.setattr(
        module,
        "analyse",
        lambda path: {
            "tempo": 120.0,
            "energy": 0.5,
            "brightness": 2000.0,
            "music_key": "C",
            "mode": "major",
        },
    )
    monkeypatch.setattr(
        sys, "argv", ["analyze_audio.py", "--library", str(library), "--db", str(db)]
    )

    assert module.main() == 0
    assert "измерено 1" in capsys.readouterr().out

    con = sqlite3.connect(db)
    rows = con.execute("SELECT path, tempo, music_key FROM audio_features").fetchall()
    con.close()
    assert rows == [("ok.m4a", 120.0, "C")]
