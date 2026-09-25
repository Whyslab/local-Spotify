#!/usr/bin/env bash
# Rebuild .venv after Arch moves Python to a new minor version (3.14 -> 3.15).
#
# A venv is tied to the Python it was made with: after such an upgrade every
# unit (music-adder and the timers) fails at the next start with
# ModuleNotFoundError. The failure notice says so; this puts it right.
#
#   ./deploy/rebuild-venv.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

have="$(sed -n 's/^version *= *\([0-9]*\.[0-9]*\).*/\1/p' .venv/pyvenv.cfg 2>/dev/null || true)"
now="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$have" == "$now" && "${1:-}" != "--force" ]]; then
  echo "The venv already runs Python $now; nothing to do (--force rebuilds anyway)."
  exit 0
fi

echo "Rebuilding .venv: Python ${have:-?} -> $now"
systemctl --user stop music-adder || true
python3 -m venv --clear .venv
.venv/bin/pip install -q -r adder/requirements.txt
if [[ -f scripts/requirements-analysis.txt ]]; then
  .venv/bin/pip install -q -r scripts/requirements-analysis.txt
fi
systemctl --user start music-adder
curl -s --max-time 20 --retry 10 --retry-connrefused http://127.0.0.1:8787/health; echo
