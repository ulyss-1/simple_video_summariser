"""Spend of failed and cancelled analyze attempts (issue #252, architecture.md 6).

``analyze_failed_usage`` holds usage that no ``analyses`` row carries. The write
is one statement in its own transaction.
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg

from common.models import Usage

type FailedOutcome = Literal["failed", "cancelled"]


def has_spend(usage: Usage) -> bool:
    return bool(
        usage.calls
        or usage.input_tokens
        or usage.output_tokens
        or usage.cache_read_tokens
        or usage.cache_write_tokens
    )


def insert_failed_usage(
    conn: psycopg.Connection[Any],
    *,
    video_id: str,
    job_id: int | None,
    model: str,
    outcome: FailedOutcome,
    usage: Usage,
) -> bool:
    """Record ``usage`` for an attempt that did not produce an analysis. False if it spent nothing."""
    if not has_spend(usage):
        return False
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO analyze_failed_usage
                (video_id, job_id, model, outcome, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, calls, cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                video_id,
                job_id,
                model,
                outcome,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.calls,
                usage.cost_usd,
            ),
        )
    return True
