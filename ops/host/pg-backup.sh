#!/usr/bin/env bash
# Encrypted off-host backup of the production ytdigest Postgres database (#150).
#
#   pg-backup.sh           take one backup (what the systemd timer runs)
#   pg-backup.sh --check   validate config, credentials, restic and the stack; change nothing
#   pg-backup.sh --init    create the restic repository once (refuses if it exists)
#
# Online logical dump (pg_dump -Fc, PostgreSQL 18 client inside the db
# container, one exported snapshot shared with the manifest) -> restic to an
# R2 bucket -> restic check -> retention. A failure at any stage exits non-zero,
# prints the stage and no secret, and never deletes an older snapshot.
# Runbook: _docs/backup-restore.md.

set -Eeuo pipefail
PROG="pg-backup"
# shellcheck source=ops/host/pg-backup-lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pg-backup-lib.sh"
trap 'on_err $LINENO' ERR

MODE="run"
case "${1:-}" in
  "") ;;
  --check) MODE="check" ;;
  --init) MODE="init" ;;
  *) printf 'usage: %s [--check|--init]\n' "$0" >&2; exit 2 ;;
esac

PROJECT="${YTDIGEST_BACKUP_PROJECT:-$PROD_PROJECT}"   # tests aim this at a throwaway stack
COMPOSE_FILE_PATH="${YTDIGEST_BACKUP_COMPOSE_FILE:-compose.yml}"
WORK=""
COPROC_PID=""
SNAPSHOT_ID=""
BYTES=0
ADDED=0

dc() { (cd "$REPO_ROOT" && timeout --foreground "${DOCKER_TIMEOUT:-300}" docker compose -p "$PROJECT" -f "$COMPOSE_FILE_PATH" "$@"); }

