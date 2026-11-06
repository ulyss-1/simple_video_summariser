#!/usr/bin/env bash
# Status and freshness of the off-host Postgres backups (#150).
#
#   pg-backup-status.sh                timer, last service result, newest snapshot, repository size
#   pg-backup-status.sh --check-fresh  exit 0 fresh, 1 stale or no snapshot, 2 cannot tell
#   pg-backup-status.sh --latest-id    print the newest snapshot id only (for pg-restore-drill.sh)
#
# Nothing here prints a credential, the account id or the bucket name.
# Runbook: _docs/backup-restore.md.

set -Eeuo pipefail
PROG="pg-backup-status"
# shellcheck source=ops/host/pg-backup-lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pg-backup-lib.sh"
trap 'on_err $LINENO' ERR

MODE="status"
case "${1:-}" in
  "") ;;
  --check-fresh) MODE="fresh" ;;
  --latest-id) MODE="latest" ;;
  *) printf 'usage: %s [--check-fresh|--latest-id]\n' "$0" >&2; exit 2 ;;
esac

load_config
check_restic

if ! rows="$(snapshot_rows)"; then
  printf '%s: FAILED stage=repository: cannot list snapshots (repository unreachable or credentials wrong)\n' "$PROG" >&2
  exit 2
fi
newest="$(tail -n1 <<<"$rows")"
newest_epoch="${newest%% *}"
newest_id="${newest#* }"

if [[ $MODE == latest ]]; then
  if [[ -z $newest ]]; then
    printf '%s: FAILED stage=snapshots: the repository has no snapshot\n' "$PROG" >&2
    exit 1
  fi
  printf '%s\n' "$newest_id"
  exit 0
fi

limit=$(((10#${CONF[RPO_HOURS]} + 10#${CONF[GRACE_HOURS]}) * 3600))

if [[ $MODE == fresh ]]; then
  if [[ -z $newest ]]; then
    printf '%s: STALE: the repository has no snapshot\n' "$PROG" >&2
    exit 1
  fi
  age=$(($(now_epoch) - newest_epoch))
  if ((age > limit)); then
    printf '%s: STALE: newest snapshot %s is %d s old, limit %d s (RPO %sh + grace %sh)\n' \
      "$PROG" "$newest_id" "$age" "$limit" "${CONF[RPO_HOURS]}" "${CONF[GRACE_HOURS]}" >&2
    exit 1
  fi
  log "event=fresh outcome=ok snapshot=$newest_id age_s=$age limit_s=$limit"
  exit 0
fi

echo "== timer =="
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-timers --all --no-pager ytdigest-pg-backup.timer 2>&1 || true
  echo "enabled: $(systemctl is-enabled ytdigest-pg-backup.timer 2>&1 || true)"
  echo "== last service run =="
  systemctl show ytdigest-pg-backup.service --no-pager \
    -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp 2>&1 || true
else
  echo "(systemd is not available here)"
fi
echo "== newest off-host snapshot =="
if [[ -z $newest ]]; then
  echo "none"
else
  age=$(($(now_epoch) - newest_epoch))
  echo "id: $newest_id"
  echo "age_seconds: $age (limit $limit)"
  if ((age > limit)); then echo "freshness: STALE"; else echo "freshness: OK"; fi
fi
if [[ -z $rows ]]; then total=0; else total="$(wc -l <<<"$rows" | tr -d ' ')"; fi
echo "snapshots_total: $total"
echo "== repository size =="
err="$(mktemp)"
out="$(mktemp)"
if restic_run "$err" "$out" stats --mode raw-data --json --retry-lock 2m; then
  echo "raw_data_bytes: $(sed -n 's/.*"total_size":\([0-9]*\).*/\1/p' "$out")"
else
  echo "unavailable"
fi
rm -f -- "$err" "$out"
