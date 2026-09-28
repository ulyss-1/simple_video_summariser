"""Static checks of the speech-to-text image inputs (issue #56, architecture.md 11.3, 16.2).

No Docker is invoked: these read `ops/Dockerfile.whisper` and `.dockerignore`
as text, so they run under `pytest -m "not integration"`.
`test_whisper_image_docker.py` builds the image and checks the runtime.
"""

from __future__ import annotations

import re

from tests.test_backend_image import (
    REPO_ROOT,
    _copy_sources,
    _dockerignore_lines,
    _instructions,
)

DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile.whisper"


def _steps() -> list[tuple[str, str]]:
    return _instructions(DOCKERFILE)


def _install_run() -> str:
    installs = [a for n, a in _steps() if n == "RUN" and "pip install" in a]
    assert len(installs) == 1
    return installs[0]


def test_single_from_is_the_backend_image() -> None:
    froms = [args for name, args in _steps() if name == "FROM"]
    assert froms == ["ytdigest-backend:latest"]


def test_install_uses_binary_only_wheels_from_the_requirements_file() -> None:
    run = _install_run()
    assert "/opt/venv/bin/pip install" in run
    assert "--only-binary=:all:" in run
    assert "--no-cache-dir" in run
    assert "-r requirements.whisper.txt" in run


def test_install_targets_the_base_venv_and_nothing_else() -> None:
    run = _install_run()
    assert "venv" not in run.replace("/opt/venv", "")
    assert " -U" not in run and "--upgrade" not in run
    assert "requirements.backend.txt" not in run
    assert not any(
        name == "RUN" and "apt-get" in args for name, args in _steps()
    ), "ffmpeg and PyAV's libraries are already in the base image"


def test_install_fails_loudly_pointing_at_architecture_16_2() -> None:
    run = _install_run()
    match = re.search(r"\|\|\s*\((.*)\)\s*$", run)
    assert match, "the install must end in an `|| (... exit 1)` branch"
    branch = match.group(1)
    assert "ERROR:" in branch
    assert "architecture.md 16.2" in branch
    assert branch.rstrip().endswith("exit 1")
    # The message cannot tell a missing wheel from a bad pin, so it names both.
    assert "no binary wheel" in branch
    assert "pin is wrong" in branch
    # pip's own output must stay visible.
    assert "/dev/null" not in run
    assert "-q" not in run.split()


def test_requirements_file_is_not_left_in_app() -> None:
    run = _install_run()
    if "--mount=type=bind" in run:
        return  # nothing is written into the layer
    workdir = "/app"
    copied = False
    for name, args in _steps():
        if name == "WORKDIR":
            workdir = args
        if name == "COPY" and "requirements.whisper.txt" in args:
            copied = True
            assert not workdir.startswith("/app")
            assert "rm " in run
    assert copied, "requirements.whisper.txt is never provided to the build"


def test_workdir_is_app_at_the_end() -> None:
    workdirs = [args for name, args in _steps() if name == "WORKDIR"]
    assert not workdirs or workdirs[-1] == "/app"


def test_no_application_code_or_backend_inputs_are_copied() -> None:
    for name, args in _steps():
        if name in {"COPY", "ADD"}:
            for source in _copy_sources(args):
                assert not source.startswith(
                    ("common", "adapters", "services", "migrations")
                )
                assert source not in {
                    "requirements.backend.txt",
                    "pyproject.toml",
                    "alembic.ini",
                    ".",
                    "./",
                }


def test_thread_cache_and_verbosity_environment_is_set() -> None:
    env = " ".join(a for n, a in _steps() if n == "ENV")
    assert re.search(r"\bOMP_NUM_THREADS=4\b", env)
    assert "HF_HOME=/models" in env
    assert re.search(r"\bCT2_VERBOSE=0\b", env)


def test_offline_mode_is_not_baked_in() -> None:
    assert "HF_HUB_OFFLINE" not in " ".join(a for n, a in _steps() if n == "ENV")


def test_thread_settings_are_explained_in_a_comment() -> None:
    comments = "\n".join(
        line
        for line in DOCKERFILE.read_text().splitlines()
        if line.lstrip().startswith("#")
    )
    assert "OMP_NUM_THREADS" in comments
    assert "WHISPER_THREADS" in comments
    assert "cpu_threads" in comments


def test_models_directory_is_created_and_owned_by_app_before_the_final_user() -> None:
    steps = _steps()
    last_user = max(i for i, (n, _) in enumerate(steps) if n == "USER")
    index = next(
        i
        for i, (n, a) in enumerate(steps)
        if n == "RUN" and "mkdir" in a and "/models" in a
    )
    assert index < last_user
    assert re.search(r"chown[^&|]*app:app[^&|]*/models", steps[index][1])


def test_root_is_used_only_before_the_final_non_root_user() -> None:
    users = [args for name, args in _steps() if name == "USER"]
    assert users[0] == "root"
    assert users[-1] == "app"
    assert users.count("root") == 1


def test_image_has_no_healthcheck_or_entrypoint() -> None:
    names = [name for name, _ in _steps()]
    assert "HEALTHCHECK" not in names
    assert "ENTRYPOINT" not in names


def test_any_cmd_is_exec_form() -> None:
    for name, args in _steps():
        if name == "CMD":
            assert args.startswith("["), "shell-form CMD swallows SIGTERM"


def test_image_never_runs_migrations() -> None:
    assert "alembic upgrade" not in DOCKERFILE.read_text()


def test_dockerignore_does_not_exclude_the_whisper_requirements() -> None:
    for line in _dockerignore_lines():
        assert "requirements.whisper.txt" not in line
        assert not line.startswith("!")
        assert line not in {"requirements*", "requirements*.txt", "*.txt", "**/*.txt"}
