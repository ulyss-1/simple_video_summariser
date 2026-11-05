"""The analyze handler and the local-LLM lock, on a real Postgres (issue #393).

A ``LocalSummarizer`` stands in for the Ollama adapter: its first call asks the job
scope's guard, exactly where ``OllamaSummarizer._chat`` does. ``RemoteSummarizer``
never does. The lock is read from a third connection through ``pg_locks``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from adapters.summarize.free_tier import FreeTierSummarizer, Provider
from adapters.summarize.ollama import HttpResponse
from adapters.summarize.prompt_loader import load_prompt_set
from common.errors import Cancelled, Defer, LocalLlmBusy, ToolFailureError
from common.llm_lock import LOCK_ID, LOCK_NAMESPACE_LLM, PostgresLocalLlmGuard
from common.models import Chunk, ChunkAnalysis, Roster, VideoMeta
from common.queue import PostgresQueue
from common.repo import failed_usage
from services.analyzer import handler as handler_module
from services.analyzer.handler import make_analyze_handler
from tests.adapters.summarize.test_free_tier import provider
from tests.adapters.summarize.test_free_tier_local import local, local_reply
from tests.services.analyzer.fakes import CheckpointingSummarizer, make_job
from tests.services.analyzer.test_handler import VIDEO, Rig

pytestmark = pytest.mark.integration

_LOCKS = """
    SELECT mode FROM pg_locks
    WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 2
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""


class ScopedSummarizer(CheckpointingSummarizer):
    """A job-scoped fake that writes no ``llm_batches`` row (these tests have no real job id)."""

    def _checkpoint(self, step: str) -> None:
        return None


