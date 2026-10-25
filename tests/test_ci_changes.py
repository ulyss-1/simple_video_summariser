"""scripts/ci_changes.sh decides which CI jobs a change needs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_changes.sh"


def jobs(*files: str) -> dict[str, bool]:
    out = subprocess.run(
        ["bash", str(SCRIPT)],
        input="\n".join(files) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k: v == "true" for k, v in (line.split("=") for line in out.split())}


@pytest.mark.parametrize(
    ("files", "python", "frontend", "images"),
    [
        (("web/src/routes/Library.tsx",), False, True, False),
        (("web/src/api/schema.d.ts",), True, True, True),
        (("web/openapi.json",), True, True, True),
        (("common/queue.py", "tests/test_queue.py"), True, False, True),
        (("tests/test_queue.py",), True, False, False),
        (("tests/test_frontend_image.py",), True, False, True),
        (("ops/Dockerfile.backend",), True, False, True),
        (("_docs/team/pm.md", "docs/superpowers/plans/x.md"), False, False, False),
        (("README.md",), True, False, True),
        ((".github/workflows/ci.yml",), True, True, True),
        (("some/unknown/path.txt",), True, False, True),
    ],
)
def test_jobs_follow_the_changed_paths(
    files: tuple[str, ...], python: bool, frontend: bool, images: bool
) -> None:
    assert jobs(*files) == {"python": python, "frontend": frontend, "images": images}
