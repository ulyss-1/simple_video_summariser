"""The prebuilt-base seam and the secret rule, proved with real builds (#368).

Marked `integration` (needs Docker) and `image`. Throwaway tags only; never
`ytdigest-*:latest` and never the default compose project. Static checks of the
same rules live in test_deps_images_secrets.py and test_image_build.py.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.image]

REPO_ROOT = Path(__file__).resolve().parent.parent
SUFFIX = uuid.uuid4().hex[:8]
DEPS = f"ytdigest-t368-deps-{SUFFIX}:latest"
RUNTIME_SEAM = f"ytdigest-t368-seam-{SUFFIX}:latest"
RUNTIME_SOURCE = f"ytdigest-t368-source-{SUFFIX}:latest"
CONTROL = f"ytdigest-t368-control-{SUFFIX}:latest"
MARKER = f"t368-secret-marker-{uuid.uuid4().hex}"


def _docker(*args: str, cwd: Path = REPO_ROOT, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _ok(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    assert result.returncode == 0, result.stderr[-4000:]
    return result


def _layers(tag: str) -> list[str]:
    out = _ok(_docker("image", "inspect", tag, "--format", "{{json .RootFS.Layers}}")).stdout
    layers: list[str] = json.loads(out)
    return layers


@pytest.fixture(scope="module")
def deps() -> Iterator[str]:
    _ok(
        _docker(
            "build", "-f", "ops/Dockerfile.backend", "--target", "deps", "--provenance=false",
            "-t", DEPS, ".",
        )
    )
    try:
        yield DEPS
    finally:
        _docker("image", "rm", "-f", DEPS)


def test_a_prebuilt_deps_context_replaces_the_stage_without_apt_or_pip(deps: str) -> None:
    try:
        seam = _ok(
            _docker(
                "build", "--progress=plain", "-f", "ops/Dockerfile.backend",
                "--build-context", f"deps=docker-image://{deps}", "-t", RUNTIME_SEAM, ".",
            )
        )
        source = _ok(
            _docker("build", "--progress=plain", "-f", "ops/Dockerfile.backend", "-t", RUNTIME_SOURCE, ".")
        )
        log = seam.stderr
        # No stage of the deps chain executes: the base is the given image.
        assert "apt-get" not in log and "pip install" not in log
        assert "ffmpeg" not in log
        assert "[context deps]" in log
        prebuilt = _layers(deps)
        assert _layers(RUNTIME_SEAM)[: len(prebuilt)] == prebuilt
        # The same image as a from-source build, layer for layer.
        assert _layers(RUNTIME_SEAM)[: len(prebuilt)] == _layers(RUNTIME_SOURCE)[: len(prebuilt)]
        assert "apt-get" in source.stderr or "CACHED" in source.stderr  # control: the stage exists
    finally:
        for tag in (RUNTIME_SEAM, RUNTIME_SOURCE):
            _docker("image", "rm", "-f", tag)


def _in_any_layer(tag: str, marker: str, directory: Path) -> bool:
    """Whether ``marker`` occurs in any file of `docker save`, layers decompressed."""
    archive = directory / f"{uuid.uuid4().hex}.tar"
    _ok(_docker("save", "-o", str(archive), tag))
    needle = marker.encode()
    with tarfile.open(archive) as outer:
        for member in outer:
            stream = outer.extractfile(member) if member.isfile() else None
            if stream is None:
                continue
            head = stream.read(2)
            stream.seek(0)
            reader = gzip.GzipFile(fileobj=stream) if head == b"\x1f\x8b" else stream
            tail = b""
            while chunk := reader.read(1 << 20):
                if needle in tail + chunk:
                    return True
                tail = chunk[-len(needle) :]
    return False


def test_a_dot_env_file_in_the_context_never_reaches_a_layer(
    tmp_path: Path, deps: str
) -> None:
    context = tmp_path / "context"
    (context / "ops").mkdir(parents=True)
    for name in ("ops/Dockerfile.backend", ".dockerignore", "requirements.backend.lock"):
        shutil.copy(REPO_ROOT / name, context / name)
    for name in (".env", ".env.local", "ops/.env"):
        (context / name).write_text(f"POSTGRES_PASSWORD={MARKER}\n")
    tag = f"ytdigest-t368-secret-{SUFFIX}:latest"
    try:
        _ok(
            _docker(
                "build", "-f", "ops/Dockerfile.backend", "--target", "deps",
                "--provenance=false", "-t", tag, ".", cwd=context,
            )
        )
        assert not _in_any_layer(tag, MARKER, tmp_path)
    finally:
        _docker("image", "rm", "-f", tag)


def test_the_scan_finds_a_marker_that_is_copied_in(tmp_path: Path) -> None:
    # Control: without it, "not found" above would prove nothing.
    context = tmp_path / "control"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY marker.txt /marker.txt\n")
    (context / "marker.txt").write_text(MARKER)
    try:
        _ok(_docker("build", "-t", CONTROL, ".", cwd=context))
        assert _in_any_layer(CONTROL, MARKER, tmp_path)
    finally:
        _docker("image", "rm", "-f", CONTROL)
