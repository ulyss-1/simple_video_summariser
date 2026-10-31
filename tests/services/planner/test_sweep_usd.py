"""The USD budget of the re-analysis sweep (issue #153). Needs Postgres.

Fake numbers throughout: 2e-6 USD per character, a $0.05 budget and three or four
seeded videos, never hundreds of rows. 10,000 characters estimate at $0.02.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest

from common.queue import analyze_dedupe_key
from services.planner.sweep import (
    REANALYSIS_PRIORITY,
    plan_reanalysis_sweep,
    run_reanalysis_sweep,
)
from tests.services.planner.test_sweep import VERSION, _queue, _transcript, _video

pytestmark = pytest.mark.integration

KEY = analyze_dedupe_key(VERSION, "anthropic")
PER_CHAR = 2e-6
MEDIAN = (PER_CHAR, 3)


def _cand(conn: psycopg.Connection[Any], video_id: str, chars: int, age_days: int) -> None:
    """A sweep candidate; the smaller ``age_days`` is walked first."""
    _video(conn, video_id, age_days=age_days)
    _transcript(conn, video_id, text="x" * chars)
    conn.commit()


def _job(
    conn: psycopg.Connection[Any],
    video_id: str,
    state: str,
    *,
    priority: int = REANALYSIS_PRIORITY,
    key: str = KEY,
) -> int:
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, priority, state) "
        "VALUES (%s, 'analyze', %s, %s, %s) RETURNING id",
        (video_id, key, priority, state),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _analysis(
    conn: psycopg.Connection[Any],
    video_id: str,
    transcript_id: int,
    cost: str | None,
    *,
    hour: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version,"
        " tldr, cost_usd, created_at)"
        " VALUES (%s, %s, 's', 'm', %s, 't', %s, make_timestamptz(2026, 1, 1, %s, 0, 0, 'UTC'))",
        (video_id, transcript_id, VERSION, cost, hour),
    )


def _failed(
    conn: psycopg.Connection[Any], video_id: str, job_id: int | None, cost: str | None
) -> None:
    conn.execute(
        "INSERT INTO analyze_failed_usage (video_id, job_id, model, outcome, cost_usd)"
        " VALUES (%s, %s, 'm', 'failed', %s)",
        (video_id, job_id, cost),
    )


def _plan(conn: psycopg.Connection[Any], **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "prompt_version": VERSION,
        "summarizer_name": "anthropic",
        "max_jobs_anthropic": 100,
        "max_usd_anthropic": 0.05,
        "median": MEDIAN,
    }
    params.update(kwargs)
    return plan_reanalysis_sweep(conn, **params)


# --- committed spend ---------------------------------------------------------------------------


def test_committed_spend_is_summed_to_the_cent(conn: psycopg.Connection[Any]) -> None:
    # done job with a recorded cost: 0.0123 (its estimate would be 0.002)
    _video(conn, "done0000001")
    done_t = _transcript(conn, "done0000001", text="x" * 1000)
    done_job = _job(conn, "done0000001", "done")
    _analysis(conn, "done0000001", done_t, "0.0123")
    # a later interactive Re-analyse of the same video is not sweep spend
    _analysis(conn, "done0000001", done_t, "0.6", hour=5)
    # pending job, no analysis yet: its estimate, 10,000 chars = 0.02
    _video(conn, "pend0000001")
    _transcript(conn, "pend0000001", text="y" * 10_000)
    pending_job = _job(conn, "pend0000001", "pending")
    # a dead job contributes only its failed attempts
    _video(conn, "dead0000001")
    _transcript(conn, "dead0000001", text="z" * 5000)
    dead_job = _job(conn, "dead0000001", "dead")
    _failed(conn, "done0000001", done_job, "0.0050")  # counts
    _failed(conn, "dead0000001", dead_job, "0.0011")  # counts
    _failed(conn, "pend0000001", pending_job, None)  # NULL cost = the estimate, 0.02
    _failed(conn, "dead0000001", None, "0.9")  # job_id NULL: never counted
    # an analysis made by a non-sweep job is not sweep spend
    _video(conn, "other000001")
    other_t = _transcript(conn, "other000001", text="w" * 100)
    _job(conn, "other000001", "done", priority=0)
    _analysis(conn, "other000001", other_t, "0.7")
    conn.commit()

    plan = _plan(conn)

    assert plan.est_usd_committed == pytest.approx(0.0123 + 0.02 + 0.0050 + 0.0011 + 0.02, abs=1e-9)


def test_a_done_job_without_a_recorded_cost_counts_its_estimate(
    conn: psycopg.Connection[Any],
) -> None:
    _video(conn, "free0000001")
    t = _transcript(conn, "free0000001", text="x" * 10_000)
    _job(conn, "free0000001", "done")
    _analysis(conn, "free0000001", t, None)
    conn.commit()

    assert _plan(conn).est_usd_committed == pytest.approx(0.02)


# --- the stop rule ----------------------------------------------------------------------------


def test_it_stops_at_the_first_candidate_that_does_not_fit_and_never_skips(
    conn: psycopg.Connection[Any],
) -> None:
    _cand(conn, "c0000000000", 10_000, 0)  # 0.02, fits
    _cand(conn, "c0000000001", 30_000, 1)  # 0.06, does not fit after 0.02
    _cand(conn, "c0000000002", 1_000, 2)  # 0.002 would fit, but is behind the stop
    _cand(conn, "c0000000003", 1_000, 3)

    plan = _plan(conn)

    assert plan.video_ids == ("c0000000000",)
    assert (plan.cap_stopped, plan.cap_reason, plan.would_remain_after_cap) == (True, "usd", 3)
    assert plan.est_usd_this_run == pytest.approx(0.02)
    assert plan.budget_usd == 0.05 and plan.cost_per_char_samples == 3


def test_a_candidate_estimated_exactly_at_the_remaining_budget_is_enqueued(
    conn: psycopg.Connection[Any],
) -> None:
    _cand(conn, "c0000000000", 25_000, 0)  # 25,000 x 2e-6 = 0.05 = the budget

    plan = _plan(conn)

    assert plan.video_ids == ("c0000000000",) and plan.cap_reason is None


def test_one_cent_over_is_not_enqueued(conn: psycopg.Connection[Any]) -> None:
    _cand(conn, "c0000000000", 30_000, 0)  # 0.06 against 0.05

    plan = _plan(conn)

    assert plan.video_ids == () and plan.cap_reason == "usd"


def test_committed_spend_reduces_the_room(conn: psycopg.Connection[Any]) -> None:
    _video(conn, "pend0000001")
    _transcript(conn, "pend0000001", text="y" * 20_000)  # 0.04 committed
    _job(conn, "pend0000001", "pending")
    _cand(conn, "c0000000000", 10_000, 0)  # 0.02: 0.06 > 0.05

    assert _plan(conn).video_ids == ()


def test_a_zero_budget_enqueues_nothing(conn: psycopg.Connection[Any]) -> None:
    _cand(conn, "c0000000000", 10, 0)

    plan = _plan(conn, max_usd_anthropic=0.0)
    live = run_reanalysis_sweep(
        conn,
        _queue(conn),
        prompt_version=VERSION,
        summarizer_name="anthropic",
        max_usd_anthropic=0.0,
        median=MEDIAN,
    )

    assert plan.video_ids == () and plan.cap_reason == "usd" and live == 0


def test_the_job_cap_binding_first_is_reported_as_jobs(conn: psycopg.Connection[Any]) -> None:
    for i in range(3):
        _cand(conn, f"c000000000{i}", 1_000, i)  # 0.002 each: the budget is not the limit

    plan = _plan(conn, max_jobs_anthropic=2, max_usd_anthropic=10.0)

    assert len(plan.video_ids) == 2
    assert (plan.cap_stopped, plan.cap_reason, plan.would_remain_after_cap) == (True, "jobs", 1)


def test_the_usd_budget_binding_before_the_job_cap_is_reported_as_usd(
    conn: psycopg.Connection[Any],
) -> None:
    for i in range(3):
        _cand(conn, f"c000000000{i}", 10_000, i)  # 0.02 each: two fit in 0.05

    plan = _plan(conn, max_jobs_anthropic=3)

    assert len(plan.video_ids) == 2 and plan.cap_reason == "usd"


# --- cold start ----------------------------------------------------------------------------


@pytest.mark.parametrize(("already", "expected"), [(0, 2), (2, 0)])
def test_with_no_cost_samples_at_most_the_probe_jobs_in_total_are_enqueued(
    conn: psycopg.Connection[Any], already: int, expected: int
) -> None:
    for i in range(already):
        _video(conn, f"old0000000{i}")
        _job(conn, f"old0000000{i}", "done")
    for i in range(4):
        _cand(conn, f"c000000000{i}", 1_000, i)

    plan = _plan(conn, median=(None, 0), probe_jobs=2, max_usd_anthropic=10.0)

    assert len(plan.video_ids) == expected
    assert plan.est_usd_this_run is None and plan.est_usd_committed is None
    assert plan.cost_per_char_samples == 0


def test_the_median_is_read_from_the_database_when_not_injected(
    conn: psycopg.Connection[Any],
) -> None:
    _video(conn, "rec00000001")
    t = _transcript(conn, "rec00000001", text="x" * 1000)
    conn.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version,"
        " tldr, cost_usd) VALUES ('rec00000001', %s, 's', 'claude-haiku-4-5', 'v2', 't', 0.002)",
        (t,),
    )
    _cand(conn, "c0000000000", 10_000, 0)

    plan = _plan(conn, median=None, model="claude-haiku-4-5")

    assert plan.cost_per_char_samples == 1
    assert plan.est_usd_this_run == pytest.approx(0.02)


# --- live run ---------------------------------------------------------------------------------


def test_the_dry_run_plan_and_the_live_run_enqueue_the_same_videos(
    conn: psycopg.Connection[Any],
) -> None:
    for i, chars in enumerate([10_000, 10_000, 10_000, 1_000]):
        _cand(conn, f"c000000000{i}", chars, i)
    before = conn.execute("SELECT count(*) FROM jobs").fetchone()

    plan = _plan(conn)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone() == before  # writes nothing

    run_reanalysis_sweep(
        conn,
        _queue(conn),
        prompt_version=VERSION,
        summarizer_name="anthropic",
        max_usd_anthropic=0.05,
        median=MEDIAN,
    )
    rows = conn.execute("SELECT video_id FROM jobs ORDER BY video_id").fetchall()
    assert tuple(str(r[0]) for r in rows) == tuple(sorted(plan.video_ids))
    assert len(plan.video_ids) == 2


def test_reaching_the_budget_warns_once_and_raising_it_enqueues_the_rest(
    conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    for i in range(3):
        _cand(conn, f"c000000000{i}", 10_000, i)
    queue = _queue(conn)

    def run(budget: float) -> int:
        return run_reanalysis_sweep(
            conn,
            queue,
            prompt_version=VERSION,
            summarizer_name="anthropic",
            max_usd_anthropic=budget,
            median=MEDIAN,
        )

    with caplog.at_level(logging.INFO, logger="services.planner.sweep"):
        assert run(0.05) == 2
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "planner.reanalysis_sweep.cap_reached"
    fields = warning.__dict__
    assert (fields["reason"], fields["budget_usd"], fields["unenqueued"]) == ("usd", 0.05, 1)
    assert fields["committed_usd"] == pytest.approx(0.04)

    assert run(0.05) == 0  # nothing more fits, the two enqueued jobs are untouched
    assert run(0.07) == 1  # raising the setting enqueues the rest
    assert conn.execute("SELECT count(*) FROM jobs").fetchone() == (3,)


def test_with_a_fake_queue_the_warning_has_its_fields_and_enqueue_is_called_for_the_plan(
    conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    for i in range(3):
        _cand(conn, f"c000000000{i}", 10_000, i)

    class Recording:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        def enqueue(self, kind: str, video_id: str, **kwargs: Any) -> int | None:
            self.calls.append((kind, video_id, kwargs["priority"]))
            return len(self.calls)

    plan = _plan(conn)
    queue = Recording()
    with caplog.at_level(logging.INFO, logger="services.planner.sweep"):
        enqueued = run_reanalysis_sweep(
            conn,
            queue,  # type: ignore[arg-type]
            prompt_version=VERSION,
            summarizer_name="anthropic",
            max_usd_anthropic=0.05,
            median=MEDIAN,
        )

    assert enqueued == 2
    assert queue.calls == [("analyze", v, REANALYSIS_PRIORITY) for v in plan.video_ids]
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert {k: warning.__dict__[k] for k in ("reason", "budget_usd", "unenqueued")} == {
        "reason": "usd",
        "budget_usd": 0.05,
        "unenqueued": 1,
    }
