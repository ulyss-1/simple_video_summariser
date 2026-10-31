"""FreeTierSummarizer: provider order, skips, cool-down, failures, usage, config (#364)."""

from __future__ import annotations

import json
import threading
from collections import deque
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
    bearer_transport,
    load_providers,
)
from adapters.summarize.ollama import HttpResponse
from adapters.summarize.prompt_loader import PromptSet, load_prompt_set
from common.config import FREE_TIER_MODEL, Settings, summarizer_model
from common.errors import Cancelled, LLMInvalidOutputError, LLMUnavailableError
from common.models import Roster, Summarizer, VideoMeta

FIXTURES = Path(__file__).parent / "fixtures"
ROSTER_JSON = json.loads((FIXTURES / "chat_roster_response.json").read_text())["message"]["content"]
META = VideoMeta(
    video_id="dQw4w9WgXcQ",
    channel_id="UC0123456789abcdefghijkl",
    title="Episode title",
    description="Episode description",
    duration_sec=3600,
    published_at=None,
    language="en",
    live_status=None,
    manual_subtitle_langs=(),
    auto_caption_langs=(),
)
KEY_A = "sk-secret-A"
KEY_B = "sk-secret-B"


def provider(name: str, key: str | None = "k", ctx: int = 32768) -> Provider:
    return Provider(name, f"https://{name}.example/v1", f"{name}-model", ctx, "GROQ_API_KEY", key)


def completion(content: str, *, finish: str = "stop", prompt: int = 11, out: int = 7) -> HttpResponse:
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out},
    }
    return HttpResponse(200, json.dumps(body).encode())


def status(code: int, body: str = "nope") -> HttpResponse:
    return HttpResponse(code, body.encode())


class FakeSend:
    """Replays one queued reply per provider call; records (provider name, request)."""

    def __init__(self, *replies: HttpResponse | BaseException) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(
        self, p: Provider, body: bytes, timeout: float, handle: CallHandle | None
    ) -> HttpResponse:
        self.calls.append((p.name, json.loads(body)))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def order(self) -> list[str]:
        return [name for name, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def prompts() -> PromptSet:
    return load_prompt_set("v1")


def make(
    prompts: PromptSet,
    providers: list[Provider],
    send: FakeSend,
    clock: Clock | None = None,
    cooldown: float = 120,
) -> FreeTierSummarizer:
    return FreeTierSummarizer(
        providers=providers,
        timeout_sec=30,
        cooldown_sec=cooldown,
        prompts=prompts,
        send=send,
        clock=clock or Clock(),
    )


def roster(s: FreeTierSummarizer) -> Roster:
    return s.derive_roster(META, "opening words")


def test_satisfies_the_summarizer_port(prompts: PromptSet) -> None:
    s: Summarizer = make(prompts, [provider("a")], FakeSend())
    assert (s.name, s.model) == ("free", FREE_TIER_MODEL)


def test_first_provider_answers_and_later_ones_are_not_called(prompts: PromptSet) -> None:
    send = FakeSend(completion(ROSTER_JSON))
    s = make(prompts, [provider("a"), provider("b")], send)
    roster(s)
    assert send.order == ["a"]
    assert s.last_provider == "a"
    assert send.calls[0][1]["model"] == "a-model"


def test_json_mode_and_schema_hint_are_sent(prompts: PromptSet) -> None:
    send = FakeSend(completion(ROSTER_JSON))
    roster(make(prompts, [provider("a")], send))
    request = send.calls[0][1]
    assert request["response_format"] == {"type": "json_object"}
    assert request["stream"] is False
    system = request["messages"][0]["content"]
    assert "JSON schema" in system and '"speakers"' in system


@pytest.mark.parametrize(
    "failure",
    [status(429, "rate limited"), status(503), status(400, "bad model"), OSError("boom"),
     completion(""), HttpResponse(200, b"not json")],
    ids=["429", "503", "400", "network", "empty-content", "not-json"],
)
def test_falls_through_to_the_next_provider(prompts: PromptSet, failure: Any) -> None:
    send = FakeSend(failure, completion(ROSTER_JSON))
    s = make(prompts, [provider("a"), provider("b")], send)
    roster(s)
    assert send.order == ["a", "b"]
    assert s.last_provider == "b"


def test_a_provider_without_a_key_is_never_called(prompts: PromptSet) -> None:
    send = FakeSend(completion(ROSTER_JSON))
    roster(make(prompts, [provider("a", key=None), provider("b")], send))
    assert send.order == ["b"]


def test_rate_limited_provider_cools_down_then_is_tried_again(prompts: PromptSet) -> None:
    clock = Clock()
    send = FakeSend(
        status(429), completion(ROSTER_JSON),  # a limited, b answers
        completion(ROSTER_JSON),  # a still cooling: b again
        completion(ROSTER_JSON),  # cool-down over: a first again
    )
    s = make(prompts, [provider("a"), provider("b")], send, clock, cooldown=120)
    roster(s)
    clock.now += 60
    roster(s)
    clock.now += 61
    roster(s)
    assert send.order == ["a", "b", "b", "a"]


@pytest.mark.parametrize("code", [401, 402, 403])
def test_a_rejected_key_disables_the_provider_for_good(prompts: PromptSet, code: int) -> None:
    clock = Clock()
    send = FakeSend(status(code), completion(ROSTER_JSON), completion(ROSTER_JSON))
    s = make(prompts, [provider("a"), provider("b")], send, clock)
    roster(s)
    clock.now += 10_000
    roster(s)
    assert send.order == ["a", "b", "b"]


def test_all_failing_is_unavailable_and_names_each_provider(prompts: PromptSet) -> None:
    send = FakeSend(status(429), OSError("x"))
    s = make(prompts, [provider("a"), provider("b"), provider("c", key=None)], send)
    with pytest.raises(LLMUnavailableError) as err:
        roster(s)
    text = str(err.value)
    assert "a: HTTP 429" in text and "b: no answer (OSError)" in text and "c: no key" in text


def test_error_text_never_carries_the_key(prompts: PromptSet) -> None:
    send = FakeSend(status(500, f"invalid key {KEY_A} for request"))
    s = make(prompts, [provider("a", key=KEY_A)], send)
    with pytest.raises(LLMUnavailableError) as err:
        roster(s)
    assert KEY_A not in str(err.value)


def test_length_cutoff_is_invalid_output_not_a_fallback(prompts: PromptSet) -> None:
    send = FakeSend(completion("{", finish="length"))
    s = make(prompts, [provider("a"), provider("b")], send)
    with pytest.raises(LLMInvalidOutputError):
        roster(s)
    assert send.order == ["a"]


def test_usage_is_counted_and_reset(prompts: PromptSet) -> None:
    s = make(prompts, [provider("a")], FakeSend(completion(ROSTER_JSON, prompt=100, out=40)))
    roster(s)
    usage = s.take_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.calls, usage.cost_usd) == (100, 40, 1, None)
    assert s.take_usage().calls == 0


