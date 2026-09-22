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
UNIT_CONTENT="${UNIT_TEMPLATE//%REPO%/$REPO}"
UNIT_CONTENT="${UNIT_CONTENT//%LIBRARY%/$LIBRARY_PATH_VALUE}"

printf '%s\n' "$UNIT_CONTENT" \
    > "$HOME/.config/systemd/user/music-adder.service"

# Ночные таймеры: измерение темпа (без него умное перемешивание почти слепое)
# и выгрузка полок главной в подборки «★ …». Раньше их ставили руками, и
# новая установка оставалась без обоих.
render() {
  local text
  text="$(cat "$1")"
  text="${text//%REPO%/$REPO}"
  text="${text//%LIBRARY%/$LIBRARY_PATH_VALUE}"
  text="${text//%JOBS%/${ANALYSIS_JOBS:-3}}"
  printf '%s\n' "$text"
}
for unit in music-analysis music-shelves; do
  render "$REPO/deploy/$unit.service.template" > "$HOME/.config/systemd/user/$unit.service"
  render "$REPO/deploy/$unit.timer.template" > "$HOME/.config/systemd/user/$unit.timer"
done

systemctl --user daemon-reload
systemctl --user enable --now music-adder
systemctl --user enable --now music-analysis.timer music-shelves.timer
loginctl enable-linger "$USER"

if command -v navidrome >/dev/null; then
  sudo install -d /etc/navidrome
  # Только если настроек ещё нет: повторный запуск установщика молча
  # затирал живые (например, Subsonic.ArtistParticipations — без неё Amperfy
  # перестаёт показывать альбомы с фитами).
  if [[ ! -f /etc/navidrome/navidrome.toml ]]; then
    sed "s|/home/USER|$HOME|g" "$REPO/deploy/navidrome.toml.example" | sudo tee /etc/navidrome/navidrome.toml >/dev/null
  else
    echo "Keeping existing /etc/navidrome/navidrome.toml (compare with deploy/navidrome.toml.example)"
  fi
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
  PORT_VALUE="$(sed -n 's/^PORT=//p' "$REPO/adder/.env" | head -n1 | tr -d '\r')"
  sudo ufw allow from "$LAN_SUBNET" to any port "${PORT_VALUE:-8787}" proto tcp comment "Adder LAN"
fi

echo "OK: adder http://localhost:8787 | navidrome http://localhost:4533"
