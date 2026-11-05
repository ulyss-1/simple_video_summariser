"""The advisory-lock exclusion and the deferral plumbing, on a real Postgres (issue #393).

The lock is asserted from a third connection through ``pg_locks``, never from the
guard's own state. Nothing here sleeps: every wait is on an observable state or a
bounded ``join``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from common.errors import (
    ANALYSIS_WAITING,
    LOCAL_LLM_BUSY,
    WHISPER_BUSY,
    Cancelled,
    Defer,
    LocalLlmBusy,
)
from common.llm_lock import (
    LOCK_ID,
    LOCK_NAMESPACE_LLM,
    PostgresLocalLlmGuard,
    WhisperGate,
    deferred_text,
)
from common.queue import PostgresQueue
from common.repo.jobs import deferred_job_counts, list_jobs

pytestmark = pytest.mark.integration

DEADLINE = 5.0
SMALL = timedelta(seconds=2)
VIDEO = "aaaaaaaaaaa"

_LOCKS = """
    SELECT pid, mode FROM pg_locks
    WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 2
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY pid
"""


@pytest.fixture
def observer(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def opened(head_dsn: str) -> Iterator[Callable[[], psycopg.Connection[Any]]]:
    conns: list[psycopg.Connection[Any]] = []

    def connect() -> psycopg.Connection[Any]:
        made = psycopg.connect(head_dsn)
        conns.append(made)
        return made

    yield connect
    for made in conns:
        made.close()


def held(observer: psycopg.Connection[Any]) -> list[tuple[int, str]]:
    return [
        (int(pid), str(mode))
        for pid, mode in observer.execute(_LOCKS, (LOCK_NAMESPACE_LLM, LOCK_ID)).fetchall()
    ]


def exclusive(connect: Callable[[], psycopg.Connection[Any]]) -> PostgresLocalLlmGuard:
    return PostgresLocalLlmGuard(connect, busy_defer=SMALL)


def shared(connect: Callable[[], psycopg.Connection[Any]]) -> PostgresLocalLlmGuard:
    return PostgresLocalLlmGuard(connect, shared=True, busy_defer=SMALL)


# --- modes -----------------------------------------------------------------------------


def test_the_key_is_not_one_of_the_planners() -> None:
    assert LOCK_NAMESPACE_LLM != 0x59545044


def test_an_exclusive_guard_takes_the_lock_and_release_frees_it(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    guard = exclusive(opened)
    guard.ensure_held()
    assert [mode for _, mode in held(observer)] == ["ExclusiveLock"]
    guard.ensure_held()  # later calls only check
    assert len(held(observer)) == 1
    guard.release()
    assert held(observer) == []
    guard.release()  # idempotent


def test_two_shared_holders_coexist(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    first, second = shared(opened), shared(opened)
    first.ensure_held()
    second.ensure_held()
    assert [mode for _, mode in held(observer)] == ["ShareLock", "ShareLock"]
    first.release()
    second.release()


def test_two_exclusive_attempts_exclude_each_other(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    first, second = exclusive(opened), exclusive(opened)
    first.ensure_held()
    with pytest.raises(LocalLlmBusy) as busy:
        second.ensure_held()
    assert busy.value.reason == WHISPER_BUSY
    assert len(held(observer)) == 1
    first.release()


def test_a_shared_holder_blocks_an_exclusive_attempt_with_a_bounded_answer(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    whisper = shared(opened)
    whisper.ensure_held()
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            exclusive(opened).ensure_held()
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome.append(exc)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(DEADLINE)
    assert not thread.is_alive(), "the attempt blocked instead of failing at once"
    assert isinstance(outcome[0], LocalLlmBusy)
    assert [mode for _, mode in held(observer)] == ["ShareLock"]
    whisper.release()


def test_an_exclusive_holder_blocks_a_shared_attempt_as_local_llm_busy(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    analysis = exclusive(opened)
    analysis.ensure_held()
    with pytest.raises(LocalLlmBusy) as busy:
        shared(opened).ensure_held()
    assert busy.value.reason == LOCAL_LLM_BUSY
    assert len(held(observer)) == 1
    analysis.release()


def test_both_sides_wanting_the_lock_at_once_one_holds_the_other_is_busy_and_retries_after(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    barrier = threading.Barrier(2, timeout=DEADLINE)
    guards = {"analysis": exclusive(opened), "whisper": shared(opened)}
    results: dict[str, str] = {}

    def attempt(name: str) -> None:
        barrier.wait()
        try:
            guards[name].ensure_held()
            results[name] = "held"
        except LocalLlmBusy:
            results[name] = "busy"

    threads = [threading.Thread(target=attempt, args=(name,)) for name in guards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(DEADLINE)
        assert not thread.is_alive(), "deadlock: an attempt did not return"

    assert sorted(results.values()) == ["busy", "held"]
    winner = next(name for name, outcome in results.items() if outcome == "held")
    loser = next(name for name, outcome in results.items() if outcome == "busy")
    guards[winner].release()
    guards[loser].ensure_held()  # the deferred side succeeds on its next attempt
    assert len(held(observer)) == 1
    guards[loser].release()


# --- release and loss --------------------------------------------------------------------


def test_the_lock_follows_the_connection_not_a_transaction(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    guard = exclusive(opened)
    guard.ensure_held()
    data = opened()
    data.execute("SELECT 1")
    data.rollback()
    data.execute("SELECT 1")
    data.commit()
    assert len(held(observer)) == 1
    guard.release()


def test_a_terminated_holder_frees_the_lock_for_a_second_guard(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    guard = exclusive(opened)
    guard.ensure_held()
    pid = held(observer)[0][0]
    observer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    other = exclusive(opened)
    # pg_terminate_backend signals; wait on the lock row, not the clock.
    assert _wait_until(lambda: held(observer) == []), "the lock outlived its backend"
    other.ensure_held()
    assert len(held(observer)) == 1
    other.release()
    guard.release()  # never raises on a dead connection


def test_a_lost_lock_is_retaken_when_free(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    guard = exclusive(opened)
    guard.ensure_held()
    observer.execute("SELECT pg_terminate_backend(%s)", (held(observer)[0][0],))
    assert _wait_until(lambda: held(observer) == [])
    guard.ensure_held()
    assert len(held(observer)) == 1
    guard.release()


def test_a_lost_lock_is_busy_when_a_transcriber_took_it_meanwhile(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    guard = exclusive(opened)
    guard.ensure_held()
    observer.execute("SELECT pg_terminate_backend(%s)", (held(observer)[0][0],))
    assert _wait_until(lambda: held(observer) == [])
    whisper = shared(opened)
    whisper.ensure_held()
    with pytest.raises(LocalLlmBusy):
        guard.ensure_held()
    whisper.release()
    guard.release()


def _wait_until(check: Callable[[], bool]) -> bool:
    done = threading.Event()
    deadline = threading.Timer(DEADLINE, done.set)
    deadline.start()
    try:
        while not check():
            if done.wait(0.01):
                return check()
        return True
    finally:
        deadline.cancel()


# --- the transcriber gate ------------------------------------------------------------------


def _gate(opened: Callable[[], psycopg.Connection[Any]]) -> WhisperGate:
    return WhisperGate(opened, busy_defer=SMALL)


@pytest.mark.parametrize(
    "exit_with",
    [None, RuntimeError("boom"), Cancelled(), Defer(datetime(2030, 1, 1, tzinfo=UTC)), SystemExit(1)],
    ids=["done", "error", "cancelled", "defer", "systemexit"],
)
def test_the_gate_releases_on_every_way_out(
    opened: Callable[[], psycopg.Connection[Any]],
    observer: psycopg.Connection[Any],
    exit_with: BaseException | None,
) -> None:
    data = opened()
    try:
        with _gate(opened).hold(data):
            assert [mode for _, mode in held(observer)] == ["ShareLock"]
            if exit_with is not None:
                raise exit_with
    except BaseException:
        if exit_with is None:
            raise
    assert held(observer) == []


def test_the_gate_defers_while_an_analysis_holds_the_lock(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    analysis = exclusive(opened)
    analysis.ensure_held()
    with pytest.raises(LocalLlmBusy) as busy, _gate(opened).hold(opened()):
        pytest.fail("the body must not run")
    assert busy.value.reason == LOCAL_LLM_BUSY
    assert len(held(observer)) == 1  # the analysis still holds, nothing else does
    analysis.release()


def _enqueue_waiting_analyze(
    conn: psycopg.Connection[Any], *, last_error: str | None, run_after_sec: int
) -> None:
    conn.execute(
        "INSERT INTO videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING", (VIDEO,)
    )
    conn.execute(
        "INSERT INTO jobs (kind, video_id, dedupe_key, state, run_after, last_error) "
        "VALUES ('analyze', %s, 'v1:ollama', 'pending', now() + make_interval(secs => %s), %s)",
        (VIDEO, run_after_sec, last_error),
    )


def test_a_transcribe_yields_to_an_analysis_deferred_as_whisper_busy(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    _enqueue_waiting_analyze(observer, last_error=deferred_text(WHISPER_BUSY), run_after_sec=3)
    with pytest.raises(LocalLlmBusy) as busy, _gate(opened).hold(opened()):
        pytest.fail("the body must not run")
    assert busy.value.reason == ANALYSIS_WAITING
    assert held(observer) == []


def test_a_whisper_busy_analysis_past_due_inside_the_staleness_window_still_makes_a_transcribe_yield(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    # SMALL = 2 s, so the window is 4 x 2 s = 8 s; 7 s past due is inside it.
    _enqueue_waiting_analyze(observer, last_error=deferred_text(WHISPER_BUSY), run_after_sec=-7)
    with pytest.raises(LocalLlmBusy) as busy, _gate(opened).hold(opened()):
        pytest.fail("the body must not run")
    assert busy.value.reason == ANALYSIS_WAITING


def test_a_stale_whisper_busy_analysis_does_not_block_a_transcribe(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    # 9 s past due is just outside the 8 s window: the analyzer is down or wedged.
    _enqueue_waiting_analyze(observer, last_error=deferred_text(WHISPER_BUSY), run_after_sec=-9)
    with _gate(opened).hold(opened()):
        assert [mode for _, mode in held(observer)] == ["ShareLock"]


def test_a_transcribe_starts_when_no_analysis_waits(
    opened: Callable[[], psycopg.Connection[Any]], observer: psycopg.Connection[Any]
) -> None:
    ran: list[int] = []
    with _gate(opened).hold(opened()):
        ran.append(1)
    assert ran == [1]


@pytest.mark.parametrize(
    ("last_error", "run_after_sec"),
    [
        (None, 3),  # not a whisper_busy deferral
        ("deferred: local_llm_busy", 3),  # a transcribe's reason, not an analysis's
        (deferred_text(WHISPER_BUSY), 60),  # due beyond 2 x the delay
    ],
)
def test_other_pending_analyses_do_not_make_a_transcribe_yield(
    opened: Callable[[], psycopg.Connection[Any]],
    observer: psycopg.Connection[Any],
    last_error: str | None,
    run_after_sec: int,
) -> None:
    _enqueue_waiting_analyze(observer, last_error=last_error, run_after_sec=run_after_sec)
    with _gate(opened).hold(opened()):
        assert len(held(observer)) == 1


# --- the queue side: Defer with a reason ------------------------------------------------------


@pytest.fixture
def queue(observer: psycopg.Connection[Any]) -> PostgresQueue:
    observer.execute("INSERT INTO videos (video_id) VALUES (%s)", (VIDEO,))
    return PostgresQueue(observer)


def _row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT state, attempts, priority, last_error, error_class, run_after "
        "FROM jobs WHERE id = %s",
        (job_id,),
    )
    values = cur.fetchone()
    assert values is not None
    return dict(zip(("state", "attempts", "priority", "last_error", "error_class", "run_after"), values, strict=True))


@pytest.mark.parametrize("kind", ["transcribe", "analyze"])
@pytest.mark.parametrize("reason", [LOCAL_LLM_BUSY, WHISPER_BUSY, ANALYSIS_WAITING])
def test_a_deferral_with_a_reason_stores_the_text_and_spends_no_attempt(
    queue: PostgresQueue, observer: psycopg.Connection[Any], kind: str, reason: str
) -> None:
    job_id = queue.enqueue(kind, VIDEO, priority=7)
    assert job_id is not None
    until = datetime.now(UTC) + timedelta(seconds=30)

    with queue.claim([kind], worker="w1") as job:
        assert job is not None
        raise Defer(until, reason=reason)

    row = _row(observer, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["priority"] == 7
    assert row["last_error"] == f"deferred: {reason}"
    assert row["error_class"] is None
    assert row["run_after"] > datetime.now(UTC)


def test_a_deferral_clears_an_earlier_failure_text_but_a_plain_defer_leaves_it(
    queue: PostgresQueue, observer: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("transcribe", VIDEO)
    assert job_id is not None
    observer.execute(
        "UPDATE jobs SET last_error = 'old failure', error_class = 'TRANSIENT_NETWORK' WHERE id = %s",
        (job_id,),
    )
    until = datetime.now(UTC) + timedelta(seconds=30)
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        raise Defer(until)  # existing callers: low disk, config mismatch, premiere
    row = _row(observer, job_id)
    assert (row["last_error"], row["error_class"]) == ("old failure", "TRANSIENT_NETWORK")


def test_an_analysis_completes_while_a_transcribe_claim_is_deferred(
    queue: PostgresQueue, observer: psycopg.Connection[Any]
) -> None:
    transcribe_id = queue.enqueue("transcribe", VIDEO)
    analyze_id = queue.enqueue("analyze", VIDEO)
    assert transcribe_id is not None and analyze_id is not None
    with queue.claim(["transcribe"], worker="w1") as tjob:
        assert tjob is not None
        raise Defer(datetime.now(UTC) + timedelta(hours=1), reason=LOCAL_LLM_BUSY)
    # the transcribe is deferred and not due; the analysis still claims and finishes
    with queue.claim(["analyze"], worker="w2") as ajob:
        assert ajob is not None
        assert ajob.id == analyze_id
    assert _row(observer, analyze_id)["state"] == "done"
    deferred = _row(observer, transcribe_id)
    assert (deferred["state"], deferred["last_error"]) == ("pending", "deferred: local_llm_busy")


def test_a_deferred_job_runs_once_its_run_after_has_passed(
    queue: PostgresQueue, observer: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("transcribe", VIDEO)
    assert job_id is not None
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        raise Defer(datetime.now(UTC) + timedelta(hours=1), reason=LOCAL_LLM_BUSY)
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is None  # not due yet
    observer.execute("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = %s", (job_id,))
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        assert job.id == job_id
    row = _row(observer, job_id)
    assert row["state"] == "done"
    assert row["last_error"] is None


@pytest.mark.parametrize("reason", [LOCAL_LLM_BUSY, WHISPER_BUSY, ANALYSIS_WAITING])
def test_a_deferral_on_a_job_with_a_cancel_request_still_ends_cancelled(
    queue: PostgresQueue, observer: psycopg.Connection[Any], reason: str
) -> None:
    job_id = queue.enqueue("transcribe", VIDEO)
    assert job_id is not None
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        observer.execute("UPDATE jobs SET cancel_requested_at = now() WHERE id = %s", (job_id,))
        raise Defer(datetime.now(UTC) + timedelta(seconds=30), reason=reason)
    row = _row(observer, job_id)
    assert (row["state"], row["error_class"]) == ("dead", "CANCELLED")


# --- the gauge ---------------------------------------------------------------------------------


def test_the_deferred_gauge_query_counts_pending_deferred_jobs_by_reason(
    observer: psycopg.Connection[Any],
) -> None:
    observer.execute("INSERT INTO videos (video_id) VALUES (%s)", (VIDEO,))
    assert deferred_job_counts(observer) == {}
    for kind, text, state in (
        ("transcribe", deferred_text(LOCAL_LLM_BUSY), "pending"),
        ("analyze", deferred_text(WHISPER_BUSY), "pending"),
        ("analyze", deferred_text(WHISPER_BUSY), "running"),  # not pending: not counted
        ("transcribe", "something else", "pending"),
    ):
        observer.execute(
            "INSERT INTO jobs (kind, video_id, dedupe_key, state, last_error) "
            "VALUES (%s, %s, %s, %s, %s)",
            (kind, VIDEO, f"k{kind}{state}{text}", state, text),
        )
    assert deferred_job_counts(observer) == {LOCAL_LLM_BUSY: 1, WHISPER_BUSY: 1}


def test_the_ops_job_listing_shows_the_deferral(
    queue: PostgresQueue, observer: psycopg.Connection[Any]
) -> None:
    job_id = queue.enqueue("transcribe", VIDEO)
    assert job_id is not None
    with queue.claim(["transcribe"], worker="w1") as job:
        assert job is not None
        raise Defer(datetime.now(UTC) + timedelta(seconds=30), reason=LOCAL_LLM_BUSY)
    item = next(row for row in list_jobs(observer, state="pending", limit=10) if row.id == job_id)
    assert item.last_error == "deferred: local_llm_busy"
    assert item.error_class is None
    assert item.run_after > datetime.now(UTC)
