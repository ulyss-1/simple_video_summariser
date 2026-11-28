"""``ops/host/deploy.sh``, the production release deploy (#380).

The script runs through its public CLI against a real throwaway git history (a
bare ``origin`` plus a clone that plays ``/opt/ytdigest``) with fake ``docker``,
``curl`` and ``systemctl`` first on ``PATH``. Each stub reads its behaviour from
``$STUB_DIR/scenario.json`` and appends every call to ``$STUB_DIR/calls.jsonl``,
so a test sets a scenario, runs the script and reads back what was called and
in which order. No network, no real Docker, no sleep (the health deadline is 0).
"""

from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "host" / "deploy.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

DOCKER_STUB = r'''#!{python}
import json, os, sys
from pathlib import Path
d = Path(os.environ["STUB_DIR"])
sc = json.loads((d / "scenario.json").read_text())
args = sys.argv[1:]
with (d / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps({"tool": "docker", "args": args,
                         "image_tag": os.environ.get("IMAGE_TAG"), "cwd": os.getcwd()}) + "\n")
fail = sc.get("fail", {})
if args[:1] == ["ps"]:
    print("\n".join(sc.get("containers", [])))
    sys.exit(0)
if args[:1] == ["inspect"]:
    print("\n".join(sc.get("inspect", [])))
    sys.exit(0)
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if sc.get("volume", True) else 1)
if args[:1] == ["compose"]:
    rest = args[1:]
    while rest and rest[0] in ("-p", "-f"):
        rest = rest[2:]
    sub = rest[0]
    if sub == "ps":
        if sc.get("db_running", True):
            print("dbcontainerid")
        sys.exit(0)
    if sub == "exec":
        rev = sc.get("db_rev", "")
        if "to_regclass" in rest[-1]:
            print("t" if rev else "f")
        else:
            print(rev)
        sys.exit(0)
    if sub == "config":
        print("\n".join(["db", "deps", "migrate", "api", "web", "planner", "analyzer", "transcriber"]))
        sys.exit(0)
    sys.exit(fail.get(sub, 0))
sys.exit(0)
'''

CURL_STUB = r'''#!{python}
import json, os, sys
from pathlib import Path
d = Path(os.environ["STUB_DIR"])
sc = json.loads((d / "scenario.json").read_text())
with (d / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps({"tool": "curl", "args": sys.argv[1:]}) + "\n")
sys.exit(0 if sc.get("healthy", True) else 22)
'''

SYSTEMCTL_STUB = r'''#!{python}
import json, os, sys
from pathlib import Path
d = Path(os.environ["STUB_DIR"])
sc = json.loads((d / "scenario.json").read_text())
with (d / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps({"tool": "systemctl", "args": sys.argv[1:]}) + "\n")
sys.exit(0 if sc.get("host_cloudflared") else 3)
'''

BACKUP_STUB = """#!/bin/sh
printf '{"tool": "pg-backup"}\\n' >> "$STUB_DIR/calls.jsonl"
if grep -q '"backup_fails": true' "$STUB_DIR/scenario.json"; then echo 'pg-backup: FAILED stage=dump' >&2; exit 1; fi
echo 'ts=2027-01-15T08:00:00Z prog=pg-backup event=finish outcome=ok snapshot=0123abcd4567ef bytes=1 added=1'
"""


def _migration(rev: str, down: str | None) -> str:
    down_s = f'"{down}"' if down else "None"
    return (
        f'"""Revision {rev}.\n\nrevision to land on is described here\n"""\n'
        f'revision: str = "{rev}"\ndown_revision: str | None = {down_s}\n'
    )


