# shellcheck shell=bash
# Shared by ops/host/pg-backup*.sh and pg-restore-drill.sh (#150). Sourced by
# those scripts, never executed. Runbook: _docs/backup-restore.md.
#
# Rules this file enforces for every caller:
#   * credentials come only from owner-provisioned mode-600 files outside the
#     checkout, are parsed (never sourced) and never printed or put in argv;
#   * a failure names its stage and prints nothing secret (redact());
#   * Compose variables from the caller's shell cannot retarget a stack.

BACKUP_MIN_RESTIC="0.18.1"    # the version the real-restic test ran against
BACKUP_TAG="ytdigest-pg"
BACKUP_HOST="ytdigest-pg"
PROD_PROJECT="ytdigest"
DB_SERVICE="db"
DB_USER="ytdigest"
LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$LIB_DIR/../.." && pwd)"

unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES \
  COMPOSE_ENV_FILES COMPOSE_PROJECT_DIRECTORY DOCKER_HOST DOCKER_CONTEXT
umask 077

# fd 3/4 are the real stdout/stderr. A trap can fire while a function runs with
# its own redirections (the dump), and the finish line must still reach journald.
exec 3>&1 4>&2

PROG="${PROG:-ytdigest-backup}"
STAGE="startup"
declare -A CONF=()
declare -A CRED=()

