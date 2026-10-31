"""Usage of failed and cancelled analyze attempts, counted exactly once (issue #252).

Real Postgres, the real handler, fake summarizers. No network, no real key. The
``BillingSummarizer`` follows the adapter's contract with ``JobScope`` (record the
batch, store its usage on the first fetch, count what the row holds) and keeps a
ledger of what the fake API billed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from common.errors import (
    BugError,
    Cancelled,
    LLMInvalidOutputError,
    LLMUnavailableError,
)
from common.models import Chunk, ChunkAnalysis, Roster, Usage, VideoMeta
from common.queue import PostgresQueue
from common.repo import llm_batches
from common.repo.analyses import token_usage_by_model
from services.analyzer import handler as handler_module
from tests.services.analyzer.fakes import (
    CheckpointingSummarizer,
    FakeSummarizer,
    make_job,
)
from tests.services.analyzer.test_handler import (
    VIDEO,
    Rig,
    rig_factory,  # noqa: F401 - the fixture
)

pytestmark = pytest.mark.integration

MODEL = "claude-fake"
FAILED_SQL = (
    "SELECT outcome, model, input_tokens, output_tokens, calls FROM analyze_failed_usage ORDER BY id"
)


def _job_row(rig: Rig, state: str = "running") -> int:
    row = rig.conn.execute(
        "INSERT INTO jobs (kind, video_id, dedupe_key, state, locked_by)"
        " VALUES ('analyze', %s, 'k', %s, 'w') RETURNING id",
        (VIDEO, state),
    ).fetchone()
    rig.conn.commit()
    assert row is not None
    return int(row[0])


def _run(rig: Rig, job_id: int, **job: Any) -> None:
    rig.handler(replace(make_job(VIDEO, **job), id=job_id), rig.ctx)


def _failed(rig: Rig) -> list[tuple[Any, ...]]:
    rows = rig.conn.execute(FAILED_SQL).fetchall()
    rig.conn.rollback()
    return rows


# --------------------------------------------------- recording a failed attempt


def test_a_call_that_raises_after_earlier_calls_leaves_one_row_with_exactly_their_usage(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = FakeSummarizer(model=MODEL, fail_on={"chunk:1": LLMUnavailableError("down")})
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)

    with pytest.raises(LLMUnavailableError, match="down"):
        _run(rig, job_id)

    # roster (7, 3) + chunk 0 (100, 50); the failed call added nothing
    assert _failed(rig) == [("failed", MODEL, 107, 53, 0)]
    assert rig.count("analyses") == 0


def test_a_user_cancel_is_recorded_as_cancelled_and_a_shutdown_cancel_too(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = FakeSummarizer(model=MODEL, fail_on={"chunk:1": Cancelled()})
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)

    with pytest.raises(Cancelled):
        _run(rig, job_id)

    assert _failed(rig) == [("cancelled", MODEL, 107, 53, 0)]


def test_a_call_cancelled_mid_flight_adds_nothing(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = rig_factory(FakeSummarizer(model=MODEL, fail_on={"roster": Cancelled()}))
    job_id = _job_row(rig)

    with pytest.raises(Cancelled):
        _run(rig, job_id)

    assert _failed(rig) == []


def test_an_attempt_that_spent_nothing_writes_no_row(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = rig_factory(FakeSummarizer(fail_on={"roster": LLMUnavailableError("down")}))
    job_id = _job_row(rig)

    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)

    assert _failed(rig) == []


def test_the_summarizer_is_drained_so_the_next_job_does_not_inherit_the_usage(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = FakeSummarizer(model=MODEL, fail_on={"chunk:1": LLMUnavailableError("down")})
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)

    assert summarizer.take_usage() == Usage()


def test_a_failing_write_logs_a_warning_and_the_original_error_is_raised(
    rig_factory: Callable[..., Rig],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> bool:
        raise psycopg.OperationalError("the write failed")

    monkeypatch.setattr(handler_module, "insert_failed_usage", broken)
    rig = rig_factory(FakeSummarizer(fail_on={"chunk:1": LLMInvalidOutputError("bad")}))
    job_id = _job_row(rig)

    with pytest.raises(LLMInvalidOutputError, match="bad"):
        _run(rig, job_id)

    assert [r["event"] for r in rig.logs.records("warning")] == [
        "could not record the usage of a failed analyze attempt"
    ]
    assert _failed(rig) == []


def test_the_aborted_transaction_of_the_job_connection_is_rolled_back_before_the_write(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    summarizer: CheckpointingSummarizer

    def break_transaction(kind: str) -> None:
        if kind == "chunk":
            summarizer.scope._conn.execute("SELECT 1/0")  # leaves the transaction aborted

    summarizer = CheckpointingSummarizer(model=MODEL, on_call=break_transaction)
    # the scope is only built for a JobScopedSummarizer, as the adapter is
    rig = Rig(conn, head_dsn, summarizer)
    job_id = _job_row(rig)

    with pytest.raises(psycopg.errors.DivisionByZero):
        _run(rig, job_id)

    assert _failed(rig) == [("failed", MODEL, 107 - 100, 53 - 50, 0)]


# ------------------------------------------- no double counting (the invariant)

#: What the fake API bills for each step (input, output); every figure distinct.
BILL = {
    "roster": Usage(input_tokens=11, output_tokens=1, calls=1),
    "chunks": Usage(input_tokens=220, output_tokens=2, calls=2),
    "reduce": Usage(input_tokens=3300, output_tokens=3, calls=1),
}
STEPS = ("roster", "chunks", "reduce")


@dataclass(slots=True)
class Ledger:
    """What the fake API billed: a batch when it is created, a sync call when it is made."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def bill(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.calls += usage.calls


