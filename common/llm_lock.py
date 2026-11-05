"""Mutual exclusion between a local-Ollama analysis and a Whisper run (issue #393).

On a 14 GB host a local model and a Whisper ``large-v3`` transcription cannot
share memory (``_docs/ollama-model-benchmark.md``). One Postgres session
advisory lock, per database, keeps them apart:

- an analyze job that is about to call a local model takes it **exclusive**;
- a transcribe job holds it **shared** for the rest of its handler, so two
  transcriber replicas may overlap (as before) but a second local analysis may
  not run beside them.

Everything is a non-blocking ``pg_try_*``: the #66 watchdog and the heartbeat are
armed at claim, so nothing here may wait. The side that cannot take its mode
raises ``LocalLlmBusy`` (a ``Defer``): the queue puts the job back with no attempt
spent and ``jobs.last_error`` says why.

Each guard owns the connection its lock lives on (a session lock follows the
connection, not a transaction), so no ``commit`` or ``rollback`` elsewhere can
touch it, and closing the connection is what releases it, also after a crash.
Nothing here imports an adapter or a service (AGENTS.md, import direction).
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

from common.errors import (
    ANALYSIS_WAITING,
    LOCAL_LLM_BUSY,
    WHISPER_BUSY,
    LocalLlmBusy,
)
from common.repo.jobs import DEFERRED_PREFIX, pending_job_waits

#: The advisory key, ``pg_try_advisory_lock(LOCK_NAMESPACE_LLM, LOCK_ID)``. A pair of
#: fixed integers, never ``hash()``; the planner's namespace is ``0x59545044``.
LOCK_NAMESPACE_LLM = 0x4C4C4D31
LOCK_ID = 1

#: How long a deferred job waits, +-20 % jitter. A constant tests can pass in; not an env var.
LOCK_BUSY_DEFER = timedelta(seconds=30)
_JITTER = 0.2

#: A waiting analysis counts for the transcribe yield rule only while its ``run_after`` is
#: no more than ``STALE_WAIT_FACTOR x`` the defer delay in the past. A live analyzer re-polls
#: about every delay, so a row this far past due means the analyzer is down, scaled to 0 or
#: wedged; yielding to it forever would block transcription for good.
STALE_WAIT_FACTOR = 4

type Connect = Callable[[], psycopg.Connection[Any]]
type Clock = Callable[[], datetime]


def deferred_text(reason: str) -> str:
    """What the queue stores in ``jobs.last_error`` for a ``Defer`` with ``reason``."""
    return f"{DEFERRED_PREFIX}{reason}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def jittered_until(now: datetime, delay: timedelta, roll: Callable[[], float]) -> datetime:
    """``now`` plus ``delay`` scaled by ``0.8 .. 1.2``; ``roll`` returns a number in ``[0, 1]``."""
    return now + delay * (1 - _JITTER + 2 * _JITTER * roll())


class PostgresLocalLlmGuard:
    """One side of the lock on a connection of its own.

    ``shared=False`` is the analysis side (exclusive, busy reason ``whisper_busy``);
    ``shared=True`` is the transcriber side (busy reason ``local_llm_busy``).
    """

    def __init__(
        self,
        connect: Connect,
        *,
        shared: bool = False,
        now: Clock = _utcnow,
        busy_defer: timedelta = LOCK_BUSY_DEFER,
        roll: Callable[[], float] = random.random,
    ) -> None:
        self._connect = connect
        self._shared = shared
        self._now = now
        self._busy_defer = busy_defer
        self._roll = roll
        self._conn: psycopg.Connection[Any] | None = None

    @property
    def _reason(self) -> str:
        return LOCAL_LLM_BUSY if self._shared else WHISPER_BUSY

    def ensure_held(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return
            except psycopg.Error:
                # The connection died, so the lock went with it: try once to retake it.
                self._drop()
        self._take()

    def release(self) -> None:
        self._drop()

    def _take(self) -> None:
        conn = self._connect()
        try:
            conn.autocommit = True
            function = "pg_try_advisory_lock_shared" if self._shared else "pg_try_advisory_lock"
            row = conn.execute(
                f"SELECT {function}(%s, %s)", (LOCK_NAMESPACE_LLM, LOCK_ID)
            ).fetchone()
            got = bool(row and row[0])
        except BaseException:
            with contextlib.suppress(psycopg.Error):
                conn.close()
            raise
        if not got:
            with contextlib.suppress(psycopg.Error):
                conn.close()
            raise LocalLlmBusy(
                jittered_until(self._now(), self._busy_defer, self._roll), self._reason
            )
        self._conn = conn

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            # Unlock first: close() only sends Terminate and returns, and the server frees the
            # lock when its backend gets to the disconnect, so a reader could still see it.
            # The unlock is a round trip, so the lock is gone when release returns. If it
            # fails, closing the connection still releases the lock.
            with contextlib.suppress(psycopg.Error):
                function = "pg_advisory_unlock_shared" if self._shared else "pg_advisory_unlock"
                conn.execute(f"SELECT {function}(%s, %s)", (LOCK_NAMESPACE_LLM, LOCK_ID))
            with contextlib.suppress(psycopg.Error):
                conn.close()


class NullLocalLlmGuard:
    """No lock: the CLI and the in-process queue run one stage after the other."""

    def ensure_held(self) -> None:
        return None

    def release(self) -> None:
        return None


class WhisperGateLike(Protocol):
    def hold(self, conn: Any) -> contextlib.AbstractContextManager[None]: ...


class WhisperGate:
    """The transcriber side: ``hold`` wraps one transcribe handler's work."""

    def __init__(
        self,
        connect: Connect,
        *,
        now: Clock = _utcnow,
        busy_defer: timedelta = LOCK_BUSY_DEFER,
        roll: Callable[[], float] = random.random,
    ) -> None:
        self._connect = connect
        self._now = now
        self._busy_defer = busy_defer
        self._roll = roll

    @contextmanager
    def hold(self, conn: psycopg.Connection[Any]) -> Iterator[None]:
        """Raise ``LocalLlmBusy`` instead of starting, else hold the shared lock until exit.

        ``conn`` is the handler's data connection, used for one read: is an analysis
        waiting (deferred as ``whisper_busy``, due within twice the delay and not more
        than ``STALE_WAIT_FACTOR`` delays past due)? Without
        that rule a transcription backlog would starve it, because the transcriber
        re-claims at once and the analyzer only polls after its deferral.
        """
        waiting = pending_job_waits(
            conn,
            kind="analyze",
            last_error=deferred_text(WHISPER_BUSY),
            due_within=2 * self._busy_defer,
            stale_after=STALE_WAIT_FACTOR * self._busy_defer,
        )
        conn.commit()
        if waiting:
            raise LocalLlmBusy(
                jittered_until(self._now(), self._busy_defer, self._roll), ANALYSIS_WAITING
            )
        guard = PostgresLocalLlmGuard(
            self._connect,
            shared=True,
            now=self._now,
            busy_defer=self._busy_defer,
            roll=self._roll,
        )
        guard.ensure_held()
        try:
            yield
        finally:
            guard.release()


class NullWhisperGate:
    """No lock around a transcription (the CLI and the in-process queue)."""

    @contextmanager
    def hold(self, conn: Any) -> Iterator[None]:
        yield


def log_deferral(logger: Any, busy: LocalLlmBusy) -> None:
    """The one ``job.deferred_for_lock`` INFO line per deferral (no line per poll)."""
    logger.info("job.deferred_for_lock", reason=busy.reason, until=busy.until.isoformat())
