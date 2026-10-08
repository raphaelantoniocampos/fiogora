#!/usr/bin/env bash
# Pull-based deploy for the server (see "Deploy automático" in the README).
# Deploys origin/main when it has a new commit whose CI workflow passed and no sync/task is running.
#
# Usage: auto-deploy.sh [--cron] [--force] [--dry-run]
#   (no flag)  deploy now if there is something new, always printing the status
#   --cron     used by the crontab: silent when there is nothing to do
#   --force    rebuild and restart even if the commit is already deployed
#   --dry-run  run every check without deploying
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${DEPLOY_BRANCH:-main}"
GITHUB_REPO="${DEPLOY_GITHUB_REPO:-raphaelantoniocampos/fiogora}"
CI_WORKFLOW="${DEPLOY_CI_WORKFLOW:-Tests}"
STATE_DIR="${DEPLOY_STATE_DIR:-$HOME/.local/state/fiogora-deploy}"

CRON=0
FORCE=0
DRY_RUN=0

log() { echo "$(date '+%F %T') $*"; }

# In cron mode, log only when the message changes so a waiting commit does not flood the log
log_status() {
    if [ "$CRON" = 0 ] || [ "$(cat "$STATE_DIR/last_message" 2>/dev/null)" != "$*" ]; then
        log "$*"
    fi
    echo "$*" > "$STATE_DIR/last_message"
}

# Prints the CI result for a commit: success, failure, in_progress, queued, missing or error
ci_status() {
    python3 - "$GITHUB_REPO" "$CI_WORKFLOW" "$1" <<'PY'
import json
import sys
import urllib.request

repo, workflow, sha = sys.argv[1:4]
url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&event=push"
request = urllib.request.Request(
    url,
    headers={"Accept": "application/vnd.github+json", "User-Agent": "fiogora-auto-deploy"},
)
try:
    runs = json.load(urllib.request.urlopen(request, timeout=20))["workflow_runs"]
except Exception as e:
    print(f"error ({e})")
    sys.exit()
runs = [r for r in runs if r["name"] == workflow]
# The API returns the newest run first (re-runs included)
print((runs[0]["conclusion"] or runs[0]["status"]) if runs else "missing")
PY
}

# Prints how many sync jobs/automation tasks are running. Timestamps are naive America/Sao_Paulo
# times; entries older than the cutoffs are considered stale (left behind by a crash).
running_jobs() {
    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA' <<'SQL'
select count(*) from (
    select 1 from sync_jobs
    where (status = 'running'
           and coalesce(started_at, created_at) > (now() at time zone 'America/Sao_Paulo') - interval '2 hours')
       or (status = 'pending'
           and created_at > (now() at time zone 'America/Sao_Paulo') - interval '10 minutes')
    union all
    select 1 from automation_tasks
    where status = 'running'
      and coalesce(started_at, created_at) > (now() at time zone 'America/Sao_Paulo') - interval '2 hours'
) running;
SQL
}

main() {
    for arg in "$@"; do
        case "$arg" in
            --cron) CRON=1 ;;
            --force) FORCE=1 ;;
            --dry-run) DRY_RUN=1 ;;
            *) echo "Usage: $0 [--cron] [--force] [--dry-run]" >&2; exit 2 ;;
        esac
    done

    mkdir -p "$STATE_DIR"
    if [ "$CRON" = 1 ]; then
        exec >> "$STATE_DIR/deploy.log" 2>&1
    else
        exec > >(tee -a "$STATE_DIR/deploy.log") 2>&1
        # Let tee flush before exiting, so the output does not show up after the prompt
        trap "exec >&- 2>&-; wait $!" EXIT
    fi

    exec 9>"$STATE_DIR/lock"
    if ! flock -n 9; then
        [ "$CRON" = 1 ] || log "Another deploy is already running"
        exit 0
    fi

    cd "$REPO_DIR"
    git fetch -q origin "$BRANCH"
    local target deployed short ci running
    target="$(git rev-parse "origin/$BRANCH")"
    deployed="$(cat "$STATE_DIR/deployed_commit" 2>/dev/null || true)"
    short="${target:0:7}"
    if [ "$target" = "$deployed" ] && [ "$FORCE" = 0 ]; then
        [ "$CRON" = 1 ] || log "Already up to date ($short), use --force to redeploy"
        exit 0
    fi

    ci="$(ci_status "$target")"
    if [ "$ci" != "success" ]; then
        log_status "Not deploying $short yet: CI '$CI_WORKFLOW' is '$ci'"
        exit 0
    fi

    if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ] \
        || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        log_status "Not deploying $short: checkout is not a clean '$BRANCH' branch, fix it manually"
        exit 0
    fi

    running="$(running_jobs 2>/dev/null || echo "unknown")"
    if [ "$running" = "unknown" ]; then
        log "Could not check running jobs (database down?), deploying anyway"
    elif [ "$running" != "0" ]; then
        log_status "Postponing $short: $running sync job(s)/task(s) running"
        exit 0
    fi

    if [ "$DRY_RUN" = 1 ]; then
        log "Dry run: would deploy $short: $(git log -1 --format=%s "$target")"
        exit 0
    fi

    log "Deploying $short: $(git log -1 --format=%s "$target")"
    git merge -q --ff-only "origin/$BRANCH"
    # Migrations run when the container starts: keep a backup from right before them
    "$REPO_DIR/deploy/backup-db.sh" pre-deploy || log "Pre-deploy backup failed, deploying anyway"
    if [ "$FORCE" = 1 ]; then
        docker compose up -d --build --force-recreate
    else
        docker compose up -d --build
    fi
    echo "$target" > "$STATE_DIR/deployed_commit"
    rm -f "$STATE_DIR/last_message"
    docker image prune -f > /dev/null
    log "Deployed $short"
}

# Keep `exit` on the same line: bash reads scripts lazily and `git merge` may rewrite this file
main "$@"; exit
