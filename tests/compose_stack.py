"""The compose-stack helper shared by the `image` tests (issues #58, #137)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
        values = {"POSTGRES_PASSWORD": PASSWORD, **(env or {})}
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
