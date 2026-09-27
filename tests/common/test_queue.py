"""Tests for ``common.queue`` (issue #11, architecture.md §0 C1, §2, §4-§6, §8.4).

Every test drives a real ``PostgresQueue`` against a Postgres database
migrated to head via the ``postgres_dsn`` fixture from ``tests/conftest.py``
(task #7), so the whole module is marked ``integration``. The concurrency and
crash suite (multiple workers, killed processes) is #12 - these tests cover
single-worker behaviour only, one AC bullet at a time.

Jitter is deterministic because a fixed ``random.Random`` seed is injected;
timestamps are asserted with generous tolerances since they come from the
database's own ``now()``, not the test process's clock.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row

from common.config import Settings, get_settings
from common.errors import (
    Cancelled,
    Defer,
    PermanentSourceError,
    RateLimitedError,
    ToolFailureError,
    TransientNetworkError,
)
from common.queue import Job, PostgresQueue

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    # Same pattern as tests/migrations/test_jobs.py (#11): env.py reads
    # DATABASE_URL via common.config.get_settings(), which is lru_cache'd per
    # process, so the cache has to be cleared after pointing it at the test
    # database.
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def head_dsn(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DSN for a database migrated all the way to head."""
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "head")
    return postgres_dsn


@pytest.fixture
def conn(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    # autocommit=True is a test-only convenience so the raw setup/assertion
    # SQL sprinkled through these tests (updating attempts, heartbeat_at, ...
    # directly to control time per AGENTS.md/testing-guidelines) doesn't need
    # its own explicit commits. PostgresQueue itself must work the same way
    # regardless: its methods always wrap their own work in
    # ``conn.transaction()``, which psycopg supports on an autocommit
    # connection by suspending autocommit for the block.
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"DATABASE_URL": "postgresql://u:p@h/db"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def queue(conn: psycopg.Connection[Any]) -> PostgresQueue:
    return PostgresQueue(conn, settings=_settings(), rng=random.Random(1234))


def _row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    return row


def _set(conn: psycopg.Connection[Any], job_id: int, **columns: object) -> None:
    assignments = ", ".join(f"{name} = %({name})s" for name in columns)
    params = {**columns, "id": job_id}
    with conn.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {assignments} WHERE id = %(id)s", params)


def _delta_seconds(conn: psycopg.Connection[Any], job_id: int) -> float:
    """``run_after`` minus the database's own ``now()``, in seconds."""
    with conn.cursor() as cur:
        cur.execute("SELECT run_after - now() FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    interval: timedelta = row[0]
    return interval.total_seconds()


def _count(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def test_enqueue_returns_the_new_job_id(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")

    assert isinstance(job_id, int)
    row = _row(conn, job_id)
    assert row["video_id"] == "v1"
    assert row["kind"] == "ingest"
    assert row["dedupe_key"] == "default"
    assert row["state"] == "pending"
    assert row["priority"] == 0
    assert row["payload"] == {}


@pytest.mark.parametrize("blocking_state", ["pending", "running"])
def test_enqueue_returns_none_and_touches_no_row_when_active_job_exists(
    queue: PostgresQueue, conn: psycopg.Connection[Any], blocking_state: str
) -> None:
    first_id = queue.enqueue("ingest", "v1")
    assert first_id is not None
    _set(conn, first_id, state=blocking_state)

    result = queue.enqueue("ingest", "v1")

    assert result is None
    assert _count(conn) == 1
    assert _row(conn, first_id)["state"] == blocking_state


@pytest.mark.parametrize("finished_state", ["done", "dead"])
def test_enqueue_succeeds_again_once_prior_job_is_finished(
    queue: PostgresQueue, conn: psycopg.Connection[Any], finished_state: str
) -> None:
    first_id = queue.enqueue("ingest", "v1")
    assert first_id is not None
    _set(conn, first_id, state=finished_state)

    second_id = queue.enqueue("ingest", "v1")

    assert second_id is not None
    assert second_id != first_id
    assert _count(conn) == 2
    assert _row(conn, second_id)["state"] == "pending"


def test_enqueue_unknown_kind_raises_value_error_without_touching_the_database(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    with pytest.raises(ValueError):
        queue.enqueue("bogus", "v1")

    assert _count(conn) == 0


def test_enqueue_distinct_dedupe_keys_do_not_block_each_other(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    first_id = queue.enqueue("analyze", "v1", dedupe_key="v1:ollama")
    second_id = queue.enqueue("analyze", "v1", dedupe_key="v2:ollama")

    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_yields_none_when_nothing_is_claimable(queue: PostgresQueue) -> None:
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is None


def test_claim_yields_a_running_job_committed_with_ownership_fields(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert isinstance(job, Job)
        assert job.id == job_id
        assert job.state == "running"
        assert job.locked_by == "w1"
        assert job.locked_at is not None
        assert job.heartbeat_at is not None
        assert job.attempts == 1

    row = _row(conn, job_id)
    assert row["state"] == "done"  # clean exit of the with-block


def test_handler_runs_outside_any_open_transaction(
    queue: PostgresQueue, head_dsn: str
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        # A second, independent connection must see the claim as committed
        # already. Under READ COMMITTED, it would still see 'pending' if the
        # claiming transaction were still open.
        with psycopg.connect(head_dsn) as other, other.cursor() as cur:
            cur.execute(
                "SELECT state, locked_by FROM jobs WHERE id = %s", (job_id,)
            )
            row = cur.fetchone()
        assert row == ("running", "w1")


def test_claim_order_is_priority_desc_then_run_after_asc(
    queue: PostgresQueue,
) -> None:
    now = datetime.now(UTC)
    low_priority = queue.enqueue("ingest", "v-low", priority=-5, run_after=now)
    high_priority_later = queue.enqueue(
        "ingest",
        "v-high-later",
        priority=10,
        run_after=now - timedelta(seconds=30),
    )
    high_priority_earlier = queue.enqueue(
        "ingest",
        "v-high-earlier",
        priority=10,
        run_after=now - timedelta(seconds=60),
    )
    assert low_priority and high_priority_later and high_priority_earlier

    claimed_ids = []
    for _ in range(3):
        with queue.claim(["ingest"], worker="w1") as job:
            assert job is not None
            claimed_ids.append(job.id)

    assert claimed_ids == [high_priority_earlier, high_priority_later, low_priority]


def test_claim_does_not_claim_a_job_whose_run_after_is_in_the_future(
    queue: PostgresQueue,
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    queue.enqueue("ingest", "v1", run_after=future)

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is None


def test_claim_does_not_claim_a_job_of_a_different_kind(queue: PostgresQueue) -> None:
    queue.enqueue("analyze", "v1")

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is None


# ---------------------------------------------------------------------------
# outcome: clean exit
# ---------------------------------------------------------------------------


def test_clean_exit_marks_the_job_done_and_clears_ownership(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None

    row = _row(conn, job_id)
    assert row["state"] == "done"
    assert row["finished_at"] is not None
    assert row["locked_by"] is None


# ---------------------------------------------------------------------------
# outcome: exception -> classify decides
# ---------------------------------------------------------------------------


def test_retryable_exception_records_error_fields_and_goes_pending_with_backoff(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    # Unlike Cancelled/KeyboardInterrupt/SystemExit, a plain classified
    # failure is fully handled by the queue and does not propagate: the
    # worker loop must be able to move on to the next job.
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise TransientNetworkError("dns lookup failed")

    row = _row(conn, job_id)
    assert row["state"] == "pending"
    assert row["error_class"] == "TRANSIENT_NETWORK"
    assert "TransientNetworkError" in row["last_error"]
    assert "dns lookup failed" in row["last_error"]
    assert row["locked_by"] is None
    # base backoff for attempts=1 is 5 min, ±10% jitter -> roughly 270-330s.
    assert 200 <= _delta_seconds(conn, job_id) <= 400


def test_non_retryable_exception_dead_letters_immediately(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise PermanentSourceError("removed")

    row = _row(conn, job_id)
    assert row["state"] == "dead"
    assert row["error_class"] == "PERMANENT_SOURCE"
    assert row["locked_by"] is None


def test_unclassified_exception_is_a_bug_and_dead_letters(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise RuntimeError("boom")

    row = _row(conn, job_id)
    assert row["state"] == "dead"
    assert row["error_class"] == "BUG"
    assert "RuntimeError" in row["last_error"]
    assert "boom" in row["last_error"]


# ---------------------------------------------------------------------------
# outcome: actual delay = max(computed backoff, policy.min_backoff, retry_after_sec)
# ---------------------------------------------------------------------------


def test_rate_limited_waits_at_least_the_policy_min_backoff(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise RateLimitedError("429")

    # min_backoff for RATE_LIMITED is 30 min; computed exponential backoff at
    # attempts=1 (5 min) is well below it, so the floor wins.
    delta = _delta_seconds(conn, job_id)
    assert 1750 <= delta <= 1900


def test_rate_limited_retry_after_overrides_the_min_backoff_when_longer(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise RateLimitedError("429", retry_after_sec=5400)

    delta = _delta_seconds(conn, job_id)
    assert 5350 <= delta <= 5500


# ---------------------------------------------------------------------------
# outcome: dead once attempts reaches the smaller of the two limits
# ---------------------------------------------------------------------------


def test_dead_letters_once_the_kind_max_attempts_is_reached(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")  # kind max is 4
    assert job_id is not None
    _set(conn, job_id, attempts=3)

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 4
        raise TransientNetworkError()

    row = _row(conn, job_id)
    assert row["state"] == "dead"
    assert row["attempts"] == 4


def test_dead_letters_once_the_kind_max_is_not_reached_but_the_class_max_is(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")  # kind max is 4
    assert job_id is not None
    _set(conn, job_id, attempts=2)

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 3
        raise ToolFailureError()  # TOOL_FAILURE class max is 3

    row = _row(conn, job_id)
    assert row["state"] == "dead"
    assert row["error_class"] == "TOOL_FAILURE"
    assert row["attempts"] == 3


def test_retries_when_below_both_the_kind_and_class_max(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    _set(conn, job_id, attempts=1)  # below both kind max (4) and class max (3)

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        raise ToolFailureError()

    row = _row(conn, job_id)
    assert row["state"] == "pending"


def test_transcribe_kind_dead_letters_after_its_lower_max_attempts(
    conn: psycopg.Connection[Any],
) -> None:
    q = PostgresQueue(
        conn, settings=_settings(MAX_ATTEMPTS_TRANSCRIBE=2), rng=random.Random(1)
    )
    job_id = q.enqueue("transcribe", "v1")
    assert job_id is not None
    _set(conn, job_id, attempts=1)

    with q.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 2
        raise TransientNetworkError()

    row = _row(conn, job_id)
    assert row["state"] == "dead"


# ---------------------------------------------------------------------------
# outcome: Defer
# ---------------------------------------------------------------------------


def test_defer_returns_to_pending_at_until_without_spending_an_attempt(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    until = datetime.now(UTC) + timedelta(hours=2)

    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 1
        raise Defer(until=until)

    row = _row(conn, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0  # not counted
    assert abs((row["run_after"] - until).total_seconds()) < 1
    assert row["locked_by"] is None


# ---------------------------------------------------------------------------
# outcome: Cancelled / KeyboardInterrupt / SystemExit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [Cancelled, KeyboardInterrupt, SystemExit])
def test_cancellation_returns_to_pending_now_without_spending_an_attempt_and_reraises(
    queue: PostgresQueue, conn: psycopg.Connection[Any], exc_type: type[BaseException]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with pytest.raises(exc_type), queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 1
        raise exc_type()

    row = _row(conn, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["locked_by"] is None
    assert _delta_seconds(conn, job_id) <= 1


# ---------------------------------------------------------------------------
# outcome: lost ownership -> zero rows, warning, no raise, no overwrite
# ---------------------------------------------------------------------------


def test_outcome_update_is_a_noop_when_the_job_was_reclaimed_by_another_worker(
    queue: PostgresQueue, conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None

    with caplog.at_level("WARNING"), queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        # Simulate a reap-and-reclaim by a second worker while we still
        # think we own the job.
        _set(conn, job_id, locked_by="w2", attempts=2)

    row = _row(conn, job_id)
    # The new owner's row is untouched by our (no-op) outcome update.
    assert row["locked_by"] == "w2"
    assert row["attempts"] == 2
    assert row["state"] == "running"
    assert caplog.records, "expected a warning to be logged"
    assert any(r.levelname == "WARNING" for r in caplog.records)


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_returns_true_and_advances_heartbeat_at_when_owned(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.heartbeat_at is not None
        before = job.heartbeat_at
        # Push the existing heartbeat into the past so a fresh now() is
        # provably later, without sleeping.
        _set(conn, job_id, heartbeat_at=before - timedelta(seconds=30))

        result = queue.heartbeat(job_id, "w1")

        assert result is True
        after = _row(conn, job_id)["heartbeat_at"]
        assert after > before - timedelta(seconds=30)


def test_heartbeat_returns_false_when_not_owned_by_that_worker(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        before = _row(conn, job_id)["heartbeat_at"]

        result = queue.heartbeat(job_id, "w2")

        assert result is False
        assert _row(conn, job_id)["heartbeat_at"] == before


def test_heartbeat_returns_false_for_a_job_that_is_not_running(
    queue: PostgresQueue,
) -> None:
    job_id = queue.enqueue("ingest", "v1")  # still 'pending', never claimed
    assert job_id is not None

    assert queue.heartbeat(job_id, "w1") is False


# ---------------------------------------------------------------------------
# reap_stale
# ---------------------------------------------------------------------------


def test_reap_stale_returns_stale_running_jobs_to_pending_and_counts_them(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    # Set up a 'running' job with a stale heartbeat directly, rather than
    # going through claim() then waiting - no sleeps, per
    # testing-guidelines.md.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET state='running', locked_by='w1', locked_at=now(),
                   heartbeat_at = now() - interval '10 minutes', attempts=1
            WHERE id = %s
            """,
            (job_id,),
        )

    count = queue.reap_stale(older_than_sec=300)

    assert count == 1
    row = _row(conn, job_id)
    assert row["state"] == "pending"
    assert row["locked_by"] is None


def test_reap_stale_ignores_jobs_whose_heartbeat_is_within_the_threshold(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")
    assert job_id is not None
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        # heartbeat_at was just set to now() by claim(); well within threshold.
        count = queue.reap_stale(older_than_sec=300)
        assert count == 0
        row = _row(conn, job_id)
        assert row["state"] == "running"


def test_reap_stale_dead_letters_a_job_that_already_used_its_max_attempts(
    queue: PostgresQueue, conn: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("ingest", "v1")  # kind max is 4
    assert job_id is not None
    _set(conn, job_id, attempts=3)
    with queue.claim(["ingest"], worker="w1") as job:
        assert job is not None
        assert job.attempts == 4
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET heartbeat_at = now() - interval '10 minutes' "
                "WHERE id = %s",
                (job_id,),
            )
        count = queue.reap_stale(older_than_sec=300)

    assert count == 1
    row = _row(conn, job_id)
    assert row["state"] == "dead"
    assert row["last_error"] == "worker lost: heartbeat stale"
    assert row["locked_by"] is None


def test_reap_stale_uses_settings_reap_after_sec_when_older_than_sec_omitted(
    conn: psycopg.Connection[Any],
) -> None:
    q = PostgresQueue(conn, settings=_settings(REAP_AFTER_SEC=300), rng=random.Random(1))
    job_id = q.enqueue("ingest", "v1")
    assert job_id is not None
    with q.claim(["ingest"], worker="w1") as job:
        assert job is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET heartbeat_at = now() - interval '301 seconds' "
                "WHERE id = %s",
                (job_id,),
            )
        count = q.reap_stale()

    assert count == 1
    assert _row(conn, job_id)["state"] == "pending"
