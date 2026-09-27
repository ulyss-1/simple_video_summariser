"""Tests for ``common.worker`` (issue #13, architecture.md §1, §4, §5, §11.5).

Everything here drives ``Worker`` through its public surface -
``run(max_iterations=...)`` and ``request_shutdown()`` - against a
``FakeQueue`` that reimplements just enough of ``PostgresQueue.claim``'s
contract (issue #11: plain exceptions are swallowed and recorded,
``Cancelled``/``KeyboardInterrupt``/``SystemExit`` are recorded then
re-raised) to exercise the loop without a database.

The heartbeat tick and the shutdown-grace deadline are both driven through
an injected ``timer_factory``: a ``FakeTimerFactory`` hands back a handle
whose scheduled callback the test fires by hand, so heartbeat and grace
tests need no real waiting at all (`poll_sec` idle-wait tests inject a
`sleep` spy for the same reason). The one exception is
``test_real_sigterm_stops_an_idle_worker``, which sends an actual
``SIGTERM`` to a subprocess to prove the signal handler is really
installed; a pipe (the child's stdout) makes that test wait on readiness,
not on a sleep.
"""

from __future__ import annotations

import random
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from common.errors import Cancelled
from common.queue import Job
from common.worker import JobContext, Worker

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


def make_job(**overrides: Any) -> Job:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    defaults: dict[str, Any] = {
        "id": 1,
        "video_id": "v1",
        "kind": "ingest",
        "dedupe_key": "default",
        "state": "running",
        "priority": 0,
        "payload": {},
        "attempts": 1,
        "last_error": None,
        "error_class": None,
        "run_after": now,
        "locked_by": "w1",
        "locked_at": now,
        "heartbeat_at": now,
        "finished_at": None,
        "created_at": now,
    }
    defaults.update(overrides)
    return Job(**defaults)


class FakeQueue:
    """A ``JobQueue`` double that mimics ``PostgresQueue.claim``'s outcome
    handling (issue #11) closely enough to test the worker loop: a plain
    exception raised inside the ``with`` block is recorded and swallowed;
    ``Cancelled``/``KeyboardInterrupt``/``SystemExit`` are recorded and
    re-raised.
    """

    def __init__(self, script: Sequence[Job | BaseException | None] = ()) -> None:
        self._script = list(script)
        self.claim_calls: list[tuple[tuple[str, ...], str]] = []
        self.outcomes: list[tuple[Job, str]] = []
        self.heartbeat_calls: list[tuple[int, str]] = []
        self.heartbeat_result: bool | Callable[[], bool] | Exception = True

    @contextmanager
    def claim(self, kinds: Sequence[str], *, worker: str) -> Iterator[Job | None]:
        self.claim_calls.append((tuple(kinds), worker))
        if not self._script:
            yield None
            return
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item is None:
            yield None
            return
        job = item
        try:
            yield job
        except (Cancelled, KeyboardInterrupt, SystemExit):
            self.outcomes.append((job, "pending"))
            raise
        except Exception:  # noqa: BLE001 - mirrors PostgresQueue.claim's safety net
            self.outcomes.append((job, "failed"))
        else:
            self.outcomes.append((job, "done"))

    def heartbeat(self, job_id: int, worker: str) -> bool:
        self.heartbeat_calls.append((job_id, worker))
        result = self.heartbeat_result
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        return result

    def enqueue(
        self,
        kind: str,
        video_id: str,
        *,
        dedupe_key: str = "default",
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        run_after: datetime | None = None,
    ) -> int | None:
        raise NotImplementedError("unused by worker tests")

    def reap_stale(self, older_than_sec: int | None = None) -> int:
        raise NotImplementedError("unused by worker tests")


class FakeTimerHandle:
    def __init__(self, function: Callable[[], None]) -> None:
        self.function = function
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function()


class FakeTimerFactory:
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, FakeTimerHandle]] = []

    def __call__(self, interval: float, function: Callable[[], None]) -> FakeTimerHandle:
        handle = FakeTimerHandle(function)
        self.scheduled.append((interval, handle))
        return handle

    @property
    def last(self) -> FakeTimerHandle:
        return self.scheduled[-1][1]


