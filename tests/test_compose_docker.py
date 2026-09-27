"""`docker compose` behaviour that needs the real CLI (issue #7).

compose.yml/compose.override.yml *shape* is covered without Docker in
test_compose.py; these two checks need `docker compose` itself to resolve
`${POSTGRES_PASSWORD:?required}` and to confirm the dev overlay's published
port doesn't leak into an explicit `-f compose.yml` resolution. Both are
marked integration: Docker Desktop must be running.
"""

from __future__ import annotations

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
    assert "ports" not in result.stdout
