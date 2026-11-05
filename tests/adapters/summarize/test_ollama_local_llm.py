"""``OllamaSummarizer`` and the local-LLM guard of its job scope (issue #393).

No database, no network: a fake scope counts ``ensure_held`` calls and a fake
transport records the requests that would have gone out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.summarize.ollama import OllamaSummarizer
from adapters.summarize.prompt_loader import load_prompt_set
from common.errors import LLMUnavailableError, LocalLlmBusy
from common.models import BatchCheckpoint, Usage
from tests.adapters.summarize.test_ollama import (
    META,
    FakeTransport,
    fixture_response,
    ok,
)

NOW = datetime(2026, 11, 4, tzinfo=UTC)


class CountingGuard:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.checks = 0
        self.releases = 0

    def ensure_held(self) -> None:
        self.checks += 1
        if self.busy:
            raise LocalLlmBusy(NOW, "whisper_busy")

    def release(self) -> None:
        self.releases += 1


class Scope:
    job_id = 1

    def __init__(self, guard: CountingGuard) -> None:
        self.local_llm = guard

    def cancelled(self) -> bool:
        return False

    def now(self) -> datetime:
        return NOW

    def checkpoint(self, step_key: str) -> BatchCheckpoint | None:
        return None

    def record(self, step_key: str, batch_id: str, request_sha256: str) -> BatchCheckpoint:
        raise AssertionError("a synchronous call records no batch")

    def forget(self, step_key: str) -> None:
        raise AssertionError("a synchronous call forgets no batch")

    def record_usage(self, step_key: str, usage: Usage) -> Usage:
        raise AssertionError("a synchronous call stores no batch usage")


def summarizer_with(transport: FakeTransport, scope: Scope | None) -> OllamaSummarizer:
    summarizer = OllamaSummarizer(
        host="http://h:11434",
        model="qwen3.5:4b",
        num_ctx=8192,
        timeout_sec=900,
        prompts=load_prompt_set("v1"),
        transport=transport,
        abortable_transport=lambda url, body, timeout, handle: transport(url, body, timeout),
    )
    if scope is not None:
        summarizer.begin_job(scope)
    return summarizer


def test_the_guard_is_asked_once_before_a_local_call() -> None:
    guard = CountingGuard()
    transport = FakeTransport(fixture_response("chat_roster_response.json"))
    summarizer = summarizer_with(transport, Scope(guard))

    summarizer.derive_roster(META, "opening")

    assert guard.checks == 1
    assert len(transport.requests) == 1


def test_the_guard_is_asked_again_before_the_repair_call() -> None:
    guard = CountingGuard()
    transport = FakeTransport(ok("not json at all"), fixture_response("chat_roster_response.json"))
    summarizer = summarizer_with(transport, Scope(guard))

    summarizer.derive_roster(META, "opening")

    assert len(transport.requests) == 2
    assert guard.checks == 2


def test_a_busy_guard_stops_the_call_before_any_request_and_is_not_wrapped() -> None:
    guard = CountingGuard(busy=True)
    transport = FakeTransport()
    summarizer = summarizer_with(transport, Scope(guard))

    with pytest.raises(LocalLlmBusy) as busy:
        summarizer.derive_roster(META, "opening")

    assert not isinstance(busy.value, LLMUnavailableError)
    assert transport.requests == []
    assert guard.checks == 1  # not retried, not repaired
    assert summarizer.take_usage().calls == 0


def test_the_adapter_never_releases_the_guard() -> None:
    guard = CountingGuard()
    transport = FakeTransport(fixture_response("chat_roster_response.json"))
    summarizer = summarizer_with(transport, Scope(guard))

    summarizer.derive_roster(META, "opening")
    summarizer.end_job()

    assert guard.releases == 0


def test_without_a_job_scope_no_guard_is_consulted() -> None:
    transport = FakeTransport(fixture_response("chat_roster_response.json"))
    summarizer = summarizer_with(transport, None)

    summarizer.derive_roster(META, "opening")  # the CLI path: no scope, no lock

    assert len(transport.requests) == 1


def test_the_job_scope_port_names_the_guard() -> None:
    from common.models import JobScope

    assert hasattr(JobScope, "local_llm")
