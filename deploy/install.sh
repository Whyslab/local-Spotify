#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Fail early instead of installing a systemd unit that cannot start.
if [[ ! -x "$REPO/adder/.venv/bin/python" ]]; then
  echo "ERROR: Python virtual environment not found at $REPO/adder/.venv" >&2
  echo "Run: python -m venv adder/.venv && adder/.venv/bin/pip install -r adder/requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$REPO/adder/.env" ]]; then
  echo "ERROR: Missing $REPO/adder/.env" >&2
  echo "Copy .env.example to adder/.env and set API_TOKEN." >&2
  exit 1
fi

API_TOKEN_VALUE="$(sed -n 's/^API_TOKEN=//p' "$REPO/adder/.env" | head -n1 | tr -d '\r')"
if [[ -z "$API_TOKEN_VALUE" || "$API_TOKEN_VALUE" == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" ]]; then
  echo "ERROR: API_TOKEN must be configured in adder/.env before deployment." >&2
  exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

# Keep systemd in sync with the same configurable library path used by Python.
LIBRARY_PATH_VALUE="${LIBRARY_PATH:-}"
if [[ -z "$LIBRARY_PATH_VALUE" ]]; then
  LIBRARY_PATH_VALUE="$(sed -n 's/^LIBRARY_PATH=//p' "$REPO/adder/.env" | head -n1)"
fi
LIBRARY_PATH_VALUE="${LIBRARY_PATH_VALUE:-$HOME/Music/Normalized Library}"

# Escape replacement values for sed.
escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}
REPO_ESCAPED="$(escape_sed_replacement "$REPO")"
LIBRARY_ESCAPED="$(escape_sed_replacement "$LIBRARY_PATH_VALUE")"

sed -e "s|%REPO%|$REPO_ESCAPED|g" \
    -e "s|%LIBRARY%|$LIBRARY_ESCAPED|g" \
    "$REPO/deploy/music-adder.service.template" \
    > "$HOME/.config/systemd/user/music-adder.service"

systemctl --user daemon-reload
systemctl --user enable --now music-adder
loginctl enable-linger "$USER"

if command -v navidrome >/dev/null; then
  sudo install -d /etc/navidrome
  sed "s|/home/USER|$HOME|g" "$REPO/deploy/navidrome.toml.example" | sudo tee /etc/navidrome/navidrome.toml >/dev/null
  sudo install -d /etc/systemd/system/navidrome.service.d
  sudo cp "$REPO/deploy/navidrome-override.conf" /etc/systemd/system/navidrome.service.d/override.conf
  sudo systemctl daemon-reload
  sudo systemctl restart navidrome
fi

# Firewall rules are intentionally explicit and failure is not hidden.
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  LAN_SUBNET="${LAN_SUBNET:-}"
  if [[ -z "$LAN_SUBNET" ]]; then
    LAN_SUBNET="$(ip -4 route show scope link | awk '$1 !~ /^127\./ && $1 ~ /^[0-9]+\./ {print $1; exit}')"
  fi
  if [[ -z "$LAN_SUBNET" ]]; then
    echo "ERROR: Could not determine LAN subnet. Set LAN_SUBNET explicitly." >&2
    exit 1
  fi
  sudo ufw allow from "$LAN_SUBNET" to any port 4533 proto tcp comment "Navidrome LAN"
  sudo ufw allow from "$LAN_SUBNET" to any port 8787 proto tcp comment "Adder LAN"
fi

echo "OK: adder http://localhost:8787 | navidrome http://localhost:4533"
