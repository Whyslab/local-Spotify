#!/bin/bash
# Problem #20: Backup script for adder.db and configuration
# Safe backup policy - never deletes original database

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ADDER_DIR="$PROJECT_ROOT/adder"
BACKUP_DIR="${BACKUP_DIR:-$HOME/local-spotify-backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "=== local-Spotify Backup ==="
echo "Timestamp: $TIMESTAMP"
echo "Backup directory: $BACKUP_DIR"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup SQLite database (Problem #20)
if [ -f "$ADDER_DIR/adder.db" ]; then
    # The online backup API copies a consistent snapshot even while the
    # service is writing -- a plain copy of adder.db misses what is still in
    # adder.db-wal. Always through Python: the sqlite3 CLI's ".backup '$path'"
    # broke on a path with a quote in it. The copy is then read back in full:
    # a snapshot nobody has opened is not yet a backup.
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
    [[ -x "$PYTHON" ]] || PYTHON=python3
    "$PYTHON" - "$ADDER_DIR/adder.db" "$BACKUP_DIR/adder_$TIMESTAMP.db" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
src.close()
check = dst.execute("PRAGMA integrity_check").fetchone()[0]
tasks = dst.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
dst.close()
if check != "ok":
    sys.exit(f"snapshot failed its integrity check: {check}")
print(f"  integrity ok, {tasks} tasks")
PY
    chmod 600 "$BACKUP_DIR/adder_$TIMESTAMP.db"
    echo "✓ Database backed up: adder_$TIMESTAMP.db"
else
    echo "⚠ Database not found at $ADDER_DIR/adder.db"
fi

# Backup configuration
if [ -f "$ADDER_DIR/.env" ]; then
    cp "$ADDER_DIR/.env" "$BACKUP_DIR/env_$TIMESTAMP"
    echo "✓ Configuration backed up: env_$TIMESTAMP"
else
    echo "ℹ No .env file found"
fi

# Обложки подборок и прежние версии подборок: своего источника у них нет,
# потерянное не восстановить ни из фонотеки, ни из Navidrome.
PLAYLIST_STATE=()
for dir in playlist-covers playlist-history; do
    [ -d "$ADDER_DIR/$dir" ] && PLAYLIST_STATE+=("$dir")
done
if [ ${#PLAYLIST_STATE[@]} -gt 0 ]; then
    tar -czf "$BACKUP_DIR/playlists_$TIMESTAMP.tar.gz" -C "$ADDER_DIR" "${PLAYLIST_STATE[@]}"
    echo "✓ Playlist covers and history backed up: playlists_$TIMESTAMP.tar.gz"
fi

# Keep only last 10 backups (cleanup old ones). With pipefail an `ls` that
# matched nothing (a first run, no .env) used to abort the script.
cd "$BACKUP_DIR"
# Only regular files of this script's own naming, newest first by mtime, and
# names passed NUL-separated: parsing ls split names on spaces and quotes.
for pattern in 'adder_[0-9]*.db' 'env_[0-9]*' 'playlists_[0-9]*.tar.gz'; do
    find . -maxdepth 1 -type f -name "$pattern" -printf '%T@\t%p\0' \
        | sort -zrn | tail -zn +11 | cut -zf2- | xargs -0r rm --
done
# A snapshot opened in WAL mode leaves -wal/-shm beside it; they went on living
# after their database was rotated away.
for side in adder_*.db-wal adder_*.db-shm; do
    [ -e "$side" ] || continue
    [ -e "${side%-*}" ] || rm -- "$side"
done

echo ""
echo "=== Backup Summary ==="
echo "Music Library = primary data (backed up separately)"
echo "SQLite DB = tasks, play journal, audio measurements, blind trials (backed up above)"
echo "Playlist covers and history = backed up above"
echo ".env = configuration/secrets (backed up above)"
echo ""
echo "Latest backups:"
ls -lt "$BACKUP_DIR" | head -6