now_epoch() { date -u +%s; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# One key=value line per event, for journald and for a human at a terminal.
log() { printf 'ts=%s prog=%s %s\n' "$(now_iso)" "$PROG" "$*" >&3; }

stage() { STAGE="$1"; }

# fail STAGE MESSAGE: the message must never contain a credential or URL.
fail() {
  STAGE="$1"
  printf '%s: FAILED stage=%s: %s\n' "$PROG" "$1" "$2" >&4
  exit 1
}

on_err() {
  printf '%s: FAILED stage=%s: unexpected error at line %s\n' "$PROG" "$STAGE" "$1" >&4
}

# redact: stdin -> stdout with every known secret value, every URL and the
# account id removed. Used on all tool stderr before it is shown.
redact() {
  local line secret
  while IFS= read -r line || [[ -n $line ]]; do
    for secret in "${CRED[AWS_ACCESS_KEY_ID]:-}" "${CRED[AWS_SECRET_ACCESS_KEY]:-}" \
      "${CONF[R2_ACCOUNT_ID]:-}" "${CONF[R2_ENDPOINT_HOST]:-}" "${CONF[R2_BUCKET]:-}" "${PASSWORD_VALUE:-}"; do
      [[ -n $secret ]] && line="${line//"$secret"/<redacted>}"
    done
    line="$(sed -E 's#[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]]*#<url>#g' <<<"$line")"
    printf '%s\n' "$line"
  done
}

show_tail() { # file: last lines of a tool's stderr, redacted, indented
  [[ -s $1 ]] && tail -n 5 -- "$1" | redact | sed 's/^/    /' >&4
  return 0
}

# ---- credential and config files ------------------------------------------

file_mode() { stat -c %a -- "$1"; }

check_dir() { # path stage
  local d="$1" mode
  [[ -d $d && ! -L $d ]] || fail "$2" "directory missing or a symlink: $d"
  [[ "$(stat -c %u -- "$d")" == "$(id -u)" ]] || fail "$2" "directory not owned by the running account: $d"
  mode="$(file_mode "$d")"
  (((8#$mode & 8#077) == 0)) || fail "$2" "directory mode $mode is looser than 700: $d"
}

check_secret_file() { # path stage
  local f="$1" mode
  [[ -f $f && ! -L $f ]] || fail "$2" "file missing or not a regular file: $f"
  [[ -r $f ]] || fail "$2" "file not readable: $f"
  [[ "$(stat -c %u -- "$f")" == "$(id -u)" ]] || fail "$2" "file not owned by the running account: $f"
  mode="$(file_mode "$f")"
  (((8#$mode & 8#177) == 0)) || fail "$2" "file mode $mode is looser than 600: $f"
  [[ -s $f ]] || fail "$2" "file is empty: $f"
}

# parse_kv FILE ARRAY_NAME ALLOWED_KEYS_REGEX STAGE: strict KEY=VALUE lines,
# no quoting, no expansion, errors name the line number but never the value.
parse_kv() {
  local file="$1" arr="$2" allowed="$3" st="$4" line n=0 key value
  local -n target="$arr"
  while IFS= read -r line || [[ -n $line ]]; do
    n=$((n + 1))
    [[ -z ${line//[[:space:]]/} || $line == \#* ]] && continue
    [[ $line =~ ^([A-Z][A-Z0-9_]*)=([A-Za-z0-9/+=_.:@%,-]*)$ ]] \
      || fail "$st" "$(basename -- "$file"):$n is not a plain KEY=VALUE line"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ $key =~ ^($allowed)$ ]] || fail "$st" "$(basename -- "$file"):$n unknown key $key"
    target[$key]="$value"
  done <"$file"
}

need_int() { # name min max
  local v="${CONF[$1]}"
  [[ $v =~ ^[0-9]{1,6}$ ]] && ((10#$v >= $2 && 10#$v <= $3)) \
    || fail config "$1 must be an integer from $2 to $3"
}

load_config() {
  stage config
  CONFIG_DIR="${YTDIGEST_BACKUP_CONFIG_DIR:-/etc/ytdigest-backup}"
  case "$(realpath -m -- "$CONFIG_DIR")/" in
    "$REPO_ROOT"/*) fail config "the config directory must be outside the checkout" ;;
  esac
  check_dir "$CONFIG_DIR" config
  check_secret_file "$CONFIG_DIR/backup.conf" config
  parse_kv "$CONFIG_DIR/backup.conf" CONF \
    'R2_ACCOUNT_ID|R2_BUCKET|R2_JURISDICTION|R2_ENDPOINT_HOST|RPO_HOURS|GRACE_HOURS|RETENTION_DAYS|KEEP_LAST_FLOOR|STAGING_DIR|NOTIFY_HOOK' config
  CONF[R2_JURISDICTION]="${CONF[R2_JURISDICTION]:-default}"
  CONF[RPO_HOURS]="${CONF[RPO_HOURS]:-24}"
  CONF[GRACE_HOURS]="${CONF[GRACE_HOURS]:-6}"
  CONF[RETENTION_DAYS]="${CONF[RETENTION_DAYS]:-14}"
  CONF[KEEP_LAST_FLOOR]="${CONF[KEEP_LAST_FLOOR]:-3}"
  CONF[STAGING_DIR]="${CONF[STAGING_DIR]:-/var/lib/ytdigest-backup}"
  CONF[NOTIFY_HOOK]="${CONF[NOTIFY_HOOK]:-}"
  need_int RPO_HOURS 1 720
  need_int GRACE_HOURS 0 720
  need_int RETENTION_DAYS 1 3650   # positive: 0 would keep nothing
  need_int KEEP_LAST_FLOOR 1 1000   # the floor: never fewer than this many
  [[ ${CONF[R2_BUCKET]:-} =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] \
    || fail config "R2_BUCKET missing or not a valid bucket name (3-63 of a-z 0-9 -)"
  case "${CONF[R2_JURISDICTION]}" in
    default) jur="" ;;
    eu) jur="eu." ;;
    fedramp) jur="fedramp." ;;
    *) fail config "R2_JURISDICTION must be default, eu or fedramp" ;;
  esac
  if [[ -n ${CONF[R2_ENDPOINT_HOST]:-} ]]; then
    [[ ${CONF[R2_ENDPOINT_HOST]} =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$ ]] \
      || fail config "R2_ENDPOINT_HOST must be a bare host name (no scheme, path or userinfo)"
    ENDPOINT_HOST="${CONF[R2_ENDPOINT_HOST]}"
  else
    [[ ${CONF[R2_ACCOUNT_ID]:-} =~ ^[0-9a-f]{32}$ ]] \
      || fail config "R2_ACCOUNT_ID must be the 32-character hex account id"
    ENDPOINT_HOST="${CONF[R2_ACCOUNT_ID]}.${jur}r2.cloudflarestorage.com"
  fi
  [[ ${CONF[STAGING_DIR]} == /* ]] || fail config "STAGING_DIR must be an absolute path"
  if [[ -n ${CONF[NOTIFY_HOOK]} && ${CONF[NOTIFY_HOOK]} != /* ]]; then
    fail config "NOTIFY_HOOK must be an absolute path"
  fi

  stage credentials
  check_secret_file "$CONFIG_DIR/r2.env" credentials
  parse_kv "$CONFIG_DIR/r2.env" CRED 'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY' credentials
  [[ -n ${CRED[AWS_ACCESS_KEY_ID]:-} && -n ${CRED[AWS_SECRET_ACCESS_KEY]:-} ]] \
    || fail credentials "r2.env must define AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
  check_secret_file "$CONFIG_DIR/restic.password" credentials
  PASSWORD_VALUE="$(<"$CONFIG_DIR/restic.password")"
  ((${#PASSWORD_VALUE} >= 20)) || fail credentials "restic.password is shorter than 20 characters"

  export AWS_ACCESS_KEY_ID="${CRED[AWS_ACCESS_KEY_ID]}"
  export AWS_SECRET_ACCESS_KEY="${CRED[AWS_SECRET_ACCESS_KEY]}"
  export AWS_DEFAULT_REGION=auto
  export RESTIC_PASSWORD_FILE="$CONFIG_DIR/restic.password"
  if [[ -n ${YTDIGEST_TEST_REPOSITORY:-} ]]; then
    # Tests only: lets the suite aim restic at a local directory. It is loud
    # on purpose; nothing in the units or the runbook sets it.
    printf '%s: WARNING test repository override active, NOT off-host\n' "$PROG" >&2
    export RESTIC_REPOSITORY="$YTDIGEST_TEST_REPOSITORY"
  else
    export RESTIC_REPOSITORY="s3:https://${ENDPOINT_HOST}/${CONF[R2_BUCKET]}"
  fi
}

check_restic() {
  stage prereq
  local out ver
  out="$(restic version 2>/dev/null)" || fail prereq "restic is not installed or not runnable"
  ver="$(awk 'NR==1{print $2}' <<<"$out")"
  [[ $ver =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail prereq "cannot read the restic version"
  [[ "$(printf '%s\n%s\n' "$BACKUP_MIN_RESTIC" "$ver" | sort -V | head -n1)" == "$BACKUP_MIN_RESTIC" ]] \
    || fail prereq "restic $ver is older than the pinned minimum $BACKUP_MIN_RESTIC"
}

# Run a restic command with a bounded time; stderr goes to $1, stdout to $2.
restic_run() { # errfile outfile args...
  local err="$1" out="$2"
  shift 2
  timeout --foreground "${RESTIC_TIMEOUT:-3600}" restic "$@" >"$out" 2>"$err"
}

# ---- the manifest, one query set shared by backup and drill -----------------

manifest_sql() {
  cat <<'SQL'
SELECT 'alembic ' || coalesce((SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version), '<empty>');
SELECT 'table ' || table_name || ' ' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name;
SELECT 'indexes ' || count(*) FROM pg_indexes WHERE schemaname = 'public';
SELECT 'constraints ' || count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public';
SQL
}

# ---- snapshots ----------------------------------------------------------------

# snapshot_rows: "<epoch> <id>" per snapshot of ours, newest last, on stdout.
snapshot_rows() {
  local err out times ids t
  err="$(mktemp)"
  out="$(mktemp)"
  if ! restic_run "$err" "$out" snapshots --json --tag "$BACKUP_TAG" --host "$BACKUP_HOST" --retry-lock 2m; then
    show_tail "$err"
    rm -f -- "$err" "$out"
    return 1
  fi
  mapfile -t times < <(grep -o '"time":"[^"]*"' "$out" | cut -d'"' -f4)
  mapfile -t ids < <(grep -o '"id":"[0-9a-f]\{64\}"' "$out" | cut -d'"' -f4)
  rm -f -- "$err" "$out"
  ((${#times[@]} == ${#ids[@]})) || return 1
  for i in "${!times[@]}"; do
    t="$(date -d "${times[$i]}" +%s)" || return 1
    printf '%s %s\n' "$t" "${ids[$i]}"
  done | sort -n
}
