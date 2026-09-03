"""Scheduling the audio analysis so it never competes with the machine.

A track has to be measured before the shuffle can place it, and measuring is
expensive: about 3.7 seconds and 376 MB for one track on this laptop. Running
that inside the service, unbounded, on a machine with under two gigabytes free
is how earlyoom ends up killing the browser instead of the analysis.

So it runs the same way the nightly pass does -- as its own scope, with a memory
ceiling and a lowered priority -- whether it is one new track or the whole
library. One mechanism, not two, so the bounded path cannot be the one that
gets forgotten.
"""

import logging
import shutil
import subprocess
import threading

from . import runtime

logger = logging.getLogger(__name__)

MEMORY_MAX = "1500M"
NICE = "10"

SCRIPT = runtime.PROJECT.parent / "scripts" / "analyze_audio.py"
PYTHON = runtime.PROJECT.parent / ".venv" / "bin" / "python"


def _command(args: list[str]) -> list[str]:
    """Wrap the analyser in whatever bounding the system can offer.

    systemd-run gives a real memory ceiling. Without it -- a container, another
    init -- nice alone is all there is, which is worth saying out loud rather
    than pretending the limit is in force.
    """
    base = [str(PYTHON), str(SCRIPT), *args]
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "-p",
            f"MemoryMax={MEMORY_MAX}",
            "nice",
            "-n",
            NICE,
            *base,
        ]
    logger.warning("systemd-run not available; analysis runs with nice only, no memory ceiling")
    return ["nice", "-n", NICE, *base]


def analyse_track(path: str) -> None:
    """Measure one freshly added track, in the background.

    Failure is logged and dropped on purpose: a track that could not be
    measured is still a track, it just will not be placed by tempo until the
    next full pass picks it up.
    """
    if not SCRIPT.exists() or not PYTHON.exists():
        return

    def run() -> None:
        try:
            result = subprocess.run(
                _command(["--only", path]),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                logger.info("Analysis of %s failed: %s", path, result.stderr.strip()[-160:])
        except Exception as exc:  # noqa: BLE001
            logger.info("Analysis of %s could not start: %s", path, exc)

    threading.Thread(target=run, name="analyse-track", daemon=True).start()
