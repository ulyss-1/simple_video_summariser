"""The compose-stack helpers shared by the `image` tests (issues #58, #137, #262)."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "stack-test-password"
WAIT_TIMEOUT = "600"


class Stack:
    def __init__(self, project: str, tmp_path: Path) -> None:
        self.project = project
        self.tmp_path = tmp_path

    def compose(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        files: tuple[str, ...] = ("compose.yml",),
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        file_args = [arg for f in files for arg in ("-f", f)]
        # An env file instead of process env, which ruff bans here: it also
        # keeps the developer's own .env (and its API key) out of the run.
        # WEB_HOST_PORT empty: Docker picks the host port, so a stack that already
        # holds 8080 (a demo deploy) cannot make this one fail to start.
        values = {"POSTGRES_PASSWORD": PASSWORD, "WEB_HOST_PORT": "", **(env or {})}
        env_file = self.tmp_path / f"{self.project}.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "--env-file",
                str(env_file),
                *file_args,
                *args,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def exec(self, service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", service, *cmd, timeout=120)

    def up(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.compose(
            "up", "-d", "--build", "--wait", "--wait-timeout", WAIT_TIMEOUT, *extra
        )

    def containers(self) -> dict[str, dict[str, object]]:
        out = self.compose("ps", "-a", "--format", "json").stdout.strip()
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        if len(rows) == 1 and isinstance(rows[0], list):
            rows = rows[0]
        return {row["Service"]: row for row in rows}

    def container_id(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()


class SmokeStack(Stack):
    """#58's `Stack` with a fixed list of compose files merged on every call."""

    def __init__(self, project: str, tmp_path: Path, files: tuple[str, ...]) -> None:
        super().__init__(project, tmp_path)
        self.files = files

    def compose(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        files: tuple[str, ...] | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        return super().compose(
            *args, env=env, files=files or self.files, timeout=timeout
        )

    def logs_tail(self, service: str, lines: int = 50) -> str:
        return self.compose(
            "logs", "--no-log-prefix", "--tail", str(lines), service, timeout=120
        ).stdout

    def web_port(self) -> int:
        out = self.compose("port", "web", "8080", timeout=60).stdout.strip()
        assert out.startswith("127.0.0.1:"), f"web is not on loopback: {out!r}"
        return int(out.rpartition(":")[2])


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        with err:
            raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode("utf-8", "replace")


def _video_jobs(jobs: list[dict[str, Any]], video_id: str) -> list[dict[str, Any]]:
    return [j for j in jobs if j["video_id"] == video_id]


def _all_jobs(base: str) -> list[dict[str, Any]]:
    status, doc = _http("GET", f"{base}/api/ops/jobs?limit=100")
    assert status == 200, doc
    items: list[dict[str, Any]] = doc["items"]
    return items


def _diagnostics(stack: SmokeStack, base: str, last_status: object) -> str:
    try:
        rows = [
            f"  {j['kind']} state={j['state']} attempts={j['attempts']} "
            f"error_class={j['error_class']} last_error={j['last_error']}"
            for j in _all_jobs(base)
        ]
    except Exception as err:  # noqa: BLE001 - diagnostics must not hide the failure
        rows = [f"  (jobs unavailable: {err!r})"]
    return (
        f"last observed status: {last_status!r}\n"
        "jobs:\n" + "\n".join(rows) + "\n"
        f"--- transcriber (last 50 lines) ---\n{stack.logs_tail('transcriber')}\n"
        f"--- analyzer (last 50 lines) ---\n{stack.logs_tail('analyzer')}"
    )


def _wait_until_done(
    stack: SmokeStack,
    base: str,
    started: float,
    *,
    video_id: str,
    deadline_sec: float,
    poll_sec: float = 1.0,
) -> dict[str, Any]:
    """Poll once a second until `done`; fail fast on `failed`, `unavailable` or an error."""
    last: object = None
    while True:
        status, doc = _http("GET", f"{base}/api/videos/{video_id}")
        if status == 200:
            last = doc["status"]
            if last == "done":
                assert isinstance(doc, dict)
                return doc
            if last in ("failed", "unavailable"):
                pytest.fail(
                    f"video ended as {last!r}\n{_diagnostics(stack, base, last)}"
                )
            errors = [
                j
                for j in _video_jobs(_all_jobs(base), video_id)
                if j["last_error"] is not None
            ]
            if errors:
                pytest.fail(
                    f"a job reported last_error (status {last!r})\n"
                    f"{_diagnostics(stack, base, last)}"
                )
        elif status != 404:
            pytest.fail(
                f"GET video returned {status}: {doc}\n{_diagnostics(stack, base, last)}"
            )
        if time.monotonic() - started > deadline_sec:
            pytest.fail(
                f"timeout: video not done within {deadline_sec:.0f} s\n"
                f"{_diagnostics(stack, base, last)}"
            )
        time.sleep(poll_sec)


def _leftovers(project: str) -> tuple[str, str]:
    label = f"label=com.docker.compose.project={project}"

    def lines(*cmd: str) -> str:
        return subprocess.run(
            ["docker", *cmd], capture_output=True, text=True, timeout=120, check=True
        ).stdout.strip()

    return (
        lines("ps", "-a", "--filter", label, "-q"),
        lines("volume", "ls", "--filter", label, "-q"),
    )
