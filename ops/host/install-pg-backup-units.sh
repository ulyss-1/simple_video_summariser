#!/usr/bin/env bash
# Install the backup systemd units for the deploy account (#150). Needs root.
#
#   sudo ops/host/install-pg-backup-units.sh --user <deploy-account>
#
# Copies ops/host/systemd/* to /etc/systemd/system with @USER@ and @REPO@
# filled in, creates the staging directory (mode 700, owned by the account) and
# reloads systemd. It does NOT enable or start anything: the runbook does that
# after the first manual backup works.

set -Eeuo pipefail
PROG="install-pg-backup-units"
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd -- "$here/../.." && pwd)"
dest="${YTDIGEST_SYSTEMD_DIR:-/etc/systemd/system}"
staging="${YTDIGEST_STAGING_DIR:-/var/lib/ytdigest-backup}"

user=""
while (($#)); do
  case "$1" in
    --user) user="${2:-}"; shift 2 ;;
    *) printf 'usage: %s --user <deploy-account>\n' "$0" >&2; exit 2 ;;
  esac
done
[[ $user =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { printf '%s: --user must be a valid account name\n' "$PROG" >&2; exit 2; }
[[ $repo =~ ^/[A-Za-z0-9/_.-]+$ ]] || { printf '%s: unsupported characters in the checkout path\n' "$PROG" >&2; exit 2; }

install -d -m 755 -- "$dest"
for unit in "$here"/systemd/*; do
  name="$(basename -- "$unit")"
  sed -e "s|@USER@|$user|g" -e "s|@REPO@|$repo|g" -- "$unit" >"$dest/$name"
  chmod 644 -- "$dest/$name"
done
install -d -m 700 -o "$user" -- "$staging"
systemctl daemon-reload
printf '%s: installed to %s; nothing enabled yet\n' "$PROG" "$dest"
