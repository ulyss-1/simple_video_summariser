#!/usr/bin/env bash
# Failure notification for the backup units (#150): systemd OnFailure= runs this
# with the name of the failed unit. The channel is whatever executable the owner
# names as NOTIFY_HOOK in backup.conf; nothing is hard-coded. With no hook the
# failure is only logged (journald), which the runbook says is not enough alone.
#
#   pg-backup-notify.sh <failed-unit-name>
# The hook is run as:  HOOK failure "<message>"    (the message holds no secret)

set -Eeuo pipefail
PROG="pg-backup-notify"
# shellcheck source=ops/host/pg-backup-lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pg-backup-lib.sh"
trap 'on_err $LINENO' ERR

unit="${1:-unknown-unit}"
[[ $unit =~ ^[A-Za-z0-9@._:-]+$ ]] || unit="invalid-unit-name"
message="ytdigest postgres backup problem: ${unit} failed on $(hostname -s). See: journalctl -u ${unit}"

load_config
log "event=notify unit=$unit"
hook="${CONF[NOTIFY_HOOK]}"
if [[ -z $hook ]]; then
  printf '%s: no NOTIFY_HOOK configured; failure of %s is only in the journal\n' "$PROG" "$unit" >&2
  exit 0
fi
stage notify
[[ -f $hook && -x $hook && ! -L $hook ]] || fail notify "NOTIFY_HOOK is not an executable regular file"
[[ "$(stat -c %u -- "$hook")" == "$(id -u)" ]] || fail notify "NOTIFY_HOOK is not owned by the running account"
hook_mode="$(file_mode "$hook")"
(((8#$hook_mode & 8#022) == 0)) || fail notify "NOTIFY_HOOK is writable by others"
timeout 60 "$hook" failure "$message" >/dev/null 2>&1 || fail notify "the notification hook failed"
log "event=notify outcome=ok unit=$unit"
