"""Static checks of the backend image inputs (issue #55, architecture.md 11.3-11.5).

No Docker is invoked: these read `ops/Dockerfile.backend`, `.dockerignore`
and `compose.yml` as text, so they run under `pytest -m "not integration"`.
`test_backend_image_docker.py` builds the image and checks the runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_compose import _service_block

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile.backend"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
COMPOSE_YML = REPO_ROOT / "compose.yml"

_APP_DIRS = ("common/", "adapters/", "services/", "migrations/")


def _instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, comments dropped, continuations joined."""
    logical: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text().splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue  # comment line inside a continued instruction
        if stripped.endswith("\\"):
            pending += stripped[:-1].rstrip() + " "
            continue
        logical.append(pending + stripped)
        pending = ""
    result: list[tuple[str, str]] = []
    for line in logical:
        name, _, args = line.partition(" ")
        result.append((name.upper(), args.strip()))
    return result


def _dockerignore_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _copy_sources(args: str) -> list[str]:
    parts = [p for p in args.split() if not p.startswith("--")]
    return parts[:-1]


def test_both_stages_are_python_3_14_slim() -> None:
    froms = [args for name, args in _instructions() if name == "FROM"]
    assert len(froms) == 2
    assert froms[0].split()[0] == "python:3.14-slim"
    assert froms[1].split()[0] == "python:3.14-slim"


def test_requirements_are_copied_before_any_application_code() -> None:
    copies = [args for name, args in _instructions() if name == "COPY"]
    req_index = next(
        i
        for i, args in enumerate(copies)
        if "requirements.backend.txt" in _copy_sources(args)
    )
    app_indexes = [
        i
        for i, args in enumerate(copies)
        if any(src.startswith(_APP_DIRS) for src in _copy_sources(args))
    ]
    assert app_indexes, "application code is never copied"
    assert req_index < min(app_indexes)


def test_pip_install_runs_before_application_code_is_copied() -> None:
    steps = _instructions()
    pip_index = next(
        i for i, (n, a) in enumerate(steps) if n == "RUN" and "pip install" in a
    )
    first_app_copy = next(
        i
        for i, (n, a) in enumerate(steps)
        if n == "COPY" and any(s.startswith(_APP_DIRS) for s in _copy_sources(a))
    )
    assert pip_index < first_app_copy


def test_builder_installs_only_the_backend_requirements_into_opt_venv() -> None:
    runs = [a for n, a in _instructions() if n == "RUN"]
    installs = [a for a in runs if "pip install" in a]
    assert len(installs) == 1
    assert "-r requirements.backend.txt" in installs[0]
    assert "/opt/venv" in installs[0]
    assert "pip install ." not in installs[0]
    assert "-e" not in installs[0].split()
    assert "requirements.whisper.txt" not in installs[0]
    assert ".[dev]" not in installs[0]


def test_runtime_apt_packages_are_only_ffmpeg_and_curl() -> None:
    steps = _instructions()
    last_from = max(i for i, (n, _) in enumerate(steps) if n == "FROM")
    runtime_apt = [
        a for n, a in steps[last_from:] if n == "RUN" and "apt-get install" in a
    ]
    assert len(runtime_apt) == 1
    assert "build-essential" not in runtime_apt[0]
    packages = runtime_apt[0].split("apt-get install", 1)[1].split("&&")[0].split()
    assert sorted(p for p in packages if not p.startswith("-")) == ["curl", "ffmpeg"]
    assert "rm -rf /var/lib/apt/lists/*" in runtime_apt[0]


def test_whisper_requirements_and_pyproject_are_not_copied() -> None:
    for name, args in _instructions():
        if name in {"COPY", "ADD"}:
            sources = _copy_sources(args)
            assert "requirements.whisper.txt" not in sources
            assert "pyproject.toml" not in sources
            assert "." not in sources
            assert "./" not in sources


def test_last_user_instruction_is_a_non_root_user() -> None:
    users = [args for name, args in _instructions() if name == "USER"]
    assert users
    assert users[-1] not in {"root", "0"}
    assert users[-1].split(":")[0] == "app"