class BillingSummarizer(FakeSummarizer):
    """A job-scoped, chunk-batch summarizer following the adapter's use of ``JobScope``.

    ``kinds`` says per step whether it is a checkpointed ``batch`` or a ``sync`` call.
    ``fail`` is ``(step, "before" | "after")``: raise before the step runs, or after its
    usage was counted. ``sha`` is the request digest this attempt computes.
    """

    def __init__(self, ledger: Ledger, kinds: dict[str, str]) -> None:
        super().__init__(name="anthropic", model=MODEL)
        self.ledger = ledger
        self.step_kinds = kinds
        self.fail: tuple[str, str] | None = None
        self.sha = "sha"
        self.scope: Any = None
        self.pending = Usage()
        self.batches = 0

    def begin_job(self, scope: Any) -> None:
        self.scope = scope

    def end_job(self) -> None:
        self.scope = None

    def _step(self, step: str) -> None:
        if self.fail == (step, "before"):
            raise LLMUnavailableError(f"{step} failed")
        usage = BILL[step]
        if self.step_kinds[step] == "sync":
            self.ledger.bill(usage)
        else:
            row = self.scope.checkpoint(step)
            if row is not None and row.request_sha256 != self.sha:
                self.scope.forget(step)  # the request changed: the old batch is discarded
                row = None
            if row is None:
                self.batches += 1
                self.scope.record(step, f"msgbatch_{step}_{self.batches}", self.sha)
                self.ledger.bill(usage)
            usage = self.scope.record_usage(step, usage)
        self.pending = Usage(
            input_tokens=self.pending.input_tokens + usage.input_tokens,
            output_tokens=self.pending.output_tokens + usage.output_tokens,
            calls=self.pending.calls + usage.calls,
        )
        if self.fail == (step, "after"):
            raise LLMUnavailableError(f"{step} failed after it was counted")

    def derive_roster(self, meta: VideoMeta, opening: str) -> Roster:
        self._step("roster")
        return Roster(())

    def analyze_chunks(
        self, chunks: Sequence[Chunk], roster: Roster, meta: VideoMeta
    ) -> list[ChunkAnalysis]:
        self._step("chunks")
        return [ChunkAnalysis(topics=(), claims=(), quotes=()) for _ in chunks]

    def reduce(self, partials: list[ChunkAnalysis], meta: VideoMeta) -> str:
        self._step("reduce")
        return "the tldr"

    def take_usage(self) -> Usage:
        usage, self.pending = self.pending, Usage()
        return usage


def _recorded(rig: Rig) -> tuple[int, int, int]:
    """Failed records + the analyses row + usage still on ``llm_batches`` rows (input, output, calls)."""
    row = rig.conn.execute(
        """
        SELECT COALESCE(SUM(i), 0), COALESCE(SUM(o), 0), COALESCE(SUM(c), 0) FROM (
            SELECT input_tokens i, output_tokens o, calls c FROM analyze_failed_usage
            UNION ALL SELECT input_tokens, output_tokens, calls FROM analyses
            UNION ALL SELECT input_tokens, output_tokens, calls FROM llm_batches
            WHERE NOT usage_moved
        ) t
        """
    ).fetchone()
    rig.conn.rollback()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def _kill(rig: Rig, job_id: int) -> None:
    """The queue's own dead transition: a non-retryable error through ``fail_job``."""
    job = replace(make_job(VIDEO), id=job_id)
    PostgresQueue(rig.conn).fail_job(job, "w", BugError("non-retryable"))
    rig.conn.commit()
    assert rig.scalar("SELECT state FROM jobs WHERE id = %s", job_id) == "dead"


def _metrics_tokens(rig: Rig) -> int:
    total = sum(v[0] for v in token_usage_by_model(rig.conn).values())
    rig.conn.rollback()
    return total


def _orphan_pass(rig: Rig) -> None:
    for orphan in llm_batches.list_orphans(rig.conn, limit=10, expired_after=timedelta(hours=24)):
        assert llm_batches.delete_orphan(rig.conn, orphan.job_id, orphan.step_key, orphan.batch_id)
    rig.conn.commit()


@dataclass(frozen=True, slots=True)
class Case:
    kinds: tuple[str, str, str]  # per step: "batch" or "sync"
    attempts: tuple[tuple[str, str] | None, ...]  # fail point per attempt; None = succeeds
    changed_at: int | None = None  # attempt index from which the request digest differs


