#!/usr/bin/env bash
# Restores a backup into a throwaway Postgres container and compares row counts with the live
# database. Never touches the live database. (see "Backup do banco" in the README)
#
# Usage: verify-backup.sh [file.dump]   (default: the newest backup)
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_DIR="${FIOGORA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/fiogora}"
CONTAINER="fiogora_restore_test"
TABLES="users user_credentials sync_jobs sync_logs automation_tasks ahgora_employees ahgora_leaves"

row_counts() {
    local query=""
    for table in $TABLES; do
        query+="select '$table', count(*) from $table union all "
    done
    echo "${query% union all };"
}

main() {
    local file="${1:-$(ls -t "$BACKUP_DIR"/fiogora-*.dump 2> /dev/null | head -1)}"
    if [ -z "$file" ] || [ ! -f "$file" ]; then
        echo "No backup found in $BACKUP_DIR" >&2
        exit 1
    fi
    echo "Verifying $(basename "$file")"
    cd "$REPO_DIR"

    docker run -d --rm --name "$CONTAINER" -e POSTGRES_PASSWORD=verify postgres:16-alpine > /dev/null
    trap 'docker stop "$CONTAINER" > /dev/null 2>&1 || true' EXIT
    # The image starts a temporary server to initialize, then restarts: wait for the second "ready"
    for _ in $(seq 60); do
        [ "$(docker logs "$CONTAINER" 2>&1 | grep -c 'ready to accept connections')" -ge 2 ] && break
        sleep 1
    done

    docker exec "$CONTAINER" createdb -U postgres restored
    docker exec -i "$CONTAINER" pg_restore -U postgres -d restored --no-owner --no-privileges < "$file"

    local restored live
    restored="$(docker exec "$CONTAINER" psql -U postgres -d restored -tA -F ' ' -c "$(row_counts)")"
    live="$(docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F " " -c "$0"' \
        "$(row_counts)" < /dev/null)"

    printf '%-18s %10s %10s\n' table backup live
    paste -d ' ' <(echo "$restored") <(echo "$live" | cut -d ' ' -f 2) |
        while read -r table backup now; do printf '%-18s %10s %10s\n' "$table" "$backup" "$now"; done
    echo "Restore OK (live counts can be higher: they include changes made after the backup)"
}

main "$@"
