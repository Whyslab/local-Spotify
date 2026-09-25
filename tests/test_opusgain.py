"""Loudness put into the Opus header while streaming, for the iPhone."""

import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4, MP4FreeForm

from adder import config, covers, library, navidrome, opusgain, playlists, runtime

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"


def _opus_m4a(path: Path, gain: str | None = "-6.00 dB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(FIXTURE), "-c:a", "libopus",
         "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip
    if gain is not None:
        audio = MP4(path)
        audio["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(gain.encode())]
        audio.save()
    return path


def _loudness(data: bytes, tmp_path: Path) -> float:
    probe = tmp_path / "probe.m4a"
    probe.write_bytes(data)
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(probe), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    line = next(x for x in out.splitlines() if "mean_volume" in x)
    return float(line.split(":")[1].split()[0])


def test_the_header_field_is_found_in_opus_and_not_in_aac(tmp_path):
    opus = _opus_m4a(tmp_path / "a.m4a")
    offset = opusgain.gain_offset(opus)
    assert offset is not None
    assert struct.unpack(">h", opus.read_bytes()[offset : offset + 2])[0] == 0
    assert opusgain.gain_offset(FIXTURE) is None  # AAC has no dOps


def test_a_truncated_or_junk_file_is_not_an_error(tmp_path):
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"\x00\x00\x00\x01moov" + b"\xff" * 20)
    assert opusgain.gain_offset(junk) is None
    assert opusgain.gain_offset(tmp_path / "missing.m4a") is None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    _opus_m4a(config.LIBRARY / "A" / "Singles" / "Loud.m4a", "-6.00 dB")
    _opus_m4a(config.LIBRARY / "A" / "Singles" / "Quiet.m4a", "+3.00 dB")
    library.invalidate_library_index()

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def _url(client, path):
    return client.get("/api/stream-url", params={"path": path}).json()["url"]


def test_norm_turns_a_loud_track_down_by_its_replaygain(client, tmp_path):
    path = "A/Singles/Loud.m4a"
    plain = client.get(_url(client, path))
    normed = client.get(_url(client, path) + "&norm=1")
    assert plain.status_code == normed.status_code == 200

    on_disk = (config.LIBRARY / path).read_bytes()
    assert plain.content == on_disk
    # Same length, two bytes apart, and those are the header's gain.
    assert len(normed.content) == len(on_disk)
    differ = [i for i, (x, y) in enumerate(zip(on_disk, normed.content, strict=True)) if x != y]
    offset = opusgain.gain_offset(config.LIBRARY / path)
    assert set(differ) <= {offset, offset + 1}
    assert struct.unpack(">h", normed.content[offset : offset + 2])[0] == -6 * 256

    drop = _loudness(plain.content, tmp_path) - _loudness(normed.content, tmp_path)
    assert drop == pytest.approx(6.0, abs=0.3)


def test_a_quiet_track_is_not_turned_up(client):
    path = "A/Singles/Quiet.m4a"
    normed = client.get(_url(client, path) + "&norm=1")
    assert normed.content == (config.LIBRARY / path).read_bytes()


def test_ranges_work_with_the_patched_header(client):
    path = "A/Singles/Loud.m4a"
    url = _url(client, path) + "&norm=1"
    whole = client.get(url).content

    part = client.get(url, headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-1023/{len(whole)}"
    assert part.content == whole[:1024]

    tail = client.get(url, headers={"Range": "bytes=-100"})
    assert tail.status_code == 206 and tail.content == whole[-100:]

    offset = opusgain.gain_offset(config.LIBRARY / path)
    # A range that starts on the second byte of the field still gets it patched.
    split = client.get(url, headers={"Range": f"bytes={offset + 1}-{offset + 10}"})
    assert split.content == whole[offset + 1 : offset + 11]

    past = client.get(url, headers={"Range": f"bytes={len(whole) + 5}-"})
    assert past.status_code == 416


def test_norm_does_not_bypass_the_signature(client):
    response = client.get(
        "/api/stream", params={"path": "A/Singles/Loud.m4a", "exp": "1", "sig": "x", "norm": "1"}
    )
    assert response.status_code == 403