class LocalSummarizer(ScopedSummarizer):
    """Asks the scope's guard before each 'HTTP call' and records the calls that went out."""

    def __init__(self, observe: Callable[[], list[str]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observe = observe
        self.http: list[str] = []
        self.held_during_call: list[list[str]] = []

    def derive_roster(self, meta: VideoMeta, opening: str) -> Roster:
        self.scope.local_llm.ensure_held()
        self.http.append("roster")
        self.held_during_call.append(self.observe())
        return super().derive_roster(meta, opening)


class RemoteSummarizer(ScopedSummarizer):
    """A provider that answers remotely: it never asks the guard."""

    def __init__(self, observe: Callable[[], list[str]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observe = observe
        self.held_during_call: list[list[str]] = []

    def derive_roster(self, meta: VideoMeta, opening: str) -> Roster:
        self.held_during_call.append(self.observe())
        return super().derive_roster(meta, opening)


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


class Wired:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        dsn: str,
        summarizer: CheckpointingSummarizer,
    ) -> None:
        self.dsn = dsn
        self.rig = Rig(conn, dsn, summarizer)
        self.guards: list[PostgresLocalLlmGuard] = []
        self.opened: list[psycopg.Connection[Any]] = []
        self.handler = make_analyze_handler(
            connect=lambda: psycopg.connect(dsn),
            summarizer=summarizer,
            settings=self.rig.settings,
            clock=self.rig.clock,
            local_llm_guard=self._guard,
        )

    def run(self) -> None:
        self.handler(make_job(VIDEO), self.rig.ctx)

    def _connect(self) -> psycopg.Connection[Any]:
        made = psycopg.connect(self.dsn)
        self.opened.append(made)
        return made

    def _guard(self) -> PostgresLocalLlmGuard:
        guard = PostgresLocalLlmGuard(self._connect)
        self.guards.append(guard)
        return guard

    def close(self) -> None:
        for made in self.opened:
            made.close()


@pytest.fixture
def wire(
    conn: psycopg.Connection[Any], head_dsn: str
) -> Iterator[Callable[[CheckpointingSummarizer], Wired]]:
    made: list[Wired] = []

    def build(summarizer: CheckpointingSummarizer) -> Wired:
        wired = Wired(conn, head_dsn, summarizer)
        made.append(wired)
        return wired

    yield build
    for wired in made:
        wired.close()


def _whisper_holder(dsn: str) -> psycopg.Connection[Any]:
    """A side connection holding the transcriber's shared lock."""
    holder = psycopg.connect(dsn, autocommit=True)
    holder.execute("SELECT pg_advisory_lock_shared(%s, %s)", (LOCK_NAMESPACE_LLM, LOCK_ID))
    return holder


def test_a_local_analysis_holds_the_lock_during_its_call_and_frees_it_after(
    wire: Callable[[CheckpointingSummarizer], Wired], modes: Callable[[], list[str]]
) -> None:
    summarizer = LocalSummarizer(modes)
    wired = wire(summarizer)

    wired.run()

    assert summarizer.held_during_call == [["ExclusiveLock"]]
    assert modes() == []
    assert wired.rig.count("analyses") == 1


def test_a_busy_lock_defers_the_job_before_any_local_request_and_writes_no_analysis(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    observer: psycopg.Connection[Any],
    head_dsn: str,
) -> None:
    summarizer = LocalSummarizer(modes)
    wired = wire(summarizer)
    holder = _whisper_holder(head_dsn)
    try:
        with pytest.raises(LocalLlmBusy) as busy:
            wired.run()

        assert busy.value.reason == "whisper_busy"
        assert summarizer.http == []  # no local request went out
        assert wired.rig.count("analyses") == 0
        assert modes() == ["ShareLock"]  # only the transcriber still holds
        holder.execute("SELECT 1")  # the running transcription was not interrupted
    finally:
        holder.close()


def test_the_deferral_logs_one_line_with_the_reason_and_until(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    observer: psycopg.Connection[Any],
    head_dsn: str,
) -> None:
    wired = wire(LocalSummarizer(modes))
    holder = _whisper_holder(head_dsn)
    try:
        with pytest.raises(LocalLlmBusy):
            wired.run()
    finally:
        holder.close()

    lines = [r for r in wired.rig.logs.records("info") if r["event"] == "job.deferred_for_lock"]
    assert len(lines) == 1
    assert lines[0]["reason"] == "whisper_busy"
    assert "until" in lines[0]


def test_through_the_queue_a_busy_analysis_is_pending_again_with_its_attempt_back(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    observer: psycopg.Connection[Any],
    head_dsn: str,
) -> None:
    wired = wire(LocalSummarizer(modes))
    queue = PostgresQueue(observer)
    job_id = queue.enqueue("analyze", VIDEO, dedupe_key="v1:ollama", priority=3)
    assert job_id is not None
    holder = _whisper_holder(head_dsn)
    try:
        with queue.claim(["analyze"], worker="w1") as job:
            assert job is not None
            wired.handler(replace(make_job(VIDEO), id=job.id), wired.rig.ctx)
    finally:
        holder.close()

    row = observer.execute(
        "SELECT state, attempts, priority, last_error, error_class, run_after > now() "
        "FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row == ("pending", 0, 3, "deferred: whisper_busy", None, True)


def test_a_provider_that_answers_remotely_never_touches_the_lock(
    wire: Callable[[CheckpointingSummarizer], Wired], modes: Callable[[], list[str]]
) -> None:
    summarizer = RemoteSummarizer(modes)
    wired = wire(summarizer)

    wired.run()

    assert summarizer.held_during_call == [[]]
    assert modes() == []


@pytest.mark.parametrize(
    "error",
    [
        ToolFailureError("boom"),
        Cancelled(),
        Defer(datetime(2030, 1, 1, tzinfo=UTC)),
        SystemExit(1),
    ],
    ids=["error", "cancelled", "defer", "systemexit"],
)
def test_the_lock_is_released_whichever_way_the_analysis_ends(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    error: BaseException,
) -> None:
    summarizer = LocalSummarizer(modes, fail_on={"chunk:0": error})
    wired = wire(summarizer)

    with pytest.raises(type(error)):
        wired.run()

    assert summarizer.held_during_call == [["ExclusiveLock"]]
    assert modes() == []


def test_the_failed_attempt_rollback_does_not_release_the_lock(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarizer = LocalSummarizer(modes, fail_on={"chunk:0": ToolFailureError("boom")})
    wired = wire(summarizer)
    seen: list[list[str]] = []
    real = failed_usage.insert_failed_usage

    def spy(*args: Any, **kwargs: Any) -> bool:
        seen.append(modes())  # called after conn.rollback()
        return real(*args, **kwargs)

    monkeypatch.setattr(handler_module, "insert_failed_usage", spy)

    with pytest.raises(ToolFailureError):
        wired.run()

    assert seen == [["ExclusiveLock"]]
    assert modes() == []


def test_a_deferral_writes_no_failed_usage_row(
    wire: Callable[[CheckpointingSummarizer], Wired],
    modes: Callable[[], list[str]],
    observer: psycopg.Connection[Any],
    head_dsn: str,
) -> None:
    wired = wire(LocalSummarizer(modes))
    holder = _whisper_holder(head_dsn)
    try:
        with pytest.raises(LocalLlmBusy):
            wired.run()
    finally:
        holder.close()

    assert wired.rig.count("analyze_failed_usage") == 0


# --- the free tier's local fallback through the real guard (#392) -------------------


class FreeTierProbe(FreeTierSummarizer):
    """The real free-tier chain and job hooks; each stage makes one model call, no parsing."""

    fail: BaseException | None = None

    def _ask(self, method: str) -> None:
        self._chat(method, [{"role": "user", "content": "hi"}], None, False)

    def derive_roster(self, meta: VideoMeta, opening: str) -> Roster:
        self._ask("roster")
        return Roster(())

    def analyze_chunk(self, chunk: Chunk, roster: Roster, meta: VideoMeta) -> ChunkAnalysis:
        self._ask("chunk")
        if self.fail is not None:
            raise self.fail
        return ChunkAnalysis(topics=(), claims=(), quotes=())

    def reduce(self, partials: list[ChunkAnalysis], meta: VideoMeta) -> str:
        self._ask("reduce")
        return "tldr"


class FreeWired:
    """A handler over a ``FreeTierProbe`` whose remote always fails, so every call goes local."""

    def __init__(
        self, dsn: str, conn: psycopg.Connection[Any], modes: Callable[[], list[str]]
    ) -> None:
        self.events: list[str] = []
        self.sent: list[str] = []
        self.dsn = dsn
        self.opened: list[psycopg.Connection[Any]] = []

        def send(p: Provider, body: bytes, timeout: float, handle: Any) -> HttpResponse:
            self.sent.append(p.name)
            self.events.append(f"send:{p.name}:{','.join(modes())}")
            if p.name == "remote":
                return HttpResponse(500, b"nope")
            return local_reply()

        def unload(p: Provider) -> None:
            self.events.append(f"unload:{','.join(modes())}")

        self.summarizer = FreeTierProbe(
            providers=[provider("remote"), local()],
            timeout_sec=30,
            cooldown_sec=120,
            prompts=load_prompt_set("v1"),
            send=send,
            unload=unload,
        )
        # Rig only seeds the video and builds its own (unused) handler; it is typed for the fake.
        self.rig = Rig(conn, dsn, self.summarizer)  # type: ignore[arg-type]
        self.handler = make_analyze_handler(
            connect=lambda: psycopg.connect(dsn),
            summarizer=self.summarizer,
            settings=self.rig.settings,
            clock=self.rig.clock,
            local_llm_guard=self._guard,
        )

    def _guard(self) -> PostgresLocalLlmGuard:
        def connect() -> psycopg.Connection[Any]:
            made = psycopg.connect(self.dsn)
            self.opened.append(made)
            return made

        return PostgresLocalLlmGuard(connect)

    def run(self) -> None:
        self.handler(make_job(VIDEO, dedupe_key="v1:free"), self.rig.ctx)

    def close(self) -> None:
        for made in self.opened:
            made.close()


@pytest.fixture
def free_wired(
    conn: psycopg.Connection[Any], head_dsn: str, modes: Callable[[], list[str]]
) -> Iterator[FreeWired]:
    wired = FreeWired(head_dsn, conn, modes)
    yield wired
    wired.close()


def test_free_tier_local_fallback_holds_the_lock_unloads_then_releases(
    free_wired: FreeWired, modes: Callable[[], list[str]]
) -> None:
    free_wired.run()

    sends = [e for e in free_wired.events if e.startswith("send:local-ollama")]
    assert sends and all(e == "send:local-ollama:ExclusiveLock" for e in sends)
    # The model is unloaded while the lock is still held; the handler frees it afterwards.
    assert free_wired.events[-1] == "unload:ExclusiveLock"
    assert modes() == []
    assert free_wired.rig.count("analyses") == 1


@pytest.mark.parametrize(
    "error",
    [ToolFailureError("boom"), Cancelled(), Defer(datetime(2030, 1, 1, tzinfo=UTC))],
    ids=["error", "cancelled", "defer"],
)
def test_free_tier_lock_is_released_after_the_unload_on_every_exit_path(
    free_wired: FreeWired, modes: Callable[[], list[str]], error: BaseException
) -> None:
    free_wired.summarizer.fail = error

    with pytest.raises(type(error)):
        free_wired.run()

    assert free_wired.events[-1] == "unload:ExclusiveLock"
    assert modes() == []


def test_free_tier_busy_lock_defers_with_nothing_sent_locally_and_no_analysis(
    free_wired: FreeWired, modes: Callable[[], list[str]], head_dsn: str
) -> None:
    holder = _whisper_holder(head_dsn)
    try:
        with pytest.raises(LocalLlmBusy) as busy:
            free_wired.run()

        assert busy.value.reason == "whisper_busy"
        assert free_wired.sent == ["remote"]  # one remote try, no local send, no remote retry
        assert not [e for e in free_wired.events if e.startswith("unload")]
        assert free_wired.rig.count("analyses") == 0
        assert free_wired.rig.count("analyze_failed_usage") == 0  # a deferral spends nothing
        assert modes() == ["ShareLock"]
    finally:
        holder.close()
