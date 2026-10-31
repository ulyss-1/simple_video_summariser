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

from tests.docker_steps import Step, run_steps

pytestmark = [pytest.mark.integration, pytest.mark.image]

REPO_ROOT = Path(__file__).resolve().parent.parent
# Throwaway tags (#135): a stack running on the same host owns ytdigest-*:latest.
_SUFFIX = uuid.uuid4().hex[:8]
BACKEND = f"ytdigest-t135-backend-{_SUFFIX}:latest"
DEPS = f"ytdigest-t135-deps-{_SUFFIX}:latest"
IMAGE = f"ytdigest-t135-whisper-{_SUFFIX}:latest"
DEPS_CONTEXT = f"ytdigest-deps=docker-image://{DEPS}"

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


def _steps(*commands: str) -> list[Step]:
    """Several commands in one container, each result kept apart (#330)."""
    return run_steps(lambda script: _run("sh", "-c", script), *commands)


@pytest.fixture(scope="module", autouse=True)
def images() -> Iterator[str]:
    builds = (
        ("ops/Dockerfile.backend", BACKEND, ()),
        ("ops/Dockerfile.backend", DEPS, ("--target", "deps")),
        ("ops/Dockerfile.whisper", IMAGE, ("--build-context", DEPS_CONTEXT)),
    )
    try:
        for dockerfile, tag, extra in builds:
            result = _docker("build", "-f", dockerfile, *extra, "-t", tag, ".", timeout=1800)
            assert result.returncode == 0, result.stderr[-4000:]
        yield IMAGE
    finally:
        for _, tag, _ in reversed(builds):
            _docker("image", "rm", "-f", tag)


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
    uid, name, pid = _steps("id -u", "id -un", "echo $$")
    assert uid.ok() == "10001", uid.describe()
    assert name.ok() == "app", name.describe()
    config = json.loads(_docker("image", "inspect", IMAGE).stdout)[0]["Config"]
    assert config["User"] in {"app", "10001"}
    assert not config.get("Entrypoint")
    assert not config.get("Healthcheck")
    # The container's command is the `sh` running the script; with no
    # entrypoint it is PID 1 (as the `python` command was before the fold).
    assert pid.ok() == "1", pid.describe()


def test_environment_is_set_and_overridable() -> None:
    values, path, unbuffered, pythonpath, pwd = _steps(
        "printenv OMP_NUM_THREADS HF_HOME CT2_VERBOSE",
        "printenv PATH",
        "printenv PYTHONUNBUFFERED",
        "printenv PYTHONPATH",
        "pwd",
    )
    assert values.ok().split() == ["4", "/models", "0"], values.describe()
    assert path.ok().startswith("/opt/venv/bin"), path.describe()
    assert unbuffered.ok() == "1", unbuffered.describe()
    assert pythonpath.ok() == "/app", pythonpath.describe()
    assert pwd.ok() == "/app", pwd.describe()
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
    show, imports, check = _steps(
        "pip show faster-whisper",
        "python -c 'import faster_whisper, ctranslate2, onnxruntime, av, tokenizers'",
        "pip check",
    )
    assert "Version: 1.2.1" in show.ok(), show.describe()
    imports.ok()
    check.ok()


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
    adapter, help_ = _steps(
        f'python -c "{check}"',
        # -m runs the module top level (the import) before --help exits.
        "python -m services.transcriber.main --help",
    )
    adapter.ok()
    help_.ok()


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
    # The application COPY lines need their sources to exist; the install fails first.
    for name in ("common", "adapters", "services", "migrations"):
        (context / name).mkdir()
        (context / name / "placeholder").write_text("")
    (context / "alembic.ini").write_text("")
    (context / "requirements.whisper.lock").write_text(
        f"faster-whisper==0.0.0 --hash=sha256:{'a' * 64}\n"
    )
    tag = f"ytdigest-whisper-badpin-{uuid.uuid4().hex[:8]}:latest"
    try:
        result = _docker(
            "build",
            "--progress=plain",
            "--build-context",
            DEPS_CONTEXT,
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
