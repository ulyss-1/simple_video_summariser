"""Shared pytest fixtures (architecture.md 13; task #7).

Only integration tests touch Postgres. Pytest fixtures are lazy — a fixture
only runs if a test actually requests it (directly or through another
fixture) — so with `pytest -m "not integration"` no test asks for
`postgres_dsn`, `postgres_container` is never instantiated, and Docker is
never touched. This is what makes the fast path safe to run with Docker
Desktop stopped.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException  # type: ignore[import-untyped]
from psycopg import sql
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config
from testcontainers.core.docker_client import DockerClient

from common.config import get_settings

# ``pytester`` is a built-in pytest plugin (no dependency); the shuffle plugin
# is registered here so every run is shuffled (issue #72).
pytest_plugins = ["pytester", "tests.shuffle_plugin"]

# Disable testcontainers' Ryuk reaper (#194). Every container this suite starts
# is created with ``with PostgresContainer(...)`` / ``with DockerContainer(...)``,
# so it is stopped and removed when that block exits, including on error — Ryuk
# is redundant here. Ryuk is only a crash-safety net for a hard-killed run, and
# under concurrent Docker load its connect is refused, turning a green suite
# into hundreds of ``ConnectionRefusedError`` setup errors (QA saw 902 at one
# seed; the same seed passed when rerun alone — environmental, not order
# dependence). Disabling it removes that failure mode entirely. The tradeoff —
# a container leaked if pytest is SIGKILLed mid-run — is cleared with
# ``docker ps`` / ``docker rm``. Set on the config object, not via
# ``TESTCONTAINERS_RYUK_DISABLED`` in the environment, so it holds regardless of
# when testcontainers first read its environment (and needs no os.environ write).
testcontainers_config.ryuk_disabled = True

# Literal copy of the `db` image in compose.yml (architecture.md 11.3, 11.4, 16.1: >=18.6).
# tests/test_base_image_copies.py fails until the two agree.
_POSTGRES_IMAGE = "postgres:18-alpine@sha256:10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5"
_DEFAULT_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _require_docker_daemon[T](factory: Callable[[], T], *, docker_host: str | None) -> T:
    """Build the Docker client, or fail setup with one actionable line (#77).

    Only a ``DockerException`` (what ``docker.from_env()`` raises when it can't
    reach the daemon) is rewritten; any other error propagates unchanged.
    """
    try:
        return factory()
    except DockerException as exc:
        reason = f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"
    # Outside the ``except`` so pytest doesn't print the chained docker traceback.
    pytest.fail(
        f"Docker daemon unreachable at {docker_host or _DEFAULT_DOCKER_ENDPOINT} ({reason}): "
        "start Docker Desktop and enable WSL integration, "
        'or run pytest -m "not integration"',
        pytrace=False,
    )


def _probe_postgres(dsn: str, connect_timeout_sec: int) -> None:
    with psycopg.connect(dsn, connect_timeout=connect_timeout_sec) as conn:
        conn.execute("SELECT 1").fetchone()


def _wait_for_postgres(
    dsn: str,
    container_id: str,
    *,
    probe: Callable[[str, int], None] = _probe_postgres,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_sec: float = 60.0,
    connect_timeout_sec: int = 5,
) -> None:
    parts = urlsplit(dsn)
    endpoint = f"{parts.hostname}:{parts.port}"
    started_at = monotonic()
    deadline = started_at + deadline_sec
    attempts = 0
    delay = 0.1
    last_exc: psycopg.OperationalError | None = None

    while monotonic() < deadline:
        attempts += 1
        try:
            probe(dsn, connect_timeout_sec)
            return
        except psycopg.OperationalError as exc:
            last_exc = exc

        now = monotonic()
        if now >= deadline:
            break
        sleep(min(delay, 1.0, deadline - now))
        delay = min(delay * 2, 1.0)

    elapsed = monotonic() - started_at
    raise RuntimeError(
        f"Postgres container {container_id[:12]} was not reachable at {endpoint} "
        f"after {elapsed:.1f}s and {attempts} attempts"
    ) from last_exc


@pytest.fixture(autouse=True)
def _isolate_database_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient ``DATABASE_URL`` and no cached ``Settings`` in any test (#305).

    ``get_settings()`` is ``lru_cache``'d, so a test that reached it would leave
    a ``Settings`` behind for the next one, and a developer's exported
    ``DATABASE_URL`` would hide a test that forgot its own settings. Fixtures
    that need a database (``head_dsn`` and the per-directory conftests) set the
    variable again themselves, after this one has run.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """One Postgres container for the whole test session.

    Root cause of #74: Docker Desktop (WSL) publishes the host port a moment
    after the container reports ready, so the first connect can be refused.
    The fixture waits for a real connection and ``SELECT 1`` on the mapped
    port, up to 60 s, then fails explicitly naming the endpoint. One run of
    the suite verifies it; no repeat loops (testing-guidelines.md).
    """
    client = _require_docker_daemon(
        DockerClient,
        docker_host=os.environ.get("DOCKER_HOST"),  # noqa: TID251
    )
    client.client.close()
    with PostgresContainer(_POSTGRES_IMAGE, driver=None) as container:
        _wait_for_postgres(container.get_connection_url(), container.get_container_id())
        yield container


@pytest.fixture
def postgres_dsn(postgres_container: PostgresContainer) -> Iterator[str]:
    """A ``postgresql://`` DSN for a fresh, empty database.

    Each test gets its own database on the shared container, created before
    and dropped after, so no test can see another test's data.
    """
    admin_dsn = postgres_container.get_connection_url()
    db_name = f"test_{uuid.uuid4().hex}"

    with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
        admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    try:
        yield _with_dbname(admin_dsn, db_name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
            admin_conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name))
            )


def _alembic_config() -> Config:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    # The template build must not reconfigure the process's logging (#71).
    config.attributes["configure_logger"] = False
    return config


def _run_template_step(statement: str, template: str, action: Callable[[], object]) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - re-raised naming template and statement
        pytest.fail(
            f"migrated-schema template database {template!r} failed at {statement!r}: "
            f"{type(exc).__name__}: {exc}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def migrated_template(postgres_container: PostgresContainer) -> Iterator[str]:
    """Name of a template database migrated to head by the real migrations (#290).

    Built once per session inside this session's container, so it is never
    reused from an earlier session and a new migration is picked up with no
    manual step. Once built it accepts no connections, so a clone can never
    fail with "source database is being accessed by other users".
    """
    admin_dsn = postgres_container.get_connection_url()
    template = f"template_{uuid.uuid4().hex}"
    template_dsn = _with_dbname(admin_dsn, template)

    def create() -> None:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template)))

    def upgrade() -> None:
        # env.py reads DATABASE_URL through the lru_cache'd get_settings().
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_URL", template_dsn)
            get_settings.cache_clear()
            try:
                command.upgrade(_alembic_config(), "head")
            finally:
                get_settings.cache_clear()

    def seal() -> None:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false").format(
                    sql.Identifier(template)
                )
            )

    _run_template_step(f"CREATE DATABASE {template}", template, create)
    try:
        _run_template_step("alembic upgrade head", template, upgrade)
        _run_template_step(f"ALTER DATABASE {template} ALLOW_CONNECTIONS false", template, seal)
        yield template
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false").format(sql.Identifier(template))
            )
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(template))
            )


@pytest.fixture
def head_dsn(
    postgres_container: PostgresContainer,
    migrated_template: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """A DSN for a fresh database cloned from the migrated template (#290).

    Each test gets its own database, dropped afterwards. ``DATABASE_URL`` points
    at it and the settings cache is cleared, as the per-file fixtures did.
    """
    admin_dsn = postgres_container.get_connection_url()
    db_name = f"test_{uuid.uuid4().hex}"
    statement = f"CREATE DATABASE {db_name} TEMPLATE {migrated_template}"
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(db_name), sql.Identifier(migrated_template)
                )
            )
    except psycopg.Error as exc:
        pytest.fail(
            f"cloning template database {migrated_template!r} failed at {statement!r}: "
            f"{type(exc).__name__}: {exc}",
            pytrace=False,
        )
    dsn = _with_dbname(admin_dsn, db_name)
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name))
            )


def _with_dbname(dsn: str, db_name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{db_name}"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect ``@pytest.mark.image`` tests unless ``-m`` names ``image``.

    An image build downloads packages from PyPI and Debian and takes minutes,
    so it is opt-in (issue #55). Same pattern as the ``whisper`` marker in
    ``tests/adapters/transcription/conftest.py``: a hook rather than
    ``addopts``, because ``-m`` is single-valued and a caller's own
    ``-m "not integration"`` would silently replace an ``addopts`` one.
    """
    markexpr = config.getoption("markexpr") or ""
    if "image" in markexpr:
        return

    keep = [item for item in items if "image" not in item.keywords]
    deselected = [item for item in items if "image" in item.keywords]
    if deselected:
        items[:] = keep
        config.hook.pytest_deselected(items=deselected)
