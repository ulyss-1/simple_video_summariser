"""The Anthropic batches an analyze job has submitted (issue #89).

Each call is one statement in its own transaction, so no transaction is held
open across a poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

from common.errors import LLMUnavailableError
from common.models import BatchCheckpoint, Usage

_COLUMNS = "step_key, batch_id, request_sha256, submitted_at"
_USAGE_COLUMNS = "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, calls, cost_usd"

#: The stored usage of a deleted row, moved into ``analyze_failed_usage`` (#252) -
#: only when the results were fetched (``calls`` not NULL) and something was spent.
_MOVE_USAGE_SQL = f"""
    INSERT INTO analyze_failed_usage
        (video_id, job_id, model, outcome, {_USAGE_COLUMNS})
    SELECT %(video_id)s, g.job_id, %(model)s, 'failed',
           g.input_tokens, g.output_tokens, g.cache_read_tokens, g.cache_write_tokens,
           g.calls, g.cost_usd
    FROM gone g
    WHERE g.calls IS NOT NULL AND NOT g.usage_moved
      AND (g.calls > 0 OR g.input_tokens > 0 OR g.output_tokens > 0
           OR g.cache_read_tokens > 0 OR g.cache_write_tokens > 0)
"""


def get_checkpoint(
    conn: psycopg.Connection[Any], job_id: int, step_key: str
) -> BatchCheckpoint | None:
    with conn.transaction():
        row = conn.execute(
            f"SELECT {_COLUMNS}, usage_moved FROM llm_batches WHERE job_id = %s AND step_key = %s",
            (job_id, step_key),
        ).fetchone()
        if row is not None and row[4]:
            # The job went dead and its stored usage is already in a failed-attempt
            # record (#252); resuming would count it a second time, so the batch is
            # submitted again instead.
            conn.execute(
                "DELETE FROM llm_batches WHERE job_id = %s AND step_key = %s", (job_id, step_key)
            )
            return None
    return _checkpoint(row[:4]) if row is not None else None


def insert_checkpoint(
    conn: psycopg.Connection[Any],
    job_id: int,
    step_key: str,
    batch_id: str,
    request_sha256: str,
) -> BatchCheckpoint:
    """Store ``batch_id`` for the step; if a row already exists, return that row instead."""
    with conn.transaction():
        row = conn.execute(
            f"""
            INSERT INTO llm_batches (job_id, step_key, batch_id, request_sha256)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (job_id, step_key) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            (job_id, step_key, batch_id, request_sha256),
        ).fetchone()
    if row is not None:
        return _checkpoint(row)
    existing = get_checkpoint(conn, job_id, step_key)
    if existing is None:
        raise LLMUnavailableError(f"checkpoint for step {step_key!r} changed under the insert")
    return existing


def record_usage(
    conn: psycopg.Connection[Any], job_id: int, step_key: str, usage: Usage, *, model: str = ""
) -> Usage:
    """Store ``usage`` on the row if it holds none yet; return what the row holds (#252).

    One statement decides: ``calls IS NULL`` means "results not fetched yet", so a
    second fetch of the same batch never overwrites the first. Without a row the
    given ``usage`` comes back.
    """
    with conn.transaction():
        row = conn.execute(
            f"""
            UPDATE llm_batches
            SET input_tokens = %s, output_tokens = %s, cache_read_tokens = %s,
                cache_write_tokens = %s, calls = %s, cost_usd = %s, model = %s
            WHERE job_id = %s AND step_key = %s AND calls IS NULL
            RETURNING {_USAGE_COLUMNS}
            """,
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.calls,
                usage.cost_usd,
                model,
                job_id,
                step_key,
            ),
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_USAGE_COLUMNS} FROM llm_batches WHERE job_id = %s AND step_key = %s",
                (job_id, step_key),
            ).fetchone()
    if row is None or row[4] is None:
        return usage
    return Usage(
        input_tokens=int(row[0]),
        output_tokens=int(row[1]),
        cache_read_tokens=int(row[2]),
        cache_write_tokens=int(row[3]),
        calls=int(row[4]),
        cost_usd=None if row[5] is None else float(row[5]),
    )