def test_app_user_has_uid_10001() -> None:
    text = DOCKERFILE.read_text()
    assert re.search(r"useradd\b[^\n]*-u 10001", text)


def test_image_has_no_healthcheck() -> None:
    assert all(name != "HEALTHCHECK" for name, _ in _instructions())


def test_image_has_no_shell_form_entrypoint_or_cmd() -> None:
    for name, args in _instructions():
        if name in {"ENTRYPOINT", "CMD"}:
            assert args.startswith("["), f"shell-form {name} swallows SIGTERM"


def test_image_has_no_entrypoint_at_all() -> None:
    # Services differ only by the command compose gives them.
    assert all(name != "ENTRYPOINT" for name, _ in _instructions())


def test_image_never_runs_migrations_on_startup() -> None:
    assert "alembic upgrade" not in DOCKERFILE.read_text()


def test_runtime_environment_is_set() -> None:
    env = " ".join(a for n, a in _instructions() if n == "ENV")
    assert "PATH=/opt/venv/bin:" in env
    assert "PYTHONUNBUFFERED=1" in env
    assert "PYTHONPATH=/app" in env


def test_data_audio_directory_is_created_and_owned_by_app() -> None:
    runs = " ".join(a for n, a in _instructions() if n == "RUN")
    assert "/data/audio" in runs
    assert re.search(r"chown[^&]*app:app[^&]*/data", runs)


def test_dockerignore_excludes_secrets_history_and_tests() -> None:
    lines = _dockerignore_lines()
    for entry in (
        ".env",
        ".git",
        "tests/",
        "data/",
        "**/node_modules",
        "**/__pycache__",
    ):
        assert entry in lines
    assert "*.md" in lines or "**/*.md" in lines
    assert any(line.startswith(".env") and line.endswith("*") for line in lines)


@pytest.mark.parametrize(
    "entry",
    [
        "*.egg-info",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        ".claude",
        ".remember",
        ".vscode",
        "_docs/",
        "scripts/",
        "*:Zone.Identifier",
    ],
)
def test_dockerignore_excludes_local_state_and_docs(entry: str) -> None:
    assert entry in _dockerignore_lines()


def test_dockerignore_does_not_exclude_web_as_a_whole() -> None:
    lines = _dockerignore_lines()
    assert "web" not in lines
    assert "web/" not in lines
    assert "web/**" not in lines
    assert "**/web" not in lines


def test_dockerignore_keeps_prompts_migrations_and_alembic_ini() -> None:
    for line in _dockerignore_lines():
        assert not line.startswith("!")  # no negations expected; keep it simple
        assert "prompts" not in line
        assert "migrations" not in line
        assert "alembic" not in line
        assert not line.endswith(".txt")


# ---- compose.yml `migrate` service ----


def _migrate() -> str:
    return _service_block(COMPOSE_YML.read_text(), "migrate")


def test_migrate_builds_the_backend_image_under_its_tag() -> None:
    block = _migrate()
    assert "build: {context: ., dockerfile: ops/Dockerfile.backend}" in block
    assert "image: ytdigest-backend:latest" in block


def test_migrate_runs_alembic_upgrade_head() -> None:
    assert "command: alembic upgrade head" in _migrate()


def test_migrate_environment_has_database_url_and_json_logs() -> None:
    block = _migrate()
    assert (
        "DATABASE_URL: postgresql://ytdigest:${POSTGRES_PASSWORD}@db:5432/ytdigest"
        in block
    )
    assert "LOG_FORMAT: json" in block


def test_migrate_waits_for_a_healthy_db() -> None:
    assert "depends_on: {db: {condition: service_healthy}}" in _migrate()


def test_migrate_is_on_the_internal_network_only() -> None:
    assert "networks: [internal]" in _migrate()


def test_migrate_is_one_shot_and_logs_like_the_other_services() -> None:
    block = _migrate()
    assert 'restart: "no"' in block
    assert "logging: *logging" in block


def test_migrate_publishes_no_ports() -> None:
    assert "ports:" not in _migrate()


def test_compose_header_no_longer_defers_migrate_to_later_tasks() -> None:
    assert "moves to #55/#58" not in COMPOSE_YML.read_text()
