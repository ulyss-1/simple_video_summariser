"""A cancel aborts a synchronous Anthropic or Ollama call (issue #302).

Fake transports block on an ``Event`` that only the abort hook sets (forced
interleaving, no sleeps). ``Cancelled`` must pass the adapters' blanket
``except Exception`` handlers, must not be retried or repaired, and must add
nothing to the recorded usage.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from adapters.summarize.abortable import CallHandle
from adapters.summarize.anthropic import AnthropicSummarizer
from adapters.summarize.anthropic import Response as AnthropicResponse
from adapters.summarize.ollama import HttpResponse, OllamaSummarizer
from adapters.summarize.prompt_loader import load_prompt_set
from common.errors import Cancelled, LLMUnavailableError
from common.models import BatchCheckpoint, JobScopedSummarizer
from tests.adapters.summarize.test_anthropic import META, ROSTER_JSON, ok, settings

DEADLINE_SEC = 5.0
POLL = 0.001


class Scope:
    """A ``JobScope`` whose cancel flag is a plain attribute (the handler's is ``ctx.cancelled``)."""

    job_id = 1

    def __init__(self, cancelled: Callable[[], bool] = lambda: False) -> None:
        self._cancelled = cancelled
        self.checkpoint_calls = 0

    def cancelled(self) -> bool:
        return self._cancelled()

    def now(self) -> datetime:
        return datetime(2026, 10, 26, tzinfo=UTC)

    def checkpoint(self, step_key: str) -> BatchCheckpoint | None:
        self.checkpoint_calls += 1
        return None

    def record(self, step_key: str, batch_id: str, request_sha256: str) -> BatchCheckpoint:
        raise AssertionError("a synchronous call records no batch")

    def forget(self, step_key: str) -> None:
        raise AssertionError("a synchronous call forgets no batch")


class Blocking:
    """An abortable transport that blocks until its handle is aborted."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        self.released_by_abort = False
        self.calls = 0

    def __call__(self, *_args: Any, handle: CallHandle) -> Any:
        self.calls += 1
        self.thread = threading.current_thread()
        handle.register(self.release.set)
        self.started.set()
        self.released_by_abort = self.release.wait(DEADLINE_SEC)
        raise OSError("connection closed")

    def cancel_once_started(self) -> bool:
        assert self.started.wait(DEADLINE_SEC), "the request never started"
        return True

    def assert_ended(self) -> None:
        assert self.released_by_abort, "the abort hook never ran"
        assert self.thread is not None and not self.thread.is_alive()


def plain_transport_must_not_run(*_args: Any) -> Any:
    raise AssertionError("a call with a cancel signal goes through the abortable transport")


def anthropic_with(
    abortable: Callable[..., Any], scope: Scope, *, batch: bool = False
) -> AnthropicSummarizer:
    summarizer = AnthropicSummarizer(
        settings(),
        batch=batch,
        transport=plain_transport_must_not_run,
        abortable_transport=abortable,
        cancel_poll_sec=POLL,
    )
    summarizer.begin_job(scope)
    return summarizer


def ollama_with(abortable: Callable[..., Any], scope: Scope | None) -> OllamaSummarizer:
    summarizer = OllamaSummarizer(
        host="http://h:11434",
        model="qwen3.5:4b",
        num_ctx=8192,
        timeout_sec=900,
        prompts=load_prompt_set("v1"),
        transport=plain_transport_must_not_run,
        abortable_transport=abortable,
        cancel_poll_sec=POLL,
    )
    if scope is not None:
        summarizer.begin_job(scope)
    return summarizer


# --- Anthropic, ANTHROPIC_BATCH=0 -----------------------------------------------


def test_a_cancel_aborts_a_blocked_anthropic_call_and_adds_no_usage() -> None:
    blocking = Blocking()
    scope = Scope(blocking.cancel_once_started)
    summarizer = anthropic_with(blocking, scope)

    with pytest.raises(Cancelled):  # not LLMUnavailableError
        summarizer.derive_roster(META, "opening")

    blocking.assert_ended()
    assert blocking.calls == 1  # no retry, no repair call
    usage = summarizer.take_usage()
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (0, 0, 0)
    assert scope.checkpoint_calls == 0  # the sync path creates no llm_batches row


def test_an_anthropic_response_that_arrived_in_the_flag_slice_is_used() -> None:
    finished = threading.Event()
    holder: list[threading.Thread] = []

    def answering(*_args: Any, handle: CallHandle) -> AnthropicResponse:
        holder.append(threading.current_thread())
        finished.set()
        return ok()

    def cancelled() -> bool:
        assert finished.wait(DEADLINE_SEC), "the request never ran"
        holder[0].join(DEADLINE_SEC)
        return True

    summarizer = anthropic_with(answering, Scope(cancelled))

    roster = summarizer.derive_roster(META, "opening")

    assert [s.name for s in roster.speakers] == ["Lex Fridman", "Guest One"]
    assert summarizer.take_usage().calls == 1


def test_a_failing_anthropic_transport_is_still_llm_unavailable() -> None:
    def refusing(*_args: Any, handle: CallHandle) -> Any:
        raise OSError("refused")

    summarizer = anthropic_with(refusing, Scope())

    with pytest.raises(LLMUnavailableError):
        summarizer.derive_roster(META, "opening")


def test_without_a_job_scope_anthropic_uses_the_plain_transport() -> None:
    seen: list[int] = []

    def plain(*_args: Any) -> AnthropicResponse:
        seen.append(1)
        return ok(ROSTER_JSON)

    summarizer = AnthropicSummarizer(settings(), batch=False, transport=plain)

    summarizer.derive_roster(META, "opening")

    assert seen == [1]


# --- Ollama -----------------------------------------------------------------


def test_ollama_is_a_job_scoped_summarizer() -> None:
    assert isinstance(ollama_with(Blocking(), None), JobScopedSummarizer)


def test_a_cancel_aborts_a_blocked_ollama_generation_and_adds_no_usage() -> None:
    blocking = Blocking()
    summarizer = ollama_with(blocking, Scope(blocking.cancel_once_started))

    with pytest.raises(Cancelled):
        summarizer.derive_roster(META, "opening")

    blocking.assert_ended()
    assert blocking.calls == 1
    usage = summarizer.take_usage()
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (0, 0, 0)


def test_an_ollama_response_that_arrived_in_the_flag_slice_is_used() -> None:
    finished = threading.Event()
    holder: list[threading.Thread] = []
    body = json.dumps(
        {
            "message": {"content": ROSTER_JSON},
            "prompt_eval_count": 10,
            "eval_count": 5,
            "done_reason": "stop",
        }
    ).encode()

    def answering(*_args: Any, handle: CallHandle) -> HttpResponse:
        holder.append(threading.current_thread())
        finished.set()
        return HttpResponse(200, body)

    def cancelled() -> bool:
        assert finished.wait(DEADLINE_SEC), "the request never ran"
        holder[0].join(DEADLINE_SEC)
        return True

    summarizer = ollama_with(answering, Scope(cancelled))

    roster = summarizer.derive_roster(META, "opening")

    assert [s.name for s in roster.speakers] == ["Lex Fridman", "Guest One"]
    assert summarizer.take_usage().calls == 1


def test_ollama_forgets_the_scope_at_the_end_of_the_job() -> None:
    plain_calls: list[int] = []

    def plain(*_args: Any) -> HttpResponse:
        plain_calls.append(1)
        return HttpResponse(
            200, json.dumps({"message": {"content": ROSTER_JSON}, "done_reason": "stop"}).encode()
        )

    summarizer = OllamaSummarizer(
        host="http://h:11434",
        model="qwen3.5:4b",
        num_ctx=8192,
        timeout_sec=900,
        prompts=load_prompt_set("v1"),
        transport=plain,
    )
    summarizer.begin_job(Scope())
    summarizer.end_job()

    summarizer.derive_roster(META, "opening")

    assert plain_calls == [1]  # no scope: the plain, unthreaded path
