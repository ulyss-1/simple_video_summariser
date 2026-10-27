"""Brings the whole compose stack up and checks it running (issue #58).

Marked `integration` (needs Docker Desktop) and `image` (the first `up
--build` builds three images from PyPI, Debian, the npm registry and Hugging
Face-free layers, taking minutes): deselected unless run with
`pytest -m image`. Static checks of compose.yml live in `test_compose.py`; the
resolved-config checks that need no build are in `test_compose_docker.py`.

Everything runs under a unique `-p` project name, so the developer's
`ytdigest` project and its volumes are never touched, and `down -v` runs in a
`finally`, even on failure. There is no fixed sleep: waits go through
`--wait --wait-timeout`. The only shared resources are the three `ytdigest-*`
image tags; its one published port (web) is picked by Docker, so a running
stack that holds 8080 does not get in the way.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from collections.abc import Iterator

import pytest

from tests.compose_stack import WAIT_TIMEOUT, Stack

pytestmark = [pytest.mark.integration, pytest.mark.image]

WORKERS = ("planner", "analyzer", "transcriber")
LONG_RUNNING = ("db", "api", "planner", "analyzer", "transcriber", "web")
URLOPEN = (
    "import urllib.request; "
    "urllib.request.urlopen('https://www.youtube.com', timeout=10)"
)


def _inspect(stack: Stack, service: str, template: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", template, stack.container_id(service)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    handle = Stack(f"ytdigest-t58-{uuid.uuid4().hex[:8]}", tmp_path_factory.mktemp("s"))
    try:
        result = handle.up()
        assert result.returncode == 0, result.stderr
        yield handle
    finally:
        handle.compose("down", "-v", "--remove-orphans")


def test_fresh_stack_is_healthy_and_migrate_exited_zero(stack: Stack) -> None:
    rows = stack.containers()
    assert set(rows) == {
        "db",
        "migrate",
        "api",
        "planner",
        "analyzer",
        "transcriber",
        "web",
    }
    assert rows["migrate"]["State"] == "exited"
    assert rows["migrate"]["ExitCode"] == 0
    for name in WORKERS + ("db", "api"):
        assert rows[name]["State"] == "running", name
        assert rows[name]["Health"] == "healthy", name
    assert rows["web"]["State"] == "running"


def test_a_second_up_wait_leaves_the_same_state(stack: Stack) -> None:
    result = stack.compose("up", "-d", "--wait", "--wait-timeout", WAIT_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert stack.containers()["migrate"]["ExitCode"] == 0


def test_scale_transcriber_and_analyzer_has_no_port_or_name_clash(stack: Stack) -> None:
    result = stack.compose(
        "up", "-d", "--no-build", "--wait", "--wait-timeout", WAIT_TIMEOUT,
        "--scale", "transcriber=2", "--scale", "analyzer=2",
    )  # fmt: skip
    try:
        assert result.returncode == 0, result.stderr
    finally:
        stack.compose(
            "up", "-d", "--no-build", "--wait", "--wait-timeout", WAIT_TIMEOUT,
            "--scale", "transcriber=1", "--scale", "analyzer=1",
        )  # fmt: skip


def test_web_serves_healthz_through_nginx_on_loopback(stack: Stack) -> None:
    published = stack.compose("port", "web", "8080", timeout=60).stdout.strip()
    host_port = published.rsplit(":", 1)[-1]
    assert host_port.isdigit(), published
    result = subprocess.run(
        ["curl", "-fsS", f"http://127.0.0.1:{host_port}/api/healthz"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _health_command(stack: Stack, service: str) -> str:
    config = json.loads(stack.compose("config", "--format", "json").stdout)
    test = config["services"][service]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    # `config --format json` keeps Compose's `$$` escape; Compose turns it
    # back into `$` when it creates the container, so do the same here.
    command: str = test[1].replace("$$", "$")
    return command


@pytest.mark.parametrize("service", ["planner", "transcriber"])  # backend, whisper
def test_worker_healthcheck_command_cases(stack: Stack, service: str) -> None:
    check = _health_command(stack, service)
    cases = (
        ("missing", "rm -f /tmp/heartbeat; "),
        ("fresh", "touch /tmp/heartbeat; "),
        ("stale", "touch -d @$(( $(date +%s) - 200 )) /tmp/heartbeat; "),
    )
    # One container runs the three cases in turn; each prints its own exit code.
    script = "\n".join(
        f'sh -c {shlex.quote(prelude + check)}; echo "{name}=$?"'
        for name, prelude in cases
    )
    result = stack.compose(
        "run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", service,
        "-c", script,
        timeout=300,
    )  # fmt: skip
    codes = dict(re.findall(r"^(missing|fresh|stale)=(\d+)$", result.stdout, re.MULTILINE))
    assert set(codes) == {"missing", "fresh", "stale"}, (result.stdout, result.stderr)
    assert int(codes["missing"]) != 0, result.stdout
    assert int(codes["fresh"]) == 0, result.stdout
    assert int(codes["stale"]) != 0, result.stdout


def test_service_logs_are_one_json_object_per_line(stack: Stack) -> None:
    result = stack.compose(
        "logs",
        "--no-log-prefix",
        "planner",
        "analyzer",
        "transcriber",
        "api",
        "migrate",
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines
    for line in lines:
        assert isinstance(json.loads(line), dict), line


def test_analyzer_resolves_host_docker_internal(stack: Stack) -> None:
    result = stack.exec("analyzer", "getent", "hosts", "host.docker.internal")
    assert result.returncode == 0 and result.stdout.strip()


@pytest.mark.parametrize("service", ["transcriber", "planner"])
def test_egress_services_reach_the_internet(stack: Stack, service: str) -> None:
    assert stack.exec(service, "python", "-c", URLOPEN).returncode == 0


def test_db_has_no_egress(stack: Stack) -> None:
    # Positive control: the tool must exist before a non-zero exit can mean
    # "blocked"; a missing wget would also exit non-zero (127).
    control = stack.exec("db", "sh", "-c", "command -v wget")
    assert control.returncode == 0, "wget is missing from the db image"
    result = stack.exec(
        "db", "wget", "-q", "-T", "5", "-O", "/dev/null", "https://www.youtube.com"
    )
    assert result.returncode not in (0, 126, 127), result.stderr


def test_web_cannot_reach_db_but_api_can(stack: Stack) -> None:
    # Positive control: nc must exist and succeed against something web can
    # reach (itself on :8080) before a failure toward db can mean "blocked".
    control = stack.exec("web", "sh", "-c", "nc -z -w 3 127.0.0.1 8080")
    assert control.returncode == 0, f"nc is unusable in the web image: {control.stderr}"
    assert stack.exec("web", "sh", "-c", "nc -z -w 3 db 5432").returncode != 0
    api = stack.exec(
        "api", "python", "-c",
        "import socket; socket.create_connection(('db', 5432), timeout=3)",
    )  # fmt: skip
    assert api.returncode == 0, api.stderr


def test_transcriber_limits_and_cpus_override(stack: Stack) -> None:
    assert _inspect(stack, "transcriber", "{{.HostConfig.NanoCpus}}") == "3000000000"
    assert _inspect(stack, "transcriber", "{{.HostConfig.Memory}}") == "6442450944"
    try:
        result = stack.compose(
            "up", "-d", "--no-build", "--no-deps", "--wait",
            "--wait-timeout", WAIT_TIMEOUT, "transcriber",
            env={"TRANSCRIBER_CPUS": "2.0"},
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        assert (
            _inspect(stack, "transcriber", "{{.HostConfig.NanoCpus}}") == "2000000000"
        )
    finally:
        stack.compose(
            "up", "-d", "--no-build", "--no-deps", "--wait",
            "--wait-timeout", WAIT_TIMEOUT, "transcriber",
        )  # fmt: skip


def test_audio_volume_is_writable_by_transcriber_and_deletable_by_planner(
    stack: Stack,
) -> None:
    assert (
        stack.exec("transcriber", "sh", "-c", "touch /data/audio/.probe").returncode
        == 0
    )
    assert stack.exec("planner", "rm", "/data/audio/.probe").returncode == 0


def test_failed_migrate_blocks_every_dependent(stack: Stack) -> None:
    # A separate project: `db` comes up, `migrate` points at a database that
    # does not exist and exits non-zero, and nothing downstream may start.
    override = stack.tmp_path / "bad-migrate.yml"
    override.write_text(
        "services:\n"
        "  migrate:\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql://ytdigest:${POSTGRES_PASSWORD}@db:5432/nonexistent\n"
    )
    bad = Stack(f"{stack.project}-bad", stack.tmp_path)
    files = ("compose.yml", str(override))
    try:
        result = bad.compose("up", "-d", "--no-build", files=files)
        assert result.returncode != 0
        assert "migrate" in result.stderr
        rows = bad.compose("ps", "-a", "--format", "json", files=files).stdout
        names = {
            json.loads(ln)["Service"]: json.loads(ln)["State"]
            for ln in rows.splitlines()
            if ln.strip()
        }
        assert names.get("migrate") == "exited"
        for name in ("api", "planner", "analyzer", "transcriber"):
            assert names.get(name) != "running", name
    finally:
        bad.compose("down", "-v", "--remove-orphans", files=files)


def test_workers_stop_cleanly_within_the_grace_period(stack: Stack) -> None:
    ids = {name: stack.container_id(name) for name in WORKERS}
    try:
        result = stack.compose("stop", *WORKERS)
        assert result.returncode == 0, result.stderr
        for name, container in ids.items():
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.ExitCode}}", container],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout.strip()
            assert out == "0", name
    finally:
        restored = stack.compose(
            "up", "-d", "--no-build", "--wait", "--wait-timeout", WAIT_TIMEOUT,
            *WORKERS,
        )
        assert restored.returncode == 0, restored.stderr
