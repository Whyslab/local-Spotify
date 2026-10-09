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


def _ffmpeg(*args):
    import subprocess

    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def test_quality_audit_flags_16khz_cutoff_and_clipping(tmp_path):
    """Три трека: целый шум (Opus в .m4a, как фонотека), шум со стеной на
    16 кГц (как AAC с YouTube) и перегруженный синус (срезанные пики)."""
    import pytest

    pytest.importorskip("scipy")
    quality_audit = _load("quality_audit")
    lib = tmp_path / "lib"
    (lib / "A" / "Singles").mkdir(parents=True)
    noise = "anoisesrc=d=40:c=pink:r=48000:a=0.3:seed=1"
    _ffmpeg("-f", "lavfi", "-i", noise, "-ac", "2", "-c:a", "libopus", "-b:a", "160k",
            "-f", "mp4", str(lib / "A" / "Singles" / "full.m4a"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", noise, "-af", "firequalizer=gain='if(gt(f,16000),-120,0)'",
            "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(lib / "A" / "Singles" / "cut.m4a"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=40:r=48000", "-af", "volume=12",
            "-ac", "2", "-c:a", "flac", "-sample_fmt", "s16",
            str(lib / "A" / "Singles" / "loud.flac"))  # fmt: skip

    # Громкий мастер после сжатия: выше предела, но без плоских верхушек — не клиппинг.
    _ffmpeg("-f", "lavfi", "-i", noise, "-af", "volume=8", "-ac", "2", "-c:a", "libopus", "-sample_fmt", "flt",
            "-b:a", "160k", "-f", "mp4", str(lib / "A" / "Singles" / "hot.m4a"))  # fmt: skip

    report = {row["path"]: row for row in quality_audit.audit(lib)}
    hot = report["A/Singles/hot.m4a"]
    assert hot["peak"] > 1.0 and hot["over_full_scale"] > 0 and hot["flags"] == [], hot

    full = report["A/Singles/full.m4a"]
    assert full["codec"] == "opus" and full["sample_rate"] == 48000
    assert full["flags"] == []
    cut = report["A/Singles/cut.m4a"]
    assert cut["codec"] == "aac"
    assert "cut16" in cut["flags"] and cut["above16_db"] < quality_audit.CUT_DB
    assert cut["advice"] == "upgrade_audio.py"
    loud = report["A/Singles/loud.flac"]
    assert "clipped" in loud["flags"] and loud["clipped_samples"] >= quality_audit.CLIP_SAMPLES
    assert "clipped" not in full["flags"] and "clipped" not in cut["flags"]
    # Только чтение: файлы те же.
    assert sorted(p.name for p in lib.rglob("*.*")) == [
        "cut.m4a",
        "full.m4a",
        "hot.m4a",
        "loud.flac",
    ]


def test_quality_audit_clipping_numbers_and_odd_files(tmp_path):
    """Рецензия 09.10: чистый басовый синус у предела в 16 битах — не обрезка;
    обрезанный и потом приглушённый мастер — обрезка; обрезка в одном канале
    считается; спектр белого шума даёт расчётные числа; короткий и битый файл
    не роняют проход, у битого — причина от ffmpeg."""
    import json

    import pytest

    pytest.importorskip("scipy")
    qa = _load("quality_audit")
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    a = lib / "A"
    s16 = ["-c:a", "flac", "-sample_fmt", "s16"]
    _ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=30:c=white:r=48000:a=0.3:seed=2",
            "-ac", "1", *s16, str(a / "white.flac"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "sine=f=40:d=60:r=48000", "-af", "volume=7.96",
            "-ac", "1", *s16, str(a / "subbass.flac"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=20:r=48000",
            "-af", "volume=12,aformat=sample_fmts=s16,volume=0.891",
            "-ac", "1", *s16, str(a / "lowered.flac"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=20:r=48000,volume=12",
            "-f", "lavfi", "-i", "sine=f=330:d=20:r=48000,volume=4",
            "-filter_complex", "[0][1]join=inputs=2:channel_layout=stereo",
            *s16, str(a / "left.flac"))  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", *s16, str(a / "short.flac"))
    (a / "broken.mp3").write_bytes(b"not audio at all" * 100)
    out = tmp_path / "report.jsonl"

    assert qa.main([str(lib), "--json", str(out)]) == 0
    report = {row["path"]: row for row in map(json.loads, out.read_text().splitlines())}
    assert len(report) == 6

    white = report["A/white.flac"]
    # Полосы 16.5-19 и 19.5-21 кГц против 1-16 кГц у ровного спектра: 2.5/15 и 1.5/15.
    assert white["above16_db"] == pytest.approx(-7.8, abs=1.0)
    assert white["above19_db"] == pytest.approx(-10.0, abs=1.0)
    assert white["flags"] == []
    assert report["A/subbass.flac"]["clipped_samples"] == 0
    assert report["A/subbass.flac"]["flags"] == []
    assert "clipped" in report["A/lowered.flac"]["flags"]
    left = report["A/left.flac"]
    assert "clipped" in left["flags"] and left["channels"] == 2
    assert (
        report["A/short.flac"]["flags"] == [] and report["A/short.flac"]["above16_db"] is not None
    )
    broken = report["A/broken.mp3"]
    assert broken["flags"] == ["unreadable"]
    assert "returned non-zero" not in broken["error"] and broken["error"].strip()


def test_quality_audit_refuses_a_missing_library_and_keeps_rows_on_a_crash(tmp_path, monkeypatch):
    import json

    import pytest

    pytest.importorskip("scipy")
    qa = _load("quality_audit")
    assert qa.main([str(tmp_path / "nowhere"), "--json", str(tmp_path / "r.jsonl")]) == 2
    (tmp_path / "empty").mkdir()
    assert qa.main([str(tmp_path / "empty")]) == 2
    with pytest.raises(SystemExit):
        qa.main([str(tmp_path / "empty"), "--limit", "0"])

    lib = tmp_path / "lib"
    for name in ("a", "b"):
        _tagged(lib / "X" / f"{name}.m4a", "X", name)
    real = qa.check
    calls = []

    def check(path, root):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt  # остановили посреди прохода
        return real(path, root)

    monkeypatch.setattr(qa, "check", check)
    out = tmp_path / "r.jsonl"
    with pytest.raises(KeyboardInterrupt):
        qa.main([str(lib), "--json", str(out)])
    assert [json.loads(line)["path"] for line in out.read_text().splitlines()] == ["X/a.m4a"]


def test_recon_threshold():
    # Plan 7.6: go only when more than 20 % of the 50 sampled tracks have a better legal copy.
    recon = _load("source_recon")

    def rows(better):
        return [{"better": i < better} for i in range(50)]

    assert recon.verdict(rows(10)) == ("no-go", 10, 50)
    assert recon.verdict(rows(11)) == ("go", 11, 50)
    # A hit that is not better (same quality or a different performance) does not count.
    assert recon.verdict([{"better": False, "found": "x"}] * 50)[0] == "no-go"


def test_recon_counts_only_licensed_archive_items_by_the_same_artist():
    recon = _load("source_recon")
    docs = [
        {"identifier": "rip", "creator": "Michael Jackson", "title": "Thriller - Beat It"},
        {"identifier": "cc", "creator": "Nef The Pharaoh", "title": "Beat It",
         "licenseurl": "https://creativecommons.org/licenses/by/4.0/"},
        {"identifier": "ok", "creator": ["Michael Jackson"], "title": "Beat It (live)",
         "licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/"},
    ]  # fmt: skip
    assert recon.archive_matches(docs, "Michael Jackson", "Beat It") == ["ok"]


def test_recon_bandcamp_needs_the_same_artist_and_a_free_download():
    recon = _load("source_recon")
    results = [
        {"type": "t", "name": "Beat It", "band_name": "Michael Jackson",
         "item_url_path": "https://mj.bandcamp.com/track/beat-it"},
        {"type": "t", "name": "Beat It (cover)", "band_name": "Somebody",
         "item_url_path": "https://sb.bandcamp.com/track/beat-it"},
        {"type": "a", "name": "Beat It", "band_name": "Michael Jackson",
         "item_url_root": "https://mj.bandcamp.com"},
    ]  # fmt: skip
    assert recon.bandcamp_matches(results, "Michael Jackson", "Beat It") == [
        "https://mj.bandcamp.com/track/beat-it"
    ]
    paid = '<div data-tralbum="{&quot;freeDownloadPage&quot;:null,&quot;minimum_price&quot;:1.0}">'
    nyp = '<div data-tralbum="{&quot;freeDownloadPage&quot;:null,&quot;minimum_price&quot;:0.0}">'
    free = '<div data-tralbum="{&quot;freeDownloadPage&quot;:&quot;https://x/download&quot;}">'
    assert [recon.bandcamp_free(p) for p in (paid, nyp, free)] == [False, True, True]