def delete_checkpoint(
    conn: psycopg.Connection[Any], job_id: int, step_key: str, *, video_id: str, model: str
) -> None:
    """Delete the step's row; usage it holds goes to a failed-attempt record (#252).

    The caller (``JobScope.forget``) only forgets a row whose results this attempt
    did not count, so the stored usage is billed and owned by nobody else.
    """
    with conn.transaction():
        conn.execute(
            f"""
            WITH gone AS (
                DELETE FROM llm_batches WHERE job_id = %(job_id)s AND step_key = %(step_key)s
                RETURNING job_id, usage_moved, {_USAGE_COLUMNS}
            )
            {_MOVE_USAGE_SQL}
            """,
            {"job_id": job_id, "step_key": step_key, "video_id": video_id, "model": model},
        )


def delete_job_checkpoints(conn: psycopg.Connection[Any], job_id: int) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM llm_batches WHERE job_id = %s", (job_id,))


@dataclass(frozen=True, slots=True)
class OrphanBatch:
    """A row whose job is over (#160) or gone (#251: ``job_id`` is None).

    ``expired``: older than ``expired_after``, by the DB clock.
    """

    job_id: int | None
    step_key: str
    batch_id: str
    expired: bool


def list_orphans(
    conn: psycopg.Connection[Any], *, limit: int, expired_after: timedelta
) -> list[OrphanBatch]:
    """Rows of ``dead`` / ``done`` jobs and rows whose job was deleted, oldest first.

    At most ``limit``. A deleted job leaves ``job_id`` NULL (#251, ``ON DELETE
    SET NULL``). Rows of ``pending`` and ``running`` jobs are never returned:
    #89's resume depends on them.
    """
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT b.job_id, b.step_key, b.batch_id, b.submitted_at < now() - %s
            FROM llm_batches b
            LEFT JOIN jobs j ON j.id = b.job_id
            WHERE b.job_id IS NULL OR j.state IN ('dead', 'done')
            ORDER BY b.submitted_at, b.job_id, b.step_key
            LIMIT %s
            """,
            (expired_after, limit),
        ).fetchall()
    return [OrphanBatch(job_id, step_key, batch_id, bool(expired)) for job_id, step_key, batch_id, expired in rows]


def delete_orphan(
    conn: psycopg.Connection[Any], job_id: int | None, step_key: str, batch_id: str
) -> bool:
    """Delete one orphan row: its job is ``dead`` / ``done``, or its ``job_id`` is NULL.

    One statement: the job's state is re-checked (and share-locked, so a
    concurrent ``retry_dead`` waits) in the same ``DELETE``. A row whose job
    was deleted after ``list_orphans`` (its ``job_id`` is now NULL) goes too,
    so the batch is not cancelled a second time. Returns whether a row went.

    No usage is handled here (#252): a trigger already moved a row's stored usage
    to ``analyze_failed_usage`` when its job went ``dead`` or was deleted, and a
    ``done`` job's usage is on its ``analyses`` row.
    """
    with conn.transaction():
        row = conn.execute(
            """
            DELETE FROM llm_batches b
            WHERE b.step_key = %s AND b.batch_id = %s
              AND (
                b.job_id IS NULL
                OR (
                  b.job_id = %s
                  AND EXISTS (
                    SELECT 1 FROM jobs j
                    WHERE j.id = b.job_id AND j.state IN ('dead', 'done')
                    FOR SHARE OF j
                  )
                )
              )
            RETURNING 1
            """,
            (step_key, batch_id, job_id),
        ).fetchone()
    return row is not None


def _checkpoint(row: Any) -> BatchCheckpoint:
    step_key, batch_id, request_sha256, submitted_at = row
    return BatchCheckpoint(
        step_key=step_key,
        batch_id=batch_id,
        request_sha256=request_sha256,
        submitted_at=submitted_at,
    )
