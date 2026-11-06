"""Stubs and helpers for the Postgres backup script tests (#150).

The scripts under ``ops/host/`` run through their public CLI with a fake
``docker``, ``restic``, ``date``, ``df`` and ``systemctl`` first on ``PATH``.
Every stub reads its behaviour from ``$STUB_DIR/scenario.json`` and appends each
call to ``$STUB_DIR/calls.jsonl``, so a test sets the scenario, runs the script
and reads back exactly what was called. No network, no real credential, no
sleep: the clock is a number in a file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "ops" / "host"
BACKUP = HOST / "pg-backup.sh"
STATUS = HOST / "pg-backup-status.sh"
DRILL = HOST / "pg-restore-drill.sh"
NOTIFY = HOST / "pg-backup-notify.sh"

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
BUCKET = "ytdigest-test-bucket"
ACCESS_KEY = "AKIATESTACCESSKEY0001"
SECRET_KEY = "sEcReT/key+value0123456789ABCDEFGH"
RESTIC_PASSWORD = "correct-horse-battery-staple-xyz"
SECRETS = (ACCOUNT_ID, BUCKET, ACCESS_KEY, SECRET_KEY, RESTIC_PASSWORD)

NOW = 1_800_000_000  # a fixed "now" (2027-01-15 08:00:00 UTC)
HOUR = 3600

MANIFEST_LINES = [
    "alembic 0007_example",
    "table channels 3",
    "table videos 12",
    "table transcripts 0",
    "indexes 9",
    "constraints 14",
]

RESTIC_STUB = r'''#!{python}
import hashlib, json, os, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

d = Path(os.environ["STUB_DIR"])
sc = json.loads((d / "scenario.json").read_text())
state_file = d / "restic_state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {
    "snapshots": [], "exists": sc.get("repo_exists", True), "n": 0}
args = sys.argv[1:]
password = Path(os.environ["RESTIC_PASSWORD_FILE"]).read_text() if os.environ.get("RESTIC_PASSWORD_FILE") else ""
with (d / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps({"tool": "restic", "args": args, "env": {
        "has_key": bool(os.environ.get("AWS_ACCESS_KEY_ID")),
        "has_secret": bool(os.environ.get("AWS_SECRET_ACCESS_KEY")),
        "region": os.environ.get("AWS_DEFAULT_REGION"),
        "repo": os.environ.get("RESTIC_REPOSITORY"),
        "password_file_set": bool(os.environ.get("RESTIC_PASSWORD_FILE"))}}) + "\n")

def save():
    state_file.write_text(json.dumps(state))

def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone(timedelta(hours=1))).isoformat()

def die(sub):
    rc = sc.get("fail", {}).get(sub)
    if rc:
        if sc.get("leak"):
            sys.stderr.write(
                "Fatal: unable to open config file: Stat: Get "
                f"\"{os.environ.get('RESTIC_REPOSITORY')}/config?X-Amz-Credential={os.environ.get('AWS_ACCESS_KEY_ID')}\": "
                f"403 secret={os.environ.get('AWS_SECRET_ACCESS_KEY')} password={password} "
                f"host https://user:pw@{sc.get('leak_host', 'x.example')}/b\n")
        else:
            sys.stderr.write("Fatal: something failed\n")
        sys.exit(rc)

if not args:
    sys.exit(2)
sub = args[0]
if sub == "version":
    print(f"restic {sc.get('version', '0.18.1')} compiled with go1.25.0 on linux/amd64")
    sys.exit(0)
for s in sc.get("seed_snapshots", []):
    if not any(x["id"] == s["id"] for x in state["snapshots"]):
        state["snapshots"].append({"id": s["id"], "time": iso(s["epoch"]), "path": None})
die(sub)
if sub == "cat":
    sys.exit(0 if state["exists"] else 1)
if sub == "init":
    state["exists"] = True
    save()
    sys.exit(0)
if sub == "snapshots":
    print(json.dumps([{"time": s["time"], "id": s["id"], "short_id": s["id"][:8],
                       "hostname": "ytdigest-pg", "tags": ["ytdigest-pg"]} for s in state["snapshots"]], separators=(",", ":")))
    sys.exit(0)
if sub == "backup":
    src = Path(args[-1])
    state["n"] += 1
    sid = hashlib.sha256(f"snap{state['n']}".encode()).hexdigest()
    store = d / "repo" / sid
    shutil.copytree(src, store)
    total = sum(p.stat().st_size for p in src.iterdir())
    now = int((d / "now").read_text())
    state["snapshots"].append({"id": sid, "time": iso(now), "path": str(src)})
    save()
    print(json.dumps({"message_type": "status", "percent_done": 1}, separators=(",", ":")))
    print(json.dumps({"message_type": "summary", "files_new": 2, "data_added": 777,
                      "total_bytes_processed": total, "snapshot_id": sid}, separators=(",", ":")))
    sys.exit(0)
if sub == "check":
    sys.exit(0)
if sub == "forget":
    ids = [a for a in args[1:] if len(a) == 64 and all(c in "0123456789abcdef" for c in a)]
    state["snapshots"] = [s for s in state["snapshots"] if s["id"] not in ids]
    save()
    sys.exit(0)
if sub == "stats":
    print(json.dumps({"total_size": 12345, "total_file_count": 4}, separators=(",", ":")))
    sys.exit(0)
if sub == "restore":
    sid = args[1]
    target = Path(args[args.index("--target") + 1])
    snap = next((s for s in state["snapshots"] if s["id"].startswith(sid)), None)
    if snap is None or not (d / "repo" / snap["id"]).exists():
        sys.stderr.write("Fatal: no matching ID found\n")
        sys.exit(1)
    dest = target / "var" / "stage" / "out"
    dest.mkdir(parents=True)
    for f in (d / "repo" / snap["id"]).iterdir():
        shutil.copy(f, dest / f.name)
    sys.exit(0)
sys.exit(2)
'''

DOCKER_STUB = r'''#!{python}
import json, os, signal, sys, time
from pathlib import Path

d = Path(os.environ["STUB_DIR"])
sc = json.loads((d / "scenario.json").read_text())
args = sys.argv[1:]

def record(extra=None):
    with (d / "calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"tool": "docker", "args": args,
                             "compose_env": os.environ.get("COMPOSE_PROJECT_NAME"),
                             **(extra or {})}) + "\n")

if args[:1] == ["info"]:
    record()
    if sc.get("docker_info_rc"):
        sys.exit(sc["docker_info_rc"])
    print(sc.get("docker_root", "/var/lib/docker"))
    sys.exit(0)
if args[:1] == ["ps"]:
    record()
    print(sc.get("leftover_containers", ""))
    sys.exit(0)
if args[:2] == ["volume", "ls"]:
    record()
    print(sc.get("leftover_volumes", ""))
    sys.exit(0)

project = args[args.index("-p") + 1] if "-p" in args else None
rest = args[1:]
if "exec" not in rest:
    record({"project": project})
    verb = next((a for a in rest if a in ("up", "down")), None)
    if verb == "up" and sc.get("up_rc"):
        sys.stderr.write("up failed\n")
        sys.exit(sc["up_rc"])
    sys.exit(0)

tail = rest[rest.index("exec") + 1:]
tail = [a for a in tail if a != "-T"]
service, cmd = tail[0], tail[1:]
record({"project": project, "service": service})
prog = cmd[0]

def marker_and_block(name):
    (d / name).write_text("1")
    signal.pause()

if prog == "pg_isready":
    sys.exit(sc.get("ready_rc", 0))
if prog == "pg_dump":
    if sc.get("dump_block"):
        marker_and_block("dump_started")
    if sc.get("dump_rc"):
        sys.stderr.write("pg_dump: error\n")
        sys.exit(sc["dump_rc"])
    if not sc.get("dump_empty"):
        sys.stdout.buffer.write(b"PGDMP" + sc.get("dump_payload", "dump-bytes").encode())
    sys.exit(0)
if prog == "pg_restore":
    sys.stdin.buffer.read()
    key = "list_rc" if "--list" in cmd else "restore_rc"
    if sc.get(key):
        sys.stderr.write("pg_restore: error\n")
        sys.exit(sc[key])
    sys.exit(0)
if prog == "psql":
    if "-c" in cmd:
        print(sc.get("target_objects", 0))
        sys.exit(0)
    if sc.get("psql_rc"):
        sys.stderr.write("psql: could not connect\n")
        sys.exit(sc["psql_rc"])
    snapshot_session = False
    ro = False
    for line in sys.stdin:
        if "pg_export_snapshot" in line:
            snapshot_session = True
            print("snapshot " + sc.get("snapshot", "00000003-0000002A-1"))
        elif "pg_database_size" in line:
            print("dbsize " + str(sc.get("dbsize", 1048576)))
        elif line.startswith("SELECT 'alembic '"):
            lines = sc["manifest_lines"] if snapshot_session else sc["restored_lines"]
            for entry in lines:
                print(entry)
        elif "@@END@@" in line:
            print("@@END@@")
        elif "BEGIN READ ONLY" in line:
            ro = True
        sys.stdout.flush()
    if ro and sc.get("ro_rc"):
        sys.stderr.write("ERROR: invalid index present\n")
        sys.exit(sc["ro_rc"])
    sys.exit(0)
sys.exit(2)
'''

DATE_STUB = """#!/bin/bash
if [[ $# -eq 2 && $1 == -u && $2 == +* ]]; then
  exec /usr/bin/date -u -d "@$(cat "$STUB_DIR/now")" "$2"
fi
exec /usr/bin/date "$@"
"""

DF_STUB = """#!/bin/bash
printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\\n'
printf 'stub 1 1 %s 1%% /\\n' "$(cat "$STUB_DIR/df_avail")"
"""

SYSTEMCTL_STUB = """#!/bin/bash
printf '%s\\n' "$*" >>"$STUB_DIR/systemctl.log"
case "$1" in
  list-timers) echo "NEXT LEFT LAST PASSED UNIT ACTIVATES"; echo "Fri 03:00 6h Thu 02:31 21h ytdigest-pg-backup.timer ytdigest-pg-backup.service" ;;
  is-enabled) echo enabled ;;
  show) printf 'Result=success\\nExecMainStatus=0\\n' ;;