def make_worker(
    queue: FakeQueue,
    handler: Callable[[Job, JobContext], None],
    *,
    heartbeat_queue: FakeQueue | None = None,
    liveness_path: Path,
    poll_sec: float = 5,
    heartbeat_sec: float = 60,
    shutdown_grace_sec: float = 20,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
    timer_factory: FakeTimerFactory | None = None,
) -> Worker:
    return Worker(
        "w1",
        ["ingest"],
        handler,
        queue,
        poll_sec=poll_sec,
        liveness_path=liveness_path,
        heartbeat_sec=heartbeat_sec,
        shutdown_grace_sec=shutdown_grace_sec,
        heartbeat_queue_factory=(lambda: heartbeat_queue) if heartbeat_queue is not None else (lambda: queue),
        sleep=sleep if sleep is not None else (lambda _s: None),
        rng=rng if rng is not None else random.Random(0),
        timer_factory=timer_factory if timer_factory is not None else FakeTimerFactory(),
        install_signal_handlers=False,
    )


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def test_run_claims_runs_handler_and_repeats(tmp_path: Path) -> None:
    job1, job2 = make_job(id=1), make_job(id=2)
    queue = FakeQueue([job1, job2])
    seen: list[int] = []

    worker = make_worker(queue, lambda job, ctx: seen.append(job.id), liveness_path=tmp_path / "hb")
    worker.run(max_iterations=2)

    assert seen == [1, 2]
    assert queue.outcomes == [(job1, "done"), (job2, "done")]


def test_idle_wait_uses_poll_sec_with_up_to_20_percent_jitter(tmp_path: Path) -> None:
    sleeps: list[float] = []
    queue = FakeQueue([None])
    worker = make_worker(
        queue,
        lambda job, ctx: None,
        liveness_path=tmp_path / "hb",
        poll_sec=10,
        sleep=sleeps.append,
        rng=random.Random(42),
    )
    worker.run(max_iterations=1)

    assert len(sleeps) == 1
    assert 8.0 <= sleeps[0] <= 12.0


def test_each_job_runs_inside_bound_context_that_is_gone_after(tmp_path: Path) -> None:
    job = make_job(id=7, video_id="abc", kind="ingest", attempts=3)
    queue = FakeQueue([job])
    captured: dict[str, Any] = {}

    def handler(job: Job, ctx: JobContext) -> None:
        captured.update(structlog.contextvars.get_contextvars())

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb")
    worker.run(max_iterations=1)

    assert captured == {"job_id": 7, "video_id": "abc", "kind": "ingest", "attempt": 3}
    assert structlog.contextvars.get_contextvars() == {}


def test_handler_that_raises_does_not_stop_the_loop(tmp_path: Path) -> None:
    job1, job2 = make_job(id=1), make_job(id=2)
    queue = FakeQueue([job1, job2])
    seen: list[int] = []

    def handler(job: Job, ctx: JobContext) -> None:
        seen.append(job.id)
        if job.id == 1:
            raise ValueError("boom")

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb")
    worker.run(max_iterations=2)

    assert seen == [1, 2]
    assert queue.outcomes == [(job1, "failed"), (job2, "done")]


def test_database_unreachable_backs_off_up_to_60s_and_carries_on(tmp_path: Path) -> None:
    queue = FakeQueue([ConnectionError("db down"), ConnectionError("still down"), make_job(id=9)])
    sleeps: list[float] = []
    seen: list[int] = []

    worker = make_worker(
        queue,
        lambda job, ctx: seen.append(job.id),
        liveness_path=tmp_path / "hb",
        sleep=sleeps.append,
    )
    worker.run(max_iterations=3)

    assert seen == [9]
    assert sleeps == [1.0, 2.0]  # doubles each failure, from _INITIAL_DB_BACKOFF_SEC


def test_database_backoff_is_capped_at_max_db_backoff_sec(tmp_path: Path) -> None:
    queue = FakeQueue([ConnectionError("x")] * 10 + [None])
    sleeps: list[float] = []
    worker = Worker(
        "w1",
        ["ingest"],
        lambda job, ctx: None,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        shutdown_grace_sec=20,
        sleep=sleeps.append,
        max_db_backoff_sec=8,
        install_signal_handlers=False,
        heartbeat_queue_factory=lambda: queue,
    )
    worker.run(max_iterations=10)

    assert max(sleeps) <= 8


