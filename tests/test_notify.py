"""Desktop notifications: batched, worded for people, and never in the way."""

import pytest

from adder import config, notify


@pytest.fixture()
def shown(monkeypatch):
    """Capture what would pop up instead of calling notify-send."""
    popups = []
    monkeypatch.setattr(config, "DESKTOP_NOTIFICATIONS", True)
    monkeypatch.setattr(notify, "BATCH_SECONDS", 60)  # flushed by hand below
    monkeypatch.setattr(notify, "_show", lambda s, b, urgent: popups.append((s, b, urgent)))
    monkeypatch.setattr(notify, "_added", notify._Batch(*_args(notify._added)))
    monkeypatch.setattr(notify, "_failed", notify._Batch(*_args(notify._failed)))
    yield popups
    notify.flush_now()


def _args(batch):
    return batch.one, batch.many, batch.urgent


def test_one_track_is_named(shown):
    notify.track_added("Adele", "Hello")
    notify.flush_now()

    assert shown == [("Трек добавлен", "Adele — Hello", False)]


def test_a_playlist_import_is_one_notification(shown):
    for n in range(12):
        notify.track_added("A", f"Song {n}")
    notify.flush_now()

    assert len(shown) == 1
    summary, body, urgent = shown[0]
    assert summary == "Добавлено треков: 12"
    assert body.splitlines() == ["A — Song 0", "A — Song 1", "A — Song 2", "и ещё 9"]


def test_failures_are_urgent_and_separate(shown):
    notify.track_added("A", "B")
    notify.track_failed("C — D", "видео недоступно")
    notify.flush_now()

    assert ("Не удалось скачать", "C — D: видео недоступно", True) in shown
    assert len(shown) == 2


def test_switched_off_means_nothing_is_gathered(shown, monkeypatch):
    monkeypatch.setattr(config, "DESKTOP_NOTIFICATIONS", False)

    notify.track_added("A", "B")
    notify.flush_now()

    assert shown == []


def test_missing_notify_send_is_not_an_error(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)

    notify._show("x", "y", False)  # must simply return


def test_the_session_bus_is_found_when_the_unit_did_not_pass_it(monkeypatch, tmp_path):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(notify, "Path", lambda p: tmp_path / "bus")
    (tmp_path / "bus").touch()

    assert notify._environment()["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={tmp_path / 'bus'}"


def test_notify_send_is_called_with_the_desktop_conventions(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    notify._show("Трек добавлен", "Adele — Hello", urgent=True)

    (cmd,) = calls
    assert cmd[0] == "/usr/bin/notify-send"
    assert cmd[cmd.index("--app-name") + 1] == "local-Spotify"
    assert cmd[cmd.index("--urgency") + 1] == "critical"
    assert cmd[-2:] == ["Трек добавлен", "Adele — Hello"]
