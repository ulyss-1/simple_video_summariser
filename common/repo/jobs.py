"""Job read helpers (issues #28, #39, #40, #44, architecture.md §2).

Writes go through the ``JobQueue`` port; this module only reads what a
handler needs to decide what to do next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row


class _Unset:
    """Sentinel distinguishing "no filter" from an explicit ``None`` (#44)."""

    def __repr__(self) -> str:
        return "UNSET"


#: Default for ``list_jobs(error_class=...)``: no filter at all. Passing
#: ``None`` explicitly means "only rows with a NULL ``error_class``" (#114).
UNSET: Final = _Unset()


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


def latest_job(conn: psycopg.Connection[Any], video_id: str, kind: str) -> tuple[int, str] | None:
    """``(id, state)`` of the newest ``kind`` job for ``video_id``, or ``None`` (#40).

    Ordered like ``latest_job_state``: ``created_at`` then ``id``.
    """
    row = conn.execute(
        """
        SELECT id, state FROM jobs
        WHERE video_id = %s AND kind = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (video_id, kind),
    ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


def queue_depth(conn: psycopg.Connection[Any]) -> dict[tuple[str, str], int]:
    """Number of jobs per ``(kind, state)``, for ``/healthz`` (#39) and ``/metrics`` (#60).

    Only groups that have at least one row appear; zero-filling the known
    kinds and states is the caller's job. Read-only.
    """
    rows = conn.execute("SELECT kind, state, count(*) FROM jobs GROUP BY kind, state").fetchall()
    return {(str(kind), str(state)): int(count) for kind, state, count in rows}


@dataclass(frozen=True, slots=True)
class JobRow:
    """One ``jobs`` row for the Ops view (#44). ``payload`` is never exposed."""

    id: int
    video_id: str
    video_title: str | None
    kind: str
    dedupe_key: str
    state: str
    priority: int
    attempts: int
    error_class: str | None
    last_error: str | None
    run_after: datetime
    locked_by: str | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


_LIST_JOBS_SQL = """
    SELECT j.id, j.video_id, v.title AS video_title, j.kind, j.dedupe_key,
           j.state, j.priority, j.attempts, j.error_class, j.last_error,
           j.run_after, j.locked_by, j.heartbeat_at, j.finished_at, j.created_at
    FROM jobs j
    LEFT JOIN videos v ON v.video_id = j.video_id
    WHERE (%(state)s::text IS NULL OR j.state = %(state)s)
      AND (%(kind)s::text IS NULL OR j.kind = %(kind)s)
      AND (
          %(error_class_set)s = false
          OR (%(error_class)s::text IS NULL AND j.error_class IS NULL)
          OR j.error_class = %(error_class)s
      )
      AND (%(before_id)s::bigint IS NULL OR j.id < %(before_id)s)
    ORDER BY j.id DESC
    LIMIT %(limit)s
"""


def list_jobs(
    conn: psycopg.Connection[Any],
    *,
    state: str | None = None,
    kind: str | None = None,
    error_class: str | None | _Unset = UNSET,
    before_id: int | None = None,
    limit: int,
) -> list[JobRow]:
    """One page of ``jobs``, newest first, for the Ops view (#44).

    Filters combine with AND. ``error_class`` is a three-way switch: the
    default ``UNSET`` means "no filter", ``None`` means "only rows whose
    ``error_class`` is NULL" (#114's reaped-but-unclassed jobs), and any other
    string filters on that exact value. ``before_id`` is a keyset cursor:
    only rows with ``id < before_id``. Fetches ``limit + 1`` rows so the
    caller can see whether another page follows without a separate
    ``count(*)``; trimming to ``limit`` is the caller's job. One statement,
    bound parameters only, read-only.
    """
    error_class_set = error_class is not UNSET
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _LIST_JOBS_SQL,
            {
                "state": state,
                "kind": kind,
                "error_class_set": error_class_set,
                "error_class": None if error_class is UNSET else error_class,
                "before_id": before_id,
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    return [JobRow(**row) for row in rows]


def get_job(conn: psycopg.Connection[Any], job_id: int) -> JobRow | None:
    """One job in the Ops view's item shape (#44), or ``None`` without a row.

    Used after ``PostgresQueue.retry_dead`` to answer with the shared
    ``JobOut`` shape (``video_title`` included) instead of duplicating the
    join inline in the route.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT j.id, j.video_id, v.title AS video_title, j.kind, j.dedupe_key,
                   j.state, j.priority, j.attempts, j.error_class, j.last_error,
                   j.run_after, j.locked_by, j.heartbeat_at, j.finished_at, j.created_at
            FROM jobs j
            LEFT JOIN videos v ON v.video_id = j.video_id
            WHERE j.id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    return None if row is None else JobRow(**row)
