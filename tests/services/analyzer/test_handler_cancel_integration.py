"""A stored cancel request, through a real worker, to a dead/CANCELLED analyze job (issue #318).

One video, one job, one worker call. The handler is held at a deterministic checkpoint
(after the roster batch was recorded) by an ``Event``; the cancel is stored, one
heartbeat tick is fired by hand, and the handler is released. The orphan pass then
cancels the kept ``llm_batches`` row.
"""

from __future__ import annotations

import random
import threading
from typing import Any

import psycopg
import pytest

from common.models import OrphanOutcome
from common.queue import Job, PostgresQueue
from common.repo.jobs import cancel_video
from common.worker import JobContext, Worker
from services.analyzer.orphans import ORPHAN_EXPIRED_AFTER, PassCounts, run_pass
from tests.common.test_cancel_races import DEADLINE_SEC, Call, ManualTimers
from tests.services.analyzer.fakes import CheckpointingSummarizer
from tests.services.analyzer.test_handler import ROSTER, VIDEO, Rig

pytestmark = pytest.mark.integration

ROSTER_BATCH = "msgbatch_roster"


class HeldSummarizer(CheckpointingSummarizer):
    """Records the roster checkpoint, then waits for the test before returning."""

    def __init__(self, at_checkpoint: threading.Event, go_on: threading.Event) -> None:
        super().__init__(roster=ROSTER)
        self._at_checkpoint = at_checkpoint
        self._go_on = go_on

    def derive_roster(self, meta: Any, opening: str) -> Any:
        roster = super().derive_roster(meta, opening)
        self._at_checkpoint.set()
        assert self._go_on.wait(DEADLINE_SEC), "the analyzer was never released"
        return roster


class RecordingCanceller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cancel_orphan_batch(self, batch_id: str) -> OrphanOutcome:
        self.calls.append(batch_id)
        return "cancelled"


def count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    conn.rollback()
    assert row is not None
    return int(row[0])


def test_a_cancel_stored_while_the_analyzer_runs_ends_the_job_cancelled_keeps_its_batch_and_the_pass_cancels_it_once(
    conn: psycopg.Connection[Any], head_dsn: str, tmp_path: Any
) -> None:
    at_checkpoint = threading.Event()
    go_on = threading.Event()
    summarizer = HeldSummarizer(at_checkpoint, go_on)
    rig = Rig(conn, head_dsn, summarizer)
    admin = psycopg.connect(head_dsn, autocommit=True)
    hb = psycopg.connect(head_dsn, autocommit=True)
    queue = PostgresQueue(admin, rng=random.Random(1))
    job_id = queue.enqueue("analyze", VIDEO, dedupe_key="v1:fake")
    assert job_id is not None
    timers = ManualTimers()
    seen_ctx: list[JobContext] = []

    def handler(job: Job, ctx: JobContext) -> None:
        seen_ctx.append(ctx)
        rig.handler(job, ctx)

    worker = Worker(
        "w1",
        ["analyze"],
        handler,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        heartbeat_queue_factory=lambda: PostgresQueue(hb),
        runtime_limit_sec=lambda job: None,
        timer_factory=timers,
        install_signal_handlers=False,
        sleep=lambda seconds: None,
    )
    try:
        runner = Call(lambda: worker.run(max_iterations=1))
        assert at_checkpoint.wait(DEADLINE_SEC), "the analyzer never reached its checkpoint"
        batches = admin.execute("SELECT job_id, batch_id FROM llm_batches").fetchall()
        assert batches == [(job_id, ROSTER_BATCH)]

        assert [e.outcome for e in cancel_video(admin, VIDEO)] == ["cancel_requested"]
        timers.fire(60.0)  # one heartbeat tick delivers the request
        [ctx] = seen_ctx
        assert ctx._cancel_event.wait(DEADLINE_SEC), "the heartbeat tick never set ctx.cancelled"
        go_on.set()
        runner.result("the worker")

        stored = admin.execute(
            "SELECT state, error_class, attempts FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        assert stored == ("dead", "CANCELLED", 1)  # no refund, no retry
        assert admin.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('pending', 'running')"
        ).fetchone() == (0,)
        assert admin.execute("SELECT job_id, batch_id FROM llm_batches").fetchall() == batches
        assert count(conn, "analyses") == 0
        assert (count(conn, "topics"), count(conn, "claims"), count(conn, "quotes")) == (0, 0, 0)

        canceller = RecordingCanceller()
        counts = run_pass(
            lambda: psycopg.connect(head_dsn),
            canceller,
            limit=5,
            expired_after=ORPHAN_EXPIRED_AFTER,
            stop=lambda: False,
        )

        assert counts == PassCounts(cancelled=1)
        assert canceller.calls == [batches[0][1]]
        assert admin.execute("SELECT count(*) FROM llm_batches").fetchone() == (0,)
    finally:
        go_on.set()
        admin.close()
        hb.close()
