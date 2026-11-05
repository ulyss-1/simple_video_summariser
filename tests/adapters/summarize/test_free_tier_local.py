"""FreeTierSummarizer's last-resort local Ollama provider (#392): request, replies, order, guard, config."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from adapters.summarize.abortable import CallHandle
from adapters.summarize.factory import SummarizerConfigError, build_summarizer
from adapters.summarize.free_tier import (
    DEFAULT_PROVIDERS_FILE,
    FreeTierConfigError,
    FreeTierSummarizer,
    Provider,
    load_providers,
)
from adapters.summarize.ollama import MAX_RESPONSE_BYTES, HttpResponse
from adapters.summarize.prompt_loader import PromptSet, load_prompt_set
from common.errors import (
    Cancelled,
    Defer,
    LLMInvalidOutputError,
    LLMUnavailableError,
    LocalLlmBusy,
)
from common.models import Chunk, Roster
from tests.adapters.summarize.test_free_tier import (
    GOOD,
    KEY_A,
    META,
    ROSTER_JSON,
    Clock,
    FakeSend,
    completion,
    provider,
    roster,
    settings,
    status,
    write,
)

LOCAL_MODEL = "gemma4:e4b"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def local(host: str = "http://host.docker.internal:11434", ctx: int = 16384) -> Provider:
    return Provider("local-ollama", host, LOCAL_MODEL, ctx, "", None, "ollama", 900)


def local_reply(
    content: str = ROSTER_JSON, *, prompt: int = 21, out: int = 9, reason: str = "stop"
) -> HttpResponse:
    body = {
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": prompt,
        "eval_count": out,
        "done_reason": reason,
    }
    return HttpResponse(200, json.dumps(body).encode())


class Guard:
    """The #393 ``LocalLlmGuard``: ``events`` records the order of lock calls."""

    def __init__(self, events: list[str], *, busy: bool) -> None:
        self.events = events
        self.busy = busy

    def ensure_held(self) -> None:
        self.events.append("ensure_held")
        if self.busy:
            raise LocalLlmBusy(NOW, "whisper_busy")

    def release(self) -> None:
        self.events.append("release")


class LocalScope:
    """A JobScope with the #393 ``local_llm`` guard."""

    def __init__(self, *, free: bool = True) -> None:
        self.events: list[str] = []
        self.local_llm = Guard(self.events, busy=not free)
        self.cancel = False

    def cancelled(self) -> bool:
        return self.cancel

    def now(self) -> datetime:
        return NOW


@pytest.fixture(scope="module")
def prompts() -> PromptSet:
    return load_prompt_set("v1")


def make(
    prompts: PromptSet,
    providers: list[Provider],
    send: Any,
    *,
    clock: Clock | None = None,
    unload: Any = None,
    scope: LocalScope | None = None,
    **extra: Any,
) -> FreeTierSummarizer:
    s = FreeTierSummarizer(
        providers=providers,
        timeout_sec=30,
        cooldown_sec=120,
        prompts=prompts,
        send=send,
        clock=clock or Clock(),
        unload=unload or (lambda p: None),
        **extra,
    )
    if scope is not None:
        s.begin_job(scope)  # type: ignore[arg-type]
    return s


# --- request and reply ----------------------------------------------------------


def test_local_request_is_native_ollama_and_remote_request_is_unchanged(prompts: PromptSet) -> None:
    send = FakeSend(status(500), local_reply())
    s = make(prompts, [provider("a"), local()], send, scope=LocalScope())
    roster(s)
    (remote_name, remote), (local_name, request) = send.calls
    assert (remote_name, local_name) == ("a", "local-ollama")
    assert remote["response_format"] == {"type": "json_object"}
    assert "JSON schema" in remote["messages"][0]["content"]
    assert "think" not in remote and "options" not in remote
    assert request["model"] == LOCAL_MODEL
    assert request["stream"] is False and request["think"] is False
    assert request["options"] == {"num_ctx": 16384}
    assert "format" in request and "response_format" not in request
    assert "Reply with one JSON object only" not in request["messages"][0]["content"]


