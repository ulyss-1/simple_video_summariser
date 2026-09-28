"""Tests for the planner scheduler (issue #38).

Everything here drives ``Scheduler`` through its public API (``run``,
``run_once``, ``request_shutdown``) with fake tasks, a fake clock whose
``sleep`` only records, and a fake lock provider. Nothing sleeps for real and
nothing touches Postgres (the integration tests live in
``test_main_locks.py``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from services.planner.scheduler import Scheduler, Task

# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand.

    ``sleep`` records the request and, when ``advance_on_sleep`` is set,
    moves the clock by it, so a multi-iteration run sees time pass.
    """

    def __init__(self, advance_on_sleep: bool = False) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.advance_on_sleep = advance_on_sleep
        self.on_sleep: Callable[[float], None] | None = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.advance_on_sleep:
            self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeLocks:
    """A lock provider whose lock table the test can pre-fill."""

    def __init__(self) -> None:
        self.held_elsewhere: set[str] = set()
        self.held_by_us: set[str] = set()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.resets = 0
        self.fail_try_lock = 0
        self.fail_release = False

    def try_lock(self, name: str) -> bool:
        if self.fail_try_lock > 0:
            self.fail_try_lock -= 1
            raise psycopg.OperationalError("connection refused")
        if name in self.held_elsewhere:
            return False
        self.held_by_us.add(name)
        self.acquired.append(name)
        return True

    def release(self, name: str) -> None:
        self.held_by_us.discard(name)
        self.released.append(name)
        if self.fail_release:
            raise psycopg.OperationalError("server closed the connection")

    def reset(self) -> None:
        self.resets += 1
        self.held_by_us.clear()


class Harness:
    def __init__(self, tmp_path: Path, *, advance_on_sleep: bool = False) -> None:
        self.clock = FakeClock(advance_on_sleep)
        self.locks = FakeLocks()
        self.calls: list[str] = []
        self.liveness = tmp_path / "heartbeat"
        self.scheduler: Scheduler | None = None

    def task(
        self,
        name: str,
        interval: float,
        *,
        result: int | None = None,
        duration: float = 0.0,
        raises: BaseException | None = None,
        during: Callable[[], None] | None = None,
    ) -> Task:
        def fn() -> int | None:
            self.calls.append(name)
            if during is not None:
                during()
            self.clock.now += duration
            if raises is not None:
                raise raises
            return result

        return Task(name, interval, fn)

    def build(self, *tasks: Task) -> Scheduler:
        self.scheduler = Scheduler(
            list(tasks),
            self.locks,
            clock=self.clock,
            sleep=self.clock.sleep,
            liveness_path=self.liveness,
            install_signal_handlers=False,
        )
        return self.scheduler


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


def test_startup_runs_every_task_once_in_registry_order(h: Harness) -> None:
    sched = h.build(h.task("d", 3600), h.task("a", 60), h.task("c", 3600), h.task("b", 60))

    sched.run(max_iterations=1)

    assert h.calls == ["d", "a", "c", "b"]


def test_task_is_not_due_just_before_its_interval(h: Harness) -> None:
    sched = h.build(h.task("a", 60), h.task("b", 3600))
    sched.run(max_iterations=1)
    h.calls.clear()

    h.clock.now = 59.999
    sched.run(max_iterations=1)

    assert h.calls == []


def test_task_is_due_at_exactly_its_interval(h: Harness) -> None:
    sched = h.build(h.task("a", 60), h.task("b", 3600))
    sched.run(max_iterations=1)
    h.calls.clear()

    h.clock.now = 60.0
    sched.run(max_iterations=1)

    assert h.calls == ["a"]


def test_due_time_counts_from_the_last_start_not_the_last_finish(h: Harness) -> None:
    sched = h.build(h.task("a", 60, duration=10))
    sched.run(max_iterations=1)  # starts at 0, finishes at 10
    h.calls.clear()

    h.clock.now = 59.999
    sched.run(max_iterations=1)
    assert h.calls == []

    h.clock.now = 60.0
    sched.run(max_iterations=1)
    assert h.calls == ["a"]


def test_overrunning_task_runs_again_once_without_a_catch_up_burst(h: Harness) -> None:
    sched = h.build(h.task("a", 60, duration=150))
    sched.run(max_iterations=1)  # 0 -> 150, missed two intervals
    assert h.calls == ["a"]

    sched.run(max_iterations=1)  # due straight away, once
    assert h.calls == ["a", "a"]  # 150 -> 300

    h.clock.now = 300.0
    sched.run(max_iterations=1)
    assert h.calls == ["a", "a", "a"]


def test_second_task_starts_only_after_the_first_finishes(h: Harness) -> None:
    order: list[str] = []
    sched = h.build(
        h.task("a", 60, duration=5, during=lambda: order.append("a-start")),
        h.task("b", 60, during=lambda: order.append(f"b-start@{h.clock.now:g}")),
    )

    sched.run(max_iterations=1)

    assert order == ["a-start", "b-start@5"]