ALL_BATCH = ("batch", "batch", "batch")
CASES = {
    "A: retry then success": Case(ALL_BATCH, (("reduce", "before"), None)),
    "A: two failures then success": Case(ALL_BATCH, (("reduce", "before"), ("chunks", "after"), None)),
    "A: failed right after the last fetch": Case(ALL_BATCH, (("reduce", "after"), None)),
    "B: retry then dead": Case(ALL_BATCH, (("reduce", "before"), ("reduce", "before"))),
    "B: dead at the first attempt": Case(ALL_BATCH, (("chunks", "after"),)),
    "C: sync spend only": Case(("sync", "sync", "sync"), (("reduce", "before"), ("reduce", "after"))),
    "C: sync spend then success": Case(("sync", "sync", "sync"), (("reduce", "before"), None)),
    "C: mixed": Case(("batch", "sync", "batch"), (("reduce", "before"), ("chunks", "after"), None)),
    "D: request changed then success": Case(ALL_BATCH, (("reduce", "before"), None), changed_at=1),
    "D: request changed then dead": Case(
        ALL_BATCH, (("reduce", "before"), ("reduce", "before")), changed_at=1
    ),
}


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
def test_every_billed_token_is_recorded_exactly_once_after_every_attempt(
    rig_factory: Callable[..., Rig],  # noqa: F811
    case: Case,
) -> None:
    ledger = Ledger()
    summarizer = BillingSummarizer(ledger, dict(zip(STEPS, case.kinds, strict=True)))
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    succeeded = False

    for number, fail in enumerate(case.attempts):
        summarizer.fail = fail
        summarizer.sha = "sha-changed" if case.changed_at is not None and number >= case.changed_at else "sha"
        if fail is None:
            _run(rig, job_id)
            succeeded = True
        else:
            with pytest.raises(LLMUnavailableError):
                _run(rig, job_id)
        assert _recorded(rig) == (ledger.input_tokens, ledger.output_tokens, ledger.calls), number

    if succeeded:
        analysis = rig.latest()
        # the analyses row is the full usage of one run of every step, resumed batches included
        assert analysis.input_tokens == sum(BILL[s].input_tokens for s in STEPS)
        assert analysis.calls == sum(BILL[s].calls for s in STEPS)
        assert rig.count("llm_batches") == 0
    else:
        _kill(rig, job_id)
        # at once, with the batch rows still there and no orphan pass: /metrics has it all
        assert _metrics_tokens(rig) == ledger.input_tokens
        assert _recorded(rig) == (ledger.input_tokens, ledger.output_tokens, ledger.calls)
        _orphan_pass(rig)
        assert rig.count("llm_batches") == 0
        assert _metrics_tokens(rig) == ledger.input_tokens


def test_scenario_a_the_first_failure_records_nothing_for_the_resumable_batches(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = BillingSummarizer(Ledger(), dict.fromkeys(STEPS, "batch"))
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    summarizer.fail = ("reduce", "before")

    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)

    assert _failed(rig) == []  # roster and chunks are still resumable
    stored = rig.scalar("SELECT count(*) FROM llm_batches WHERE calls IS NOT NULL")
    assert stored == 2


def test_scenario_d_the_discarded_batch_usage_moves_to_a_failed_record(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = BillingSummarizer(Ledger(), dict.fromkeys(STEPS, "batch"))
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    summarizer.fail = ("chunks", "before")
    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)
    summarizer.fail = ("reduce", "before")
    summarizer.sha = "sha-changed"

    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)

    # the first roster batch (11 in) was replaced; the new one sits on its row
    assert _failed(rig) == [("failed", MODEL, 11, 1, 1)]


def test_deleting_a_dead_job_with_a_surviving_video_keeps_the_batch_usage(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    ledger = Ledger()
    summarizer = BillingSummarizer(ledger, dict.fromkeys(STEPS, "batch"))
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    summarizer.fail = ("reduce", "before")
    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)
    _kill(rig, job_id)
    rig.conn.execute("UPDATE jobs SET error_class = 'CANCELLED' WHERE id = %s", (job_id,))
    rig.conn.commit()

    # purge_cancelled_jobs only deletes jobs of deleted videos; any job delete behaves alike
    rig.conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
    rig.conn.commit()
    _orphan_pass(rig)

    assert rig.count("jobs") == 0 and rig.count("llm_batches") == 0
    assert _metrics_tokens(rig) == ledger.input_tokens > 0


def test_a_dead_job_retried_by_hand_submits_again_and_does_not_recount_the_moved_usage(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    ledger = Ledger()
    summarizer = BillingSummarizer(ledger, dict.fromkeys(STEPS, "batch"))
    rig = rig_factory(summarizer)
    job_id = _job_row(rig)
    summarizer.fail = ("reduce", "before")
    with pytest.raises(LLMUnavailableError):
        _run(rig, job_id)
    _kill(rig, job_id)
    rig.conn.execute("UPDATE jobs SET state = 'running' WHERE id = %s", (job_id,))
    rig.conn.commit()
    summarizer.fail = None

    _run(rig, job_id)

    # roster and chunks were billed again (their old usage is in the failed record)
    assert _recorded(rig) == (ledger.input_tokens, ledger.output_tokens, ledger.calls)