def test_local_reply_counts_usage_and_logs(prompts: PromptSet, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    send = FakeSend(status(500), local_reply("<think>hm</think>" + ROSTER_JSON, prompt=100, out=40))
    s = make(prompts, [provider("a"), local()], send, scope=LocalScope())
    assert roster(s).speakers
    usage = s.take_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (100, 40, None)
    assert usage.calls == 1
    record = next(r for r in caplog.records if r.message == "free_tier_call")
    assert record.provider == "local-ollama"  # type: ignore[attr-defined]
    assert record.model == LOCAL_MODEL  # type: ignore[attr-defined]
    assert record.finish_reason == "stop"  # type: ignore[attr-defined]
    assert (record.input_tokens, record.output_tokens) == (100, 40)  # type: ignore[attr-defined]
    assert hasattr(record, "duration_ms")


@pytest.mark.parametrize(
    "reply",
    [local_reply(reason="length"), local_reply(prompt=16000, out=384)],
    ids=["done-length", "context-full"],
)
def test_overflow_is_invalid_output_not_a_cooldown(prompts: PromptSet, reply: HttpResponse) -> None:
    send = FakeSend(reply, local_reply())
    s = make(prompts, [provider("a", key=KEY_A), local()], send, scope=LocalScope())
    s._cooling_until["a"] = 10**9  # remote passed over, local is the only one asked
    with pytest.raises(LLMInvalidOutputError):
        roster(s)
    assert "local-ollama" not in s._cooling_until


@pytest.mark.parametrize(
    "failure",
    [
        HttpResponse(200, b"x" * (MAX_RESPONSE_BYTES + 1)),
        HttpResponse(200, b"not json"),
        HttpResponse(200, b'{"error": "model not loaded"}'),
        status(404),
        status(500),
        status(400, "bad request"),
        OSError("refused"),
        TimeoutError("slow"),
    ],
    ids=["too-large", "bad-json", "error-key", "404", "500", "400", "oserror", "timeout"],
)
def test_local_failures_cool_the_provider_down(prompts: PromptSet, failure: Any) -> None:
    clock = Clock()
    send = FakeSend(status(500), failure, status(500), local_reply())
    s = make(prompts, [provider("a"), local()], send, clock=clock, scope=LocalScope())
    with pytest.raises(LLMUnavailableError, match="local-ollama: "):
        roster(s)
    # Right after: the remote is cooling and so is local, nothing is sent.
    n = len(send.calls)
    with pytest.raises(LLMUnavailableError):
        roster(s)
    assert len(send.calls) == n
    clock.now += 121
    send.replies = type(send.replies)([status(500), local_reply()])
    roster(s)
    assert send.order[-1] == "local-ollama"


def test_host_password_never_leaks(prompts: PromptSet, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    host = "http://user:hunter2@ollama.lan:11434/secret?token=abc"
    send = FakeSend(status(500), OSError(f"cannot reach {host}"))
    s = make(prompts, [provider("a"), local(host)], send, scope=LocalScope())
    with pytest.raises(LLMUnavailableError) as err:
        roster(s)
    text = str(err.value) + " ".join(str(r.__dict__) + r.getMessage() for r in caplog.records)
    assert "hunter2" not in text and "token=abc" not in text


def test_local_url_is_host_plus_api_chat() -> None:
    assert local("http://h:11434/").url == "http://h:11434/api/chat"


# --- order -----------------------------------------------------------------------


def test_local_answers_only_after_remotes_fail_and_next_call_starts_at_remote(prompts: PromptSet) -> None:
    send = FakeSend(status(500), status(500), local_reply(), completion(ROSTER_JSON))
    s = make(prompts, [provider("a"), provider("b"), local()], send, scope=LocalScope(), )
    roster(s)
    assert send.order == ["a", "b", "local-ollama"]
    s._cooling_until.clear()
    roster(s)
    assert send.order[3] == "a"


def test_a_remote_answer_never_touches_local_or_the_lock(prompts: PromptSet) -> None:
    scope = LocalScope()
    send = FakeSend(completion(ROSTER_JSON))
    s = make(prompts, [provider("a"), local()], send, scope=scope)
    roster(s)
    s.end_job()
    assert send.order == ["a"] and scope.events == []


def test_keyless_remotes_count_as_failed(prompts: PromptSet) -> None:
    send = FakeSend(local_reply())
    s = make(prompts, [provider("a", key=None), provider("b"), local()], send, scope=LocalScope())
    s._cooling_until["b"] = 10**9
    roster(s)
    assert send.order == ["local-ollama"]


# --- guard, defer, unload ------------------------------------------------------------


def test_lock_is_verified_before_every_local_send(prompts: PromptSet) -> None:
    scope = LocalScope()
    seen: list[list[str]] = []

    def send(p: Provider, body: bytes, timeout: float, handle: CallHandle | None) -> HttpResponse:
        seen.append(list(scope.events))
        return status(500) if p.name == "a" else local_reply()

    s = make(prompts, [provider("a"), local()], send, scope=scope)
    roster(s)
    roster(s)  # "a" is cooling down: only the local send happens
    assert scope.events == ["ensure_held", "ensure_held"]
    # a, local, local: each local send saw one more check than the one before it.
    assert seen == [[], ["ensure_held"], ["ensure_held", "ensure_held"]]


def test_busy_lock_defers_the_job_with_no_send_cooldown_or_retry(prompts: PromptSet) -> None:
    scope = LocalScope(free=False)
    send = FakeSend(status(500))
    s = make(prompts, [provider("a"), local()], send, scope=scope)
    with pytest.raises(LocalLlmBusy) as err:
        roster(s)
    assert isinstance(err.value, Defer) and not isinstance(err.value, LLMUnavailableError)
    assert err.value.reason == "whisper_busy"
    assert send.order == ["a"]  # the local send never went out, the remote was not retried
    assert "local-ollama" not in s._cooling_until
    assert scope.events == ["ensure_held"]  # asked once, not retried
    assert s.take_usage().calls == 0  # no call was answered


def test_busy_lock_but_a_remote_answers_succeeds(prompts: PromptSet) -> None:
    scope = LocalScope(free=False)
    s = make(prompts, [provider("a"), local()], FakeSend(completion(ROSTER_JSON)), scope=scope)
    assert roster(s).speakers
    assert scope.events == []  # never reached the local provider


def test_end_job_unloads_and_never_releases_the_lock(prompts: PromptSet) -> None:
    scope = LocalScope()
    unloaded: list[str] = []

    def unload(p: Provider) -> None:
        scope.events.append("unload")
        unloaded.append(p.model)

    s = make(prompts, [provider("a"), local()], FakeSend(status(500), local_reply()), unload=unload, scope=scope)
    roster(s)
    s.end_job()
    assert unloaded == [LOCAL_MODEL]
    # The analyze handler releases the lock after end_job returns, so unload comes first.
    assert scope.events == ["ensure_held", "unload"]
    s.end_job()  # idempotent
    assert scope.events == ["ensure_held", "unload"]


def test_unload_failure_is_logged_and_end_job_does_not_raise(
    prompts: PromptSet, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    scope = LocalScope()

    def unload(p: Provider) -> None:
        raise OSError("down")

    s = make(prompts, [provider("a"), local()], FakeSend(status(500), local_reply()), unload=unload, scope=scope)
    roster(s)
    s.end_job()
    assert any(r.message == "free_tier_local_unload_failed" for r in caplog.records)


def test_lock_is_not_released_by_the_adapter_after_cancel(prompts: PromptSet) -> None:
    scope = LocalScope()
    scope.cancel = True

    def slow(p: Provider, body: bytes, timeout: float, handle: CallHandle | None) -> HttpResponse:
        if p.name == "a":
            return status(500)
        assert handle is not None
        closed = threading.Event()
        handle.register(closed.set)
        closed.wait(5)
        raise OSError("closed")

    s = make(prompts, [provider("a"), local()], slow, scope=scope, cancel_poll_sec=0.01)
    with pytest.raises(Cancelled):
        roster(s)
    assert "local-ollama" not in s._cooling_until
    s.end_job()
    assert scope.events == ["ensure_held"]


def test_no_job_scope_skips_local(prompts: PromptSet) -> None:
    send = FakeSend(status(500))
    s = make(prompts, [provider("a"), local()], send)
    with pytest.raises(LLMUnavailableError, match="local-ollama: no job scope"):
        roster(s)
    assert send.order == ["a"]


def test_local_alone_is_refused(prompts: PromptSet) -> None:
    with pytest.raises(FreeTierConfigError, match="SUMMARIZER=ollama"):
        make(prompts, [provider("a", key=None), local()], FakeSend())


# --- configuration -----------------------------------------------------------------

OLLAMA_TABLE = """
[[provider]]
name = "local-ollama"
protocol = "ollama"
model = "gemma4:e4b"
"""


def test_shipped_file_ends_with_local_and_flag_gates_it() -> None:
    off = load_providers(DEFAULT_PROVIDERS_FILE, settings(GROQ_API_KEY=KEY_A))
    on = load_providers(
        DEFAULT_PROVIDERS_FILE,
        settings(GROQ_API_KEY=KEY_A, FREE_LLM_LOCAL_FALLBACK=True, OLLAMA_NUM_CTX=16384),
    )
    assert [p.name for p in off] == ["groq", "gemini", "openrouter", "cerebras", "puter"]
    assert [p.name for p in on] == [*(p.name for p in off), "local-ollama"]
    last = on[-1]
    assert (last.protocol, last.model, last.context_tokens, last.api_key) == (
        "ollama", "gemma4:e4b", 16384, None,
    )
    assert last.timeout_sec == 900 and last.base_url == settings().OLLAMA_HOST


def test_a_20000_token_chunk_is_split_with_local_on_and_whole_with_it_off(
    prompts: PromptSet,
) -> None:
    line = "[{sec}] " + "x" * 100  # about 36 tokens by the 3-bytes-per-token estimate
    text = "\n".join(line.format(sec=i) for i in range(560))  # about 20000 tokens
    chunk = Chunk(seq=0, start_sec=0, end_sec=560, text=text)
    answer = completion('{"topics": [], "claims": [], "quotes": []}')

    def sends(providers: list[Provider], replies: int) -> int:
        send = FakeSend(*[answer] * replies)
        s = make(prompts, providers, send)
        s.analyze_chunk(chunk, Roster(()), META)
        return len(send.calls)

    # Budget 14336 with the local window (two pieces), 30720 without (one request).
    assert sends([provider("a"), local()], 2) == 2
    assert sends([provider("a")], 1) == 1


def test_flag_default_is_false() -> None:
    assert settings().FREE_LLM_LOCAL_FALLBACK is False


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (OLLAMA_TABLE + GOOD, "must be the last"),
        (GOOD + OLLAMA_TABLE + OLLAMA_TABLE.replace("local-ollama", "l2"), "at most one"),
        (GOOD + OLLAMA_TABLE + 'base_url = "http://x"\n', "base_url"),
        (GOOD + OLLAMA_TABLE + 'key_setting = "GROQ_API_KEY"\n', "key_setting"),
        (GOOD + OLLAMA_TABLE + "context_tokens = 8192\n", "context_tokens"),
        (GOOD + OLLAMA_TABLE.replace('model = "gemma4:e4b"\n', ""), "'model'"),
    ],
    ids=["not-last", "two", "base-url", "key-setting", "context", "no-model"],
)
@pytest.mark.parametrize("flag", [False, True])
def test_bad_ollama_tables_are_refused_even_when_dropped(
    tmp_path: Path, text: str, message: str, flag: bool
) -> None:
    with pytest.raises(FreeTierConfigError, match=message):
        load_providers(write(tmp_path, text), settings(FREE_LLM_LOCAL_FALLBACK=flag))


def test_small_local_context_is_refused_with_the_flag_on(tmp_path: Path) -> None:
    path = write(tmp_path, GOOD + OLLAMA_TABLE)
    cfg = settings(FREE_LLM_LOCAL_FALLBACK=True, OLLAMA_NUM_CTX=2048, OLLAMA_OUTPUT_RESERVE_TOKENS=512)
    with pytest.raises(FreeTierConfigError, match="OLLAMA_NUM_CTX"):
        load_providers(path, cfg)


def test_factory_wraps_the_config_error(tmp_path: Path) -> None:
    path = write(tmp_path, OLLAMA_TABLE + GOOD)
    with pytest.raises(SummarizerConfigError, match="last"):
        build_summarizer(settings(GROQ_API_KEY=KEY_A, FREE_LLM_PROVIDERS_FILE=str(path)))


def test_prompt_budget_follows_the_local_window() -> None:
    cfg: dict[str, Any] = {"GROQ_API_KEY": KEY_A, "OLLAMA_NUM_CTX": 16384}
    on = FreeTierSummarizer.from_settings(settings(**cfg, FREE_LLM_LOCAL_FALLBACK=True))
    off = FreeTierSummarizer.from_settings(settings(**cfg))
    assert (on._budget, off._budget) == (14336, 30720)
    assert on.model == "free-tier"