# ---------------------------------------------------------------------------
# isolation and failure paths
# ---------------------------------------------------------------------------


def test_a_raising_task_is_logged_with_class_and_traceback(h: Harness) -> None:
    sched = h.build(h.task("a", 60, raises=RuntimeError("boom")))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["task"] == "a"
    assert errors[0]["error_class"] == "RuntimeError"
    assert errors[0]["exc_info"]  # the traceback rides along


def test_a_raising_task_does_not_stop_the_other_tasks(h: Harness) -> None:
    sched = h.build(h.task("a", 60, raises=RuntimeError("boom")), h.task("b", 60))

    sched.run(max_iterations=1)

    assert h.calls == ["a", "b"]


def test_a_failed_task_waits_for_its_next_interval_instead_of_looping(h: Harness) -> None:
    sched = h.build(h.task("a", 60, raises=RuntimeError("boom")), h.task("b", 3600))
    sched.run(max_iterations=1)
    h.calls.clear()

    sched.run(max_iterations=1)
    h.clock.now = 59.999
    sched.run(max_iterations=1)
    assert h.calls == []

    h.clock.now = 60.0
    sched.run(max_iterations=1)
    assert h.calls == ["a"]


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3)])
def test_keyboard_interrupt_and_system_exit_are_not_swallowed(h: Harness, exc: BaseException) -> None:
    sched = h.build(h.task("a", 60, raises=exc), h.task("b", 60))

    with pytest.raises(type(exc)):
        sched.run(max_iterations=1)

    assert h.calls == ["a"]
    assert h.locks.released == ["a"]  # the lock does not leak


def test_database_outage_backs_off_doubling_up_to_60_seconds(h: Harness) -> None:
    h.locks.fail_try_lock = 8
    sched = h.build(h.task("a", 3600))

    slept_per_iteration: list[float] = []
    for _ in range(8):
        before = len(h.clock.sleeps)
        sched.run(max_iterations=1)
        slept_per_iteration.append(sum(h.clock.sleeps[before:]))

    assert slept_per_iteration == [1, 2, 4, 8, 16, 32, 60, 60]
    assert h.locks.resets == 8  # a fresh connection is opened each time
    assert h.calls == []  # nothing ran without its lock


def test_task_runs_once_the_database_is_back_and_backoff_starts_over(h: Harness) -> None:
    h.locks.fail_try_lock = 2
    sched = h.build(h.task("a", 60))
    sched.run(max_iterations=2)
    assert h.calls == []

    sched.run(max_iterations=1)
    assert h.calls == ["a"]

    h.locks.fail_try_lock = 1
    h.clock.now = 60.0
    before = len(h.clock.sleeps)
    sched.run(max_iterations=1)
    assert sum(h.clock.sleeps[before:]) == 1  # not 4: the success reset it


def test_an_outage_is_logged_and_does_not_raise(h: Harness) -> None:
    h.locks.fail_try_lock = 1
    sched = h.build(h.task("a", 60))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    (line,) = _events(logs, "planner.database_unavailable")
    assert line["log_level"] == "error"
    assert line["error_class"] == "OperationalError"


def test_a_failing_unlock_is_logged_and_does_not_mask_the_task(h: Harness) -> None:
    h.locks.fail_release = True
    sched = h.build(h.task("a", 60, result=1), h.task("b", 60))

    sched.run(max_iterations=1)

    assert h.calls == ["a", "b"]
    assert h.locks.resets >= 1


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------


def test_task_runs_while_holding_its_lock_and_releases_it(h: Harness) -> None:
    held_during: list[bool] = []
    sched = h.build(h.task("a", 60, during=lambda: held_during.append("a" in h.locks.held_by_us)))

    sched.run(max_iterations=1)

    assert held_during == [True]
    assert h.locks.released == ["a"]
    assert h.locks.held_by_us == set()


def test_lock_is_released_after_a_task_raises(h: Harness) -> None:
    sched = h.build(h.task("a", 60, raises=ValueError("nope")))

    sched.run(max_iterations=1)

    assert h.locks.acquired == ["a"]
    assert h.locks.released == ["a"]


def test_a_locked_task_is_skipped_with_a_warning(h: Harness) -> None:
    h.locks.held_elsewhere.add("a")
    sched = h.build(h.task("a", 60), h.task("b", 60))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    assert h.calls == ["b"]
    assert "a" not in h.locks.released  # never took it, so never unlocks it
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["task"] == "a"
    assert "another planner" in warnings[0]["event"]


def test_a_skipped_task_is_retried_at_its_next_interval(h: Harness) -> None:
    h.locks.held_elsewhere.add("a")
    sched = h.build(h.task("a", 60), h.task("b", 3600))
    sched.run(max_iterations=1)
    h.locks.held_elsewhere.clear()

    h.clock.now = 59.999
    sched.run(max_iterations=1)
    assert h.calls == ["b"]

    h.clock.now = 60.0
    sched.run(max_iterations=1)
    assert h.calls == ["b", "a"]


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