def _git(cwd: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Rig:
    tmp: Path
    origin: Path
    work: Path
    prod: Path
    stub: Path
    state: Path

    def scenario(self, **kw: Any) -> None:
        (self.stub / "scenario.json").write_text(json.dumps(kw))

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full = {
            **GIT_ENV,
            "HOME": str(self.tmp),
            "PATH": f"{self.stub / 'bin'}:/usr/bin:/bin",
            "STUB_DIR": str(self.stub),
            "YTDIGEST_DEPLOY_STATE_DIR": str(self.state),
            "YTDIGEST_DEPLOY_HEALTH_TIMEOUT_SEC": "0",
            **env,
        }
        return subprocess.run(
            [str(self.prod / "ops" / "host" / "deploy.sh"), *args],
            check=False, cwd=self.tmp, env=full, capture_output=True, text=True, timeout=60,
        )

    def calls(self) -> list[dict[str, Any]]:
        path = self.stub / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()]

    def compose_subs(self) -> list[str]:
        """The Compose subcommand of each compose call, plus pg-backup and curl."""
        out = []
        for c in self.calls():
            if c["tool"] == "docker" and c["args"][:1] == ["compose"]:
                rest = c["args"][1:]
                while rest and rest[0] in ("-p", "-f"):
                    rest = rest[2:]
                out.append("run " + rest[-1] if rest[0] == "run" else rest[0])
            elif c["tool"] in ("pg-backup", "curl"):
                out.append(c["tool"])
        return out

    def state_json(self) -> dict[str, Any]:
        return json.loads((self.state / "state.json").read_text())

    def head(self) -> str:
        return _git(self.prod, "rev-parse", "HEAD")

    def write_env(self, text: str) -> None:
        (self.prod / ".env").write_text(text)


