"""The names of the phone's offline stores are part of its data, not of the code.

sw.js deletes every Cache API store not in its KNOWN list when a new version
activates (web/sw.js, "activate"). Renaming one of the stores below - even a
harmless-looking "v1" to "v2" - would therefore wipe every track downloaded on
every phone the moment the new service worker took over. If a rename is ever
really needed, it needs a migration that copies the old store first; then
change the names here.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
PINNED = {
    "audio": "offline-audio-v1",
    "meta": "offline-meta-v1",
    "covers": "offline-covers-v1",
}


def _constants(js: str) -> dict[str, str]:
    return dict(re.findall(r'^const (\w+) = "([^"]+)";', js, flags=re.MULTILINE))


def test_offline_cache_names_are_stable():
    offline = _constants((WEB / "offline.js").read_text(encoding="utf-8"))
    worker_js = (WEB / "sw.js").read_text(encoding="utf-8")
    worker = _constants(worker_js)

    assert offline["OFFLINE_AUDIO"] == PINNED["audio"]
    assert offline["OFFLINE_META"] == PINNED["meta"]
    assert offline["OFFLINE_EXTRAS"] == PINNED["covers"]
    assert worker["AUDIO"] == PINNED["audio"]
    assert worker["META"] == PINNED["meta"]
    assert worker["COVERS"] == PINNED["covers"]

    # And activate keeps them: every pinned store is in KNOWN.
    known = re.search(r"const KNOWN = \[([^\]]*)\];", worker_js)
    assert known, "KNOWN not found in sw.js"
    kept = {worker[name.strip()] for name in known.group(1).split(",") if name.strip()}
    assert set(PINNED.values()) <= kept
