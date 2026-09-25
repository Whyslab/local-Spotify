"""scripts/duplicate_audit.py and scripts/audit_library.py find tag duplicates."""

import importlib.util
from pathlib import Path

from mutagen.mp4 import MP4

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tagged(path: Path, artist: str, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FIXTURE.read_bytes())
    audio = MP4(path)
    audio["\xa9ART"] = [artist]
    audio["\xa9nam"] = [title]
    audio.save()
    return path


def test_metadata_is_read_from_mp4_tag_lists(tmp_path):
    # Tags are lists; .lower() on the list raised and every file read as untagged.
    dup = _load("duplicate_audit")
    meta = dup.extract_metadata(_tagged(tmp_path / "a.m4a", "Artist", "Song"))
    assert (meta["artist"], meta["title"]) == ("artist", "song")


def test_audit_library_groups_two_copies_of_one_song(tmp_path):
    audit = _load("audit_library")
    one = _tagged(tmp_path / "A" / "one.m4a", "Artist", "Song")
    two = _tagged(tmp_path / "B" / "two.m4a", "Artist", "Song")
    other = _tagged(tmp_path / "C" / "x.m4a", "Artist", "Other")
    groups = audit.find_duplicates([one, two, other])
    assert [sorted(p.name for p in group) for group in groups] == [["one.m4a", "two.m4a"]]
