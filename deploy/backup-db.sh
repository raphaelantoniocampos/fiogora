#!/usr/bin/env bash
# Database backup for the server (see "Backup do banco" in the README).
# Writes a compressed pg_dump to $BACKUP_DIR and removes backups older than $BACKUP_RETENTION_DAYS.
#
# Usage: backup-db.sh [label]   (the label goes in the file name: "daily", "pre-deploy", ...)
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_DIR="${FIOGORA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/fiogora}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log() { echo "$(date '+%F %T') $*"; }

main() {
    local label="${1:-manual}"
    # The dump has password hashes and encrypted credentials: owner-only files
    umask 077
    mkdir -p "$BACKUP_DIR"
    cd "$REPO_DIR"

    local file
    file="$BACKUP_DIR/fiogora-$(date +%Y%m%d-%H%M%S)-$label.dump"
    # Expanded now: $file is local to main and gone when the EXIT trap runs
    trap "rm -f '$file.tmp'" EXIT

    docker compose exec -T db sh -c \
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=9' \
        < /dev/null > "$file.tmp"
    # A file pg_restore cannot read is not a backup
    docker compose exec -T db pg_restore --list < "$file.tmp" > /dev/null
    mv "$file.tmp" "$file"
    log "Backup created: $(basename "$file") ($(du -h "$file" | cut -f1))"

    # Only reached after a good backup, so old ones are never all deleted while backups fail
    find "$BACKUP_DIR" -maxdepth 1 -name 'fiogora-*.dump' -mtime +"$RETENTION_DAYS" -print -delete |
        while read -r old; do log "Removed old backup: $(basename "$old")"; done
}

main "$@"
