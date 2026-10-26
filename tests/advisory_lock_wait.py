"""Wait until Postgres has really released a session-level advisory lock (#305).

``psycopg``'s ``conn.close()`` sends Terminate and returns; the backend frees
its advisory locks while it exits, a moment later. The observable condition is
the lock's row leaving ``pg_locks``, so tests wait on that, with a bounded
deadline, instead of probing the lock itself.

The clock, the sleep and the probe are injectable, as in ``_wait_for_postgres``
(``tests/conftest.py``).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

_GRANTED_ADVISORY_PIDS = """
SELECT pid FROM pg_locks
WHERE locktype = 'advisory' AND granted
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND classid = %s::oid AND objid = %s::oid AND objsubid = 2
ORDER BY pid
"""


def granted_advisory_pids(conn: psycopg.Connection, namespace: int, task_id: int) -> list[int]:
    """Backends holding ``pg_try_advisory_lock(namespace, task_id)`` in this database."""
    rows = conn.execute(_GRANTED_ADVISORY_PIDS, (namespace, task_id)).fetchall()
    return [int(row[0]) for row in rows]


def wait_until_advisory_lock_released(
    probe: Callable[[], list[int]],
    *,
    namespace: int,
    task_id: int,
    holder_pid: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_sec: float = 5.0,
    interval_sec: float = 0.02,
) -> None:
    """Return once ``probe()`` reports no holder; fail at the deadline.

    ``probe`` returns the pids of the granted rows for the key.
    """
    deadline = monotonic() + deadline_sec
    while probe():
        now = monotonic()
        if now >= deadline:
            raise AssertionError(
                f"advisory lock ({namespace:#x}, {task_id}) still held by backend "
                f"{holder_pid} {deadline_sec:.1f} s after holder.close()"
            )
        sleep(min(interval_sec, deadline - now))
