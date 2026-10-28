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
    urllib_transport,
)
from adapters.summarize.prompt_loader import PromptSet, load_prompt_set
from adapters.summarize.repair import Message
from common.config import FREE_TIER_MODEL, Settings, get_settings
from common.errors import Cancelled, LLMInvalidOutputError, LLMUnavailableError

_logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS_FILE = Path(__file__).with_name("free_providers.toml")

#: The ``Settings`` fields a provider may name as its ``key_setting``.
KEY_SETTINGS = ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY")

MIN_CONTEXT_TOKENS = 4096
_OUTPUT_RESERVE_TOKENS = 2048
_MAX_REASON_CHARS = 160
_UNUSABLE_STATUSES = {401, 403}


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

    @property
    def url(self) -> str:
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

    providers: list[Provider] = []
    for index, entry in enumerate(entries, 1):
        where = f"{path} provider #{index}"
        if not isinstance(entry, dict):
            raise FreeTierConfigError(f"{where}: must be a table")
        name = _text(entry, "name", where)
        if any(p.name == name for p in providers):
            raise FreeTierConfigError(f"{where}: duplicate provider name {name!r}")
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
        secret = getattr(settings, key_setting)
        key = secret.get_secret_value().strip() if secret is not None else ""
        providers.append(
            Provider(name, base_url, _text(entry, "model", where), context, key_setting, key or None)
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
        send: Send = _bearer_send,
        cancel_poll_sec: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not providers:
            raise FreeTierConfigError("no free-tier providers configured")
        if not any(p.api_key for p in providers):
            raise FreeTierConfigError(
                "no free-tier provider has an API key; set one of "
                + ", ".join(sorted({p.key_setting for p in providers}))
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
            output_reserve_tokens=_OUTPUT_RESERVE_TOKENS,
            timeout_sec=timeout_sec,
            prompts=prompts,
            **extra,
        )
        self._providers = providers
        self._cooldown_sec = cooldown_sec
        self._send = send
        self._cooling_until: dict[str, float] = {}
        self._disabled: set[str] = set()
        self.last_provider: str | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        transport: Transport = urllib_transport,
        send: Send = _bearer_send,
    ) -> FreeTierSummarizer:
        """``transport`` exists for the base signature and is not used."""
        s = settings or get_settings()
        path = s.FREE_LLM_PROVIDERS_FILE or DEFAULT_PROVIDERS_FILE
        return cls(
            providers=load_providers(path, s),
            timeout_sec=s.FREE_LLM_TIMEOUT_SEC,
            cooldown_sec=s.FREE_LLM_COOLDOWN_SEC,
            prompts=load_prompt_set(s.PROMPT_VERSION),
            send=send,
        )

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
            if skipped is not None:
                failures.append(f"{provider.name}: {skipped}")
                continue
            payload: dict[str, Any] = {
                "model": provider.model,
                "messages": sent,
                "stream": False,
            }
            if format is not None:
                payload["response_format"] = {"type": "json_object"}
            request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content = self._try(provider, method, request, is_repair, failures)
            if content is not None:
                return _strip_think(content)
        raise LLMUnavailableError("no free-tier provider answered: " + "; ".join(failures))

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
        if provider.api_key is None:
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

        def fail(reason: str, *, disable: bool = False) -> None:
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
                response = self._send(provider, request, self._timeout, None)
            else:
                response = call_abortably(
                    lambda handle: self._send(provider, request, self._timeout, handle),
                    cancelled=scope.cancelled,
                    poll_interval=self._cancel_poll_sec,
                )
        except Cancelled:
            raise
        except Exception as err:  # noqa: BLE001 - any transport failure moves on to the next provider
            fail(f"no answer ({type(err).__name__})")
            return None

        status = response.status
        if status in _UNUSABLE_STATUSES:
            fail(f"HTTP {status}, key rejected", disable=True)
            return None
        if not 200 <= status < 300:
            snippet = response.body[:MAX_BODY_IN_MESSAGE_BYTES].decode("utf-8", "replace")
            fail(f"HTTP {status} {' '.join(snippet.split())}")
            return None
        self._calls += 1
        if len(response.body) > MAX_RESPONSE_BYTES:
            fail("response too large")
            return None
        parsed = self._parse(response.body)
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
        if finish_reason == "length":
            raise LLMInvalidOutputError(
                f"{provider.name} ({provider.model}) ran out of output length "
                f"({counts[0]} prompt + {counts[1]} output tokens); send less text"
            )
        return content

    @staticmethod
    def _parse(body: bytes) -> tuple[str, str | None, tuple[int, int]] | None:
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        if not isinstance(doc, dict):
            return None
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
