"""`docker compose` behaviour that needs the real CLI (issue #7).

compose.yml/compose.override.yml *shape* is covered without Docker in
test_compose.py; these two checks need `docker compose` itself to resolve
`${POSTGRES_PASSWORD:?required}` and to confirm the dev overlay's published
port doesn't leak into an explicit `-f compose.yml` resolution. Both are
marked integration: Docker Desktop must be running.

Every ``docker compose ... config`` resolution goes through the module-scoped
``compose_config`` fixture, which runs it at most once per distinct
combination of compose files, output format and environment (#290). Tests
that need their own invocation (``POSTGRES_PASSWORD`` unset, ``up``, the
missing ``ANTHROPIC_API_KEY_FILE`` failure) call ``_run`` directly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


# Variables a developer's shell may carry that change what `config` resolves to.
_HERMETIC_UNSET = (
    "WHISPER_MODEL",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_ENV_FILES",
    "WEB_HOST_PORT",
    "DB_HOST_PORT",
    "WEB_DEV_PORT",
    "PROMPT_VERSION",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_FILE",
)

ConfigResolver = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="module")
def compose_config(tmp_path_factory: pytest.TempPathFactory) -> ConfigResolver:
    """Run ``docker compose config`` once per distinct (files, format, env).

    The environment is explicit and hermetic: an empty ``--env-file`` (the
    developer's ``.env`` is ignored), the variables above removed, and
    ``POSTGRES_PASSWORD`` set. ``env`` overrides are part of the cache key.
    """
    env_file = tmp_path_factory.mktemp("compose-config") / "empty.env"
    env_file.write_text("")
    cache: dict[
        tuple[tuple[str, ...], bool, tuple[tuple[str, str], ...]],
        subprocess.CompletedProcess[str],
    ] = {}

    def resolve(
        files: tuple[str, ...] = (),
        *,
        json_format: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        overrides = tuple(sorted((env or {}).items()))
        key = (files, json_format, overrides)
        if key not in cache:
            full_env = {k: v for k, v in os.environ.items() if k not in _HERMETIC_UNSET}  # noqa: TID251
            full_env["POSTGRES_PASSWORD"] = "integration-test-password"
            full_env.update(overrides)
            args = ["--env-file", str(env_file)]
            for name in files:
                args += ["-f", name]
            args.append("config")
            if json_format:
                args += ["--format", "json"]
            cache[key] = _run(*args, env=full_env)
        return cache[key]

    return resolve


def _services(result: subprocess.CompletedProcess[str]) -> dict[str, dict[str, object]]:
    assert result.returncode == 0, result.stderr
    services: dict[str, dict[str, object]] = json.loads(result.stdout)["services"]
    return services


PROD = ("compose.yml",)
ANTHROPIC = ("compose.yml", "compose.anthropic.yml")
ANTHROPIC_DEV = ("compose.yml", "compose.override.yml", "compose.anthropic.yml")


@pytest.mark.parametrize("populated_default_env", [False, True])
def test_up_refuses_to_start_without_postgres_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, populated_default_env: bool
) -> None:
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    if populated_default_env:
        populated_env = tmp_path / "populated.env"
        populated_env.write_text("POSTGRES_PASSWORD=developer-password\n")
        monkeypatch.setenv("COMPOSE_ENV_FILES", str(populated_env))
    else:
        monkeypatch.delenv("COMPOSE_ENV_FILES", raising=False)
    project = f"issue-224-{tmp_path.name.replace('_', '-')}"

    result = _run(
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "-f",
        "compose.yml",
        "up",
        "-d",
        "db",
    )

    assert result.returncode != 0
    assert "POSTGRES_PASSWORD" in result.stderr


def _resolved(
    compose_config: ConfigResolver, **env: str
) -> dict[str, dict[str, object]]:
    result = compose_config(PROD, env=env)
    services = _services(result)
    assert "warning" not in result.stderr.lower()  # no `$` interpolation
    return services


def _env(services: dict[str, dict[str, object]], name: str) -> dict[str, str]:
    env = services[name]["environment"]
    assert isinstance(env, dict)
    return env


SHARED = ("PROMPT_VERSION", "SUMMARIZER", "CHUNK_SEC", "OVERLAP_SEC")


def test_shared_variables_are_equal_across_planner_analyzer_and_transcriber(
    compose_config: ConfigResolver,
) -> None:
    services = _resolved(compose_config)
    for key in SHARED:
        values = {
            _env(services, n)[key] for n in ("planner", "analyzer", "transcriber")
        }
        assert len(values) == 1, key


def test_prompt_version_override_reaches_all_three_workers(
    compose_config: ConfigResolver,
) -> None:
    services = _resolved(compose_config, PROMPT_VERSION="v2")
    for name in ("planner", "analyzer", "transcriber"):
        assert _env(services, name)["PROMPT_VERSION"] == "v2"


def test_anthropic_api_key_only_on_the_analyzer(
    compose_config: ConfigResolver,
) -> None:
    services = _resolved(compose_config)
    assert "ANTHROPIC_API_KEY" in _env(services, "analyzer")
    for name in ("migrate", "api", "planner", "transcriber"):
        assert "ANTHROPIC_API_KEY" not in _env(services, name)


def test_web_is_the_only_published_port_and_on_loopback(
    compose_config: ConfigResolver,
) -> None:
    services = _resolved(compose_config)
    for name, service in services.items():
        if name != "web":
            assert "ports" not in service, name
    ports = services["web"]["ports"]
    assert isinstance(ports, list) and len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert str(ports[0]["published"]) == "8080"
    assert ports[0]["target"] == 8080


def test_web_host_port_is_overridable_and_empty_lets_docker_pick(
    compose_config: ConfigResolver,
) -> None:
    # A demo deploy may hold 8080: another project must be able to start (#58).
    chosen = _resolved(compose_config, WEB_HOST_PORT="18080")["web"]["ports"]
    assert str(chosen[0]["published"]) == "18080"  # type: ignore[index]
    picked = _resolved(compose_config, WEB_HOST_PORT="")["web"]["ports"]
    assert str(picked[0].get("published", "")) in ("", "0")  # type: ignore[index]
    assert picked[0]["target"] == 8080  # type: ignore[index]


def test_planner_has_one_replica_and_worker_healthcheck_keeps_its_dollars(
    compose_config: ConfigResolver,
) -> None:
    services = _resolved(compose_config)
    deploy = services["planner"]["deploy"]
    assert isinstance(deploy, dict) and deploy["replicas"] == 1
    health = services["planner"]["healthcheck"]
    assert isinstance(health, dict)
    # Verified against Docker Compose v2.40.3: `config --format json`
    # re-escapes every "$" as "$$" (the compose.yml escape for a literal "$").
    # The container still receives single dollars; the doubled form in the
    # resolved config is expected, not a bug. A single "$" in compose.yml
    # would be interpolated by Compose and fail this assertion.
    assert (
        "test $$(( $$(date +%s) - $$(stat -c %Y /tmp/heartbeat) )) -lt 180"
        in health["test"][1]
    )
    assert services["planner"]["healthcheck"] == services["analyzer"]["healthcheck"]
    assert services["planner"]["healthcheck"] == services["transcriber"]["healthcheck"]


@pytest.mark.parametrize("populated_default_env", [False, True])
def test_config_fails_naming_postgres_password_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, populated_default_env: bool
) -> None:
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    if populated_default_env:
        populated_env = tmp_path / "populated.env"
        populated_env.write_text("POSTGRES_PASSWORD=developer-password\n")
        monkeypatch.setenv("COMPOSE_ENV_FILES", str(populated_env))
    else:
        monkeypatch.delenv("COMPOSE_ENV_FILES", raising=False)

    result = _run("--env-file", str(env_file), "-f", "compose.yml", "config")

    assert result.returncode != 0
    assert "POSTGRES_PASSWORD" in result.stderr


# --- development overlay (issue #59) ---------------------------------------
#
# Resolved-config checks: only the `docker compose` CLI, no build, no running
# stack. Hermetic through the ``compose_config`` fixture.

Config = dict[str, dict[str, object]]
SOURCE_DIRS = ("common", "adapters", "services")
# The web files the dev overlay binds (read-only) into /home/node/app.
WEB_SOURCE_FILES = (
    "src", "index.html", "index.css", "public", "package.json", "package-lock.json",
    "tsconfig.json", "vite.config.ts",
)  # fmt: skip
BACKEND_SERVICES = ("api", "planner", "analyzer", "transcriber")


def _dev(compose_config: ConfigResolver, **env: str) -> Config:
    return _services(compose_config((), env=env))


def _binds(service: dict[str, object]) -> dict[str, tuple[str, bool]]:
    """target -> (source, read_only) for every bind mount of a service."""
    out: dict[str, tuple[str, bool]] = {}
    volumes = service.get("volumes", [])
    assert isinstance(volumes, list)
    for vol in volumes:
        if vol["type"] == "bind":
            out[vol["target"]] = (vol["source"], bool(vol.get("read_only")))
    return out


def _volumes(service: dict[str, object]) -> dict[str, str]:
    volumes = service.get("volumes", [])
    assert isinstance(volumes, list)
    return {v["target"]: v["type"] for v in volumes}


def _env_of(service: dict[str, object]) -> dict[str, str]:
    env = service["environment"]
    assert isinstance(env, dict)
    return env


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_dev_config_mounts_backend_source_read_only(
    compose_config: ConfigResolver, name: str
) -> None:
    binds = _binds(_dev(compose_config)[name])
    assert binds == {f"/app/{d}": (str(REPO_ROOT / d), True) for d in SOURCE_DIRS}


def test_dev_config_migrate_mounts_migrations_read_only(
    compose_config: ConfigResolver
) -> None:
    services = _dev(compose_config)
    assert _binds(services["migrate"]) == {
        "/app/migrations": (str(REPO_ROOT / "migrations"), True)
    }
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]


def test_dev_config_mounts_nothing_else_and_keeps_named_volumes(
    compose_config: ConfigResolver
) -> None:
    services = _dev(compose_config)
    sources = {src for svc in services.values() for src, _ in _binds(svc).values()}
    assert sources == {str(REPO_ROOT / d) for d in (*SOURCE_DIRS, "migrations")} | {
        str(REPO_ROOT / "web" / n) for n in WEB_SOURCE_FILES
    }
    assert _volumes(services["planner"])["/data/audio"] == "volume"
    assert _volumes(services["transcriber"])["/data/audio"] == "volume"
    assert _volumes(services["transcriber"])["/models"] == "volume"
    assert _volumes(services["db"]) == {"/var/lib/postgresql": "volume"}


def test_dev_config_api_runs_uvicorn_reload_workers_keep_commands(
    compose_config: ConfigResolver
) -> None:
    services = _dev(compose_config)
    assert services["api"]["command"] == [
        "uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000",
        "--reload",
        "--reload-dir", "/app/common",
        "--reload-dir", "/app/adapters",
        "--reload-dir", "/app/services",
    ]  # fmt: skip
    assert services["planner"]["command"] == ["python", "-m", "services.planner.main"]
    assert services["analyzer"]["command"] == ["python", "-m", "services.analyzer.main"]
    assert services["transcriber"]["command"] == [
        "python", "-m", "services.transcriber.main"
    ]  # fmt: skip


WATCHED_WORKERS = ("planner", "analyzer", "transcriber")


def _watch_rules(service: dict[str, object]) -> list[dict[str, object]]:
    develop = service["develop"]
    assert isinstance(develop, dict)
    rules = develop["watch"]
    assert isinstance(rules, list)
    return rules


def test_dev_config_only_the_three_workers_have_a_watch(
    compose_config: ConfigResolver
) -> None:
    services = _dev(compose_config)
    assert {n for n, svc in services.items() if "develop" in svc} == set(
        WATCHED_WORKERS
    )
    for name in ("api", "web", "migrate", "db"):
        assert "develop" not in services[name], name


@pytest.mark.parametrize("name", WATCHED_WORKERS)
def test_dev_config_worker_watch_restarts_on_its_own_sources_only(
    compose_config: ConfigResolver, name: str
) -> None:
    rules = _watch_rules(_dev(compose_config)[name])
    assert {r["action"] for r in rules} == {"restart"}
    assert sorted(str(r["path"]) for r in rules) == sorted(
        str(REPO_ROOT / p) for p in ("common", "adapters", f"services/{name}")
    )
    for rule in rules:
        # The bare directory form matters: creating `__pycache__` is itself an
        # event, which `**/__pycache__/**` alone does not ignore.
        assert set(rule["ignore"]) >= {  # type: ignore[call-overload]
            "**/__pycache__", "**/__pycache__/**", "**/*.pyc"
        }  # fmt: skip


def test_dev_config_web_is_the_vite_dev_server_without_a_build(
    compose_config: ConfigResolver
) -> None:
    web = _dev(compose_config)["web"]
    assert re.fullmatch(r"node:24-alpine@sha256:[0-9a-f]{64}", str(web["image"]))
    assert "build" not in web
    command = " ".join(web["command"])  # type: ignore[arg-type]
    assert "npm ci" in command and "npm run dev" in command
    assert "--host 0.0.0.0" in command and "--port 5173" in command
    assert "--strictPort" in command
    assert web["working_dir"] == "/home/node/app"
    assert _env_of(web)["API_PROXY_TARGET"] == "http://api:8000"
    assert list(web["networks"]) == ["edge"]  # type: ignore[call-overload]
    # Only source files, read-only; node_modules lives in the container layer,
    # so there is no volume at all (nothing nested in a bind source).
    assert _binds(web) == {
        f"/home/node/app/{n}": (str(REPO_ROOT / "web" / n), True)
        for n in WEB_SOURCE_FILES
    }
    assert _volumes(web) == {f"/home/node/app/{n}": "bind" for n in WEB_SOURCE_FILES}


def test_dev_host_ports_are_overridable_and_empty_lets_docker_pick(
    compose_config: ConfigResolver
) -> None:
    fixed = _dev(compose_config, DB_HOST_PORT="15432", WEB_DEV_PORT="15173")
    assert str(fixed["db"]["ports"][0]["published"]) == "15432"  # type: ignore[index]
    assert str(fixed["web"]["ports"][0]["published"]) == "15173"  # type: ignore[index]
    picked = _dev(compose_config, DB_HOST_PORT="", WEB_DEV_PORT="")
    for name, target in (("db", 5432), ("web", 5173)):
        port = picked[name]["ports"][0]  # type: ignore[index]
        assert str(port.get("published", "")) in ("", "0")
        assert port["target"] == target


def test_dev_config_publishes_exactly_db_5432_and_web_5173_on_loopback(
    compose_config: ConfigResolver
) -> None:
    services = _dev(compose_config)
    published = {
        (name, str(p["published"]), p["target"], p.get("host_ip"))
        for name, svc in services.items()
        for p in svc.get("ports", [])  # type: ignore[attr-defined]
    }
    assert published == {
        ("db", "5432", 5432, "127.0.0.1"),
        ("web", "5173", 5173, "127.0.0.1"),
    }
    # Compose returns networks sorted alphabetically, not in declaration order.
    assert set(services["db"]["networks"]) == {"internal", "devhost"}  # type: ignore[call-overload]


def test_dev_config_whisper_model_defaults_to_tiny_and_follows_the_variable(
    compose_config: ConfigResolver
) -> None:
    unset = _dev(compose_config)
    assert _env_of(unset["transcriber"])["WHISPER_MODEL"] == "tiny"
    base = _dev(compose_config, WHISPER_MODEL="base")
    assert _env_of(base["transcriber"])["WHISPER_MODEL"] == "base"


def test_dev_config_adds_no_secrets_or_new_env_keys(
    compose_config: ConfigResolver
) -> None:
    dev = _dev(compose_config)
    prod = _services(compose_config(PROD))
    for name, svc in dev.items():
        added = set(_env_of(svc)) - set(prod[name].get("environment", {}))  # type: ignore[call-overload]
        assert added <= {"API_PROXY_TARGET"}, name
    assert "ANTHROPIC_API_KEY" not in _env_of(dev["transcriber"])
    assert _env_of(dev["transcriber"]).keys() == _env_of(prod["transcriber"]).keys()


def test_anthropic_overlay_requires_host_file_configuration(
    compose_config: ConfigResolver,
) -> None:
    # Resolves in the same hermetic environment (ANTHROPIC_API_KEY_FILE unset).
    result = compose_config(ANTHROPIC, json_format=False)

    assert result.returncode != 0
    assert "ANTHROPIC_API_KEY_FILE" in result.stderr


@pytest.fixture(scope="module")
def anthropic_key_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    secret_file = tmp_path_factory.mktemp("anthropic-key") / "anthropic-key"
    secret_file.write_text("file-backed-fake-key")
    return secret_file


def _anthropic_config(
    compose_config: ConfigResolver, key_file: Path, *, dev: bool
) -> tuple[Config, str]:
    result = compose_config(
        ANTHROPIC_DEV if dev else ANTHROPIC,
        env={
            "ANTHROPIC_API_KEY": "environment-sentinel-key",
            "ANTHROPIC_API_KEY_FILE": str(key_file),
        },
    )
    return _services(result), result.stdout


@pytest.mark.parametrize("dev", [False, True])
def test_anthropic_overlay_resolves_without_key_in_any_environment(
    compose_config: ConfigResolver, anthropic_key_file: Path, dev: bool
) -> None:
    services, raw = _anthropic_config(compose_config, anthropic_key_file, dev=dev)

    assert "environment-sentinel-key" not in raw
    for name, service in services.items():
        raw_env = service.get("environment", {})
        assert isinstance(raw_env, dict)
        env: dict[str, str] = raw_env
        assert "ANTHROPIC_API_KEY" not in env, name
        if name == "analyzer":
            assert env["ANTHROPIC_API_KEY_FILE"] == "/run/secrets/anthropic_api_key"
            assert service["secrets"] == [
                {"source": "anthropic_api_key", "target": "anthropic_api_key"}
            ]
        else:
            assert "ANTHROPIC_API_KEY_FILE" not in env, name
            assert "secrets" not in service, name


def test_anthropic_development_overlay_preserves_dev_behavior(
    compose_config: ConfigResolver, anthropic_key_file: Path
) -> None:
    services, _ = _anthropic_config(compose_config, anthropic_key_file, dev=True)

    command = services["api"]["command"]
    assert isinstance(command, list) and "--reload" in command
    assert _env_of(services["transcriber"])["WHISPER_MODEL"] == "tiny"
    assert _binds(services["analyzer"])["/app/common"] == (
        str(REPO_ROOT / "common"), True
    )
    ports = services["web"]["ports"]
    assert isinstance(ports, list)
    assert str(ports[0]["published"]) == "5173"


def test_prod_config_has_none_of_the_dev_overlay(
    compose_config: ConfigResolver
) -> None:
    result = compose_config(PROD)
    services, raw = _services(result), result.stdout
    for needle in (
        '"type": "bind"',
        "--reload",
        "node:24-alpine",
        "5173",
        "devhost",
        "API_PROXY_TARGET",
        "develop",
    ):
        assert needle not in raw, needle
    assert '"published": "5432"' not in raw
    # Only `web` publishes a port (#58); `db`'s 5432 is dev-overlay only.
    assert raw.count('"published"') == 1
    assert "5432" not in raw.replace("db:5432", "")
    for svc in services.values():
        assert not _binds(svc)
    assert _env_of(services["transcriber"])["WHISPER_MODEL"] == "large-v3"
    web = services["web"]
    assert web["image"] == "ytdigest-web:latest"
    assert "build" in web
    ports = web["ports"]
    assert isinstance(ports, list) and len(ports) == 1
    assert (ports[0]["host_ip"], str(ports[0]["published"]), ports[0]["target"]) == (
        "127.0.0.1", "8080", 8080
    )  # fmt: skip
