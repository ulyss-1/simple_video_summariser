"""The in-process ``JobQueue`` the CLI runs its jobs on (issue #31)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.errors import Defer
from common.queue import Job, JobQueue, PostgresQueue
from services.cli.inprocess import InProcessQueue

VID = "iG9CE55wbtY"


def claim_one(
    queue: InProcessQueue, kinds: tuple[str, ...] = ("ingest", "transcribe", "analyze")
) -> Job | None:
    with queue.claim(kinds, worker="cli") as job:
        return job


def test_enqueue_returns_distinct_ids_and_claim_hands_back_the_job_fields() -> None:
    queue = InProcessQueue()
    first = queue.enqueue("ingest", VID, priority=10, payload={"origin": "adhoc"})
    second = queue.enqueue("analyze", VID, dedupe_key="v1:ollama", priority=10)
    assert first is not None and second is not None and first != second

    job = claim_one(queue)
    assert job is not None
    assert (job.id, job.video_id, job.kind, job.priority) == (first, VID, "ingest", 10)
    assert job.dedupe_key == "default"
    assert job.payload == {"origin": "adhoc"}
    assert job.state == "running"
    assert job.attempts == 1


def test_handoff_uses_the_greater_of_the_running_parent_and_supplied_priority() -> None:
    queue = InProcessQueue()
    parent_id = queue.enqueue("ingest", VID, priority=12)
    assert parent_id is not None

    with queue.claim(["ingest"], worker="cli") as parent:
        assert parent is not None
        successor_id = queue.handoff(
            parent.id, "transcribe", VID, priority=10, payload={"language": "en"}
        )

    successor = claim_one(queue, ("transcribe",))
    assert successor is not None
    assert successor.id == successor_id
    assert successor.priority == 12
    assert successor.payload == {"language": "en"}


def test_jobs_run_in_fifo_order_whatever_their_priority() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID, priority=0)
    queue.enqueue("transcribe", VID, priority=99)
    queue.enqueue("analyze", VID, dedupe_key="k", priority=10)
    kinds = []
    while (job := claim_one(queue)) is not None:
        kinds.append(job.kind)
    assert kinds == ["ingest", "transcribe", "analyze"]


def test_claim_returns_none_when_the_queue_is_empty() -> None:
    assert claim_one(InProcessQueue()) is None


def test_claim_only_hands_out_the_kinds_asked_for() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID)
    assert claim_one(queue, ("analyze",)) is None
    job = claim_one(queue, ("ingest",))
    assert job is not None and job.kind == "ingest"


def test_a_claimed_job_is_not_handed_out_again_after_a_clean_exit() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID)
    assert claim_one(queue) is not None
    assert claim_one(queue) is None


def test_a_duplicate_kind_and_dedupe_key_returns_none_while_the_first_is_pending() -> None:
    queue = InProcessQueue()
    assert queue.enqueue("analyze", VID, dedupe_key="v1:ollama") is not None
    assert queue.enqueue("analyze", VID, dedupe_key="v1:ollama") is None
    assert queue.enqueue("analyze", VID, dedupe_key="v1:anthropic") is not None
    assert queue.enqueue("ingest", VID, dedupe_key="v1:ollama") is not None


def test_a_duplicate_of_the_running_job_returns_none_like_postgres() -> None:
    queue = InProcessQueue()
    queue.enqueue("analyze", VID, dedupe_key="k")
    with queue.claim(["analyze"], worker="cli") as job:
        assert job is not None
        assert queue.enqueue("analyze", VID, dedupe_key="k") is None


def test_a_finished_job_no_longer_blocks_the_same_kind_and_key() -> None:
    queue = InProcessQueue()
    queue.enqueue("analyze", VID, dedupe_key="k")
    assert claim_one(queue) is not None
    assert queue.enqueue("analyze", VID, dedupe_key="k") is not None


def test_a_raising_handler_propagates_and_the_job_is_not_retried() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID)
    with pytest.raises(RuntimeError, match="boom"), queue.claim(["ingest"], worker="cli"):
        raise RuntimeError("boom")
    assert claim_one(queue) is None


def test_defer_propagates_and_the_job_is_not_put_back() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID)
    until = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(Defer) as excinfo, queue.claim(["ingest"], worker="cli"):
        raise Defer(until)
    assert excinfo.value.until == until
    assert claim_one(queue) is None


def test_heartbeat_is_true_and_reap_stale_is_zero() -> None:
    queue = InProcessQueue()
    assert queue.heartbeat(1, "cli") is True
    assert queue.reap_stale() == 0
    assert queue.reap_stale(60) == 0


def test_pending_counts_jobs_not_yet_claimed() -> None:
    queue = InProcessQueue()
    assert queue.pending == 0
    queue.enqueue("ingest", VID)
    queue.enqueue("analyze", VID)
    assert queue.pending == 2
    claim_one(queue)
    assert queue.pending == 1


def test_a_dedupe_key_claims_only_a_job_with_exactly_that_key() -> None:
    queue = InProcessQueue()
    queue.enqueue("analyze", VID, dedupe_key="v1:ollama")
    second = queue.enqueue("analyze", "BZiu46G4ukc", dedupe_key="v2:ollama")

    with queue.claim(("analyze",), worker="cli", dedupe_key="v2:ollama") as job:
        assert job is not None and job.id == second
    assert claim_one(queue, ("analyze",)) is not None  # the v1 job was left pending


# -- the fuller JobQueue port (issue #201) ------------------------------------

# mypy checks that both implementations satisfy the port; no runtime assertion
# is needed, the annotated bindings are the guard.
_ports: list[JobQueue] = [InProcessQueue(), PostgresQueue.__new__(PostgresQueue)]


def test_force_pending_merges_force_and_raises_priority_of_a_pending_job() -> None:
    queue = InProcessQueue()
    job_id = queue.enqueue("analyze", VID, dedupe_key="k", payload={"a": 1}, priority=0)
    assert job_id is not None

    assert queue.force_pending(job_id, 10) is True

    job = claim_one(queue)
    assert job is not None
    assert job.payload == {"a": 1, "force": True}
    assert job.priority == 10


def test_force_pending_never_lowers_priority_and_misses_an_unknown_or_running_job() -> None:
    queue = InProcessQueue()
    job_id = queue.enqueue("analyze", VID, priority=20)
    assert job_id is not None
    assert queue.force_pending(job_id, 10) is True
    assert queue.force_pending(job_id + 99, 10) is False

    with queue.claim(("analyze",), worker="cli") as job:
        assert job is not None
        assert job.priority == 20
        assert queue.force_pending(job_id, 30) is False


def test_raise_priority_raises_only_the_videos_pending_jobs_below_the_priority() -> None:
    queue = InProcessQueue()
    low = queue.enqueue("ingest", VID, priority=0)
    high = queue.enqueue("analyze", VID, dedupe_key="k", priority=15)
    other = queue.enqueue("ingest", "OTHERVIDEO1", dedupe_key="o", priority=0)
    assert low is not None and high is not None and other is not None

    assert queue.raise_priority(VID, 10) == [low]

    first = claim_one(queue, ("ingest",))
    assert first is not None and first.id == low and first.priority == 10
    assert queue.raise_priority(VID, 10) == []


def test_fail_job_is_a_no_op_because_nothing_is_retried() -> None:
    queue = InProcessQueue()
    queue.enqueue("ingest", VID)
    job = claim_one(queue)
    assert job is not None
    queue.fail_job(job, "cli", RuntimeError("x"))
    assert queue.pending == 0
