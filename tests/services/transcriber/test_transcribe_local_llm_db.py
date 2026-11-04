"""The transcribe handler and the local-LLM lock, on a real Postgres (issue #393)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from common.errors import Cancelled, LocalLlmBusy, ToolFailureError
from common.llm_lock import (
    LOCK_ID,
    LOCK_NAMESPACE_LLM,
    PostgresLocalLlmGuard,
    WhisperGate,
)
from common.queue import PostgresQueue
from tests.services.transcriber.fakes import make_job
from tests.services.transcriber.test_transcribe import (
    VID,
    FakeAudio,
    FakeTranscriber,
    Rig,
    make_rig,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.integration

SMALL = timedelta(seconds=2)
_LOCKS = """
    SELECT mode FROM pg_locks
    WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 2
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""


@pytest.fixture
def observer(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def modes(observer: psycopg.Connection[Any]) -> Callable[[], list[str]]:
    def read() -> list[str]:
        rows = observer.execute(_LOCKS, (LOCK_NAMESPACE_LLM, LOCK_ID)).fetchall()
        return sorted(str(mode) for (mode,) in rows)

    return read


@pytest.fixture
def gate_rig(
    make_rig: Callable[..., Rig],  # noqa: F811
) -> Callable[..., Rig]:
    def build(**kwargs: Any) -> Rig:
        holder: list[Rig] = []

        def connect() -> psycopg.Connection[Any]:
            return holder[0]._connect()

        gate = WhisperGate(connect, busy_defer=SMALL)
        rig = make_rig(whisper_gate=gate, **kwargs)
        holder.append(rig)
        return rig

    return build


def test_a_running_transcription_holds_the_shared_lock_and_frees_it_when_done(
    gate_rig: Callable[..., Rig], modes: Callable[[], list[str]]
) -> None:
    seen: list[list[str]] = []
    rig = gate_rig(transcriber=FakeTranscriber(on_call=lambda _: seen.append(modes())))

    rig.run()

    assert seen == [["ShareLock"]]
    assert modes() == []
    assert rig.count("transcripts") == 1


def test_while_an_analysis_holds_the_lock_the_job_defers_and_fetches_no_audio(
    gate_rig: Callable[..., Rig], modes: Callable[[], list[str]], head_dsn: str
) -> None:
    audio, transcriber = FakeAudio(), FakeTranscriber()
    rig = gate_rig(audio=audio, transcriber=transcriber)
    analysis = PostgresLocalLlmGuard(lambda: psycopg.connect(head_dsn), busy_defer=SMALL)
    analysis.ensure_held()
    try:
        with pytest.raises(LocalLlmBusy) as busy:
            rig.run()

        assert busy.value.reason == "local_llm_busy"
        assert audio.calls == []
        assert transcriber.calls == []
        assert modes() == ["ExclusiveLock"]  # the analysis is not interrupted
    finally:
        analysis.release()


def test_through_the_queue_a_busy_transcribe_is_pending_again_with_its_attempt_back(
    gate_rig: Callable[..., Rig], observer: psycopg.Connection[Any], head_dsn: str
) -> None:
    rig = gate_rig()
    queue = PostgresQueue(observer)
    job_id = queue.enqueue("transcribe", VID, priority=4)
    assert job_id is not None
    analysis = PostgresLocalLlmGuard(lambda: psycopg.connect(head_dsn), busy_defer=SMALL)
    analysis.ensure_held()
    try:
        with queue.claim(["transcribe"], worker="w1") as job:
            assert job is not None
            rig.handler(replace(make_job(VID, priority=4), id=job.id, kind="transcribe"), rig.ctx)
    finally:
        analysis.release()

    row = observer.execute(
        "SELECT state, attempts, priority, last_error, error_class, run_after > now() "
        "FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row == ("pending", 0, 4, "deferred: local_llm_busy", None, True)


def test_the_deferral_logs_one_line_with_the_reason_and_until(
    gate_rig: Callable[..., Rig], head_dsn: str
) -> None:
    rig = gate_rig()
    analysis = PostgresLocalLlmGuard(lambda: psycopg.connect(head_dsn), busy_defer=SMALL)
    analysis.ensure_held()
    try:
        with pytest.raises(LocalLlmBusy):
            rig.run()
    finally:
        analysis.release()

    lines = [r for r in rig.logs.records("info") if r["event"] == "job.deferred_for_lock"]
    assert len(lines) == 1
    assert lines[0]["reason"] == "local_llm_busy"
    assert "until" in lines[0]


def test_a_transcribe_yields_to_a_waiting_analysis(
    gate_rig: Callable[..., Rig], observer: psycopg.Connection[Any], modes: Callable[[], list[str]]
) -> None:
    audio = FakeAudio()
    rig = gate_rig(audio=audio)
    observer.execute(
        "INSERT INTO jobs (kind, video_id, dedupe_key, state, run_after, last_error) "
        "VALUES ('analyze', %s, 'v1:ollama', 'pending', now() + interval '3 seconds', "
        "'deferred: whisper_busy')",
        (VID,),
    )

    with pytest.raises(LocalLlmBusy) as busy:
        rig.run()

    assert busy.value.reason == "analysis_waiting"
    assert audio.calls == []
    assert modes() == []


@pytest.mark.parametrize(
    "outcome",
    [ToolFailureError("whisper died"), Cancelled(), SystemExit(1)],
    ids=["error", "cancelled", "systemexit"],
)
def test_the_lock_is_released_whichever_way_the_transcription_ends(
    gate_rig: Callable[..., Rig], modes: Callable[[], list[str]], outcome: BaseException
) -> None:
    seen: list[list[str]] = []
    rig = gate_rig(transcriber=FakeTranscriber(outcome, on_call=lambda _: seen.append(modes())))

    with pytest.raises(type(outcome)):
        rig.run()

    assert seen == [["ShareLock"]]
    assert modes() == []


def test_a_cancel_that_stops_the_whisper_child_releases_the_lock(
    gate_rig: Callable[..., Rig], modes: Callable[[], list[str]]
) -> None:
    holder: list[Rig] = []
    rig = gate_rig(
        transcriber=FakeTranscriber(on_call=lambda _: holder[0].ctx._cancel(), progress=(1.0,))
    )
    holder.append(rig)

    with pytest.raises(Cancelled):
        rig.run()

    assert modes() == []


def test_an_ingest_job_never_touches_the_lock(
    modes: Callable[[], list[str]], observer: psycopg.Connection[Any]
) -> None:
    # The gate is built only for the transcribe handler (services/transcriber/main.py);
    # this pins that nothing in the ingest module reaches for the lock.
    from services.transcriber import ingest

    assert "llm_lock" not in vars(ingest)
    assert modes() == []
