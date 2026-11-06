# Postgres backup and restore runbook (#150)

Protects the `ytdigest` Postgres database on the Compose deploy host against
loss of the disk or of the `pgdata` volume. Nightly, an encrypted logical
backup (`pg_dump -Fc`) is pushed with **restic** to a **Cloudflare R2** bucket.
A separate command restores any snapshot into a disposable Postgres and proves
it.

Not covered: `audio`, `whisper-models`, images, the checkout, `.env`, API keys,
raw `pgdata` copies, point-in-time recovery, and the k3s rollout (#210).

> **Never run `docker compose down -v` on the server.** It deletes `pgdata`.
> Nothing in this runbook does. The only `down -v` anywhere is inside
> `pg-restore-drill.sh`, on a disposable `ytdigest-drill-*` project it
> refuses to confuse with production.

Status of this work: the scripts, units and tests are in the repo and pass
against stubs and a real local restic and Postgres. **No real R2 bucket has
been used yet.** What that leaves unproven is listed in section 11. Issue #150
stays open until the owner records the evidence in section 10.

---

## 0. Steps you must do (owner)

Everything below needs you: a Cloudflare account, the deploy host, and your
password manager. Do them in order. Dashboard labels were checked against
Cloudflare's documentation on 2026-11-06 and may have moved; the concepts are
stable.

### A. Cloudflare account and R2

1. **Account.** Sign up (or sign in) at <https://dash.cloudflare.com>. Use an
   email you will still own in five years and turn on two-factor
   authentication (Profile, Authentication). A card is **likely required**:
   R2 asks you to complete a checkout to add an R2 subscription, and
   third-party guides report a card or PayPal is requested for verification
   and not charged inside the free tier. Cloudflare's own page does not state
   it, so treat it as required and confirm at signup.
2. **Billing alert.** In the dashboard open Notifications, add a billing or
   usage notification to your email, so a runaway bill cannot be silent.
3. **Enable R2.** Dashboard, Storage & databases, **R2 object storage**,
   Overview. Complete the R2 subscription flow. Free tier, per month,
   Standard storage: 10 GB-month stored, 1 million Class A operations,
   10 million Class B operations, free egress. Past that you are billed, not
   blocked (about $0.015 per GB-month stored). Our expected use is about
   130 MB (section 8).
4. **Create the bucket.** R2 Overview, **Create bucket**.
   - Name: for example `ytdigest-pg-backup`. 3 to 63 characters, lowercase
     letters, digits and hyphens.
   - **Location: choose "Specify jurisdiction", European Union (EU).** This
     is **irreversible**: the jurisdiction is fixed when the bucket is created
     and cannot be changed, and it decides the endpoint host. A location hint
     is only best-effort and is not a residency guarantee. If you have a reason
     to pick another jurisdiction, do it now, and set `R2_JURISDICTION`
     accordingly in step 11. **If the bucket already exists in the default
     jurisdiction, that is acceptable:** set `R2_JURISDICTION=default`
     (endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`). The caveat is
     residency: without a jurisdiction, Cloudflare only honours a best-effort
     location hint, so objects may be stored outside the EU, and this cannot be
     changed later (a different jurisdiction means a new bucket and a new
     repository). Cloudflare is still a US company; the jurisdiction is about
     where objects are stored.
   - Storage class: **Standard** (never Infrequent Access: it has no free
     allowance, a 30-day minimum and a retrieval fee).
   - Leave public access **off**: do not enable the `r2.dev` subdomain and do
     not attach a custom domain.
5. **Note the account id** (R2 Overview, right-hand panel, "Account ID"; also
   the first path segment of the dashboard URL). The S3 endpoint is
   `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com` for the EU jurisdiction
   and `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` otherwise. **You never
   type the endpoint anywhere**: the scripts build it from the account id and
   `R2_JURISDICTION`. Region is always `auto`.
6. **Create the read-write API token.** R2 Overview, **Manage** next to API
   tokens, **Create Account API token** (a user token also works).
   - Name `ytdigest-pg-backup-rw`.
   - Permission **Object Read & Write**.
   - **Specify bucket(s): select only the backup bucket.** Not Admin, not
     "all buckets". Only the two Object levels can be limited to a bucket.
   - TTL: forever (or a long TTL and a calendar reminder to renew it).
   - Client IP filtering: leave empty unless the host has a fixed address.
   - Create. Copy the **Access Key ID** and the **Secret Access Key** right
     away; the secret is shown once. Ignore the "Token value" (a Cloudflare
     API bearer token we do not use); do not store it.
7. **Create a read-only token** for restore drills from another machine: same
   steps, name `ytdigest-pg-backup-ro`, permission **Object Read only**, same
   single bucket. Put its Access Key ID and Secret in your password manager.
   R2 has no write-only or no-delete level, so the read-write token on the host
   can also delete backups (section 9).

### B. The restic password (do this before anything touches the bucket)

8. **Generate and store it away from the host.** On your own computer, not the
   server:

   ```bash
   openssl rand -hex 32
   ```

   Save the 64 characters in your password manager as "ytdigest restic
   repository password", **and** keep one offline copy (printed in a sealed
   envelope or on a USB stick) somewhere other than the server. Open both
   copies and check they read back correctly. **If this password is lost, every
   backup is unrecoverable, and nothing on the host can restore it.** Write
   down who holds it: the owner.

### C. The deploy host

Run these as the deploy account in the checkout (`/opt/ytdigest`). Commands that
change the system are marked **(sudo)**.

9. **Install restic, minimum 0.18.1.** **(sudo)**

   ```bash
   sudo apt install restic
   restic version        # must print 0.18.1 or newer
   ```

   Ubuntu 26.04's package is 0.18.1. If your release ships older, install the
   official binary instead and verify its checksum against the release's
   `SHA256SUMS` (for 0.18.1 the amd64 archive is
   `680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d`,
   `restic_0.18.1_linux_amd64.bz2`, from
   <https://github.com/restic/restic/releases/tag/v0.18.1>):

   ```bash
   cd "$(mktemp -d)"
   curl -fLO https://github.com/restic/restic/releases/download/v0.18.1/restic_0.18.1_linux_amd64.bz2
   echo '680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d  restic_0.18.1_linux_amd64.bz2' | sha256sum -c -
   bunzip2 restic_0.18.1_linux_amd64.bz2
   sudo install -m 755 restic_0.18.1_linux_amd64 /usr/local/bin/restic
   ```

10. **Create the credential directory** (owner: the deploy account, mode 700,
    outside the checkout) **(sudo)**, and the staging directory:

    ```bash
    sudo install -d -m 700 -o "$USER" -g "$USER" /etc/ytdigest-backup
    sudo install -d -m 700 -o "$USER" -g "$USER" /var/lib/ytdigest-backup
    ```

11. **Write the three files.** Never put these values on a command line, in
    `.env`, in Git or in a unit file. `read -s` does not echo, and `printf` is a
    shell builtin, so nothing lands in `ps` output or history:

    ```bash
    umask 077
    cp ops/host/backup.conf.example /etc/ytdigest-backup/backup.conf
    "${EDITOR:-nano}" /etc/ytdigest-backup/backup.conf      # account id, bucket, jurisdiction

    read -rsp 'R2 Access Key ID (read-write token): ' K; printf '\n'
    read -rsp 'R2 Secret Access Key: ' S; printf '\n'
    printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' "$K" "$S" > /etc/ytdigest-backup/r2.env
    unset K S

    read -rsp 'restic repository password (from step 8): ' P; printf '\n'
    printf '%s' "$P" > /etc/ytdigest-backup/restic.password
    unset P

    chmod 600 /etc/ytdigest-backup/*
    stat -c '%a %U %n' /etc/ytdigest-backup /etc/ytdigest-backup/*
    ```

    Expected: the directory `700`, each file `600`, all owned by you. The
    scripts refuse to run with anything looser, with a symlink, or with an
    empty file.

12. **Initialise the repository once, then check everything.**

    ```bash
    ops/host/pg-backup.sh --init     # creates the encrypted repository in the bucket
    ops/host/pg-backup.sh --check    # config, credentials, restic version, db up, bucket reachable
    ```

    `--init` refuses if a repository already exists. `--check` changes nothing.

13. **First manual backup.**

    ```bash
    ops/host/pg-backup.sh
    ops/host/pg-backup-status.sh
    ```

    Expected: `event=finish outcome=ok snapshot=<64 hex> bytes=... added=...`,
    then a status report with `freshness: OK`. Note the database size
    (`bytes=`).

14. **Restore drill** into a throwaway PostgreSQL 18 (needs no network, takes
    about a minute):

    ```bash
    ops/host/pg-restore-drill.sh --snapshot "$(ops/host/pg-backup-status.sh --latest-id)"
    ```

    Expected last lines: `event=verified ... tables=N alembic=...` and
    `outcome=verified`. Check afterwards that nothing is left over:
    `docker ps -a --filter name=ytdigest-drill` and
    `docker volume ls --filter name=ytdigest-drill` print nothing.

15. **Install and enable the timers.** **(sudo)**

    ```bash
    sudo ops/host/install-pg-backup-units.sh --user "$USER"
    sudo systemctl enable --now ytdigest-pg-backup.timer ytdigest-pg-backup-fresh.timer
    systemctl list-timers 'ytdigest-pg-backup*'
    ```

    The backup runs nightly at 02:30 UTC (plus up to 10 minutes of jitter) and,
    with `Persistent=true`, a night missed while the host was off runs after
    boot. The freshness check runs every 6 hours.

16. **Set up failure notification.** The units call
    `ytdigest-pg-backup-notify@<unit>.service` on any failure, which runs the
    executable named by `NOTIFY_HOOK` in `backup.conf`. Without a hook, a failure
    is only in the journal, which nobody reads. Pick a channel (email, Telegram,
    or a dead-man's-switch such as Healthchecks.io) and write the hook. Example
    for a Healthchecks.io-style ping, with the secret URL in a `600` curl config file read with `-K`, so the URL is never a command-line argument (visible in `ps`):

    ```bash
    umask 077
    read -rsp 'Failure ping URL: ' U; printf '\n'
    printf 'url = "%s"\n' "$U" > /etc/ytdigest-backup/notify-curl.conf; unset U
    chmod 600 /etc/ytdigest-backup/notify-curl.conf
    printf '%s\n' '#!/bin/bash' '# $1 = failure, $2 = message' \
      'curl -fsS -m 20 -K /etc/ytdigest-backup/notify-curl.conf --data-raw "$2" >/dev/null' \
      > /etc/ytdigest-backup/notify-hook.sh
    chmod 700 /etc/ytdigest-backup/notify-hook.sh
    ```

    Add `NOTIFY_HOOK=/etc/ytdigest-backup/notify-hook.sh` to `backup.conf`, then
    prove the path end to end:

    ```bash
    sudo systemctl start 'ytdigest-pg-backup-notify@test.service'
    ```

    You should receive the message. (A hook that is not an executable regular
    file owned by you, or is writable by others, is refused.)

17. **Wait for one timer-triggered run** (the next 02:30 UTC), then collect the
    evidence in section 10.

18. Optional hardening later: bucket lock (section 9), a periodic extra copy of
    one snapshot to your own computer using the read-only token.

**Evidence to paste back** (secrets removed): see section 10.

---

## 1. Design

```
db container --pg_dump -Fc--> staging (700) --pg_restore --list--> restic backup --> R2 bucket
   (one exported snapshot       |                                      |
    shared with the manifest)   +-- removed on every exit path         +-- restic check, then forget --prune
```

| File                                                | Role                                                                             |
| --------------------------------------------------- | -------------------------------------------------------------------------------- |
| `ops/host/pg-backup.sh`                             | the backup (`--check`, `--init` for setup)                                       |
| `ops/host/pg-backup-status.sh`                      | status report, `--check-fresh`, `--latest-id`                                    |
| `ops/host/pg-restore-drill.sh`                      | restore into a disposable stack and verify; `--fetch-only` for disaster recovery |
| `ops/host/pg-restore-drill.compose.yml`             | the disposable PostgreSQL 18 (no network, own volume)                            |
| `ops/host/pg-backup-notify.sh`                      | runs your `NOTIFY_HOOK`                                                          |
| `ops/host/pg-backup-lib.sh`                         | shared checks, redaction, manifest query                                         |
| `ops/host/systemd/*` + `install-pg-backup-units.sh` | service and timer units                                                          |
| `ops/host/backup.conf.example`                      | the non-secret configuration                                                     |

How one backup works:

1. It checks the config files (owner, mode `600`/`700`, not symlinks, not
   empty), the restic version, and that the `db` service of project `ytdigest`
   (`-f compose.yml`) is running and ready. It never starts, migrates or
   restarts anything.
2. It takes the lock, clears leftovers of an interrupted run, and opens one
   `REPEATABLE READ` session that exports a snapshot and records a **manifest**:
   the alembic revision, the exact row count of every table, and the index and
   constraint counts. `pg_dump --snapshot` uses the same snapshot, so the
   manifest matches the dump even though the workers keep writing.
3. It checks free space, dumps in custom format, and reads the dump back with
   `pg_restore --list` (the PostgreSQL 18 client in the `db` container). Only
   then is a `.partial` file renamed and published.
4. `restic backup` uploads the dump and manifest (encrypted, authenticated),
   `restic check` verifies the repository, and only after that does
   `restic forget --prune` apply retention (`--keep-within 14d --keep-last 3`). If the check
   fails the new snapshot is withdrawn and every older one is kept. On the 1st
   of each UTC month the check also reads 10% of the data.
5. The plaintext staging directory is removed on success, failure, `SIGINT` and
   `SIGTERM`. The next run retries with nothing to clean by hand.

Credentials are read from `/etc/ytdigest-backup/` (override the directory with
`YTDIGEST_BACKUP_CONFIG_DIR`; it must be outside the checkout). They reach
restic only as environment variables of that process and a password file path,
never as arguments. Every restic message is filtered before it is shown: known
secret values, the account id, the bucket and any URL are removed. Failures
print `FAILED stage=<name>` and nothing else sensitive; stages are `config`,
`credentials`, `prereq`, `staging`, `docker`, `db`, `lock`, `manifest`, `disk`,
`dump`, `validate`, `upload`, `verify`, `retention`.

`YTDIGEST_TEST_REPOSITORY`, `YTDIGEST_BACKUP_PROJECT` and
`YTDIGEST_BACKUP_COMPOSE_FILE` exist only so the test suite can aim the scripts
at a local directory and a throwaway stack. The units and this runbook never set
them; the script prints a warning if the repository override is set.

---

## 2. Prerequisites and version checks

- The deploy host from `_docs/deploy-host.md` with the stack deployed and `db`
  healthy; the deploy account in the `docker` group.
- `restic` 0.18.1 or newer (`restic version`). 0.18.1 is the version the
  repository tests ran against with a real restic; older versions are not
  claimed to work and the scripts refuse them.
- GNU coreutils `timeout`, `flock`, `stat`, `date`, `df`; bash 4.3 or newer;
  systemd. All are in a stock Ubuntu.
- Free space in the staging directory of at least the database size plus
  64 MB (the script checks and stops with stage `disk`).

---

## 3. Status and freshness

```bash
ops/host/pg-backup-status.sh                # timer, last service result, newest snapshot, repository size
ops/host/pg-backup-status.sh --check-fresh  # exit 0 fresh, 1 stale or no snapshot, 2 cannot tell
ops/host/pg-backup-status.sh --latest-id    # newest snapshot id, for the drill
journalctl -u ytdigest-pg-backup.service --since '2 days ago' --no-pager
```

Stale means the newest snapshot is older than `RPO_HOURS + GRACE_HOURS`
(24 + 6 = 30 hours by default; exactly 30 hours is still fresh). The journal
records, per run, `event=start`, `event=finish outcome=ok|failed`, the stage on
failure, the snapshot id and byte counts. It never records dump contents or
credentials. `ytdigest-pg-backup-fresh.timer` runs the freshness check every
6 hours and notifies through the same hook, so a host that stops running the
timer at all is caught too. A dead-man's-switch channel (a ping that must
arrive) additionally catches a host that is off; app dead-letter alerting (#67)
is not backup monitoring.

---

## 4. Retention and cadence

- Nightly 02:30 UTC, 24 h RPO, 6 h grace. A snapshot is kept only if it is
  newer than 14 days (counted back from the newest snapshot; exactly 14 days old is
  removed) and older ones are removed automatically: `restic forget --prune --keep-within 14d --keep-last 3`
  (`RETENTION_DAYS=14`, `KEEP_LAST_FLOOR=3` in `backup.conf`; both must be
  positive integers, `0` is rejected).
- **Safety floor.** `--keep-last 3` always keeps the newest three snapshots,
  however old. After a long outage of backups, or a clock jump, the age rule
  alone could delete every known-good snapshot; the floor cannot. restic counts
  `--keep-within` back from the newest snapshot, not from the wall clock.
- **Do not use an R2 bucket lifecycle (expiry) rule for this.** A lifecycle
  rule deletes objects by age, including restic pack files that newer snapshots
  still reference, and corrupts the repository. Retention is restic's job only.
  If you later add bucket lock (section 9), its age must stay **shorter** than
  `RETENTION_DAYS` and it must exclude the `locks/` prefix.
- Retention runs only after the new snapshot is uploaded and `restic check`
  passed. A failed backup or check never deletes an older snapshot.
- Every snapshot is a full dump, and restic deduplicates compressed dumps
  poorly, so plan for roughly the database size per snapshot.

---

## 5. Restore drill (what to run, how often)

```bash
ops/host/pg-restore-drill.sh --snapshot <id>          # id from pg-backup-status.sh --latest-id
```

It requires an explicit id (`latest` is rejected so a drill always names what it
tested). It fetches and decrypts the snapshot, validates it with
`pg_restore --list`, starts a PostgreSQL 18 in a new Compose project named
`ytdigest-drill-<time>-<pid>` (no network, its own volume) and waits for
readiness with `--wait --wait-timeout 120`. It refuses if the project is
`ytdigest` or does not match `ytdigest-drill-*`, if that project already has
containers or volumes, or if the target database is not empty. Then it runs
`pg_restore --exit-on-error`, compares the alembic revision, every table's row
count and the index and constraint counts with the manifest taken at backup
time, checks no index is invalid, and reads every table. Empty tables are
valid. The disposable project, its volume and all plaintext are removed on
every exit path; the encrypted snapshot stays. A failed run exits non-zero and
verifies nothing.

Run it after setup, after any change to the scripts, and at least once a
quarter. Drills from another machine need only restic, Docker, the checkout,
`backup.conf` and the **read-only** token in `r2.env`, plus the restic password.

---

## 6. Production recovery

First decide which case you are in.

### 6a. Disaster recovery: the host or its disk is gone

1. Prepare a host per `_docs/deploy-host.md` (Docker, checkout, `.env`).
   Install restic (step 9). Recreate `/etc/ytdigest-backup/` from your password
   manager: `backup.conf` (account id, bucket, jurisdiction), `r2.env` (the
   read-write or the read-only token), `restic.password`. All `600`.
2. Find and fetch the snapshot. Do not start the application services.

   ```bash
   ops/host/pg-backup-status.sh --latest-id
   ops/host/pg-restore-drill.sh --snapshot <id>        # prove it first, in a disposable stack
   ops/host/pg-restore-drill.sh --snapshot <id> --fetch-only /var/lib/ytdigest-backup/dr
   ```

   The last command prints the path of `ytdigest.dump` (its `manifest.txt` is
   next to it).

3. Start only the database, on its new empty volume, and confirm it is empty:

   ```bash
   docker compose -f compose.yml up -d --wait db
   docker compose -f compose.yml exec -T db psql -U ytdigest -d ytdigest -c '\dt'   # "Did not find any relations"
   ```

   On a brand-new volume the first healthcheck can pass while the entrypoint is
   still initialising, and `psql` then says "the database system is starting
   up". Wait until
   `docker compose -f compose.yml exec -T db pg_isready -h 127.0.0.1 -U ytdigest -d ytdigest`
   succeeds (the TCP listener only exists once the real server is up), then retry.

4. Restore, stopping at the first error:

   ```bash
   docker compose -f compose.yml exec -T db pg_restore --exit-on-error --no-owner -U ytdigest -d ytdigest \
     < "<the ytdigest.dump path printed by --fetch-only>"
   ```

5. **Inspect before migrating.** Read the restored revision and compare with
   `manifest.txt`:

   ```bash
   docker compose -f compose.yml exec -T db psql -U ytdigest -d ytdigest -c 'SELECT * FROM alembic_version'
   ```

   Only if the checkout expects a newer revision run
   `docker compose -f compose.yml run --rm migrate`. Then
   `docker compose -f compose.yml up -d --build` and re-run the smoke check
   (`_docs/deploy-host.md` section 12).

6. Delete the plaintext: `rm -rf /var/lib/ytdigest-backup/dr`.

### 6b. In-place replacement: the host is alive but the database is damaged

A deliberate maintenance procedure, never automatic. It never overlays files
into a live Postgres data directory and never edits `pgdata` in place.

1. Announce the outage. Stop every writer, then the database:
   `docker compose -f compose.yml stop web api planner analyzer transcriber db`.
2. **Preserve the failed volume.** While `db` is stopped, copy it aside (a raw
   copy of a stopped server is safe; the rule is only "never copy a live one"):

   ```bash
   BACKUP_IMG="$(docker compose -f compose.yml config --images | grep '^postgres:')"   # the pinned image already on the host
   FAILED="ytdigest_pgdata_failed_$(date -u +%Y%m%d)"
   docker volume create "$FAILED"
   docker run --rm --pull=never -v ytdigest_pgdata:/from:ro -v "$FAILED":/to "$BACKUP_IMG" cp -a /from/. /to/
   for v in ytdigest_pgdata "$FAILED"; do
     docker run --rm --pull=never -v "$v":/v:ro "$BACKUP_IMG" sh -c 'find /v -type f | wc -l; du -sk /v'
   done
   ```

   The two file counts and sizes must be equal and not zero. Do not delete
   `ytdigest_pgdata` until they match and the restore below is verified; keep
   the `_failed_` volume until you are sure.

3. Prove the snapshot with the drill (6a step 2), then fetch it.
4. Get fresh storage: with `db` stopped and the old volume safely copied, remove
   only the container and the volume by name (not `down -v`):

   ```bash
   docker compose -f compose.yml rm -f db
   docker volume rm ytdigest_pgdata
   ```

   Then continue from 6a step 3 (start `db`, confirm empty, restore, inspect,
   migrate only if needed, start the services).

---

## 7. Things that go wrong

- **Key (restic password) lost.** Every snapshot is permanently unreadable.
  Start a new repository (new bucket or an empty one), run `--init` with a new
  password stored the way step 8 says, and take a backup at once. This is why
  the password is kept in two places away from the host.
- **`FAILED stage=auth: no usable answer from the repository within 45s`**
  (also from `--check` and `--init`). restic retries a rejected request with
  growing waits for minutes, so every run first asks the repository for its
  config under a 45 s cap (`AUTH_PROBE_TIMEOUT` in the environment; real
  uploads keep the long `RESTIC_TIMEOUT`). No answer in time means one of: a
  wrong key pair in `r2.env`, a deleted or expired API token, a token not
  scoped to this bucket (Object Read and Write on the bucket), a wrong bucket
  name or account id in `backup.conf`, or R2 unreachable. Fix: in the
  Cloudflare dashboard confirm the token exists and covers the bucket, create a
  new token if not, rewrite `r2.env`, then run `pg-backup.sh --check`. The
  message names no key, account id or bucket on purpose. A repository that
  answers but is not initialised fails fast at stage `auth` or `repository`
  with a different message (run `--init`).
- **R2 unreachable or a token revoked.** The backup fails at stage `auth`,
  `upload` or
  `verify`, the freshness check turns red after 30 hours, and local data is
  unaffected. Check Cloudflare status and the token; nothing needs cleaning up,
  the next timer run retries by itself. Create a new token and rewrite
  `r2.env` if it was revoked.
- **Disk full on the host.** Stage `disk` stops before the dump; `dump` means
  the disk filled during it. Free space in `/var/lib/ytdigest-backup`
  (`docker builder prune`, `docker image prune`) and run the backup again.
- **Interrupted run** (reboot, `kill`, power loss). Nothing to do. The next run
  removes the leftover `work` directory under the lock before starting, and an
  interrupted `restic backup` leaves no snapshot.
- **Repository lock contention.** Stage `lock` means another backup on this host
  is still running. Stage `upload`/`retention` with a lock message means another
  machine (a drill, a manual `restic`) holds the repository lock; restic waits up
  to 2 minutes first. If a crashed run left a stale lock, check nobody is
  running restic, then `restic unlock` with the same environment
  (`set -a; . /etc/ytdigest-backup/r2.env; set +a`, plus `RESTIC_REPOSITORY` and
  `RESTIC_PASSWORD_FILE`). Never `unlock --remove-all` while a run is active.
- **Over the free tier.** Overage is billed, not blocked. The status report
  prints the repository size so growth is visible.
- **Version drift.** If `restic` is upgraded and the scripts reject it or break,
  the freshness alert fires; fix by pinning the version in step 9.

---

## 8. R2 specifics and cost

Checked on 2026-11-06 against Cloudflare's documentation.

- Free tier per month (Standard storage only): 10 GB-month stored, 1 million
  Class A operations, 10 million Class B operations, free egress. Infrequent
  Access has no free allowance. Overage is billed ($0.015 per GB-month,
  $4.50 per million Class A, $0.36 per million Class B) and there is no
  documented hard cap or spending limit.
- Expected use: the development database is about 11 MB, so about 14
  nightly dumps of the full size (restic dedupes compressed dumps poorly) is
  about 160 MB, **about 1.6% of the 10 GB**. It stays inside the free tier until
  the database reaches roughly 700 MB. A run uses a few hundred
  operations. Record the real database size at the first backup.
- **Data in an unpaid or suspended account is not guaranteed.** Cloudflare
  publishes nothing about R2 data when an invoice goes unpaid, and there are
  anecdotal reports of billing-related suspensions. Treat R2 as **one** copy:
  keep the billing alert, watch the freshness check, and consider an
  occasional manual copy of a snapshot to your own computer.
- Endpoint: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, or with the EU
  jurisdiction `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com` (FedRAMP:
  `.fedramp.`). Region `auto`. restic uses path-style addressing for non-AWS
  endpoints, which R2 accepts.

---

## 9. Residual risk and optional hardening

- **A compromised host can delete the backups.** The read-write token on the host
  can delete objects, and R2 has no write-only level and no object versioning.
  The scripts and token scope limit accidents, not an attacker with the token.
- **Bucket lock** is optional hardening: R2 bucket lock rules make objects
  undeletable for a retention age or indefinitely, and S3 deletes return 409.
  restic must delete and rewrite objects on `forget --prune` and removes its own
  `locks/` files, so a lock rule must exclude the `locks/` prefix and use an age
  shorter than the snapshot retention, or prune must run off-host with the rule
  relaxed. A third-party tutorial says bucket lock needs a paid plan; Cloudflare's
  page does not say so, so check in the dashboard. This is not implemented; if
  you want it, ask for a follow-up issue.
- Keep the restic password and the tokens out of the host's backups and chat.

---

## 10. Evidence for closing #150

Run on the real host after step 17 and paste into the issue. **Redact** any
account id, bucket name, access key, secret, signed URL or password that
appears; the scripts print none of them, but check. **Never paste**
`backup.conf`, `r2.env`, `restic.password` or a dump.

1. Backend type: "restic over S3 to Cloudflare R2, <default or EU> jurisdiction" (no account
   details).
2. `restic version | head -1`.
3. `stat -c '%a %U %n' /etc/ytdigest-backup /etc/ytdigest-backup/*` (modes only).
4. The output of the manual backup (the `event=start` and `event=finish` lines).
5. `systemctl list-timers 'ytdigest-pg-backup*'` and
   `systemctl is-enabled ytdigest-pg-backup.timer`.
6. The journal of the timer-triggered run:
   `journalctl -u ytdigest-pg-backup.service --since '2 days ago' --no-pager | grep event=`.
7. `ops/host/pg-backup-status.sh` output (shows newest snapshot id and age).
8. `ops/host/pg-backup-status.sh --check-fresh; echo exit=$?`
9. The drill output, from `event=start` through `outcome=verified`, plus the empty
   result of `docker ps -a --filter name=ytdigest-drill`.
10. The failure-notification test from step 16 (state which channel received it).
11. Real database size (the `bytes=` of the first run) and the repository size
    from the status report.
12. Any command in this runbook that did not work as written (it will be fixed
    and rechecked before closure).

---

## 11. Not proven by the repository tests

- A real restic run against a real R2 bucket: `init`, `backup`, `check`,
  `forget --prune` and `restore` over the S3 backend with R2. The tests use a
  stateful stub restic and, where restic is installed, the real restic 0.18.1
  against a local directory. restic's documentation does not mention R2 and
  gives no minimum version for its S3 backend; third-party reports say it works
  including prune.
- The minimum restic version. 0.18.1 is the tested version, not a proven floor.
- The systemd units and timers on the real host (the unit files are checked
  statically only, and `systemd-analyze calendar` accepted the schedules).
- The Cloudflare dashboard labels, whether a card is required, and whether
  bucket lock needs a paid plan.
- A timer-triggered run, and the notification hook on your channel.
