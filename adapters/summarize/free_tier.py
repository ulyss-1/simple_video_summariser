"""Free-tier summarizer: an ordered list of providers, first available wins (task #364).

``FreeTierSummarizer`` is the Ollama adapter with a different ``_chat``: prompts,
chunking, parsing and the one repair attempt are inherited, and each model call
goes to the first provider of ``free_providers.toml`` that answers. Every
provider speaks the OpenAI chat-completions protocol.

Per call, a provider is skipped when it has no API key, was disabled by a
401/403, or is cooling down after a 429, 5xx, rejected request or network
failure. When none answers, ``LLMUnavailableError`` leaves the job to the queue's
retry, as an unreachable Ollama does. A reply cut off by the length limit is
``LLMInvalidOutputError``, as for Ollama.

``analyses.model`` is the fixed label ``FREE_TIER_MODEL``: the provider changes
between calls, and ``analysis_exists`` and the display preference key on the
model. The provider and model that served each call are in the log.

The transcript leaves for a third party. API keys come from ``Settings``
(``common.config`` is the only reader of the environment); error text never
carries one.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from adapters.summarize.abortable import (
    CallHandle,
    attach,
    build_abortable_opener,
    call_abortably,
)
from adapters.summarize.ollama import (
    MAX_BODY_IN_MESSAGE_BYTES,
    MAX_RESPONSE_BYTES,
    HttpResponse,
    OllamaSummarizer,
    Transport,
    _count,
    _strip_think,
    build_ollama_payload,
    parse_ollama_reply,
    urllib_transport,
)
from adapters.summarize.ollama import _scrub as scrub_host
from adapters.summarize.prompt_loader import PromptSet, load_prompt_set
from adapters.summarize.repair import Message
from common.config import FREE_TIER_MODEL, Settings, get_settings
from common.errors import (
    BugError,
    Cancelled,
    LLMInvalidOutputError,
    LLMUnavailableError,
)

_logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS_FILE = Path(__file__).with_name("free_providers.toml")

#: The ``Settings`` fields a provider may name as its ``key_setting``.
KEY_SETTINGS = (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "CEREBRAS_API_KEY",
    "PUTER_AUTH_TOKEN",
)

#: How a provider is spoken to. ``openai``: chat completions at ``base_url`` +
#: ``/chat/completions``. ``puter``: the call route behind Puter's ``puter.ai.chat``,
#: ``base_url`` is the whole URL (undocumented by Puter, so best effort).
PROTOCOLS = ("openai", "puter", "ollama")
_THINK_BLOCK = re.compile(r"\A\s*<(think|thought)>.*?</\1>", re.DOTALL)

MIN_CONTEXT_TOKENS = 4096
_OUTPUT_RESERVE_TOKENS = 2048
_MAX_REASON_CHARS = 160
_UNUSABLE_STATUSES ={401, 402, 403}  # 402: Gemini prepay credit depleted


class FreeTierConfigError(ValueError):
    """The provider list or its keys can never work. The message names the problem."""


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    base_url: str
    model: str
    context_tokens: int
    key_setting: str
    api_key: str | None  # None: the key setting is unset, the provider is skipped
    protocol: str = "openai"
    timeout_sec: float | None = None  # None: the summarizer's own timeout

    @property
    def url(self) -> str:
        if self.protocol == "puter":
            return self.base_url
        if self.protocol == "ollama":
            return self.base_url.rstrip("/") + "/api/chat"
        return self.base_url.rstrip("/") + "/chat/completions"


def _text(entry: dict[str, Any], field: str, where: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FreeTierConfigError(f"{where}: '{field}' must be a non-empty string")
    return value.strip()


def load_providers(path: Path, settings: Settings) -> list[Provider]:
    """The validated providers of ``path``, in file order, keys taken from ``settings``."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise FreeTierConfigError(f"cannot read {path}: {type(err).__name__}: {err}") from err
    entries = document.get("provider")
    if not isinstance(entries, list) or not entries:
        raise FreeTierConfigError(f"{path}: needs at least one [[provider]] table")

    if sum(1 for e in entries if isinstance(e, dict) and e.get("protocol") == "ollama") > 1:
        raise FreeTierConfigError(f"{path}: at most one provider may use protocol ollama")

    providers: list[Provider] = []
    for index, entry in enumerate(entries, 1):
        where = f"{path} provider #{index}"
        if not isinstance(entry, dict):
            raise FreeTierConfigError(f"{where}: must be a table")
        name = _text(entry, "name", where)
        if any(p.name == name for p in providers):
            raise FreeTierConfigError(f"{where}: duplicate provider name {name!r}")
        if entry.get("protocol") == "ollama":
            for forbidden in ("base_url", "key_setting", "context_tokens"):
                if forbidden in entry:
                    raise FreeTierConfigError(
                        f"{where}: protocol ollama takes no '{forbidden}' "
                        "(it uses OLLAMA_HOST and OLLAMA_NUM_CTX)"
                    )
            if index != len(entries):
                raise FreeTierConfigError(f"{where}: protocol ollama must be the last provider")
            if settings.FREE_LLM_LOCAL_FALLBACK and settings.OLLAMA_NUM_CTX < MIN_CONTEXT_TOKENS:
                raise FreeTierConfigError(
                    f"{where}: OLLAMA_NUM_CTX must be >= {MIN_CONTEXT_TOKENS} for the local provider"
                )
            local = Provider(
                name,
                settings.OLLAMA_HOST,
                _text(entry, "model", where),
                settings.OLLAMA_NUM_CTX,
                "",
                None,
                "ollama",
                float(settings.OLLAMA_TIMEOUT_SEC),
            )
            if settings.FREE_LLM_LOCAL_FALLBACK:
                providers.append(local)
            continue
        base_url = _text(entry, "base_url", where)
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise FreeTierConfigError(f"{where}: base_url must be an https:// URL without credentials")
        context = entry.get("context_tokens")
        if isinstance(context, bool) or not isinstance(context, int) or context < MIN_CONTEXT_TOKENS:
            raise FreeTierConfigError(
                f"{where}: context_tokens must be an integer >= {MIN_CONTEXT_TOKENS}"
            )
        key_setting = _text(entry, "key_setting", where)
        if key_setting not in KEY_SETTINGS:
            raise FreeTierConfigError(
                f"{where}: key_setting must be one of {', '.join(KEY_SETTINGS)}"
            )
        protocol = entry.get("protocol", "openai")
        if protocol not in PROTOCOLS:
            raise FreeTierConfigError(f"{where}: protocol must be one of {', '.join(PROTOCOLS)}")
        secret = getattr(settings, key_setting)
        key = secret.get_secret_value().strip() if secret is not None else ""
        providers.append(
            Provider(
                name, base_url, _text(entry, "model", where), context, key_setting, key or None, protocol
            )
        )
    return providers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another host: refuse it."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = build_abortable_opener(_NoRedirect())


