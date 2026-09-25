"""Downloading: how yt-dlp is invoked and how its results and failures are read.

run_yt_dlp is mocked wherever YouTube would be involved. The tests of
run_yt_dlp itself start a local Python child process, never yt-dlp.
"""

import json
import subprocess
import sys

import pytest

from adder import config, runtime


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from adder import config, ingest, runtime

    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "")
    runtime.TMP_DIR.mkdir()
    runtime.shutdown_event.clear()
    return ingest


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeYtDlp:
    """Stands in for run_yt_dlp; records each command and replies as configured."""

    def __init__(self, reply):
        self.reply = reply
        self.commands = []

    def __call__(self, cmd, timeout):
        self.commands.append(cmd)
        return self.reply(cmd)


# ---------------------------------------------------------------------------
# yt_meta
# ---------------------------------------------------------------------------


def test_meta_parses_json_and_never_expands_playlists(app, monkeypatch):
    info = {"id": "abc123", "title": "Artist - Song", "uploader": "Artist"}
    fake = FakeYtDlp(lambda cmd: completed(cmd, stdout=json.dumps(info)))
    monkeypatch.setattr(app, "run_yt_dlp", fake)

    assert app.yt_meta("https://www.youtube.com/watch?v=abc123") == info

    cmd = fake.commands[0]
    assert cmd[cmd.index(sys.executable) :][:3] == [sys.executable, "-m", "yt_dlp"]
    assert "-J" in cmd
    assert "--no-playlist" in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc123"
    assert "--cookies-from-browser" not in cmd


def test_cookies_are_sent_with_the_metadata_request(app, monkeypatch):
    # YouTube's bot check applies to the metadata request, which runs first.
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "firefox")
    fake = FakeYtDlp(lambda cmd: completed(cmd, stdout="{}"))
    monkeypatch.setattr(app, "run_yt_dlp", fake)

    app.yt_meta("https://www.youtube.com/watch?v=abc123")

    cmd = fake.commands[0]
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"


def test_meta_failure_reports_the_error_line(app, monkeypatch):
    stderr = (
        "WARNING: [youtube] No supported JavaScript runtime could be found\n"
        "ERROR: [youtube] abc123: Video unavailable. This video has been removed by the uploader\n"
    )
    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(lambda cmd: completed(cmd, 1, "", stderr)))

    with pytest.raises(RuntimeError) as excinfo:
        app.yt_meta("https://www.youtube.com/watch?v=abc123")

    assert str(excinfo.value).startswith("ERROR: [youtube] abc123: Video unavailable")
    assert app.classify_error(str(excinfo.value)) == "youtube_not_found"


# ---------------------------------------------------------------------------
# yt_download
# ---------------------------------------------------------------------------


def test_download_extracts_m4a_into_tmp_and_returns_it(app, monkeypatch):
    def reply(cmd):
        (runtime.TMP_DIR / "abc123.m4a").write_bytes(b"audio")
        return completed(cmd)

    fake = FakeYtDlp(reply)
    monkeypatch.setattr(app, "run_yt_dlp", fake)

    result = app.yt_download("https://www.youtube.com/watch?v=abc123", "abc123")

    assert result == runtime.TMP_DIR / "abc123.m4a"
    cmd = fake.commands[0]
    # The best stream (Opus on YouTube) is extracted as it is, never re-encoded.
    assert cmd[cmd.index("--audio-format") + 1] == "best"
    assert "--audio-quality" not in cmd
    assert cmd[cmd.index("-f") + 1] == "bestaudio/best"
    assert cmd[cmd.index("-o") + 1] == str(runtime.TMP_DIR / "abc123.%(ext)s")
    assert "--no-playlist" in cmd


def test_an_unreadable_non_m4a_download_is_returned_as_it_is(app, monkeypatch):
    # Not audio at all: left for the integrity check, which decodes and rejects it.
    def reply(cmd):
        (runtime.TMP_DIR / "abc123.mp4").write_bytes(b"audio")
        return completed(cmd)

    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(reply))

    assert app.yt_download("u", "abc123") == runtime.TMP_DIR / "abc123.mp4"


FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "tone.m4a"


