"""`docker compose` behaviour that needs the real CLI (issue #7).

compose.yml/compose.override.yml *shape* is covered without Docker in
test_compose.py; these two checks need `docker compose` itself to resolve
`${POSTGRES_PASSWORD:?required}` and to confirm the dev overlay's published
port doesn't leak into an explicit `-f compose.yml` resolution. Both are
marked integration: Docker Desktop must be running.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_up_refuses_to_start_without_postgres_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    try:
        result = _run("up", "-d", "db")
        assert result.returncode != 0
        assert "POSTGRES_PASSWORD" in result.stderr
    finally:
        _run("down", "-v")


def test_explicit_compose_yml_config_has_no_published_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "integration-test-password")

    result = _run("-f", "compose.yml", "config")

    assert result.returncode == 0
    # Only `web` publishes a port (#58); `db`'s 5432 is dev-overlay only.
    assert result.stdout.count("published:") == 1
    assert "5432" not in result.stdout.replace("db:5432", "")
    # The dev-only `devhost` network (compose.override.yml, #7 QA fix) must
    # never reach the production resolution.
    assert "devhost" not in result.stdout


def test_default_config_attaches_db_to_the_dev_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override merged in (the default, local behaviour): `db` picks up the
    # non-internal `devhost` network alongside `internal`, which is what
    # actually makes the `127.0.0.1:5432` publish reachable from the host -
    # `internal: true` alone blocks port forwarding regardless of the
    # `ports:` mapping (#7 QA FAIL root cause).
    monkeypatch.setenv("POSTGRES_PASSWORD", "integration-test-password")

    result = _run("config")

    assert result.returncode == 0
    assert "devhost" in result.stdout
    assert "127.0.0.1" in result.stdout


def _resolved(
    monkeypatch: pytest.MonkeyPatch, **env: str
) -> dict[str, dict[str, object]]:
    monkeypatch.setenv("POSTGRES_PASSWORD", "integration-test-password")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result = _run("-f", "compose.yml", "config", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr.lower()  # no `$` interpolation
    services: dict[str, dict[str, object]] = json.loads(result.stdout)["services"]
    return services


def _env(services: dict[str, dict[str, object]], name: str) -> dict[str, str]:
    env = services[name]["environment"]
    assert isinstance(env, dict)
    return env


SHARED = ("PROMPT_VERSION", "SUMMARIZER", "CHUNK_SEC", "OVERLAP_SEC")


def test_shared_variables_are_equal_across_planner_analyzer_and_transcriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _resolved(monkeypatch)
    for key in SHARED:
        values = {
            _env(services, n)[key] for n in ("planner", "analyzer", "transcriber")
        }
        assert len(values) == 1, key


def test_prompt_version_override_reaches_all_three_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _resolved(monkeypatch, PROMPT_VERSION="v2")
    for name in ("planner", "analyzer", "transcriber"):
        assert _env(services, name)["PROMPT_VERSION"] == "v2"


def test_anthropic_api_key_only_on_the_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _resolved(monkeypatch)
    assert "ANTHROPIC_API_KEY" in _env(services, "analyzer")
    for name in ("migrate", "api", "planner", "transcriber"):
        assert "ANTHROPIC_API_KEY" not in _env(services, name)


def test_web_is_the_only_published_port_and_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _resolved(monkeypatch)
    for name, service in services.items():
        if name != "web":
            assert "ports" not in service, name
    ports = services["web"]["ports"]
    assert isinstance(ports, list) and len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert str(ports[0]["published"]) == "8080"
    assert ports[0]["target"] == 80


def test_planner_has_one_replica_and_worker_healthcheck_keeps_its_dollars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _resolved(monkeypatch)
    deploy = services["planner"]["deploy"]
    assert isinstance(deploy, dict) and deploy["replicas"] == 1
    health = services["planner"]["healthcheck"]
    assert isinstance(health, dict)
    assert "$(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) ))" in health["test"][1]
    assert services["planner"]["healthcheck"] == services["analyzer"]["healthcheck"]
    assert services["planner"]["healthcheck"] == services["transcriber"]["healthcheck"]


def test_config_fails_naming_postgres_password_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    result = _run("-f", "compose.yml", "config")
    assert result.returncode != 0
    assert "POSTGRES_PASSWORD" in result.stderr
