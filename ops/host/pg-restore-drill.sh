#!/usr/bin/env bash
# Restore one off-host snapshot into a disposable PostgreSQL 18 and prove it (#150).
#
#   pg-restore-drill.sh --snapshot <snapshot-id> [--project ytdigest-drill-NAME]
#
# `latest` is not accepted: resolve it first with `pg-backup-status.sh --latest-id`.
# Fetches and decrypts the snapshot, validates the dump with pg_restore --list,
# restores with --exit-on-error into an empty database of a throwaway Compose
# project, then compares alembic_version, per-table row counts, index and
# constraint counts with the manifest recorded at backup time and runs
# read-only queries. The disposable project and all plaintext are removed on
# every path. It never touches the production project, volume or database.
# Runbook: _docs/backup-restore.md.

set -Eeuo pipefail
PROG="pg-restore-drill"
# shellcheck source=ops/host/pg-backup-lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pg-backup-lib.sh"
trap 'on_err $LINENO' ERR

DRILL_COMPOSE="$LIB_DIR/pg-restore-drill.compose.yml"
DRILL_DB="ytdrill"
SNAPSHOT=""
PROJECT=""
FETCH_DIR=""

while (($#)); do
  case "$1" in
    --snapshot) SNAPSHOT="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --fetch-only) FETCH_DIR="${2:-}"; shift 2 ;;
    *) printf 'usage: %s --snapshot <id> [--project ytdigest-drill-NAME | --fetch-only DIR]\n' "$0" >&2; exit 2 ;;
  esac
done

stage arguments
[[ -n $SNAPSHOT ]] || fail arguments "--snapshot <id> is required (find it with pg-backup-status.sh --latest-id)"
[[ $SNAPSHOT != latest ]] || fail arguments "'latest' is not accepted; resolve it with pg-backup-status.sh --latest-id"
[[ $SNAPSHOT =~ ^[0-9a-f]{8,64}$ ]] || fail arguments "the snapshot id must be 8 to 64 lowercase hex characters"

stage target
[[ -n $PROJECT ]] || PROJECT="ytdigest-drill-$(date -u +%Y%m%dt%H%M%S)-$$"
# Refuse before anything else is read or started.
[[ $PROJECT != "$PROD_PROJECT" ]] || fail target "refusing: $PROD_PROJECT is the production project"
[[ $PROJECT =~ ^ytdigest-drill-[a-z0-9][a-z0-9-]{2,40}$ ]] \
  || fail target "refusing: the project name must match ytdigest-drill-<name>"

drill_dc() {
  timeout --foreground "${DOCKER_TIMEOUT:-300}" docker compose --project-directory "$LIB_DIR" --env-file /dev/null \
    -p "$PROJECT" -f "$DRILL_COMPOSE" "$@"
}

WORK=""
STARTED=0
cleanup() {
  local rc=$?
  trap - EXIT ERR
  if ((STARTED)); then
    # Only ever the validated disposable project; the encrypted snapshot stays.
    if [[ $PROJECT != "$PROD_PROJECT" && $PROJECT == ytdigest-drill-* ]]; then
      drill_dc down -v --remove-orphans >/dev/null 2>&1 || log "event=cleanup outcome=failed project=$PROJECT"
    fi
  fi
  [[ -n $WORK ]] && rm -rf -- "$WORK"
  if ((rc == 0)) && [[ -n $FETCH_DIR ]]; then
    log "event=finish outcome=fetched snapshot=$SNAPSHOT"
  elif ((rc == 0)); then
    log "event=finish outcome=verified snapshot=$SNAPSHOT project=$PROJECT"
  else
    log "event=finish outcome=failed stage=$STAGE snapshot=$SNAPSHOT project=$PROJECT"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

load_config
check_restic

if [[ -n $FETCH_DIR ]]; then
  # Disaster recovery step 1: decrypt a snapshot to a new directory under the
  # staging directory and print the dump path. No Docker, nothing restored.
  stage fetch
  check_dir "${CONF[STAGING_DIR]}" fetch
  case "$(realpath -m -- "$FETCH_DIR")/" in
    "$(realpath -m -- "${CONF[STAGING_DIR]}")"/?*) ;;
    *) fail fetch "--fetch-only DIR must be a new directory inside STAGING_DIR" ;;
  esac
  [[ ! -e $FETCH_DIR ]] || fail fetch "$FETCH_DIR already exists"
  mkdir -m 700 -- "$FETCH_DIR"
  err="$(mktemp)"
  restic_run "$err" /dev/null restore "$SNAPSHOT" --target "$FETCH_DIR" --verify --retry-lock 2m \
    || { show_tail "$err"; rm -f -- "$err"; fail fetch "restic could not restore the snapshot"; }
  rm -f -- "$err"
  found="$(find "$FETCH_DIR" -type f -name ytdigest.dump | head -n1)"
  [[ -n $found && -s $found ]] || fail fetch "the snapshot does not contain ytdigest.dump"
  printf '%s\n' "$found"
  exit 0
