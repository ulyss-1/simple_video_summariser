"""Redacted ``last_error`` reaches the database (issue #196); sentinels only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from common.repo import planner_runs
from services.planner.scheduler import Scheduler, Task
from tests.common.test_redact import SENTINELS
from tests.services.planner.test_scheduler import FakeLocks

pytestmark = pytest.mark.integration


class _RecordingToDb:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def task_started(self, name: str, *, interval_sec: float, at: datetime) -> None:
        planner_runs.record_started(self._conn, name, interval_sec=interval_sec, at=at)

    def task_finished(self, name: str, *, interval_sec: float, at: datetime, outcome: Any,
                      error: str | None, count: int | None) -> None:
        planner_runs.record_finished(
            self._conn, name, interval_sec=interval_sec, at=at,
            outcome=outcome, error=error, count=count,
        )

    def task_skipped_locked(self, name: str, *, interval_sec: float, at: datetime) -> None:
        planner_runs.record_skipped_locked(self._conn, name, interval_sec=interval_sec, at=at)


def _run_failing(tmp_path: Path, recorder: Any, message: str) -> None:
    def boom() -> int:
        raise RuntimeError(message)

    Scheduler(
        [Task("a", 60, boom)], FakeLocks(), clock=lambda: 0.0, sleep=lambda _s: None,
        liveness_path=tmp_path / "hb", install_signal_handlers=False, recorder=recorder,
        wall_clock=lambda: datetime(2026, 10, 10, tzinfo=UTC),
    ).run_once()


@pytest.mark.integration
@pytest.mark.parametrize("kind", SENTINELS)
def test_planner_task_runs_row_never_holds_the_secret(
    kind: str, tmp_path: Path, conn: psycopg.Connection[Any]
) -> None:
    text, secret = SENTINELS[kind]

    _run_failing(tmp_path, _RecordingToDb(conn), text)

    (row,) = planner_runs.list_task_runs(conn)
    assert row.last_error is not None
    assert secret not in row.last_error