cleanup() {
  local rc=$?
  trap - EXIT ERR
  if [[ -n $COPROC_PID ]]; then
    kill "$COPROC_PID" 2>/dev/null || true
  fi
  # Plaintext goes on every path: success, failure, SIGINT, SIGTERM.
  [[ -n $WORK ]] && rm -rf -- "$WORK"
  if [[ $MODE == run ]]; then
    if ((rc == 0)); then
      log "event=finish outcome=ok snapshot=$SNAPSHOT_ID bytes=$BYTES added=$ADDED"
    else
      log "event=finish outcome=failed stage=$STAGE snapshot=${SNAPSHOT_ID:-none}"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

load_config
check_restic

if [[ $MODE == init ]]; then
  stage init
  err="$(mktemp)"
  out="$(mktemp)"
  if restic_run "$err" "$out" cat config; then
    rm -f -- "$err" "$out"
    fail init "the repository already exists; refusing to initialise it again"
  fi
  if ! restic_run "$err" "$out" init; then
    show_tail "$err"
    rm -f -- "$err" "$out"
    fail init "restic init failed (bucket missing, wrong token, or endpoint unreachable)"
  fi
  rm -f -- "$err" "$out"
  log "event=init outcome=ok"
  exit 0
fi

# ---- local prerequisites -------------------------------------------------------
stage staging
STAGING="${CONF[STAGING_DIR]}"
check_dir "$STAGING" staging
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" \
  || fail docker "docker is not reachable"
case "$(realpath -m -- "$STAGING")/" in
  "$docker_root"/*) fail staging "STAGING_DIR is inside Docker's data root" ;;
  "$REPO_ROOT"/*) fail staging "STAGING_DIR is inside the checkout" ;;
esac

stage db
dc exec -T "$DB_SERVICE" pg_isready -U "$DB_USER" -d ytdigest -q >/dev/null 2>&1 \
  || fail db "the $DB_SERVICE service of project $PROJECT is not running or not ready"

if [[ $MODE == check ]]; then
  stage repository
  err="$(mktemp)"
  out="$(mktemp)"
  restic_run "$err" "$out" cat config --retry-lock 1m || {
    show_tail "$err"
    rm -f -- "$err" "$out"
    fail repository "the restic repository is unreachable, not initialised, or the credentials are wrong"
  }
  rm -f -- "$err" "$out"
  log "event=check outcome=ok"
  exit 0
fi

log "event=start project=$PROJECT"

stage lock
exec 9>"$STAGING/.lock"
flock -n 9 || fail lock "another backup is already running"

stage staging
WORK="$STAGING/work"
rm -rf -- "$WORK"            # leftovers of an interrupted run, now that we hold the lock
mkdir -m 700 -- "$WORK"
ERR="$WORK/stderr"
: >"$ERR"

# ---- manifest and a consistent snapshot, then the dump -----------------------------
stage manifest
trap '' PIPE
coproc PSQL { DOCKER_TIMEOUT=10800 dc exec -T "$DB_SERVICE" psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d ytdigest 2>>"$ERR"; }
COPROC_PID="$PSQL_PID"
{
  printf '%s\n' "BEGIN ISOLATION LEVEL REPEATABLE READ;"
  printf '%s\n' "SELECT 'snapshot ' || pg_export_snapshot();"
  printf '%s\n' "SELECT 'dbsize ' || pg_database_size('ytdigest');"
  manifest_sql
  printf '%s\n' "SELECT '@@END@@';"
} >&"${PSQL[1]}" || fail manifest "the database session could not be opened"

snapshot=""
dbsize=""
: >"$WORK/manifest.body"
while :; do
  IFS= read -r -t 120 -u "${PSQL[0]}" line \
    || { show_tail "$ERR"; fail manifest "the database session ended or timed out before the manifest was complete"; }
  case "$line" in
    "@@END@@") break ;;
    "snapshot "*) snapshot="${line#snapshot }" ;;
    "dbsize "*) dbsize="${line#dbsize }" ;;
    *) printf '%s\n' "$line" >>"$WORK/manifest.body" ;;
  esac
done
[[ $snapshot =~ ^[0-9A-F-]+$ ]] || fail manifest "no usable snapshot id from the database"
[[ $dbsize =~ ^[0-9]+$ ]] || fail manifest "no database size from the database"
grep -q '^alembic ' "$WORK/manifest.body" || fail manifest "alembic_version was not read"
grep -q '^table ' "$WORK/manifest.body" || fail manifest "the database has no application tables"

stage disk
need_kb=$((dbsize / 1024 + 65536))
avail_kb="$(df -Pk -- "$STAGING" | awk 'NR==2{print $4}')"
[[ $avail_kb =~ ^[0-9]+$ ]] || fail disk "cannot read free space of the staging directory"
((avail_kb >= need_kb)) || fail disk "staging needs about ${need_kb} KB free, has ${avail_kb} KB"

stage dump
if ! DOCKER_TIMEOUT=7200 dc exec -T "$DB_SERVICE" pg_dump -U "$DB_USER" -d ytdigest -Fc \
  --snapshot="$snapshot" >"$WORK/ytdigest.dump.partial" 2>>"$ERR"; then
  show_tail "$ERR"
  fail dump "pg_dump failed (disk full or database error)"
fi
[[ -s $WORK/ytdigest.dump.partial ]] || fail dump "pg_dump produced an empty file"

# The dump is read back by the same PostgreSQL 18 client before anything is published.
stage validate
dc exec -T "$DB_SERVICE" pg_restore --list <"$WORK/ytdigest.dump.partial" >/dev/null 2>>"$ERR" \
  || { show_tail "$ERR"; fail validate "pg_restore --list cannot read the dump"; }
# End the snapshot session: EOF on its stdin makes psql exit and roll back.
psql_in="${PSQL[1]}"
exec {psql_in}>&-
wait "$COPROC_PID" 2>/dev/null || true
COPROC_PID=""

mkdir -m 700 -- "$WORK/out"
mv -- "$WORK/ytdigest.dump.partial" "$WORK/out/ytdigest.dump"
{
  printf '# format 1\n# utc %s\n# dbsize %s\n' "$(now_iso)" "$dbsize"
  cat "$WORK/manifest.body"
} >"$WORK/out/manifest.txt"
chmod 600 -- "$WORK/out/"*

# ---- publish, verify, then (only then) retention ----------------------------------------
stage upload
RERR="$WORK/restic.err"
ROUT="$WORK/restic.out"
if ! restic_run "$RERR" "$ROUT" backup --json --tag "$BACKUP_TAG" --host "$BACKUP_HOST" \
  --retry-lock 2m "$WORK/out"; then
  show_tail "$RERR"
  fail upload "restic backup failed (repository unreachable, authentication, space or lock)"
fi
summary="$(grep -F '"message_type":"summary"' "$ROUT" | tail -n1 || true)"
SNAPSHOT_ID="$(sed -n 's/.*"snapshot_id":"\([0-9a-f]\{8,64\}\)".*/\1/p' <<<"$summary")"
BYTES="$(sed -n 's/.*"total_bytes_processed":\([0-9]\{1,\}\).*/\1/p' <<<"$summary")"
ADDED="$(sed -n 's/.*"data_added":\([0-9]\{1,\}\).*/\1/p' <<<"$summary")"
[[ -n $SNAPSHOT_ID && -n $BYTES ]] || fail upload "restic did not report a snapshot id"
: "${ADDED:=0}"

stage verify
check_args=(check --retry-lock 2m)
# A deeper read of a slice of the pack data on the first of each UTC month.
[[ "$(date -u +%d)" == "01" ]] && check_args+=(--read-data-subset=10%)
if ! restic_run "$RERR" "$ROUT" "${check_args[@]}"; then
  show_tail "$RERR"
  # The unverified snapshot must not count as a good one. Older ones stay.
  restic_run "$RERR" "$ROUT" forget "$SNAPSHOT_ID" --retry-lock 2m || true
  fail verify "restic check failed; the new snapshot was withdrawn, older snapshots are untouched"
fi

stage retention
if ! restic_run "$RERR" "$ROUT" forget --prune --retry-lock 2m \
  --tag "$BACKUP_TAG" --host "$BACKUP_HOST" --group-by host,tags \
  --keep-daily "${CONF[KEEP_DAILY]}" --keep-weekly "${CONF[KEEP_WEEKLY]}"; then
  show_tail "$RERR"
  fail retention "retention failed; the new snapshot is stored and verified, nothing was lost"
fi

stage done
exit 0