esac
exit 0
"""

DEFAULT_CONF = (
    f"R2_ACCOUNT_ID={ACCOUNT_ID}\n"
    f"R2_BUCKET={BUCKET}\n"
    "R2_JURISDICTION=eu\n"
)


@dataclass
class Rig:
    root: Path
    scenario: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stub = self.root / "stub"
        self.bin = self.stub / "bin"
        self.cfg = self.root / "cfg"
        self.staging = self.root / "staging"
        for p, mode in ((self.bin, 0o755), (self.cfg, 0o700), (self.staging, 0o700)):
            p.mkdir(parents=True)
            p.chmod(mode)
        py = sys.executable
        for name, body in (
            ("restic", RESTIC_STUB.replace("{python}", py)),
            ("docker", DOCKER_STUB.replace("{python}", py)),
            ("date", DATE_STUB),
            ("df", DF_STUB),
            ("systemctl", SYSTEMCTL_STUB),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.write_file("backup.conf", DEFAULT_CONF + f"STAGING_DIR={self.staging}\n")
        self.write_file("r2.env", f"AWS_ACCESS_KEY_ID={ACCESS_KEY}\nAWS_SECRET_ACCESS_KEY={SECRET_KEY}\n")
        self.write_file("restic.password", RESTIC_PASSWORD)
        (self.stub / "df_avail").write_text("999999999")
        self.set_now(NOW)
        self.set(
            manifest_lines=MANIFEST_LINES,
            restored_lines=MANIFEST_LINES,
            dbsize=1048576,
        )

    def write_file(self, name: str, text: str, mode: int = 0o600) -> Path:
        path = self.cfg / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def set(self, **values: Any) -> None:
        self.scenario.update(values)
        (self.stub / "scenario.json").write_text(json.dumps(self.scenario))

    def set_now(self, epoch: int) -> None:
        (self.stub / "now").write_text(str(epoch))

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_DIR": str(self.stub),
            "YTDIGEST_BACKUP_CONFIG_DIR": str(self.cfg),
            "LC_ALL": "C",
            **(extra or {}),
        }

    def run(
        self, script: Path, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.set()
        return subprocess.run(
            ["bash", str(script), *args],
            env=self.env(extra_env),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def popen(self, script: Path, *args: str) -> subprocess.Popen[str]:
        self.set()
        return subprocess.Popen(
            ["bash", str(script), *args],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        path = self.stub / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def restic_subcommands(self) -> list[str]:
        return [c["args"][0] for c in self.calls("restic") if c["args"][0] != "version"]

    def restic_calls(self, sub: str) -> list[list[str]]:
        return [c["args"] for c in self.calls("restic") if c["args"][0] == sub]

    def seed(self, *snapshots: tuple[str, int]) -> None:
        self.set(seed_snapshots=[{"id": i, "epoch": e} for i, e in snapshots])

    def wait_for(self, marker: str, deadline_sec: float = 15.0) -> None:
        path = self.stub / marker
        end = time.monotonic() + deadline_sec
        while not path.exists():
            if time.monotonic() > end:
                raise AssertionError(f"stub never reached {marker}")
            time.sleep(0.02)

    def leaked(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        text = result.stdout + result.stderr
        return [s for s in SECRETS if s in text]


def snap_id(n: int) -> str:
    return f"{n:064x}"


def tree_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

