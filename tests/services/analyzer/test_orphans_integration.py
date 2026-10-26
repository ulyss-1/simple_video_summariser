"""The orphan pass end to end (issue #160): real Postgres and queue, the real
``AnthropicSummarizer`` on a ``FakeTransport``. No network, no real key."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from adapters.summarize.anthropic import AnthropicSummarizer
from common.errors import BugError
from common.models import OrphanOutcome
from common.queue import PostgresQueue
from common.repo.llm_batches import insert_checkpoint
from services.analyzer.orphans import ORPHAN_EXPIRED_AFTER, PassCounts, run_pass
from tests.adapters.summarize.test_anthropic import (
    API,
    BATCH_ID,
    FakeTransport,
    fixture_response,
    settings,
)

pytestmark = pytest.mark.integration

DEAD_VIDEO = "aaaaaaaaaaa"
PENDING_VIDEO = "bbbbbbbbbbb"
OTHER_BATCH = "msgbatch_PENDING000000000000"


def batch_rows(conn: psycopg.Connection[Any]) -> list[tuple[int, str]]:
    conn.rollback()
    return [
        (int(r[0]), str(r[1]))
        for r in conn.execute("SELECT job_id, batch_id FROM llm_batches ORDER BY job_id").fetchall()
    ]


def test_a_dead_lettered_jobs_batch_is_cancelled_and_a_pending_jobs_is_not(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    for video in (DEAD_VIDEO, PENDING_VIDEO):
        conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video,))
    queue = PostgresQueue(conn)
    dead_id = queue.enqueue("analyze", DEAD_VIDEO, dedupe_key="v1:anthropic")
    pending_id = queue.enqueue("analyze", PENDING_VIDEO, dedupe_key="v1:anthropic")
    assert dead_id is not None and pending_id is not None
    conn.commit()

    # Claim the first job, record its in-progress batch, and fail it for good.
    with queue.claim(["analyze"], worker="w1", dedupe_key="v1:anthropic") as job:
        assert job is not None and job.id == dead_id
        insert_checkpoint(conn, job.id, "roster", BATCH_ID, "sha")
        conn.commit()
        raise BugError("non-retryable")  # claim() dead-letters it through _fail
    insert_checkpoint(conn, pending_id, "roster", OTHER_BATCH, "sha")
    conn.commit()
    state = conn.execute("SELECT state FROM jobs WHERE id = %s", (dead_id,)).fetchone()
    assert state == ("dead",)
    conn.rollback()

    transport = FakeTransport(fixture_response("batch_in_progress.json"))
    canceller = AnthropicSummarizer(settings(), batch=True, transport=transport)

    counts = run_pass(
        lambda: psycopg.connect(head_dsn),
        canceller,
        limit=5,
        expired_after=ORPHAN_EXPIRED_AFTER,
        stop=lambda: False,
    )

    assert counts is not None and counts.cancelled == 1
    assert [(r.method, r.url) for r in transport.requests] == [
        ("POST", f"{API}/v1/messages/batches/{BATCH_ID}/cancel")
    ]
    assert batch_rows(conn) == [(pending_id, OTHER_BATCH)]


def test_the_pass_cancels_the_batch_of_a_cancelled_job_but_not_of_a_running_one_with_a_request(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    """#237: a ``CANCELLED`` job's batch is cancelled by the orphan pass; a running one waits."""
    for video in (DEAD_VIDEO, PENDING_VIDEO):
        conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video,))
    ids = []
    for video, state, error_class in (
        (DEAD_VIDEO, "dead", "CANCELLED"),
        (PENDING_VIDEO, "running", None),
    ):
        row = conn.execute(
            "INSERT INTO jobs (video_id, kind, dedupe_key, state, error_class, locked_by, "
            "locked_at, finished_at, cancel_requested_at) VALUES (%s, 'analyze', 'v1:anthropic', "
            "%s, %s, CASE WHEN %s = 'running' THEN 'w1' END, "
            "CASE WHEN %s = 'running' THEN now() END, "
            "CASE WHEN %s = 'dead' THEN now() END, now()) RETURNING id",
            (video, state, error_class, state, state, state),
        ).fetchone()
        assert row is not None
        ids.append(int(row[0]))
    insert_checkpoint(conn, ids[0], "roster", BATCH_ID, "sha")
    insert_checkpoint(conn, ids[1], "roster", OTHER_BATCH, "sha")
    conn.commit()

    transport = FakeTransport(fixture_response("batch_in_progress.json"))
    canceller = AnthropicSummarizer(settings(), batch=True, transport=transport)

    counts = run_pass(
        lambda: psycopg.connect(head_dsn),
        canceller,
        limit=5,
        expired_after=ORPHAN_EXPIRED_AFTER,
        stop=lambda: False,
    )

    assert counts is not None and counts.cancelled == 1
    assert [(r.method, r.url) for r in transport.requests] == [
        ("POST", f"{API}/v1/messages/batches/{BATCH_ID}/cancel")
    ]
    assert batch_rows(conn) == [(ids[1], OTHER_BATCH)]