fi

stage staging
check_dir "${CONF[STAGING_DIR]}" staging
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" || fail docker "docker is not reachable"
case "$(realpath -m -- "${CONF[STAGING_DIR]}")/" in
  "$docker_root"/*) fail staging "STAGING_DIR is inside Docker's data root" ;;
esac
WORK="$(mktemp -d -p "${CONF[STAGING_DIR]}" drill.XXXXXX)"
chmod 700 -- "$WORK"

stage target
leftover="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)" || fail docker "docker is not reachable"
volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)" || fail docker "docker is not reachable"
[[ -z $leftover && -z $volumes ]] || fail target "project $PROJECT already has containers or volumes; pick a new name"

log "event=start snapshot=$SNAPSHOT project=$PROJECT"

stage fetch
restic_run "$WORK/restic.err" "$WORK/restic.out" restore "$SNAPSHOT" --target "$WORK/restored" --verify --retry-lock 2m \
  || { show_tail "$WORK/restic.err"; fail fetch "restic could not restore the snapshot (unknown id, repository unreachable or wrong credentials)"; }
DUMP="$(find "$WORK/restored" -type f -name ytdigest.dump | head -n1)"
MANIFEST="$(find "$WORK/restored" -type f -name manifest.txt | head -n1)"
[[ -n $DUMP && -s $DUMP && -n $MANIFEST && -s $MANIFEST ]] \
  || fail fetch "the snapshot does not contain ytdigest.dump and manifest.txt"

stage start
STARTED=1
drill_dc up -d --wait --wait-timeout 120 "$DB_SERVICE" >/dev/null 2>"$WORK/up.err" \
  || { show_tail "$WORK/up.err"; fail start "the disposable PostgreSQL did not become healthy within 120 s"; }

psql_drill() { drill_dc exec -T "$DB_SERVICE" psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DRILL_DB" "$@"; }

stage target
existing="$(psql_drill -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')")" \
  || fail target "cannot inspect the disposable database"
[[ $existing == 0 ]] || fail target "the target database is not empty; refusing to restore into it"

stage validate
drill_dc exec -T "$DB_SERVICE" pg_restore --list <"$DUMP" >/dev/null 2>"$WORK/list.err" \
  || { show_tail "$WORK/list.err"; fail validate "pg_restore --list cannot read the dump"; }

stage restore
drill_dc exec -T "$DB_SERVICE" pg_restore --exit-on-error --no-owner --no-privileges \
  -U "$DB_USER" -d "$DRILL_DB" <"$DUMP" >/dev/null 2>"$WORK/restore.err" \
  || { show_tail "$WORK/restore.err"; fail restore "pg_restore --exit-on-error failed"; }

stage verify
manifest_sql | psql_drill >"$WORK/actual.raw" 2>"$WORK/verify.err" \
  || { show_tail "$WORK/verify.err"; fail verify "cannot read the restored database"; }
grep -v '^#' "$MANIFEST" | LC_ALL=C sort >"$WORK/expected.txt"
LC_ALL=C sort "$WORK/actual.raw" >"$WORK/actual.txt"
grep -q '^alembic ' "$WORK/actual.txt" || fail verify "alembic_version is missing from the restored database"
grep -q '^table ' "$WORK/actual.txt" || fail verify "the restored database has no tables"
if ! diff -u "$WORK/expected.txt" "$WORK/actual.txt" >"$WORK/diff.txt"; then
  # Only table names, counts and the alembic revision: no secret can be in here.
  sed 's/^/    /' "$WORK/diff.txt" >&2
  fail verify "the restored database differs from the manifest recorded at backup time"
fi

# Representative read-only queries: every table is readable and every index is valid.
psql_drill <<'SQL' >/dev/null 2>"$WORK/ro.err" || { show_tail "$WORK/ro.err"; fail verify "a read-only query failed on the restored database"; }
BEGIN READ ONLY;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' LOOP
    EXECUTE format('SELECT * FROM public.%I LIMIT 1', r.table_name);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_index WHERE NOT indisvalid) THEN
    RAISE EXCEPTION 'invalid index present';
  END IF;
END $$;
COMMIT;
SQL

stage done
tables="$(grep -c '^table ' "$WORK/actual.txt")"
log "event=verified snapshot=$SNAPSHOT tables=$tables $(grep '^alembic ' "$WORK/actual.txt" | tr ' ' '=')"
exit 0
