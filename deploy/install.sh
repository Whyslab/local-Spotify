#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Fail early instead of installing a systemd unit that cannot start.
if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "ERROR: Python virtual environment not found at $REPO/.venv" >&2
  echo "Run: python -m venv .venv && .venv/bin/pip install -r adder/requirements.txt" >&2
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

# Generate the systemd unit.
# Keep the library path quoted in the generated unit so paths containing
# spaces remain a single systemd argument.
UNIT_TEMPLATE="$(cat "$REPO/deploy/music-adder.service.template")"
# Quoted replacements: bash 5.2 otherwise expands "&" in them to the match,
# turning a library path like "Rock & Roll" into "Rock %LIBRARY% Roll".
UNIT_CONTENT="${UNIT_TEMPLATE//%REPO%/"$REPO"}"
UNIT_CONTENT="${UNIT_CONTENT//%LIBRARY%/"$LIBRARY_PATH_VALUE"}"

printf '%s\n' "$UNIT_CONTENT" \
    > "$HOME/.config/systemd/user/music-adder.service"

systemctl --user daemon-reload
systemctl --user enable --now music-adder
loginctl enable-linger "$USER"

# Navidrome must read the same folder the adder writes to. An existing
# Navidrome config belongs to the user and is never overwritten.
if command -v navidrome >/dev/null; then
  NAVIDROME_CONF=/etc/navidrome/navidrome.toml
  NAVIDROME_OVERRIDE=/etc/systemd/system/navidrome.service.d/override.conf
  NAVIDROME_CHANGED=0

  if sudo test -e "$NAVIDROME_CONF"; then
    echo "Navidrome config $NAVIDROME_CONF already exists; leaving it unchanged."
    echo "  Check that it contains: MusicFolder = \"$LIBRARY_PATH_VALUE\""
  else
    NAVIDROME_TEMPLATE="$(cat "$REPO/deploy/navidrome.toml.example")"
    # Quoted replacement for the same reason as the unit above.
    NAVIDROME_CONTENT="${NAVIDROME_TEMPLATE//\/home\/USER\/Music\/Normalized Library/"$LIBRARY_PATH_VALUE"}"
    sudo install -d /etc/navidrome
    printf '%s\n' "$NAVIDROME_CONTENT" | sudo tee "$NAVIDROME_CONF" >/dev/null
    echo "Wrote $NAVIDROME_CONF (MusicFolder = $LIBRARY_PATH_VALUE)"
    NAVIDROME_CHANGED=1
  fi

  if sudo test -e "$NAVIDROME_OVERRIDE"; then
    echo "Navidrome override $NAVIDROME_OVERRIDE already exists; leaving it unchanged."
  else
    sudo install -d "$(dirname "$NAVIDROME_OVERRIDE")"
    sudo cp "$REPO/deploy/navidrome-override.conf" "$NAVIDROME_OVERRIDE"
    NAVIDROME_CHANGED=1
  fi

  if [[ "$NAVIDROME_CHANGED" == 1 ]]; then
    sudo systemctl daemon-reload
    sudo systemctl restart navidrome
  fi
else
  echo "Navidrome not found; skipping its configuration. See README, step 6."
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
