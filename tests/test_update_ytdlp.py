"""The weekly yt-dlp update: upgrade, probe, roll back when the new one is broken."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "update_ytdlp", Path(__file__).resolve().parents[1] / "scripts" / "update_ytdlp.py"
)
update_ytdlp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update_ytdlp)


class World:
    """Installed versions, what pip is asked to do, and which versions can read YouTube."""

    def __init__(self, installed, latest, working):
        self.installed = dict(installed)
        self.latest = latest
        self.working = set(working)
        self.pip_calls = []
        self.notes = []

    def versions(self):
        return dict(self.installed)

    def pip(self, *args):
        self.pip_calls.append(args)
        if args[0] == "--upgrade":
            self.installed.update(self.latest)
        else:
            for pin in args:
                name, version = pin.split("==")
                self.installed[name] = version

    def probe(self):
        ok = self.installed["yt-dlp"] in self.working
        return ok, "" if ok else "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm"

    def notify(self, summary, body, urgent=False):
        self.notes.append(summary)


@pytest.fixture()
def world(monkeypatch):
    def make(installed, latest, working):
        w = World(installed, latest, working)
        for name in ("versions", "pip", "probe", "notify"):
            monkeypatch.setattr(update_ytdlp, name, getattr(w, name))
        return w

    return make


OLD = {"yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0"}
NEW = {"yt-dlp": "2026.9.20", "yt-dlp-ejs": "0.8.1"}


def test_nothing_new_means_no_probe(world):
    w = world(OLD, OLD, working=[])

    assert update_ytdlp.update() == 0
    assert w.installed == OLD
    assert w.notes == []


def test_a_working_new_version_is_kept(world):
    w = world(OLD, NEW, working=[NEW["yt-dlp"]])

    assert update_ytdlp.update() == 0
    assert w.installed == NEW


def test_a_broken_new_version_is_rolled_back(world):
    w = world(OLD, NEW, working=[OLD["yt-dlp"]])

    assert update_ytdlp.update() == 1
    assert w.installed == OLD
    assert w.pip_calls[-1] == ("yt-dlp==2026.8.19", "yt-dlp-ejs==0.8.0")
    assert w.notes == ["yt-dlp: обновление откатено"]


def test_when_both_fail_youtube_is_blamed_not_the_update(world):
    w = world(OLD, NEW, working=[])

    assert update_ytdlp.update() == 1
    assert w.installed == OLD
    assert w.notes == ["yt-dlp: YouTube недоступен"]
