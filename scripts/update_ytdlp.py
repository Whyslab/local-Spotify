#!/usr/bin/env python3
"""Keep yt-dlp current, and never leave a broken version installed.

YouTube changes its player every few weeks and an old yt-dlp stops being able
to download; until now that was noticed only when tasks started failing. This
upgrades yt-dlp and yt-dlp-ejs, then asks the new version for the metadata of
one short, long-lived video. If that fails the previous versions are put back.
If the previous versions fail too, the problem is the network or YouTube
itself, not the update, and that is what the log says.

The service runs yt-dlp as a new process for every call, so an upgrade takes
effect with the next task; nothing has to be restarted.

Run weekly by deploy/music-ytdlp-update.timer, or by hand:

    .venv/bin/python scripts/update_ytdlp.py
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# The pins in requirements.txt hold during the upgrade too: a bare
# "pip install --upgrade" would step past "<2027" the day 2027.1 appears.
CONSTRAINTS = REPO / "adder" / "requirements.txt"
NETWORK_WAIT_SECONDS = 600

PACKAGES = ["yt-dlp", "yt-dlp-ejs"]
# "Me at the zoo": the first video on YouTube, 19 seconds long, not going away.
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

logger = logging.getLogger("update_ytdlp")


def versions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


def pip(*args: str) -> None:
    # --no-cache-dir: ~/.cache is read-only under the systemd unit.
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
         "-c", str(CONSTRAINTS), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )  # fmt: skip


def network_up(host: str = "pypi.org", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=10):
            return True
    except OSError:
        return False


def wait_for_network(limit: float = NETWORK_WAIT_SECONDS) -> bool:
    """The timer is Persistent: after a night asleep it fires right at wake-up,
    often before Wi-Fi is back, and network-online.target means nothing to a
    user manager. So the script waits for the network itself."""
    deadline = time.monotonic() + limit
    while True:
        if network_up():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(15)


def probe() -> tuple[bool, str]:
    """Can the installed yt-dlp read a YouTube video right now?"""
    from adder import ingest

    try:
        result = ingest.run_yt_dlp(
            [*ingest.ytdlp_base(), "-J", "--skip-download", "--no-playlist", PROBE_URL],
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if result.returncode == 0:
        return True, ""
    return False, ingest.ytdlp_error(result.stderr)


def notify(summary: str, body: str, urgent: bool = False) -> None:
    """Tell the person at the desktop, when the service knows how to."""
    try:
        from adder import notify as notifications
    except ImportError:
        return
    notifications.send(summary, body, urgent=urgent)


def update() -> int:
    if not wait_for_network():
        # Not a failure of anything here; next week's run tries again.
        logger.warning("No network for %d s; skipping this week's update", NETWORK_WAIT_SECONDS)
        return 0
    before = versions()
    try:
        pip("--upgrade", *PACKAGES)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "stderr", "") or ""
        logger.error("pip could not upgrade yt-dlp: %s", output.strip().splitlines()[-1:] or exc)
        return 1
    after = versions()

    if after == before:
        logger.info("yt-dlp is up to date (%s)", before["yt-dlp"])
        return 0

    ok, reason = probe()
    if ok:
        logger.info("Updated %s -> %s", before, after)
        return 0

    logger.error("New yt-dlp %s cannot read YouTube: %s", after["yt-dlp"], reason)
    pinned = [f"{name}=={version}" for name, version in before.items() if version]
    try:
        pip(*pinned)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.error("Rollback to %s failed; the new version stays", before)
        return 1  # the unit fails, and its failure notice says so
    old_ok, old_reason = probe()
    if old_ok:
        logger.error("Rolled back to %s; the new version stays skipped until next week", before)
        notify(
            "yt-dlp: обновление откатено",
            f"Версия {after['yt-dlp']} не смогла прочитать YouTube, оставлена {before['yt-dlp']}.",
        )
    else:
        logger.error(
            "Kept %s; it fails too (%s), so YouTube or the network is the problem",
            before,
            old_reason,
        )
        notify(
            "yt-dlp: YouTube недоступен",
            f"Ни новая, ни прежняя версия yt-dlp не читают YouTube: {old_reason[:150]}",
            urgent=True,
        )
    # Handled and reported above, once. Returning 1 here also fired the
    # unit's generic failure notice: two messages for one event.
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return update()


if __name__ == "__main__":
    sys.exit(main())