def _codec(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def _packets(path):
    """Checksum of the compressed audio itself, whatever the container."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def _opus(tmp_path):
    source = tmp_path / "abc123.opus"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(FIXTURE), "-c:a", "libopus", str(source)],
        check=True,
    )  # fmt: skip
    return source


def test_opus_download_is_moved_into_m4a_without_re_encoding(app, monkeypatch):
    source = _opus(runtime.TMP_DIR)
    expected = _packets(source)

    def reply(cmd):
        return completed(cmd)  # yt-dlp "produced" the .opus made above

    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(reply))

    result = app.yt_download("u", "abc123")

    assert result == runtime.TMP_DIR / "abc123.m4a"
    assert _codec(result) == "opus"
    assert _packets(result) == expected  # the very same Opus data: copied, not encoded
    assert not source.exists()
    assert sorted(p.name for p in runtime.TMP_DIR.iterdir()) == ["abc123.m4a"]


def test_a_codec_mp4_cannot_carry_is_converted_to_aac(app, tmp_path):
    source = tmp_path / "x.ogg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(FIXTURE), "-c:a", "libvorbis", str(source)],
        check=True,
    )  # fmt: skip

    result = app.ensure_m4a(source)

    assert result == tmp_path / "x.m4a"
    assert _codec(result) == "aac"
    ok, message = app.validate_audio_integrity(result, from_outside=True)
    assert ok, message


def test_an_aac_mp4_download_becomes_m4a_untouched(app, monkeypatch):
    source = runtime.TMP_DIR / "abc123.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(FIXTURE), "-c", "copy", "-f", "mp4", str(source)],
        check=True,
    )  # fmt: skip
    # With a ReplayGain tag: ffprobe's csv output then reads "aac," (an empty
    # side-data field), which once passed for an unknown codec and sent good
    # audio through a second encode.
    from mutagen.mp4 import MP4, MP4FreeForm

    tagged = MP4(source)
    tagged["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(b"-3.00 dB")]
    tagged.save()
    expected = _packets(source)
    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(lambda cmd: completed(cmd)))

    result = app.yt_download("u", "abc123")

    assert result == runtime.TMP_DIR / "abc123.m4a"
    assert _codec(result) == "aac"
    assert _packets(result) == expected


def test_lossless_audio_stays_lossless(app, tmp_path):
    source = tmp_path / "x.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(FIXTURE), str(source)], check=True)

    assert _codec(app.ensure_m4a(source)) == "alac"


def test_a_failed_remux_leaves_nothing_behind_and_reads_as_a_download_error(
    app, tmp_path, monkeypatch
):
    source = _opus(tmp_path)
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[0] == "ffmpeg":
            (tmp_path / "abc123.m4a.tmp").write_bytes(b"half")
            # ffmpeg's own words must not decide the error class.
            return subprocess.CompletedProcess(cmd, 1, "", "Stream map '0:a:0' not found")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(app.subprocess, "run", run)

    with pytest.raises(RuntimeError) as excinfo:
        app.ensure_m4a(source)

    assert app.classify_error(str(excinfo.value)) == "download_error"
    assert sorted(p.name for p in tmp_path.glob("abc123.*")) == ["abc123.opus"]


def test_a_remux_that_hangs_is_cleaned_up(app, tmp_path, monkeypatch):
    source = _opus(tmp_path)
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[0] == "ffmpeg":
            (tmp_path / "abc123.m4a.tmp").write_bytes(b"half")
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(app.subprocess, "run", run)

    with pytest.raises(RuntimeError) as excinfo:
        app.ensure_m4a(source)

    assert app.classify_error(str(excinfo.value)) == "download_error"
    assert sorted(p.name for p in tmp_path.glob("abc123.*")) == ["abc123.opus"]


def test_an_unreadable_download_is_left_for_the_integrity_check(app, tmp_path):
    junk = tmp_path / "x.opus"
    junk.write_bytes(b"not audio")

    assert app.ensure_m4a(junk) == junk
    assert junk.exists()


def test_download_without_output_file_is_an_error(app, monkeypatch):
    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(lambda cmd: completed(cmd)))

    with pytest.raises(RuntimeError):
        app.yt_download("u", "abc123")


def test_missing_ffmpeg_surfaces_as_a_dependency_error(app, monkeypatch):
    stderr = "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install\n"
    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(lambda cmd: completed(cmd, 1, "", stderr)))

    with pytest.raises(RuntimeError) as excinfo:
        app.yt_download("u", "abc123")

    assert app.classify_error(str(excinfo.value)) == "dependency_error"


# ---------------------------------------------------------------------------
# run_yt_dlp, with a local child process standing in for yt-dlp
# ---------------------------------------------------------------------------


def python(code):
    return [sys.executable, "-c", code]


def test_run_collects_large_output_without_deadlocking(app):
    # More than a pipe buffer on both streams at once.
    code = "import sys; sys.stdout.write('o' * 300000); sys.stderr.write('e' * 300000)"

    result = app.run_yt_dlp(python(code), timeout=30)

    assert result.returncode == 0
    assert len(result.stdout) == 300000
    assert len(result.stderr) == 300000


def test_run_reports_a_non_zero_exit(app):
    result = app.run_yt_dlp(
        python("import sys; print('ERROR: boom', file=sys.stderr); sys.exit(1)"), 30
    )

    assert result.returncode == 1
    assert app.ytdlp_error(result.stderr) == "ERROR: boom"


def test_run_kills_a_hung_process_on_timeout(app):
    with pytest.raises(subprocess.TimeoutExpired):
        app.run_yt_dlp(python("import time; time.sleep(30)"), timeout=0.5)


def test_run_stops_the_process_on_shutdown(app):
    runtime.shutdown_event.set()
    try:
        with pytest.raises(runtime.ShutdownRequested):
            app.run_yt_dlp(python("import time; time.sleep(30)"), timeout=30)
    finally:
        runtime.shutdown_event.clear()


def test_yt_dlp_and_deno_cache_in_a_writable_place(app, tmp_path, monkeypatch):
    # ~/.cache is read-only under the systemd unit; adder/ is writable.
    monkeypatch.setattr(runtime, "CACHE_DIR", tmp_path / "cache")

    result = app.run_yt_dlp(python("import os; print(os.environ['XDG_CACHE_HOME'])"), timeout=30)

    assert result.stdout.strip() == str(tmp_path / "cache")
    assert (tmp_path / "cache").is_dir()


def test_an_unfinished_part_file_is_never_taken_for_the_result(app, monkeypatch):
    def reply(cmd):
        (runtime.TMP_DIR / "abc123.webm.part").write_bytes(b"half")
        (runtime.TMP_DIR / "abc123.ytdl").write_bytes(b"state")
        return completed(cmd)

    monkeypatch.setattr(app, "run_yt_dlp", FakeYtDlp(reply))

    with pytest.raises(RuntimeError, match="produced no audio file") as excinfo:
        app.yt_download("u", "abc123")
    # Not a missing video: this must not read as youtube_not_found.
    assert app.classify_error(str(excinfo.value)) != "youtube_not_found"
