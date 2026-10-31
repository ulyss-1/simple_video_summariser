"""Layer-cache behaviour of the backend and whisper images (issue #135).

Marked `integration` (needs Docker) and `image`. The images are built from a
temporary copy of the build context under throwaway tags, so a running stack's
`ytdigest-backend:latest` / `ytdigest-whisper:latest` are never touched. The
assertions read the `--progress=plain` build log (the `CACHED` marker of the
whisper `pip install` step), never timing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.image]

REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTEXT_ENTRIES = (
    ".dockerignore",
    "alembic.ini",
    "requirements.backend.lock",
    "requirements.whisper.lock",
    "ops",
    "common",
    "adapters",
    "services",
    "migrations",
)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


@dataclass
class Built:
    context: Path
    suffix: str

    def tag(self, kind: str, round_: str = "") -> str:
        return f"ytdigest-t135-{kind}-{self.suffix}{round_}:latest"


def _docker(*args: str, cwd: Path = REPO_ROOT, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _build_all(built: Built, round_: str = "") -> str:
    """Build backend, deps and whisper; return the whisper build's plain log."""
    ctx = str(built.context)
    for dockerfile, extra, kind in (
        ("ops/Dockerfile.backend", [], "backend"),
        # --provenance=false as in compose.yml: the attestation's timestamps
        # would give each rebuild of deps a new digest and miss the cache.
        ("ops/Dockerfile.backend", ["--target", "deps", "--provenance=false"], "deps"),
    ):
        result = _docker(
            "build", "-f", dockerfile, *extra, "-t", built.tag(kind, round_), ctx
        )
        assert result.returncode == 0, result.stderr[-4000:]
    result = _docker(
        "build",
        "--progress=plain",
        "-f",
        "ops/Dockerfile.whisper",
        "--build-context",
        f"ytdigest-deps=docker-image://{built.tag('deps', round_)}",
        "-t",
        built.tag("whisper", round_),
        ctx,
        cwd=built.context,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stderr


def _pip_install_cached(log: str) -> bool:
    """Whether the whisper `pip install` step of a plain build log is CACHED."""
    step = re.search(r"^#(\d+) \[[^\]]*\] RUN .*pip install", log, re.MULTILINE)
    assert step is not None, "no pip install step in the build log"
    return re.search(rf"^#{step.group(1)} CACHED\b", log, re.MULTILINE) is not None


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Built]:
    context = tmp_path_factory.mktemp("t135-context")
    for entry in _CONTEXT_ENTRIES:
        source = REPO_ROOT / entry
        if source.is_dir():
            shutil.copytree(source, context / entry, ignore=_IGNORE)
        else:
            shutil.copy(source, context / entry)
    state = Built(context, uuid.uuid4().hex[:8])
    try:
        _build_all(state)
        yield state
    finally:
        for round_ in ("", "-code", "-lock"):
            for kind in ("whisper", "deps", "backend"):
                _docker("image", "rm", "-f", state.tag(kind, round_))


def _run(image: str, *command: str) -> str:
    result = _docker("run", "--rm", image, *command, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_code_change_leaves_the_whisper_install_cached_and_a_lock_change_does_not(
    built: Built,
) -> None:
    marker = f"# t135-marker-{uuid.uuid4().hex}"
    config = built.context / "common" / "config.py"
    original = config.read_text()
    config.write_text(original + f"\n{marker}\n")
    log = _build_all(built, "-code")
    assert _pip_install_cached(log)
    assert marker in _run(built.tag("whisper", "-code"), "cat", "/app/common/config.py")
    config.write_text(original)

    # Control: the same check must fail when the whisper lock really changes.
    lock = built.context / "requirements.whisper.lock"
    lock.write_text(lock.read_text() + f"\n# t135 {uuid.uuid4().hex}\n")
    log = _build_all(built, "-lock")
    assert not _pip_install_cached(log)


def test_app_directory_is_the_same_in_both_images(built: Built) -> None:
    listing = (
        "find /app -type f | sort; "
        "find /app -type f -printf '%u:%g %p\\n' | sort; stat -c '%U:%G' /app"
    )
    backend = _run(built.tag("backend"), "sh", "-c", listing)
    whisper = _run(built.tag("whisper"), "sh", "-c", listing)
    assert "/app/common/config.py" in backend
    assert backend == whisper


def test_deps_layers_are_shared_not_duplicated(built: Built) -> None:
    def layers(kind: str) -> list[str]:
        result = _docker("image", "inspect", "--format", "{{json .RootFS.Layers}}", built.tag(kind))
        assert result.returncode == 0, result.stderr
        parsed: list[str] = json.loads(result.stdout)
        return parsed

    deps = layers("deps")
    assert deps
    for kind in ("backend", "whisper"):
        assert layers(kind)[: len(deps)] == deps