def test_liveness_file_touched_on_every_loop_iteration(tmp_path: Path) -> None:
    liveness = tmp_path / "hb"
    queue = FakeQueue([None, None])
    worker = make_worker(queue, lambda job, ctx: None, liveness_path=liveness)
    worker.run(max_iterations=2)

    assert liveness.exists()


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------


def test_heartbeat_ticks_on_a_separate_queue_while_handler_runs(tmp_path: Path) -> None:
    job = make_job(id=5)
    queue = FakeQueue([job])
    hb_queue = FakeQueue()
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        timer_factory.last.fire()
        timer_factory.last.fire()

    worker = make_worker(
        queue,
        handler,
        heartbeat_queue=hb_queue,
        liveness_path=tmp_path / "hb",
        timer_factory=timer_factory,
    )
    worker.run(max_iterations=1)

    assert hb_queue.heartbeat_calls == [(5, "w1"), (5, "w1")]
    assert queue.heartbeat_calls == []  # never on the claim connection


def test_heartbeat_scheduled_at_heartbeat_sec_interval(tmp_path: Path) -> None:
    queue = FakeQueue([make_job()])
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        pass

    worker = make_worker(
        queue, handler, liveness_path=tmp_path / "hb", heartbeat_sec=42, timer_factory=timer_factory
    )
    worker.run(max_iterations=1)

    assert timer_factory.scheduled[0][0] == 42


def test_heartbeat_stops_within_one_tick_after_handler_returns(tmp_path: Path) -> None:
    job = make_job()
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()

    worker = make_worker(queue, lambda job, ctx: None, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run(max_iterations=1)

    assert timer_factory.last.cancelled is True
    # Firing the (already-cancelled) handle after the fact must not send a
    # heartbeat: no heartbeat is sent after the job's outcome is written.
    calls_before = len(queue.heartbeat_calls)
    timer_factory.last.fire()
    assert len(queue.heartbeat_calls) == calls_before


def test_heartbeat_returning_false_cancels_the_job_and_stops_ticking(tmp_path: Path) -> None:
    job = make_job(id=3)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()
    seen_cancelled: list[bool] = []

    def handler(job: Job, ctx: JobContext) -> None:
        queue.heartbeat_result = False
        timer_factory.last.fire()
        seen_cancelled.append(ctx.cancelled)

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run(max_iterations=1)

    assert seen_cancelled == [True]
    # Reaped: heartbeat stopped rescheduling itself (only the manual fire happened).
    assert len(queue.heartbeat_calls) == 1


def test_heartbeat_failure_is_logged_and_retried_next_tick(tmp_path: Path) -> None:
    job = make_job(id=4)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()
    tick_count = 0

    def handler(job: Job, ctx: JobContext) -> None:
        nonlocal tick_count
        queue.heartbeat_result = ConnectionError("blip")
        timer_factory.last.fire()  # tick 1: fails
        tick_count += 1
        queue.heartbeat_result = True
        timer_factory.last.fire()  # tick 2: succeeds, rescheduled by tick 1
        tick_count += 1
        assert not ctx.cancelled

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run(max_iterations=1)

    assert tick_count == 2
    assert len(queue.heartbeat_calls) == 2


def test_liveness_file_touched_on_heartbeat_tick(tmp_path: Path) -> None:
    liveness = tmp_path / "hb"
    job = make_job()
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        liveness.unlink()  # prove the *tick*, not job start, recreates it
        assert not liveness.exists()
        timer_factory.last.fire()
        assert liveness.exists()

    worker = make_worker(queue, handler, liveness_path=liveness, timer_factory=timer_factory)
    worker.run(max_iterations=1)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_request_shutdown_while_idle_stops_the_loop_without_a_new_claim(tmp_path: Path) -> None:
    queue = FakeQueue([make_job()])  # would be claimable if the loop ran again
    worker = make_worker(queue, lambda job, ctx: None, liveness_path=tmp_path / "hb")

    worker.request_shutdown()
    worker.run()  # no max_iterations: must return on its own

    assert queue.claim_calls == []


def test_shutdown_signaled_during_idle_wait_stops_within_one_poll(tmp_path: Path) -> None:
    queue = FakeQueue([None])
    worker = make_worker(queue, lambda job, ctx: None, liveness_path=tmp_path / "hb", poll_sec=5)
    # Simulate the signal arriving while the idle wait is in progress.
    worker._sleep = lambda _s: worker.request_shutdown()

    worker.run()  # unbounded; must still return promptly

    assert queue.claim_calls == [(("ingest",), "w1")]


def test_shutdown_while_handler_runs_arms_grace_and_finishing_in_time_exits_clean(tmp_path: Path) -> None:
    job = make_job(id=11)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()  # signal arrives mid-handler
        # ... handler finishes its work before the grace period expires ...

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory, shutdown_grace_sec=20)
    worker.run()

    assert queue.outcomes == [(job, "done")]
    intervals = [interval for interval, _handle in timer_factory.scheduled]
    assert 20 in intervals  # grace timer armed
    assert all(handle.cancelled for _interval, handle in timer_factory.scheduled)