class ScriptedCanceller:
    """An ``OrphanBatchCanceller`` with a scripted outcome per batch id; records every call."""

    def __init__(self, outcomes: dict[str, OrphanOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[str] = []

    def cancel_orphan_batch(self, batch_id: str) -> OrphanOutcome:
        self.calls.append(batch_id)
        return self._outcomes[batch_id]


def _insert_job(
    conn: psycopg.Connection[Any], video: str, state: str, error_class: str | None
) -> int:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video,))
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, error_class, locked_by, "
        "locked_at, finished_at, cancel_requested_at) VALUES (%s, 'analyze', 'v1:anthropic', "
        "%s, %s, CASE WHEN %s = 'running' THEN 'w1' END, "
        "CASE WHEN %s = 'running' THEN now() END, "
        "CASE WHEN %s = 'dead' THEN now() END, now()) RETURNING id",
        (video, state, error_class, state, state, state),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_pass_acts_on_each_cancelled_jobs_row_once_by_its_outcome_and_skips_a_running_request(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    """#237 / #318: the real store over Postgres with a scripted canceller, three outcomes."""
    cancelled_batch = "msgbatch_CANCELLED00000000000"
    gone_batch = "msgbatch_NOTCANCELLABLE00000000"
    later_batch = "msgbatch_RETRYLATER0000000000"
    running_batch = "msgbatch_RUNNING00000000000000"
    dead = {
        cancelled_batch: _insert_job(conn, "aaaaaaaaaaa", "dead", "CANCELLED"),
        gone_batch: _insert_job(conn, "bbbbbbbbbbb", "dead", "CANCELLED"),
        later_batch: _insert_job(conn, "ccccccccccc", "dead", "CANCELLED"),
    }
    running_id = _insert_job(conn, "ddddddddddd", "running", None)
    for batch_id, job_id in {**dead, running_batch: running_id}.items():
        insert_checkpoint(conn, job_id, "roster", batch_id, "sha")
    conn.commit()
    canceller = ScriptedCanceller(
        {
            cancelled_batch: "cancelled",
            gone_batch: "not_cancellable",
            later_batch: "retry_later",
        }
    )
    kept = sorted([(dead[later_batch], later_batch), (running_id, running_batch)])

    def one_pass() -> PassCounts | None:
        return run_pass(
            lambda: psycopg.connect(head_dsn),
            canceller,
            limit=5,
            expired_after=ORPHAN_EXPIRED_AFTER,
            stop=lambda: False,
        )

    assert one_pass() == PassCounts(cancelled=1, not_cancellable=1, retry_later=1)
    assert sorted(canceller.calls) == sorted([cancelled_batch, gone_batch, later_batch])
    assert batch_rows(conn) == kept

    assert one_pass() == PassCounts(retry_later=1)
    assert sorted(canceller.calls) == sorted(
        [cancelled_batch, gone_batch, later_batch, later_batch]
    )  # the second pass called only the retry_later row, once
    assert running_batch not in canceller.calls
    assert batch_rows(conn) == kept
