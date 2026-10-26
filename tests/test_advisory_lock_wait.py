"""The advisory-lock release wait, in fake time (#305). No Postgres."""

from __future__ import annotations

import pytest

from tests.advisory_lock_wait import wait_until_advisory_lock_released


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(clock: FakeClock, rows: list[list[int]]) -> None:
    remaining = list(rows)

    def probe() -> list[int]:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    wait_until_advisory_lock_released(
        probe,
        namespace=0x59545044,
        task_id=1,
        holder_pid=1234,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def test_a_lock_free_on_the_first_probe_returns_without_sleeping() -> None:
    clock = FakeClock()

    _wait(clock, [[]])

    assert clock.sleeps == []


def test_a_lock_free_on_the_third_probe_returns_after_two_sleeps() -> None:
    clock = FakeClock()

    _wait(clock, [[1234], [1234], []])

    assert clock.sleeps == [0.02, 0.02]


def test_a_lock_that_is_never_free_fails_at_the_deadline_with_a_clear_message() -> None:
    clock = FakeClock()

    with pytest.raises(AssertionError) as excinfo:
        _wait(clock, [[1234]])

    assert str(excinfo.value) == (
        "advisory lock (0x59545044, 1) still held by backend 1234 5.0 s after holder.close()"
    )
    assert clock.now == pytest.approx(5.0)