def test_grace_expiry_cancels_and_job_is_released_to_pending_without_spending_an_attempt(
    tmp_path: Path,
) -> None:
    job = make_job(id=12)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()
        timer_factory.last.fire()  # grace period expires
        ctx.check_cancelled()  # cooperative handler: raises Cancelled

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run()  # must return normally (process exit 0), not raise

    assert queue.outcomes == [(job, "pending")]


def test_handler_ignoring_cancellation_keeps_running_worker_does_not_kill_it(tmp_path: Path) -> None:
    job = make_job(id=13)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()
    finished: list[bool] = []

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()
        timer_factory.last.fire()  # grace expires; ctx.cancelled is now True
        assert ctx.cancelled
        finished.append(True)  # handler ignores it and completes anyway

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run()

    assert finished == [True]
    assert queue.outcomes == [(job, "done")]


def test_second_signal_during_grace_cancels_immediately(tmp_path: Path) -> None:
    job = make_job(id=14)
    queue = FakeQueue([job])
    timer_factory = FakeTimerFactory()

    def handler(job: Job, ctx: JobContext) -> None:
        worker.request_shutdown()
        assert not ctx.cancelled
        assert timer_factory.last.cancelled is False
        worker.request_shutdown()  # second signal: cancel now, don't wait out the timer
        assert ctx.cancelled
        assert timer_factory.last.cancelled is True

    worker = make_worker(queue, handler, liveness_path=tmp_path / "hb", timer_factory=timer_factory)
    worker.run()


def test_run_returns_after_max_iterations_even_without_shutdown(tmp_path: Path) -> None:
    queue = FakeQueue([None, None, None])
    worker = make_worker(queue, lambda job, ctx: None, liveness_path=tmp_path / "hb")
    worker.run(max_iterations=2)

    assert len(queue.claim_calls) == 2


# ---------------------------------------------------------------------------
# Real SIGTERM (subprocess)
# ---------------------------------------------------------------------------


def test_real_sigterm_stops_an_idle_worker(tmp_path: Path) -> None:
    liveness = tmp_path / "heartbeat"
    script = tmp_path / "run_worker.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from contextlib import contextmanager
            from pathlib import Path
            from common.worker import Worker

            class IdleQueue:
                def heartbeat(self, job_id, worker):
                    return True

                @contextmanager
                def claim(self, kinds, *, worker):
                    yield None

            worker = Worker(
                "w1", ["ingest"], lambda job, ctx: None, IdleQueue(),
                poll_sec=0.2, liveness_path=Path({str(liveness)!r}),
                heartbeat_sec=60, shutdown_grace_sec=20,
                heartbeat_queue_factory=lambda: IdleQueue(),
            )
            worker._install_signals()  # avoid a race between READY and run()'s own install
            print("READY", flush=True)
            worker.run()
            print("DONE", flush=True)
            """
        )
    )

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "READY"

        proc.terminate()  # SIGTERM
        returncode = proc.wait(timeout=5)
        remaining_output = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert returncode == 0, remaining_output
    assert "DONE" in remaining_output