def bearer_transport(
    url: str, body: bytes, timeout: float, api_key: str, handle: CallHandle | None = None
) -> HttpResponse:
    """POST ``body`` as JSON with a bearer key. HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    attach(request, handle)
    try:
        with _opener.open(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as err:
        with err:
            return HttpResponse(err.code, err.read(MAX_RESPONSE_BYTES + 1))


#: ``send(provider, request_body, timeout_sec, handle)``; raises when there is no response.
type Send = Callable[[Provider, bytes, float, CallHandle | None], HttpResponse]


def _bearer_send(
    provider: Provider, body: bytes, timeout: float, handle: CallHandle | None
) -> HttpResponse:
    assert provider.api_key is not None
    return bearer_transport(provider.url, body, timeout, provider.api_key, handle)


def _default_send(
    provider: Provider, body: bytes, timeout: float, handle: CallHandle | None
) -> HttpResponse:
    if provider.protocol == "ollama":
        return urllib_transport(provider.url, body, timeout, handle)
    return _bearer_send(provider, body, timeout, handle)


UNLOAD_TIMEOUT_SEC = 10.0


def _ollama_unload(provider: Provider) -> None:
    """Ask Ollama to drop the model from memory now (``keep_alive: 0``). Raises on failure."""
    url = provider.base_url.rstrip("/") + "/api/generate"
    body = json.dumps({"model": provider.model, "keep_alive": 0}).encode("utf-8")
    response = urllib_transport(url, body, UNLOAD_TIMEOUT_SEC)
    if not 200 <= response.status < 300:
        raise OSError(f"HTTP {response.status}")


class FreeTierSummarizer(OllamaSummarizer):
    """``Summarizer`` over the first available free-tier provider. One per process."""

    name = "free"

    def __init__(
        self,
        *,
        providers: list[Provider],
        timeout_sec: float,
        cooldown_sec: float,
        prompts: PromptSet,
        send: Send = _default_send,
        cancel_poll_sec: float | None = None,
        clock: Callable[[], float] | None = None,
        output_reserve_tokens: int = _OUTPUT_RESERVE_TOKENS,
        unload: Callable[[Provider], None] = _ollama_unload,
    ) -> None:
        if not providers:
            raise FreeTierConfigError("no free-tier providers configured")
        if not any(p.api_key for p in providers):
            raise FreeTierConfigError(
                "no free-tier provider has an API key; set one of "
                + ", ".join(sorted({p.key_setting for p in providers if p.key_setting}))
                + " (for the local Ollama alone, use SUMMARIZER=ollama)"
            )
        extra: dict[str, Any] = {}
        if cancel_poll_sec is not None:
            extra["cancel_poll_sec"] = cancel_poll_sec
        if clock is not None:
            extra["clock"] = clock
        super().__init__(
            host="https://free-tier.invalid",
            model=FREE_TIER_MODEL,
            # Chunks are sized for the smallest context in the list, so any
            # provider can take any prompt.
            num_ctx=min(p.context_tokens for p in providers),
            output_reserve_tokens=output_reserve_tokens,
            timeout_sec=timeout_sec,
            prompts=prompts,
            **extra,
        )
        self._providers = providers
        self._cooldown_sec = cooldown_sec
        self._send = send
        self._unload = unload
        self._local_held: Provider | None = None  # set once this job reached the local model
        self._cooling_until: dict[str, float] = {}
        self._disabled: set[str] = set()
        self.last_provider: str | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        transport: Transport = urllib_transport,
        send: Send = _default_send,
    ) -> FreeTierSummarizer:
        """``transport`` exists for the base signature and is not used."""
        s = settings or get_settings()
        path = s.FREE_LLM_PROVIDERS_FILE or DEFAULT_PROVIDERS_FILE
        providers = load_providers(path, s)
        # The local window is the smallest, so its reserve sizes every call's budget.
        has_local = any(p.protocol == "ollama" for p in providers)
        return cls(
            providers=providers,
            timeout_sec=s.FREE_LLM_TIMEOUT_SEC,
            cooldown_sec=s.FREE_LLM_COOLDOWN_SEC,
            prompts=load_prompt_set(s.PROMPT_VERSION),
            send=send,
            output_reserve_tokens=(
                s.OLLAMA_OUTPUT_RESERVE_TOKENS if has_local else _OUTPUT_RESERVE_TOKENS
            ),
        )

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._providers)

    @property
    def local_model(self) -> str | None:
        """The model of the local provider, ``None`` when the list has none."""
        return next((p.model for p in self._providers if p.protocol == "ollama"), None)

    # --- the local provider's lock (#392) --------------------------------------------

    def end_job(self) -> None:
        """Unload the local model if this job used it.

        The analyze handler releases the #393 lock after ``end_job`` returns, so the
        model is gone from memory before Whisper may start.
        """
        used, self._local_held = self._local_held, None
        try:
            if used is not None:
                try:
                    self._unload(used)
                except Exception as err:  # noqa: BLE001 - the handler must still release the lock
                    _logger.warning(
                        "free_tier_local_unload_failed",
                        extra={"provider": used.name, "error": type(err).__name__},
                    )
        finally:
            super().end_job()

    def _local_gate(self, provider: Provider) -> str | None:
        """Why the local provider cannot be used now, else ``None`` once the lock is held.

        ``ensure_held`` may raise ``LocalLlmBusy`` (a ``Defer``): it is not caught, so a
        transcription running defers the job, spends no attempt and is no cooldown.
        """
        scope = self._scope
        if scope is None:
            return "no job scope"
        scope.local_llm.ensure_held()
        self._local_held = provider
        return None

    # --- one model call: the first provider that answers -------------------------

    def _chat(
        self,
        method: str,
        messages: list[Message],
        format: dict[str, Any] | None,
        is_repair: bool,
    ) -> str:
        sent = self._with_schema_hint(messages, format)
        failures: list[str] = []
        for provider in self._providers:
            skipped = self._skip_reason(provider)
            if skipped is None and provider.protocol == "ollama":
                skipped = self._local_gate(provider)
            if skipped is not None:
                failures.append(f"{provider.name}: {skipped}")
                continue
            if provider.protocol == "ollama":
                payload = build_ollama_payload(
                    provider.model, messages, format, provider.context_tokens
                )
            else:
                payload = self._payload(provider, sent, format)
            request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content = self._try(provider, method, request, is_repair, failures)
            if content is not None:
                return _THINK_BLOCK.sub("", _strip_think(content), count=1)
        raise LLMUnavailableError("no free-tier provider answered: " + "; ".join(failures))

    @staticmethod
    def _payload(
        provider: Provider, messages: list[Message], format: dict[str, Any] | None
    ) -> dict[str, Any]:
        if provider.protocol == "puter":
            return {
                "interface": "puter-chat-completion",
                "method": "complete",
                "args": {"model": provider.model, "messages": messages},
            }
        payload: dict[str, Any] = {"model": provider.model, "messages": messages, "stream": False}
        if format is not None:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _with_schema_hint(messages: list[Message], format: dict[str, Any] | None) -> list[Message]:
        """JSON mode guarantees JSON, not the shape: say the shape in the system message."""
        if format is None:
            return messages
        hint = (
            "\n\nReply with one JSON object only, matching this JSON schema:\n"
            + json.dumps(format, ensure_ascii=False)
        )
        out = [dict(m) for m in messages]
        for message in out:
            if message["role"] == "system":
                message["content"] += hint
                break
        else:
            out.insert(0, {"role": "system", "content": hint.strip()})
        return out

    def _skip_reason(self, provider: Provider) -> str | None:
        if provider.api_key is None and provider.protocol != "ollama":
            return f"no key ({provider.key_setting} unset)"
        if provider.name in self._disabled:
            return "disabled (key rejected)"
        until = self._cooling_until.get(provider.name)
        if until is not None and self._clock() < until:
            return "cooling down"
        return None

    def _try(
        self,
        provider: Provider,
        method: str,
        request: bytes,
        is_repair: bool,
        failures: list[str],
    ) -> str | None:
        """The reply text, or ``None`` after recording why this provider was passed over."""
        started = self._clock()

        timeout = provider.timeout_sec or self._timeout

        def fail(reason: str, *, disable: bool = False) -> None:
            if provider.protocol == "ollama":
                reason = scrub_host(reason, provider.base_url)
            reason = self._scrub(reason, provider)[:_MAX_REASON_CHARS]
            if disable:
                self._disabled.add(provider.name)
            else:
                self._cooling_until[provider.name] = self._clock() + self._cooldown_sec
            failures.append(f"{provider.name}: {reason}")
            _logger.warning(
                "free_tier_provider_skipped",
                extra={"provider": provider.name, "model": provider.model, "reason": reason},
            )

        try:
            scope = self._scope
            if scope is None:
                response = self._send(provider, request, timeout, None)
            else:
                response = call_abortably(
                    lambda handle: self._send(provider, request, timeout, handle),
                    cancelled=scope.cancelled,
                    poll_interval=self._cancel_poll_sec,
                )
        except Cancelled:
            raise
        except Exception as err:  # noqa: BLE001 - any transport failure moves on to the next provider
            fail(f"no answer ({type(err).__name__})")
            return None

        status = response.status
        parsed: tuple[str, str | None, tuple[int, int]] | None
        if provider.protocol == "ollama":
            # Ollama's own reply shape and status handling; a 4xx here is a skip, never a BUG.
            self._calls += 1
            try:
                reply = parse_ollama_reply(response, model=provider.model)
            except (LLMUnavailableError, BugError) as err:
                fail(str(err))
                return None
            parsed = (reply.content, reply.done_reason, (reply.input_tokens, reply.output_tokens))
        else:
            if status in _UNUSABLE_STATUSES:
                fail(f"HTTP {status}, key rejected or out of credit", disable=True)
                return None
            if not 200 <= status < 300:
                snippet = response.body[:MAX_BODY_IN_MESSAGE_BYTES].decode("utf-8", "replace")
                fail(f"HTTP {status} {' '.join(snippet.split())}")
                return None
            self._calls += 1
            if len(response.body) > MAX_RESPONSE_BYTES:
                fail("response too large")
                return None
            parsed = self._parse(response.body, provider.protocol)
            if parsed is None:
                fail("reply is not a chat completion")
                return None
        content, finish_reason, counts = parsed
        self._input_tokens += counts[0]
        self._output_tokens += counts[1]
        self.last_provider = provider.name
        _logger.info(
            "free_tier_call",
            extra={
                "method": method,
                "provider": provider.name,
                "model": provider.model,
                "input_tokens": counts[0],
                "output_tokens": counts[1],
                "duration_ms": int((self._clock() - started) * 1000),
                "finish_reason": finish_reason,
                "repair": is_repair,
                "request_chars": len(request),
                "response_chars": len(content),
            },
        )
        if provider.protocol == "ollama" and sum(counts) >= provider.context_tokens:
            finish_reason = "length"  # the window is full: the reply may be cut
        if finish_reason == "length":
            raise LLMInvalidOutputError(
                f"{provider.name} ({provider.model}) ran out of output length "
                f"({counts[0]} prompt + {counts[1]} output tokens); send less text"
            )
        return content

    @staticmethod
    def _parse(body: bytes, protocol: str = "openai") -> tuple[str, str | None, tuple[int, int]] | None:
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        if not isinstance(doc, dict):
            return None
        if protocol == "puter":
            # {"success": true, "result": {"message": ..., "finish_reason": ..., "usage": ...}}
            result = doc.get("result")
            if doc.get("success") is not True or not isinstance(result, dict):
                return None
            doc = {"choices": [result], "usage": result.get("usage")}
        choices = doc.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        usage = doc.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        counts = (
            _count(usage.get("prompt_tokens"), "prompt_tokens"),
            _count(usage.get("completion_tokens"), "completion_tokens"),
        )
        return content, reason if isinstance(reason, str) else None, counts

    @staticmethod
    def _scrub(text: str, provider: Provider) -> str:
        return text.replace(provider.api_key, "***") if provider.api_key else text
