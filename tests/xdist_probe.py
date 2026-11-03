"""Probe tests run by ``tests/test_xdist_isolation_db.py`` under real xdist workers (#296).

Not collected by the normal suite (the name does not match ``test_*.py``);
``xdist_probe_a.py`` and ``xdist_probe_b.py`` import these (and the real conftest fixtures) so the two sides are two
modules, which ``--dist loadfile`` puts on two workers. Each test reports what it
saw to ``$XDIST_PROBE_DIR/<name>_<side>.json`` and asserts its own invariant, so a
failure here fails the outer run. The two sides rendezvous through marker files in
the same directory, with a bounded deadline. Both sides must run their tests in the
same order (the outer run uses ``--no-shuffle``), or a rendezvous would wait on a
different test.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from testcontainers.community.postgres import PostgresContainer

from tests.xdist_support import rendezvous

OTHER = {"a": "b", "b": "a"}
EXTRA_CONNECTIONS = {"a": 3, "b": 7}
# A fixture's own bookkeeping connection is never left open, so the only client
# backends in a container are the ones a probe opens plus its counting connection.


def _dir() -> Path:
    return Path(os.environ["XDIST_PROBE_DIR"])  # noqa: TID251 - the outer test's channel to the probes


def _worker() -> str:
    return os.environ["PYTEST_XDIST_WORKER"]  # noqa: TID251 - xdist's own variable


@pytest.fixture
def side(request: pytest.FixtureRequest) -> str:
    module: ModuleType = request.module
    value = module.SIDE
    assert isinstance(value, str)
    return value


def _report(name: str, side: str, **data: Any) -> None:
    (_dir() / f"{name}_{side}.json").write_text(json.dumps({"worker": _worker(), **data}))


def _insert_channel(dsn: str, side: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO channels (channel_id, active, monitor_from) VALUES (%s, true, %s)",
            (f"UC{side}", datetime(2026, 1, 1, tzinfo=UTC)),
        )


@pytest.fixture
def drop_signal(postgres_container: PostgresContainer, side: str) -> Any:
    """Requested before ``head_dsn``: its teardown runs after ``head_dsn`` dropped the database."""
    box: dict[str, str] = {}
    yield box
    with psycopg.connect(postgres_container.get_connection_url(), autocommit=True) as admin:
        names = {row[0] for row in admin.execute("SELECT datname FROM pg_database")}
    assert box["db"] not in names, "head_dsn teardown did not drop the database it created"
    (_dir() / f"dropped_{side}").write_text("dropped")


def test_identity_first(
    postgres_container: PostgresContainer, migrated_template: str, side: str
) -> None:
    _report("identity", side, container=postgres_container.get_container_id(), template=migrated_template)


def test_identity_second_same_container(
    postgres_container: PostgresContainer, migrated_template: str, side: str
) -> None:
    _report(
        "identity_again", side,
        container=postgres_container.get_container_id(), template=migrated_template,
    )


def test_concurrent_tests_never_share_a_database(head_dsn: str, side: str) -> None:
    rendezvous(_dir(), f"alive_{side}", f"alive_{OTHER[side]}")  # both tests alive at once
    _insert_channel(head_dsn, side)
    with psycopg.connect(head_dsn) as conn:
        db = conn.execute("SELECT current_database()").fetchone()
        rows = [r[0] for r in conn.execute("SELECT channel_id FROM channels")]
    assert db is not None
    assert rows == [f"UC{side}"], f"saw rows {rows} in {db[0]}"
    _report("concurrent", side, db=db[0], rows=rows)


def test_dropping_one_workers_database_does_not_touch_the_other(
    drop_signal: dict[str, str], head_dsn: str, side: str
) -> None:
    _insert_channel(head_dsn, side)
    with psycopg.connect(head_dsn) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    drop_signal["db"] = row[0]
    if side == "b":
        # Finisher: wait until the holder has an open transaction, then end the test,
        # so head_dsn's teardown drops this database while the holder still holds its own.
        rendezvous(_dir(), "finisher_ready", "holding_a")
        return
    with psycopg.connect(head_dsn) as holder:
        holder.execute("INSERT INTO channels (channel_id, active, monitor_from) VALUES ('UCtx', true, %s)", (datetime(2026, 1, 2, tzinfo=UTC),))
        rendezvous(_dir(), "holding_a", "dropped_b")  # b's drop has completed by now
        holder.commit()
        count = holder.execute("SELECT count(*) FROM channels").fetchone()
    assert count == (2,)  # its own two rows, intact after the other worker's DROP
    _report("drop", side, count=2)


def test_template_clone_not_blocked_across_workers(
    postgres_container: PostgresContainer, migrated_template: str, side: str
) -> None:
    clone = f"test_{uuid.uuid4().hex}"
    admin_dsn = postgres_container.get_connection_url()
    rendezvous(_dir(), f"clone_{side}", f"clone_{OTHER[side]}")  # both clone at this instant
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(clone), sql.Identifier(migrated_template)
            )
        )
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(clone)))
    _report("clone", side, clone=clone)


def test_unfiltered_pg_stat_activity_counts_see_only_own_worker(head_dsn: str, side: str) -> None:
    extra = EXTRA_CONNECTIONS[side]
    held = [psycopg.connect(head_dsn) for _ in range(extra)]
    try:
        rendezvous(_dir(), f"conns_{side}", f"conns_{OTHER[side]}")  # both sides hold theirs now
        with psycopg.connect(head_dsn) as conn:
            # The repo's pattern: no datname filter.
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'"
            ).fetchone()
        assert row is not None
        # Own held connections plus this counting one; the other side's 3 or 7 are not visible.
        assert row[0] == extra + 1, f"saw {row[0]} client backends, expected {extra + 1}"
        _report("stat", side, count=row[0])
        rendezvous(_dir(), f"counted_{side}", f"counted_{OTHER[side]}")  # keep ours open until both counted
    finally:
        for conn_ in held:
            conn_.close()
