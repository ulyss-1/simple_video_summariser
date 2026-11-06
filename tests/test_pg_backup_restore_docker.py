"""Backup then restore a small Postgres fixture with the real scripts (#150).

Marked `integration` (needs Docker Desktop). A throwaway Compose project
`t150-src-<id>` runs the *real* `db` service of `compose.yml` (PostgreSQL 18,
pinned image) with a few rows; `ops/host/pg-backup.sh` dumps it online and
`ops/host/pg-restore-drill.sh` restores the snapshot into a second disposable
project `ytdigest-drill-t150-<id>` and compares it with the manifest taken at
backup time. restic is stateful-fake (always) or the real binary against a
local directory (when installed): both go through `YTDIGEST_TEST_REPOSITORY`,
never an R2 bucket. This proves the script wiring, `pg_export_snapshot` plus
`pg_dump --snapshot`, `pg_restore --exit-on-error` and the cleanup, NOT the
R2 backend, which no test can reach without a real bucket.

Every project name is unique to this test; the developer's or the production
`ytdigest` project, its containers and volumes are never inspected or removed.
No fixed sleeps: readiness is `up --wait --wait-timeout`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.compose_stack import PASSWORD, REPO_ROOT, Stack, _leftovers
from tests.ops.backup_support import RESTIC_STUB

pytestmark = pytest.mark.integration

HOST = REPO_ROOT / "ops" / "host"
BACKUP = HOST / "pg-backup.sh"
STATUS = HOST / "pg-backup-status.sh"
DRILL = HOST / "pg-restore-drill.sh"
RESTIC_PASSWORD = "integration-restic-password-0123456789"

FIXTURE_SQL = """
CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('fixture_0001');
CREATE TABLE channels (id serial PRIMARY KEY, title text NOT NULL UNIQUE);
CREATE TABLE videos (
  id serial PRIMARY KEY,
  channel_id int NOT NULL REFERENCES channels(id),
  title text NOT NULL
);
CREATE INDEX videos_channel_idx ON videos (channel_id);
CREATE TABLE transcripts (id serial PRIMARY KEY, video_id int REFERENCES videos(id), body text);
INSERT INTO channels (title) VALUES ('one'), ('two');
INSERT INTO videos (channel_id, title) VALUES (1, 'a'), (1, 'b'), (2, 'c');
"""


def _run(cmd: list[str], env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False, cwd=REPO_ROOT
    )


class Fixture:
    def __init__(self, tmp_path: Path, variant: str) -> None:
        self.tmp_path = tmp_path
        self.id = uuid.uuid4().hex[:8]
        self.source = f"t150-src-{self.id}"
        self.drill = f"ytdigest-drill-t150-{self.id}"
        self.stack = Stack(self.source, tmp_path)
        self.cfg = tmp_path / "cfg"
        self.staging = tmp_path / "staging"
        self.repo = tmp_path / "repo"
        self.bin = tmp_path / "bin"
        self.variant = variant
        for p in (self.cfg, self.staging, self.bin):
            p.mkdir(mode=0o700)
        docker = shutil.which("docker")
        assert docker is not None, "docker CLI is required"
        path_dirs = [str(Path(docker).parent), "/usr/bin", "/bin"]
        if variant == "fake-restic":
            stub = tmp_path / "stub"
            stub.mkdir()
            (stub / "scenario.json").write_text("{}")
            (stub / "now").write_text("0")
            restic = self.bin / "restic"
            restic.write_text(RESTIC_STUB.replace("{python}", sys.executable))
            restic.chmod(0o755)
            self.stub: Path | None = stub
            path_dirs.insert(0, str(self.bin))
        else:
            real = shutil.which("restic")
            assert real is not None
            path_dirs.insert(0, str(Path(real).parent))
            self.stub = None
        self.path = ":".join(path_dirs)
        (self.cfg / "backup.conf").write_text(
            f"R2_ACCOUNT_ID={'0' * 32}\nR2_BUCKET=local-test-bucket\nSTAGING_DIR={self.staging}\n"
        )
        (self.cfg / "r2.env").write_text("AWS_ACCESS_KEY_ID=unused\nAWS_SECRET_ACCESS_KEY=unused\n")
        (self.cfg / "restic.password").write_text(RESTIC_PASSWORD)
        for f in self.cfg.iterdir():
            f.chmod(0o600)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": self.path,
            "HOME": str(Path.home()),
            "YTDIGEST_BACKUP_CONFIG_DIR": str(self.cfg),
            "YTDIGEST_TEST_REPOSITORY": str(self.repo),
            "YTDIGEST_BACKUP_PROJECT": self.source,
            "YTDIGEST_BACKUP_COMPOSE_FILE": "compose.yml",
            "POSTGRES_PASSWORD": PASSWORD,  # for Compose interpolation only
            **extra,
        }
        if self.stub is not None:
            env["STUB_DIR"] = str(self.stub)
        return env

    def psql(self, sql: str) -> subprocess.CompletedProcess[str]:
        proc = self.stack.compose(
            "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-U", "ytdigest", "-d", "ytdigest", "-tA",
            "-c", sql, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        return proc


def _wait_for_tcp_ready(fx: Fixture, deadline_sec: float = 60.0) -> None:
    """compose.yml's healthcheck uses the socket, which the entrypoint's temporary
    init server also answers ("the database system is starting up" follows). The
    real server is the one listening on TCP, so wait for that, with a deadline."""
    end = time.monotonic() + deadline_sec
    while True:
        probe = fx.stack.compose(
            "exec", "-T", "db", "pg_isready", "-h", "127.0.0.1", "-U", "ytdigest", "-d", "ytdigest", timeout=60
        )
        if probe.returncode == 0:
            return
        assert time.monotonic() < end, f"db never listened on TCP within {deadline_sec:.0f} s: {probe.stdout}"
        time.sleep(0.5)


@pytest.fixture(params=["fake-restic", "real-restic"])
def fx(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Fixture]:
    if request.param == "real-restic" and shutil.which("restic") is None:
        pytest.skip("restic is a host dependency, not installed here; the fake covers the wiring")
    fixture = Fixture(tmp_path, request.param)
    try:
        up = fixture.stack.compose("up", "-d", "--wait", "--wait-timeout", "120", "db")
        assert up.returncode == 0, up.stderr
        _wait_for_tcp_ready(fixture)
        fixture.psql(FIXTURE_SQL)
        yield fixture
    finally:
        fixture.stack.compose("down", "-v", "--remove-orphans")
        subprocess.run(
            ["docker", "compose", "-p", fixture.drill, "-f", str(HOST / "pg-restore-drill.compose.yml"),
             "--env-file", "/dev/null", "down", "-v", "--remove-orphans"],
            capture_output=True, timeout=120, check=False,
        )


def _initialise(fx: Fixture) -> None:
    if fx.variant == "real-restic":
        result = _run(["bash", str(BACKUP), "--init"], fx.env())
        assert result.returncode == 0, result.stderr
    else:
        assert fx.stub is not None
        (fx.stub / "scenario.json").write_text(json.dumps({"repo_exists": True}))


def _backup(fx: Fixture) -> str:
    _initialise(fx)
    result = _run(["bash", str(BACKUP)], fx.env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "event=finish outcome=ok" in result.stdout
    assert not (fx.staging / "work").exists()  # no plaintext dump left behind
    latest = _run(["bash", str(STATUS), "--latest-id"], fx.env())
    assert latest.returncode == 0, latest.stderr
    snapshot = latest.stdout.strip()
    assert len(snapshot) == 64
    return snapshot


def test_backup_then_restore_drill_proves_the_recovered_database(fx: Fixture) -> None:
    snapshot = _backup(fx)
    if fx.variant == "real-restic":
        fresh = _run(["bash", str(STATUS), "--check-fresh"], fx.env())
        assert fresh.returncode == 0, fresh.stderr

    drill = _run(["bash", str(DRILL), "--snapshot", snapshot, "--project", fx.drill], fx.env(), timeout=900)
    assert drill.returncode == 0, drill.stdout + drill.stderr
    assert "outcome=verified" in drill.stdout
    assert "tables=4" in drill.stdout  # alembic_version + channels + videos + transcripts, one empty
    assert _leftovers(fx.drill) == ("", "")  # disposable project and volume are gone
    assert not list(fx.staging.glob("drill.*"))

    # the source was only read: still running, rows untouched
    assert fx.psql("SELECT count(*) FROM videos").stdout.strip() == "3"


def test_a_tampered_manifest_fails_verification_and_still_cleans_up(fx: Fixture) -> None:
    if fx.variant == "real-restic":
        pytest.skip("tampering needs write access to the fake repository files")
    snapshot = _backup(fx)
    assert fx.stub is not None
    manifest = fx.stub / "repo" / snapshot / "manifest.txt"
    manifest.write_text(manifest.read_text().replace("table videos 3", "table videos 4"))

    drill = _run(["bash", str(DRILL), "--snapshot", snapshot, "--project", fx.drill], fx.env(), timeout=900)
    assert drill.returncode != 0
    assert "stage=verify" in drill.stderr
    assert "-table videos 4" in drill.stderr
    assert _leftovers(fx.drill) == ("", "")


def test_the_production_project_is_refused_before_docker_is_asked_anything(fx: Fixture) -> None:
    snapshot = _backup(fx)
    drill = _run(["bash", str(DRILL), "--snapshot", snapshot, "--project", "ytdigest"], fx.env())
    assert drill.returncode != 0
    assert "stage=target" in drill.stderr
    assert "production project" in drill.stderr
