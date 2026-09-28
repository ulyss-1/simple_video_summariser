"""Summarizer selection (issue #33, architecture.md 8.2).

``build_summarizer`` turns the one ``SUMMARIZER`` setting into a ``Summarizer``
port implementation. It lives in ``adapters/`` so the analyzer service and the
CLI (#31) can share it without either importing the other.

Building makes no network call: the Ollama adapter only records its URL and
the Anthropic adapter only records its key, so an unreachable host or a wrong
key shows up when a job runs, not at startup. What *is* checked at build time
is configuration that can never work, so the process fails once, at startup,
with a message naming the variable.
"""

from __future__ import annotations

from adapters.summarize.anthropic import AnthropicSummarizer
from adapters.summarize.ollama import OllamaSummarizer
from common.config import Settings
from common.models import Summarizer


class SummarizerConfigError(ValueError):
    """The settings cannot produce a working summarizer. The message names the variable."""


def build_summarizer(settings: Settings) -> Summarizer:
    """The summarizer for ``settings.SUMMARIZER``; its ``name`` equals that value."""
    if settings.SUMMARIZER == "ollama":
        return OllamaSummarizer.from_settings(settings)
    if settings.SUMMARIZER == "anthropic":
        secret = settings.ANTHROPIC_API_KEY
        if secret is None or not secret.get_secret_value().strip():
            raise SummarizerConfigError(
                "ANTHROPIC_API_KEY must be set to a non-empty value when SUMMARIZER=anthropic"
            )
        return AnthropicSummarizer(settings)
    raise SummarizerConfigError(
        f"SUMMARIZER must be 'ollama' or 'anthropic', got {settings.SUMMARIZER!r}"
    )
