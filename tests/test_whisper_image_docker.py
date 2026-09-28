"""Builds the backend and whisper images and checks the latter's runtime (issue #56).

Marked `integration` (needs Docker Desktop) and `image` (builds download
packages, and the model-cache test downloads the `tiny` Whisper model):
deselected unless run with `pytest -m image`. Static checks of the same file
live in `test_whisper_image.py`. Every container, volume and temporary build
context created here is removed afterwards, even on failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.image]

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = "ytdigest-backend:latest"
IMAGE = "ytdigest-whisper:latest"

# Generates a 4 s 16 kHz mono sine with the image's ffmpeg and transcribes it
# with the real `tiny` model; a sine has no speech, so zero segments is fine.
_TRANSCRIBE = textwrap.dedent(
    """
    import subprocess
    from adapters.transcription.faster_whisper import FasterWhisperTranscriber
    from common.models import AudioRef

    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=4", "-ar", "16000", "-ac", "1",
         "/data/audio/t.wav"],
        check=True,
    )
    result = FasterWhisperTranscriber("tiny", "int8", 1).transcribe(
        AudioRef("t.wav", 1, 4.0)
    )
    print("TRANSCRIBED", result.engine_meta["engine"])
    """
)


def _docker(
    *args: str, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run(
    *command: str, extra: tuple[str, ...] = (), image: str = IMAGE, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return _docker("run", "--rm", *extra, image, *command, timeout=timeout)


def _ok(*command: str, image: str = IMAGE) -> str:
    result = _run(*command, image=image)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def images() -> str:
    for dockerfile, tag in (("ops/Dockerfile.backend", BACKEND), ("ops/Dockerfile.whisper", IMAGE)):
        result = _docker("build", "-f", dockerfile, "-t", tag, ".", timeout=1800)
        assert result.returncode == 0, result.stderr[-4000:]
    return IMAGE


@pytest.fixture
def volumes() -> Iterator[list[str]]:
    """Names of throwaway volumes; every one is removed afterwards."""
    created: list[str] = []
    try:
        yield created
    finally:
        for name in created:
            _docker("volume", "rm", "-f", name)


def _volume(volumes: list[str], kind: str) -> str:
    name = f"ytdigest-test-{kind}-{uuid.uuid4().hex[:8]}"
    volumes.append(name)
    return name


def test_runs_as_app_uid_10001_with_no_entrypoint_or_healthcheck() -> None:
    assert _ok("id", "-u") == "10001"
    assert _ok("id", "-un") == "app"
    config = json.loads(_docker("image", "inspect", IMAGE).stdout)[0]["Config"]
    assert config["User"] in {"app", "10001"}
    assert not config.get("Entrypoint")
    assert not config.get("Healthcheck")
    assert _ok("python", "-c", "import os; print(os.getpid())") == "1"


def test_environment_is_set_and_overridable() -> None:
    assert _ok("printenv", "OMP_NUM_THREADS", "HF_HOME", "CT2_VERBOSE").split() == [
        "4",
        "/models",
        "0",
    ]
    assert _ok("printenv", "PATH").startswith("/opt/venv/bin")
    assert _ok("printenv", "PYTHONUNBUFFERED") == "1"
    assert _ok("printenv", "PYTHONPATH") == "/app"
    assert _ok("pwd") == "/app"
    override = _run("printenv", "OMP_NUM_THREADS", extra=("-e", "OMP_NUM_THREADS=2"))
    assert override.stdout.strip() == "2"


def test_app_directory_matches_the_backend_image() -> None:
    whisper = set(_ok("ls", "-A", "/app").split())
    backend = set(_ok("ls", "-A", "/app", image=BACKEND).split())
    assert whisper == backend


@pytest.mark.parametrize(("path", "kind"), [("/models", "models"), ("/data/audio", "audio")])
def test_volume_directories_are_owned_by_app_and_writable_on_a_fresh_volume(
    volumes: list[str], path: str, kind: str
) -> None:
    assert _ok("stat", "-c", "%U:%G", path) == "app:app"
    volume = _volume(volumes, kind)
    result = _run("touch", f"{path}/x", extra=("-v", f"{volume}:{path}"))
    assert result.returncode == 0, result.stderr


def test_tmp_is_writable_by_app() -> None:
    assert _run("touch", "/tmp/heartbeat").returncode == 0


def test_faster_whisper_is_installed_at_the_pinned_version() -> None:
    assert "Version: 1.2.1" in _ok("pip", "show", "faster-whisper")
    imports = "import faster_whisper, ctranslate2, onnxruntime, av, tokenizers"
    assert _run("python", "-c", imports).returncode == 0
    assert _run("pip", "check").returncode == 0


def test_backend_packages_are_unchanged_in_the_whisper_image() -> None:
    backend = set(_ok("pip", "freeze", image=BACKEND).splitlines())
    whisper = set(_ok("pip", "freeze").splitlines())
    assert backend <= whisper, sorted(backend - whisper)
    assert any(line.startswith("faster-whisper==") for line in whisper - backend)


@pytest.mark.parametrize("module", ["pytest", "mypy", "testcontainers", "torch"])
def test_dev_and_heavy_packages_are_absent(module: str) -> None:
    result = _run("python", "-c", f"import {module}")
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


def test_runtime_has_no_build_toolchain() -> None:
    assert _ok("sh", "-c", "command -v gcc cc make || true") == ""


def test_adapter_sees_the_library_without_loading_a_model() -> None:
    check = (
        "from adapters.transcription.faster_whisper import "
        "FasterWhisperTranscriber as T; T('tiny','int8',1).check_available()"
    )
    assert _run("python", "-c", check).returncode == 0
    assert _run("python", "-c", "import services.transcriber.main").returncode == 0
    assert _run("python", "-m", "services.transcriber.main", "--help").returncode == 0


def test_whisper_threads_overrides_the_default_of_zero() -> None:
    code = (
        "from adapters.transcription.faster_whisper import "
        "FasterWhisperTranscriber as T; print(T()._threads)"
    )
    env = ("-e", "DATABASE_URL=postgresql://x@h/d", "-e", "WHISPER_THREADS=2")
    assert _run("python", "-c", code, extra=env).stdout.strip() == "2"


def test_transcriber_without_database_url_exits_2_naming_it() -> None:
    result = _run("python", "-m", "services.transcriber.main")
    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "DATABASE_URL" in output
    assert "Traceback" not in output


def test_model_is_cached_on_the_volume_and_reused_offline(volumes: list[str]) -> None:
    models = _volume(volumes, "models")
    audio = _volume(volumes, "audio")
    mounts = (
        "-e",
        "DATABASE_URL=postgresql://x@h/d",
        "-v",
        f"{models}:/models",
        "-v",
        f"{audio}:/data/audio",
    )

    first = _run("python", "-c", _TRANSCRIBE, extra=mounts, timeout=900)
    assert first.returncode == 0, first.stderr
    assert "TRANSCRIBED" in first.stdout

    listing = _run(
        "sh",
        "-c",
        "ls /models/hub && stat -c %u /models/hub/models--Systran--faster-whisper-tiny"
        " && ls -A /home/app",
        extra=("-v", f"{models}:/models"),
    )
    assert listing.returncode == 0, listing.stderr
    assert "models--Systran--faster-whisper-tiny" in listing.stdout
    assert "10001" in listing.stdout.split()
    assert ".cache" not in listing.stdout.split()

    # A new container on the same volume, with no network at all.
    second = _run(
        "python", "-c", _TRANSCRIBE, extra=(*mounts, "--network", "none"), timeout=300
    )
    assert second.returncode == 0, second.stderr
    assert "TRANSCRIBED" in second.stdout


def test_a_bad_pin_fails_the_build_with_pips_error_then_the_architecture_pointer(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    (context / "ops").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "ops" / "Dockerfile.whisper", context / "ops")
    (context / "requirements.whisper.txt").write_text("faster-whisper==0.0.0\n")
    tag = f"ytdigest-whisper-badpin-{uuid.uuid4().hex[:8]}:latest"
    try:
        result = _docker(
            "build",
            "--progress=plain",
            "-f",
            str(context / "ops" / "Dockerfile.whisper"),
            "-t",
            tag,
            str(context),
            timeout=600,
        )
    finally:
        _docker("image", "rm", "-f", tag)
    assert result.returncode != 0
    lines = [line.split(" ", 2)[-1] for line in result.stderr.splitlines()]
    pip_error = next(
        i for i, line in enumerate(lines) if "No matching distribution found" in line
    )
    pointer = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("ERROR:") and "architecture.md 16.2" in line
    )
    assert pip_error < pointer
