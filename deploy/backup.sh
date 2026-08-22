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
    sqlite3 "$ADDER_DIR/adder.db" ".backup '$BACKUP_DIR/adder_$TIMESTAMP.db'"
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

# Keep only last 10 backups (cleanup old ones)
cd "$BACKUP_DIR"
ls -t adder_*.db 2>/dev/null | tail -n +11 | xargs -r rm --
ls -t env_* 2>/dev/null | tail -n +11 | xargs -r rm --

echo ""
echo "=== Backup Summary ==="
echo "Music Library = primary data (backed up separately)"
echo "SQLite DB = task state (backed up above)"
echo ".env = configuration/secrets (backed up above)"
echo ""
echo "Latest backups:"
ls -lt "$BACKUP_DIR" | head -6
