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

# One value from adder/.env, read the way python-dotenv reads it: surrounding
# whitespace and one pair of matching quotes removed. A plain sed kept the
# quotes, so LIBRARY_PATH="/srv/music" reached the unit with them.
env_value() {
  local value
  value="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$REPO/adder/.env" | head -n1 | tr -d '\r')"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 && ( ( "${value:0:1}" == '"' && "${value: -1}" == '"' ) || ( "${value:0:1}" == "'" && "${value: -1}" == "'" ) ) ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

API_TOKEN_VALUE="$(env_value API_TOKEN)"
if [[ -z "$API_TOKEN_VALUE" || "$API_TOKEN_VALUE" == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET" ]]; then
  echo "ERROR: API_TOKEN must be configured in adder/.env before deployment." >&2
  exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

# Keep systemd in sync with the same configurable library path used by Python.
LIBRARY_PATH_VALUE="${LIBRARY_PATH:-}"
if [[ -z "$LIBRARY_PATH_VALUE" ]]; then
  LIBRARY_PATH_VALUE="$(env_value LIBRARY_PATH)"
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
SHUTDOWN_TIMEOUT_VALUE="$(env_value SHUTDOWN_TIMEOUT)"
if [[ ! "$SHUTDOWN_TIMEOUT_VALUE" =~ ^[0-9]+$ ]]; then
  SHUTDOWN_TIMEOUT_VALUE=30
fi
UNIT_CONTENT="${UNIT_CONTENT//%STOP_TIMEOUT%/"$((SHUTDOWN_TIMEOUT_VALUE + 15))"}"

printf '%s\n' "$UNIT_CONTENT" \
    > "$HOME/.config/systemd/user/music-adder.service"

# Ночные таймеры: измерение темпа (без него умное перемешивание почти слепое)
# и выгрузка полок главной в подборки «★ …». Раньше их ставили руками, и
# новая установка оставалась без обоих.
render() {
  local text
  text="$(cat "$1")"
  # Quoted replacements, for the same "&" reason as the main unit above.
  text="${text//%REPO%/"$REPO"}"
  text="${text//%LIBRARY%/"$LIBRARY_PATH_VALUE"}"
  text="${text//%JOBS%/"${ANALYSIS_JOBS:-3}"}"
  printf '%s\n' "$text"
}
# music-ytdlp-update: weekly yt-dlp upgrade that rolls itself back if the new
# version cannot read YouTube.
for unit in music-analysis music-shelves music-ytdlp-update; do
  render "$REPO/deploy/$unit.service.template" > "$HOME/.config/systemd/user/$unit.service"
  render "$REPO/deploy/$unit.timer.template" > "$HOME/.config/systemd/user/$unit.timer"
done

systemctl --user daemon-reload
systemctl --user enable --now music-adder
systemctl --user enable --now music-analysis.timer music-shelves.timer music-ytdlp-update.timer
loginctl enable-linger "$USER"

if command -v navidrome >/dev/null; then
  sudo install -d /etc/navidrome
  # Только если настроек ещё нет: повторный запуск установщика молча
  # затирал живые (например, Subsonic.ArtistParticipations — без неё Amperfy
  # перестаёт показывать альбомы с фитами).
  # MusicFolder — тот же LIBRARY_PATH, что у службы: раньше подставлялся
  # $HOME/Music/Normalized Library, и при своём пути Navidrome смотрел не туда.
  if [[ ! -f /etc/navidrome/navidrome.toml ]]; then
    NAVIDROME_TEMPLATE="$(cat "$REPO/deploy/navidrome.toml.example")"
    NAVIDROME_CONTENT="${NAVIDROME_TEMPLATE//\/home\/USER\/Music\/Normalized Library/"$LIBRARY_PATH_VALUE"}"
    printf '%s\n' "$NAVIDROME_CONTENT" | sudo tee /etc/navidrome/navidrome.toml >/dev/null
    echo "Wrote /etc/navidrome/navidrome.toml (MusicFolder = $LIBRARY_PATH_VALUE)"
  else
    echo "Keeping existing /etc/navidrome/navidrome.toml (compare with deploy/navidrome.toml.example)"
    echo "  Check that it contains: MusicFolder = \"$LIBRARY_PATH_VALUE\""
  fi
  sudo install -d /etc/systemd/system/navidrome.service.d
  sudo cp "$REPO/deploy/navidrome-override.conf" /etc/systemd/system/navidrome.service.d/override.conf
  sudo systemctl daemon-reload
  sudo systemctl restart navidrome
fi

PORT_VALUE="$(env_value PORT)"
PORT_VALUE="${PORT_VALUE:-8787}"

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
  sudo ufw allow from "$LAN_SUBNET" to any port "$PORT_VALUE" proto tcp comment "Adder LAN"
fi

echo "OK: adder http://localhost:$PORT_VALUE | navidrome http://localhost:4533"