def _commit(work: Path, files: dict[str, str], msg: str) -> None:
    for rel, text in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """History: v1.0.0 (head 0002) and v1.1.0 (head 0003) on release; v1.2.0 only on master."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "master")
    _commit(work, {
        ".gitignore": ".env\n",
        "compose.yml": "name: ytdigest\nservices: {}\n",
        "ops/host/deploy.sh": SCRIPT.read_text(),
        "ops/host/pg-backup.sh": BACKUP_STUB,
        "migrations/versions/0001_init.py": _migration("0001", None),
        "migrations/versions/0002_more.py": _migration("0002", "0001"),
    }, "one")
    _git(work, "tag", "v1.0.0")
    _commit(work, {"migrations/versions/0003_next.py": _migration("0003", "0002")}, "two")
    _git(work, "tag", "-a", "-m", "release 1.1.0", "v1.1.0")
    _git(work, "branch", "release")
    _commit(work, {"migrations/versions/0004_dev.py": _migration("0004", "0003")}, "three")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(origin), "master", "release", "--tags")
    prod = tmp_path / "prod"
    _git(tmp_path, "clone", "-q", "--no-tags", str(origin), str(prod))
    (prod / ".env").write_text("COMPOSE_FILE=compose.yml\nPOSTGRES_PASSWORD=x\n")
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB), ("systemctl", SYSTEMCTL_STUB)):
        p = stub / "bin" / name
        p.write_text(body.replace("{python}", sys.executable))
        p.chmod(0o755)
    for tool in ("flock",):
        assert shutil.which(tool), tool
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    r = Rig(tmp_path, origin, work, prod, stub, state)
    r.scenario(db_rev="0002")
    return r


def _one_line_refusal(res: subprocess.CompletedProcess[str], needle: str) -> None:
    assert res.returncode != 0
    lines = res.stderr.strip().splitlines()
    assert len(lines) == 1, res.stderr
    assert lines[0].startswith("ytdigest-deploy: refused: ")
    assert needle in lines[0]


# ---- tag eligibility -----------------------------------------------------------


@pytest.mark.parametrize(
    "tag", ["1.0.0", "v1.0", "v1.0.0-rc1", "v1.0.0.1", "vx.0.0", "v1.0.0;id", "release", "V1.0.0"]
)
def test_a_tag_not_matching_the_release_pattern_is_refused_before_anything_runs(
    rig: Rig, tag: str
) -> None:
    res = rig.run(tag)
    _one_line_refusal(res, "does not match")
    assert rig.calls() == []
    assert not (rig.state / "state.json").exists()


def test_a_tag_that_is_not_an_ancestor_of_origin_release_is_refused(rig: Rig) -> None:
    before = rig.head()
    res = rig.run("v1.2.0")
    _one_line_refusal(res, "not an ancestor of origin/release")
    assert "build" not in rig.compose_subs()
    assert rig.head() == before
    assert not (rig.state / "state.json").exists()


def test_a_tag_missing_on_origin_is_refused(rig: Rig) -> None:
    _one_line_refusal(rig.run("v9.9.9"), "cannot fetch tag v9.9.9")


def test_a_tag_moved_on_origin_is_refused_never_forced(rig: Rig) -> None:
    assert rig.run("v1.0.0").returncode == 0
    _git(rig.work, "tag", "-f", "v1.1.0", "release~1")
    _git(rig.work, "push", "-q", "-f", str(rig.origin), "refs/tags/v1.1.0")
    _git(rig.prod, "fetch", "-q", "origin", "refs/tags/v1.1.0:refs/tags/v1.1.0", "--force")
    _git(rig.work, "tag", "-f", "-a", "-m", "moved", "v1.1.0", "release")
    _git(rig.work, "push", "-q", "-f", str(rig.origin), "refs/tags/v1.1.0")
    _one_line_refusal(rig.run("v1.1.0"), "a moved tag is never forced")


def test_a_dirty_checkout_is_refused(rig: Rig) -> None:
    (rig.prod / "stray.txt").write_text("x")
    _one_line_refusal(rig.run("v1.0.0"), "uncommitted or untracked")
    assert "build" not in rig.compose_subs()


# ---- prod and dev cannot be mixed up -----------------------------------------------


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ("COMPOSE_FILE=compose.yml:compose.override.yml\n", "merges compose.override.yml"),
        ("POSTGRES_PASSWORD=x\n", "sets no COMPOSE_FILE"),
        ("COMPOSE_FILE=\n", "sets no COMPOSE_FILE"),
        ("COMPOSE_FILE=compose.override.yml:compose.yml\n", "must start with compose.yml"),
        ("COMPOSE_FILE=compose.yml\nCOMPOSE_PROJECT_NAME=ytdigest-dev\n", "COMPOSE_PROJECT_NAME=ytdigest-dev"),
    ],
)
def test_an_env_that_would_merge_the_dev_overlay_is_refused(rig: Rig, env: str, needle: str) -> None:
    rig.write_env(env)
    _one_line_refusal(rig.run("v1.0.0"), needle)
    assert "build" not in rig.compose_subs()


def test_compose_file_from_the_shell_naming_the_override_is_refused(rig: Rig) -> None:
    res = rig.run("v1.0.0", COMPOSE_FILE="compose.yml:compose.override.yml")
    _one_line_refusal(res, "names compose.override.yml")


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("/ytdigest-api-1|/srv/x/compose.yml,/srv/x/compose.override.yml||PATH=/bin,", "compose.override.yml"),
        ("/ytdigest-api-1|/opt/ytdigest/compose.yml|/app/common,/app/services,|PATH=/bin,", "bind mount"),
        ("/ytdigest-transcriber-1|/opt/ytdigest/compose.yml||PATH=/bin,WHISPER_MODEL=tiny,", "WHISPER_MODEL=tiny"),
    ],
)
def test_existing_ytdigest_containers_made_with_the_dev_overlay_abort_the_deploy(
    rig: Rig, line: str, needle: str
) -> None:
    rig.scenario(db_rev="0002", containers=["c1"], inspect=[line])
    _one_line_refusal(rig.run("v1.0.0"), needle)
    assert "build" not in rig.compose_subs()


def test_prod_containers_with_secret_binds_and_large_v3_pass_the_dev_overlay_check(rig: Rig) -> None:
    rig.scenario(
        db_rev="0002",
        containers=["c1", "c2"],
        inspect=[
            "/ytdigest-cloudflared-1|/opt/ytdigest/compose.yml,/opt/ytdigest/compose.tunnel.yml|/run/secrets/cloudflared_credentials,|PATH=/bin,",
            "/ytdigest-transcriber-1|/opt/ytdigest/compose.yml||PATH=/bin,WHISPER_MODEL=large-v3,",
        ],
    )
    res = rig.run("v1.0.0")
    assert res.returncode == 0, res.stderr


# ---- the happy path, the order and the Compose invocation -----------------------------


def test_a_deploy_runs_the_stages_in_order_with_explicit_project_files_and_the_tag(rig: Rig) -> None:
    rig.scenario(db_rev="0002")
    res = rig.run("v1.1.0")
    assert res.returncode == 0, res.stderr
    assert rig.compose_subs() == ["ps", "exec", "exec", "build", "pg-backup", "run migrate", "up", "curl"]
    for c in rig.calls():
        if c["tool"] == "docker" and c["args"][:1] == ["compose"]:
            assert c["args"][1:5] == ["-p", "ytdigest", "-f", "compose.yml"]
            assert "compose.override.yml" not in c["args"]
            assert "down" not in c["args"]
            assert c["image_tag"] == "v1.1.0"
            assert c["cwd"] == str(rig.prod)
    up = next(c["args"] for c in rig.calls() if "up" in c.get("args", []))
    assert up[up.index("up"):][:4] == ["up", "-d", "--wait", "--wait-timeout"]
    assert "--no-deps" not in up
    curl = next(c["args"] for c in rig.calls() if c["tool"] == "curl")
    assert curl[-1] == "http://127.0.0.1:8080/api/healthz"
    assert "--max-time" in curl
    assert rig.head() == _git(rig.prod, "rev-parse", "v1.1.0^{commit}")
    assert "snapshot=0123abcd4567ef" in res.stdout


def test_the_state_file_records_the_last_attempt_and_last_good_with_mode_600(rig: Rig) -> None:
    assert rig.run("v1.1.0").returncode == 0
    st = rig.state_json()
    sha = _git(rig.prod, "rev-parse", "v1.1.0^{commit}")
    for key in ("last_attempt", "last_good"):
        rec = st[key]
        assert rec["tag"] == "v1.1.0"
        assert rec["sha"] == sha
        assert rec["alembic_head"] == "0003"
        assert rec["result"] == "ok"
        assert rec["deployed_at"].endswith("Z") and rec["deployed_at"][:2] == "20"
    assert (rig.state / "state.json").stat().st_mode & 0o777 == 0o600
    assert "event=finish tag=v1.1.0" in (rig.state / "deploy.log").read_text()


def test_no_backup_when_the_tag_brings_no_new_alembic_revision(rig: Rig) -> None:
    rig.scenario(db_rev="0003")
    assert rig.run("v1.1.0").returncode == 0
    assert "pg-backup" not in rig.compose_subs()
    assert "run migrate" in rig.compose_subs()


def test_a_fresh_host_without_a_database_needs_no_backup(rig: Rig) -> None:
    rig.scenario(db_running=False, volume=False)
    assert rig.run("v1.0.0").returncode == 0
    assert rig.compose_subs() == ["ps", "build", "run migrate", "up", "curl"]


def test_a_stopped_db_with_an_existing_volume_is_refused(rig: Rig) -> None:
    rig.scenario(db_running=False, volume=True)
    _one_line_refusal(rig.run("v1.0.0"), "db is not running")


def test_listed_overlays_are_passed_explicitly(rig: Rig) -> None:
    rig.write_env(
        "COMPOSE_FILE=compose.yml:compose.anthropic.yml:compose.tunnel.yml\n"
        "ANTHROPIC_API_KEY_FILE=/home/d/.config/ytdigest/k\nCLOUDFLARED_TUNNEL_ID=abc\n"
    )
    assert rig.run("v1.0.0").returncode == 0
    for c in rig.calls():
        if c["tool"] == "docker" and c["args"][:1] == ["compose"]:
            assert c["args"][1:9] == [
                "-p", "ytdigest", "-f", "compose.yml", "-f", "compose.anthropic.yml",
                "-f", "compose.tunnel.yml",
            ]


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ("COMPOSE_FILE=compose.yml\nCLOUDFLARED_TUNNEL_ID=abc\n", "does not list compose.tunnel.yml"),
        ("COMPOSE_FILE=compose.yml\nANTHROPIC_API_KEY_FILE=/k\n", "does not list compose.anthropic.yml"),
        ("COMPOSE_FILE=compose.yml:compose.bakeoff.yml\n", "unsupported file"),
    ],
)
def test_a_configured_but_unlisted_overlay_is_refused(rig: Rig, env: str, needle: str) -> None:
    rig.write_env(env)
    _one_line_refusal(rig.run("v1.0.0"), needle)


def test_the_tunnel_overlay_is_refused_while_host_cloudflared_service_is_active(rig: Rig) -> None:
    rig.scenario(db_rev="0002", host_cloudflared=True)
    rig.write_env("COMPOSE_FILE=compose.yml:compose.tunnel.yml\nCLOUDFLARED_TUNNEL_ID=abc\n")
    _one_line_refusal(rig.run("v1.0.0"), "cloudflared.service is active")


# ---- idempotence --------------------------------------------------------------------


def test_re_running_the_deployed_tag_is_a_no_op_that_writes_one_log_line(rig: Rig) -> None:
    assert rig.run("v1.1.0").returncode == 0
    state_before = (rig.state / "state.json").read_bytes()
    log_before = (rig.state / "deploy.log").read_text().splitlines()
    (rig.stub / "calls.jsonl").unlink()
    res = rig.run("v1.1.0")
    assert res.returncode == 0, res.stderr
    assert rig.calls() == []
    assert (rig.state / "state.json").read_bytes() == state_before
    log_after = (rig.state / "deploy.log").read_text().splitlines()
    assert log_after[: len(log_before)] == log_before
    assert len(log_after) == len(log_before) + 1
    assert "event=noop tag=v1.1.0" in log_after[-1]


# ---- failures ---------------------------------------------------------------------


@pytest.mark.parametrize("failure", [{"fail": {"build": 1}}, {"backup_fails": True}])
def test_a_failure_before_migrate_leaves_the_old_state_and_exits_non_zero(
    rig: Rig, failure: dict[str, Any]
) -> None:
    assert rig.run("v1.0.0").returncode == 0
    good_before = rig.state_json()["last_good"]
    head_before = rig.head()
    (rig.stub / "calls.jsonl").unlink()
    rig.scenario(db_rev="0002", **failure)
    res = rig.run("v1.1.0")
    assert res.returncode != 0
    stage = "build" if "fail" in failure else "backup"
    assert f"FAILED stage={stage}" in res.stderr
    assert res.stderr.count("ytdigest-deploy: FAILED") == 1, res.stderr
    assert "untouched" in res.stderr
    subs = rig.compose_subs()
    assert "run migrate" not in subs and "up" not in subs
    assert rig.head() == head_before
    st = rig.state_json()
    assert st["last_good"] == good_before
    assert st["last_attempt"]["tag"] == "v1.1.0"
    assert st["last_attempt"]["result"] == "failed"
    assert st["last_attempt"]["stage"] == stage


@pytest.mark.parametrize(
    ("scenario", "stage"),
    [({"fail": {"run": 1}}, "migrate"), ({"fail": {"up": 1}}, "up"), ({"healthy": False}, "health")],
)
def test_a_failure_from_migrate_on_names_the_stage_the_previous_tag_and_the_rollback(
    rig: Rig, scenario: dict[str, Any], stage: str
) -> None:
    assert rig.run("v1.0.0").returncode == 0
    rig.scenario(db_rev="0002", **scenario)
    res = rig.run("v1.1.0")
    assert res.returncode != 0
    assert f"FAILED stage={stage}" in res.stderr
    assert res.stderr.count("ytdigest-deploy: FAILED") == 1, res.stderr
    assert "previous tag: v1.0.0" in res.stderr
    assert "rollback: ops/host/deploy.sh v1.0.0 --allow-schema-behind" in res.stderr
    assert "pre-deploy backup snapshot: 0123abcd4567ef" in res.stderr
    st = rig.state_json()
    assert st["last_good"]["tag"] == "v1.0.0"
    assert st["last_attempt"]["result"] == "failed"
    assert st["last_attempt"]["stage"] == stage


def test_a_failed_health_check_without_a_schema_change_suggests_a_plain_rollback(rig: Rig) -> None:
    rig.scenario(db_rev="0003")
    assert rig.run("v1.0.0", "--allow-schema-behind").returncode == 0
    rig.scenario(db_rev="0003", healthy=False)
    res = rig.run("v1.1.0")
    assert "rollback: ops/host/deploy.sh v1.0.0\n" in res.stderr + "\n"
    assert "--allow-schema-behind" not in res.stderr


# ---- rollback across a migration -------------------------------------------------------


def test_rollback_to_a_tag_behind_the_database_schema_is_refused_before_build(rig: Rig) -> None:
    rig.scenario(db_rev="0003")
    res = rig.run("v1.0.0")
    _one_line_refusal(res, "--allow-schema-behind")
    assert "build" not in rig.compose_subs()
    assert not (rig.state / "state.json").exists()


def test_allow_schema_behind_skips_migrate_and_starts_services_without_it(rig: Rig) -> None:
    rig.scenario(db_rev="0003")
    res = rig.run("v1.0.0", "--allow-schema-behind")
    assert res.returncode == 0, res.stderr
    subs = rig.compose_subs()
    assert "run migrate" not in subs and "pg-backup" not in subs
    up = next(c["args"] for c in rig.calls() if "up" in c.get("args", []))
    assert "--no-deps" in up
    services = up[up.index("--no-deps") + 1:]
    assert "migrate" not in services and "deps" not in services
    assert {"db", "api", "web", "planner", "analyzer", "transcriber"} == set(services)
    assert rig.state_json()["last_good"]["alembic_head"] == "0002"


# ---- lock and timer mode ----------------------------------------------------------------


def test_a_timer_run_during_a_running_deploy_does_nothing(rig: Rig) -> None:
    with (rig.state / "deploy.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        timer = rig.run("--latest")
        manual = rig.run("v1.1.0")
    assert timer.returncode == 0, timer.stderr
    assert "reason=locked" in timer.stdout
    assert manual.returncode != 0
    assert "another deploy is running" in manual.stderr
    assert rig.calls() == []
    assert not (rig.state / "state.json").exists()


def test_latest_deploys_the_newest_tag_on_release_and_then_is_a_no_op(rig: Rig) -> None:
    res = rig.run("--latest")
    assert res.returncode == 0, res.stderr
    assert rig.state_json()["last_good"]["tag"] == "v1.1.0"   # v1.2.0 is not on release
    (rig.stub / "calls.jsonl").unlink()
    again = rig.run("--latest")
    assert again.returncode == 0
    assert "event=noop tag=v1.1.0" in again.stdout
    assert rig.compose_subs() == []


def test_latest_does_not_retry_a_tag_whose_last_attempt_failed(rig: Rig) -> None:
    rig.scenario(db_rev="0002", fail={"build": 1})
    assert rig.run("v1.1.0").returncode != 0
    (rig.stub / "calls.jsonl").unlink()
    res = rig.run("--latest")
    assert res.returncode == 0
    assert "reason=last-attempt-failed" in res.stdout
    assert rig.compose_subs() == []


# ---- state directory, status and manual compose ------------------------------------------


def test_a_state_directory_looser_than_700_is_refused(rig: Rig) -> None:
    rig.state.chmod(0o755)
    _one_line_refusal(rig.run("v1.0.0"), "looser than 700")


def test_compose_passthrough_uses_the_deployed_tag_and_refuses_volume_removal(rig: Rig) -> None:
    _one_line_refusal(rig.run("--compose", "ps"), "no successful deploy")
    assert rig.run("v1.1.0").returncode == 0
    (rig.stub / "calls.jsonl").unlink()
    assert rig.run("--compose", "ps").returncode == 0
    (call,) = rig.calls()
    assert call["args"] == ["compose", "-p", "ytdigest", "-f", "compose.yml", "ps"]
    assert call["image_tag"] == "v1.1.0"
    _one_line_refusal(rig.run("--compose", "down", "-v"), "never remove production volumes")


def test_status_prints_the_state_file(rig: Rig) -> None:
    assert "no deploy recorded" in rig.run("--status").stdout
    assert rig.run("v1.0.0").returncode == 0
    assert json.loads(rig.run("--status").stdout)["last_good"]["tag"] == "v1.0.0"


# ---- static -------------------------------------------------------------------------------


def test_the_script_never_removes_volumes_or_runs_down() -> None:
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    text = "\n".join(code)
    assert not re.search(r"\b(dc|docker compose)\b[^\n]*\bdown\b", text)
    assert "--volumes" not in text.replace("--volumes |", "").replace("--volumes=*", "")
    # The body is one function so a checkout that rewrites this file mid-run is safe.
    assert SCRIPT.read_text().rstrip().endswith('main "$@"; exit $?')