def _task_lines(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == "planner.task"]


def test_every_run_logs_one_info_line_with_task_duration_outcome_and_count(h: Harness) -> None:
    sched = h.build(h.task("a", 60, result=7, duration=0.25))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    (line,) = _task_lines(logs)
    assert line["log_level"] == "info"
    assert (line["task"], line["outcome"], line["duration_ms"], line["count"]) == ("a", "ok", 250, 7)


def test_a_none_result_is_logged_without_a_count(h: Harness) -> None:
    sched = h.build(h.task("a", 60, result=None))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    (line,) = _task_lines(logs)
    assert line["outcome"] == "ok"
    assert "count" not in line


def test_a_zero_count_is_still_logged(h: Harness) -> None:
    sched = h.build(h.task("a", 60, result=0))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    assert _task_lines(logs)[0]["count"] == 0


def test_failed_and_skipped_runs_log_their_outcome(h: Harness) -> None:
    h.locks.held_elsewhere.add("b")
    sched = h.build(h.task("a", 60, raises=RuntimeError("x")), h.task("b", 60))

    with capture_logs() as logs:
        sched.run(max_iterations=1)

    outcomes = {e["task"]: e["outcome"] for e in _task_lines(logs)}
    assert outcomes == {"a": "failed", "b": "skipped_locked"}


def test_liveness_file_is_touched_on_every_idle_iteration(tmp_path: Path) -> None:
    h = Harness(tmp_path, advance_on_sleep=True)
    sched = h.build(h.task("a", 3600))

    for _ in range(3):
        if h.liveness.exists():
            os.utime(h.liveness, (0, 0))
        sched.run(max_iterations=1)
        assert h.liveness.stat().st_mtime > 0


def test_idle_waits_are_sliced_to_at_most_30_seconds(tmp_path: Path) -> None:
    h = Harness(tmp_path, advance_on_sleep=True)
    sched = h.build(h.task("a", 3600))

    sched.run(max_iterations=121)  # 1 startup pass + the whole gap

    assert max(h.clock.sleeps) == 30
    assert h.calls.count("a") == 2  # startup, then due again exactly at 3600


def test_the_last_idle_slice_ends_exactly_when_the_task_is_due(h: Harness) -> None:
    sched = h.build(h.task("a", 60))
    sched.run(max_iterations=1)
    h.clock.now = 50.0
    h.clock.sleeps.clear()

    sched.run(max_iterations=1)

    assert h.clock.sleeps == [10.0]


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


def test_shutdown_while_idle_returns_within_one_sleep_slice(tmp_path: Path) -> None:
    h = Harness(tmp_path, advance_on_sleep=True)
    sched = h.build(h.task("a", 3600))
    h.clock.on_sleep = lambda _s: sched.request_shutdown()

    sched.run()  # would loop forever if the shutdown were ignored

    assert h.clock.sleeps == [30]
    assert h.calls == ["a"]


def test_shutdown_during_a_task_finishes_it_and_starts_nothing_else(h: Harness) -> None:
    holder: dict[str, Scheduler] = {}
    sched = h.build(
        h.task("a", 60, during=lambda: holder["s"].request_shutdown()),
        h.task("b", 60),
    )
    holder["s"] = sched

    sched.run()

    assert h.calls == ["a"]
    assert h.locks.released == ["a"]
    assert h.clock.sleeps == []  # no idle wait after the signal


def test_a_scheduler_that_was_asked_to_stop_runs_nothing(h: Harness) -> None:
    sched = h.build(h.task("a", 60))
    sched.request_shutdown()

    sched.run()

    assert h.calls == []


# ---------------------------------------------------------------------------
# --once support
# ---------------------------------------------------------------------------


def test_run_once_runs_every_task_once_in_registry_order(h: Harness) -> None:
    sched = h.build(h.task("b", 3600), h.task("a", 60))

    ok = sched.run_once()

    assert ok is True
    assert h.calls == ["b", "a"]
    assert h.clock.sleeps == []


def test_run_once_treats_a_skipped_locked_task_as_success(h: Harness) -> None:
    h.locks.held_elsewhere.add("a")
    sched = h.build(h.task("a", 60), h.task("b", 60))

    assert sched.run_once() is True
    assert h.calls == ["b"]


def test_run_once_reports_failure_when_any_task_raised(h: Harness) -> None:
    sched = h.build(h.task("a", 60, raises=RuntimeError("x")), h.task("b", 60))

    assert sched.run_once() is False
    assert h.calls == ["a", "b"]


def test_run_once_does_not_back_off_when_the_database_is_down(h: Harness) -> None:
    h.locks.fail_try_lock = 1
    sched = h.build(h.task("a", 60), h.task("b", 60))

    assert sched.run_once() is False
    assert h.clock.sleeps == []
