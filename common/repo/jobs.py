"""Job read helpers (issues #28, #39, architecture.md §2).

Writes go through the ``JobQueue`` port; this module only reads what a
handler needs to decide what to do next.
"""

from __future__ import annotations

from typing import Any

import psycopg


def latest_job_state(conn: psycopg.Connection[Any], video_id: str, kind: str) -> str | None:
    """State of the newest ``kind`` job for ``video_id``, or ``None`` if there is none.

    "Newest" is by ``created_at`` then ``id``, so a pending job created after a
    dead one (an operator retry) is the latest.
    """
    row = conn.execute(
        """
        SELECT state FROM jobs
        WHERE video_id = %s AND kind = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (video_id, kind),
    ).fetchone()
    return None if row is None else str(row[0])


def queue_depth(conn: psycopg.Connection[Any]) -> dict[tuple[str, str], int]:
    """Number of jobs per ``(kind, state)``, for ``/healthz`` (#39) and ``/metrics`` (#60).

    Only groups that have at least one row appear; zero-filling the known
    kinds and states is the caller's job. Read-only.
    """
    rows = conn.execute("SELECT kind, state, count(*) FROM jobs GROUP BY kind, state").fetchall()
    return {(str(kind), str(state)): int(count) for kind, state, count in rows}
