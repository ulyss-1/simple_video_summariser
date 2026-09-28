"""``python -m services.cli`` and the ``ytdigest`` script name the same ``main`` (issue #31)."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_python_dash_m_reports_bad_input_with_exit_2_and_no_traceback() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "services.cli", "run", "https://evil.com/watch?v=dQw4w9WgXcQ"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_the_ytdigest_console_script_points_at_main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["ytdigest"] == "services.cli.main:main"