def test_think_block_is_stripped(prompts: PromptSet) -> None:
    send = FakeSend(completion("<think>hmm</think>" + ROSTER_JSON))
    assert roster(make(prompts, [provider("a")], send)).speakers


def test_chunks_are_sized_for_the_smallest_context(prompts: PromptSet) -> None:
    s = make(prompts, [provider("a", ctx=131072), provider("b", ctx=8192)], FakeSend())
    assert s._budget == 8192 - 2048


class Scope:
    def __init__(self) -> None:
        self.flag = False

    def cancelled(self) -> bool:
        return self.flag


def test_cancel_stops_the_call_and_does_not_try_the_next_provider(prompts: PromptSet) -> None:
    scope = Scope()
    scope.flag = True
    calls: list[str] = []

    def slow(p: Provider, body: bytes, timeout: float, handle: CallHandle | None) -> HttpResponse:
        assert handle is not None
        calls.append(p.name)
        closed = threading.Event()
        handle.register(closed.set)  # what a real socket close does
        closed.wait(5)
        raise OSError("closed")

    s = FreeTierSummarizer(
        providers=[provider("a"), provider("b")],
        timeout_sec=30,
        cooldown_sec=1,
        prompts=prompts,
        send=slow,
        cancel_poll_sec=0.01,
    )
    s.begin_job(scope)  # type: ignore[arg-type]
    with pytest.raises(Cancelled):
        roster(s)
    assert calls == ["a"]


# --- configuration ---------------------------------------------------------


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"DATABASE_URL": "postgresql://u:p@h/d", "SUMMARIZER": "free"}
    return Settings.model_validate({**base, **overrides})


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "providers.toml"
    path.write_text(text)
    return path


GOOD = """
[[provider]]
name = "a"
base_url = "https://a.example/v1"
model = "m"
context_tokens = 8192
key_setting = "GROQ_API_KEY"
"""


def test_default_list_loads_and_takes_keys_from_settings() -> None:
    providers = load_providers(DEFAULT_PROVIDERS_FILE, settings(GROQ_API_KEY=KEY_A))
    assert [p.name for p in providers] == ["groq", "gemini", "openrouter", "cerebras"]
    assert [p.api_key for p in providers] == [KEY_A, None, None, None]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "at least one"),
        (GOOD.replace("https://", "http://"), "https"),
        (GOOD.replace("https://a.example", "https://u:p@a.example"), "credentials"),
        (GOOD.replace("8192", "100"), "context_tokens"),
        (GOOD.replace("GROQ_API_KEY", "HOME"), "key_setting"),
        (GOOD + GOOD, "duplicate"),
        (GOOD.replace('model = "m"\n', ""), "'model'"),
        ("[[provider", "cannot read"),
    ],
    ids=["empty", "http", "credentials", "tiny-context", "bad-key-setting", "duplicate",
         "missing-model", "bad-toml"],
)
def test_bad_provider_files_are_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(FreeTierConfigError, match=message):
        load_providers(write(tmp_path, text), settings())


def test_factory_builds_the_free_summarizer() -> None:
    s = build_summarizer(settings(GROQ_API_KEY=KEY_A))
    assert isinstance(s, FreeTierSummarizer)
    assert summarizer_model(settings()) == FREE_TIER_MODEL


def test_factory_refuses_when_no_provider_has_a_key() -> None:
    with pytest.raises(SummarizerConfigError, match="API key"):
        build_summarizer(settings())


def test_factory_refuses_a_bad_providers_file(tmp_path: Path) -> None:
    with pytest.raises(SummarizerConfigError, match="SUMMARIZER=free"):
        build_summarizer(settings(GROQ_API_KEY=KEY_A, FREE_LLM_PROVIDERS_FILE=write(tmp_path, "")))


def test_keys_are_not_printed_by_settings_repr() -> None:
    assert KEY_A not in repr(settings(GROQ_API_KEY=KEY_A, GEMINI_API_KEY=KEY_B))


# --- the real transport ------------------------------------------------------


def test_bearer_transport_sends_the_key_and_refuses_redirects() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/ok":
                seen["auth"] = self.headers.get("Authorization", "")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert bearer_transport(base + "/ok", b"{}", 5, KEY_A).status == 200
        assert seen["auth"] == f"Bearer {KEY_A}"
        assert bearer_transport(base + "/redirect", b"{}", 5, KEY_A).status == 302
    finally:
        server.shutdown()
        server.server_close()
