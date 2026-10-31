"""Failed-attempt usage and batch usage storage against Postgres (issue #252)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from common.models import Usage
from common.repo.analyses import token_usage_by_model, total_cost_usd
from common.repo.failed_usage import insert_failed_usage
from common.repo.llm_batches import (
    delete_checkpoint,
    delete_orphan,
    insert_checkpoint,
    record_usage,
)
from tests.common.repo.test_metrics_aggregates import _analysis, _transcript

pytestmark = pytest.mark.integration

B1 = Usage(
    input_tokens=100,
    output_tokens=10,
    cache_read_tokens=5,
    cache_write_tokens=7,
    calls=2,
    cost_usd=0.5,
)
FAILED_SQL = (
    "SELECT video_id, job_id, model, outcome, input_tokens, output_tokens, cache_read_tokens, "
    "cache_write_tokens, calls, cost_usd FROM analyze_failed_usage ORDER BY id"
)


def _job(conn: psycopg.Connection[Any], state: str, n: int = 0) -> tuple[int, str]:
    vid = f"fu{n:09d}"
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (vid,))
    row = conn.execute(
        "INSERT INTO jobs (kind, video_id, dedupe_key, state) VALUES ('analyze', %s, 'k', %s) RETURNING id",
        (vid, state),
    ).fetchone()
    assert row is not None
    return int(row[0]), vid


def _failed(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return [
        (*r[:9], None if r[9] is None else float(r[9]))
        for r in conn.execute(FAILED_SQL).fetchall()
    ]


def test_the_first_stored_usage_wins_and_a_second_fetch_returns_it_without_overwriting(
    conn: psycopg.Connection[Any],
) -> None:
    job, _ = _job(conn, "running")
    insert_checkpoint(conn, job, "roster", "msgbatch_a", "sha")

    first = record_usage(conn, job, "roster", B1)
    second = record_usage(conn, job, "roster", Usage(input_tokens=999, calls=9))

    assert first == B1
    assert second == B1
    row = conn.execute("SELECT input_tokens, calls FROM llm_batches").fetchone()
    assert row == (100, 2)


def test_usage_without_a_row_comes_back_unchanged(conn: psycopg.Connection[Any]) -> None:
    job, _ = _job(conn, "running")

    assert record_usage(conn, job, "roster", B1) == B1


def test_forgetting_a_row_moves_its_stored_usage_to_a_failed_record(
    conn: psycopg.Connection[Any],
) -> None:
    job, vid = _job(conn, "running")
    insert_checkpoint(conn, job, "roster", "msgbatch_a", "sha")
    record_usage(conn, job, "roster", B1)
    insert_checkpoint(conn, job, "chunks", "msgbatch_b", "sha")  # results never fetched

    delete_checkpoint(conn, job, "roster", video_id=vid, model="claude")
    delete_checkpoint(conn, job, "chunks", video_id=vid, model="claude")

    assert _failed(conn) == [(vid, job, "claude", "failed", 100, 10, 5, 7, 2, 0.5)]
    assert conn.execute("SELECT count(*) FROM llm_batches").fetchone() == (0,)


def test_orphan_pass_rule_dead_moves_usage_done_and_deleted_job_do_not(
    conn: psycopg.Connection[Any],
) -> None:
    """Scenario E: only a dead job's stored usage goes to a failed record."""
    dead, dead_vid = _job(conn, "dead", 1)
    done, _ = _job(conn, "done", 2)
    gone, _ = _job(conn, "dead", 3)
    for job in (dead, done, gone):
        insert_checkpoint(conn, job, "roster", f"msgbatch_{job}", "sha")
        record_usage(conn, job, "roster", B1)
    conn.execute("DELETE FROM jobs WHERE id = %s", (gone,))  # its row keeps job_id NULL

    assert delete_orphan(conn, dead, "roster", f"msgbatch_{dead}", model="claude")
    assert delete_orphan(conn, done, "roster", f"msgbatch_{done}", model="claude")
    assert delete_orphan(conn, None, "roster", f"msgbatch_{gone}", model="claude")

    assert _failed(conn) == [(dead_vid, dead, "claude", "failed", 100, 10, 5, 7, 2, 0.5)]
    assert conn.execute("SELECT count(*) FROM llm_batches").fetchone() == (0,)


def test_an_orphan_row_whose_results_were_never_fetched_writes_no_record(
    conn: psycopg.Connection[Any],
) -> None:
    dead, _ = _job(conn, "dead")
    insert_checkpoint(conn, dead, "roster", "msgbatch_a", "sha")

    assert delete_orphan(conn, dead, "roster", "msgbatch_a", model="claude")

    assert _failed(conn) == []


def test_an_attempt_that_spent_nothing_writes_no_row(conn: psycopg.Connection[Any]) -> None:
    job, vid = _job(conn, "running")

    wrote = insert_failed_usage(
        conn, video_id=vid, job_id=job, model="m", outcome="failed", usage=Usage()
    )

    assert wrote is False
    assert _failed(conn) == []


def test_metrics_sum_analyses_and_failed_attempts_per_model_and_null_cost_adds_nothing(
    conn: psycopg.Connection[Any],
) -> None:
    job, vid = _job(conn, "running")
    _analysis(
        conn, vid, _transcript(conn, vid), model="claude", tokens=(10, 1), cost=0.25, usage=(2, 3, 4)
    )
    insert_failed_usage(
        conn,
        video_id=vid,
        job_id=job,
        model="claude",
        outcome="failed",
        usage=Usage(
            input_tokens=5,
            output_tokens=2,
            cache_read_tokens=1,
            cache_write_tokens=1,
            calls=1,
            cost_usd=0.5,
        ),
    )
    insert_failed_usage(  # unknown price: tokens and calls count, cost does not
        conn,
        video_id=vid,
        job_id=job,
        model="claude",
        outcome="cancelled",
        usage=Usage(input_tokens=1, calls=1, cost_usd=None),
    )
    insert_failed_usage(
        conn,
        video_id=vid,
        job_id=job,
        model="qwen",
        outcome="failed",
        usage=Usage(output_tokens=9, calls=1),
    )

    assert token_usage_by_model(conn) == {"claude": (16, 3, 3, 4, 6), "qwen": (0, 9, 0, 0, 1)}
    assert total_cost_usd(conn) == pytest.approx(0.75)
