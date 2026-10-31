"""Settings, formatting and wiring of the sweep USD budget (issue #153). No Postgres."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import ValidationError

from common.config import Settings
from services.planner.main import format_sweep_plan
from services.planner.sweep import SweepPlan, plan_reanalysis_sweep

REPO = Path(__file__).resolve().parents[3]


def _settings(**values: Any) -> Settings:
    return Settings(**{"DATABASE_URL": "postgresql://u:p@h/db", **values})


def test_the_budget_defaults_to_two_and_a_half_dollars_and_accepts_zero() -> None:
    assert _settings().SWEEP_MAX_USD_ANTHROPIC == 2.50
    assert _settings(SWEEP_MAX_USD_ANTHROPIC="0").SWEEP_MAX_USD_ANTHROPIC == 0
    assert _settings(SWEEP_MAX_USD_ANTHROPIC="0.05").SWEEP_MAX_USD_ANTHROPIC == 0.05


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "-0.01", "lots", ""])
def test_a_non_finite_negative_or_malformed_budget_fails_at_startup(value: str) -> None:
    with pytest.raises(ValidationError, match="SWEEP_MAX_USD_ANTHROPIC"):
        _settings(SWEEP_MAX_USD_ANTHROPIC=value)


def test_env_example_lists_the_budget_next_to_the_job_cap() -> None:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "SWEEP_MAX_USD_ANTHROPIC=2.50" in text
    assert text.index("SWEEP_MAX_JOBS_ANTHROPIC=") < text.index("SWEEP_MAX_USD_ANTHROPIC=")


def _plan(**over: Any) -> SweepPlan:
    values: dict[str, Any] = {
        "prompt_version": "v2",
        "summarizer": "anthropic",
        "candidates": 7,
        "already_enqueued": 2,
        "cap": 5,
        "limit": 500,
        "would_enqueue_this_run": 3,
        "would_remain_after_cap": 4,
        "cap_stopped": True,
        "video_ids": ("a", "b", "c"),
        "cap_reason": "usd",
        "budget_usd": 2.5,
        "est_usd_committed": 1.2,
        "est_usd_this_run": 0.0227,
        "cost_per_char_samples": 12,
    }
    values.update(over)
    return SweepPlan(**values)


def test_the_dry_run_line_shows_dollars_samples_and_the_reason() -> None:
    assert format_sweep_plan(_plan()) == (
        "prompt_version=v2 summarizer=anthropic candidates=7 already_enqueued=2 cap=5 "
        "would_enqueue_this_run=3 would_remain_after_cap=4 est_usd_this_run=0.0227 "
        "est_usd_committed=1.2000 budget_usd=2.5 cost_per_char_samples=12 cap_reason=usd"
    )


def test_the_dry_run_line_says_unknown_without_an_estimate() -> None:
    line = format_sweep_plan(
        _plan(est_usd_this_run=None, est_usd_committed=None, cost_per_char_samples=0)
    )

    assert "est_usd_this_run=unknown est_usd_committed=unknown" in line
    assert "cost_per_char_samples=0" in line


def test_the_dry_run_line_for_a_free_summarizer_says_none() -> None:
    line = format_sweep_plan(
        _plan(
            summarizer="ollama",
            cap=None,
            cap_reason=None,
            cap_stopped=False,
            budget_usd=None,
            est_usd_committed=None,
            est_usd_this_run=None,
            cost_per_char_samples=None,
        )
    )

    assert line.endswith(
        "est_usd_this_run=none est_usd_committed=none budget_usd=none "
        "cost_per_char_samples=none cap_reason=none"
    )


class _NoCostQueryConn:
    """Answers only the three job-count queries; any other SQL fails the test."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a cost query ran for a summarizer that has no API cost")

    def commit(self) -> None:
        self.commits += 1


class _Cursor:
    def __init__(self, conn: _NoCostQueryConn) -> None:
        self.conn = conn
        self.row: tuple[Any, ...] | None = None
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)
        assert "cost_usd" not in sql and "analyze_failed_usage" not in sql
        if "count(*)" in sql and "jobs" in sql and "FROM videos" not in sql:
            self.row = (0,)
        elif "count(*)" in sql:
            self.row = (3,)
        else:
            self.rows = [("a",), ("b",), ("c",)]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


@pytest.mark.parametrize("summarizer", ["ollama", "free"])
def test_without_a_paid_summarizer_the_plan_is_unchanged_and_no_cost_query_runs(
    summarizer: str,
) -> None:
    conn = _NoCostQueryConn()

    plan = plan_reanalysis_sweep(
        conn,  # type: ignore[arg-type]
        prompt_version="v2",
        summarizer_name=summarizer,
        max_usd_anthropic=0.0,  # would stop everything if it were read
    )

    assert plan.video_ids == ("a", "b", "c")
    assert (plan.cap, plan.cap_reason, plan.cap_stopped, plan.budget_usd) == (
        None,
        None,
        False,
        None,
    )
    assert (plan.est_usd_this_run, plan.est_usd_committed, plan.cost_per_char_samples) == (
        None,
        None,
        None,
    )
    assert conn.commits == 1


def test_the_sweep_and_the_route_call_the_shared_estimator_and_do_not_redo_it() -> None:
    for path in ("services/planner/sweep.py", "services/api/routes_read.py"):
        source = (REPO / path).read_text(encoding="utf-8")
        assert "from common.cost_estimate import estimate_cost_usd" in source, path
        assert "estimate_cost_usd(" in source, path
        assert "* per_char" not in source and "* cost_per_char" not in source, path
