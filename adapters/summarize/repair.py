"""The one repair attempt for chat-style summarizers (architecture.md 8.1).

Shared by the Ollama and Anthropic adapters. ``call`` sends a message list
and returns the model's reply text; this module decides when a second call
is made and what it contains. It never makes a third.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.summarize.schema import MAX_FEEDBACK_CHARS, parse_with_repair

type Message = dict[str, str]

MAX_FEEDBACK_BYTES = 2048


def cap_feedback(feedback: str) -> str:
    """At most ``MAX_FEEDBACK_BYTES`` UTF-8 bytes and ``MAX_FEEDBACK_CHARS`` characters."""
    text = feedback[:MAX_FEEDBACK_CHARS]
    return text.encode("utf-8")[:MAX_FEEDBACK_BYTES].decode("utf-8", "ignore")


def call_with_repair[T](
    messages: list[Message],
    *,
    call: Callable[[list[Message], bool], str],
    parse: Callable[[str], T],
    render_repair: Callable[[str], str],
) -> T:
    """Call the model, parse the reply, and on invalid output repair once.

    ``call(messages, is_repair)`` returns the reply text. The repair call
    carries the original messages, the bad reply as an ``assistant`` message
    and a ``user`` message built by ``render_repair`` from the validation
    error. A second invalid reply raises ``LLMInvalidOutputError`` (from
    ``parse_with_repair``). Exceptions from ``call`` propagate unchanged.
    """
    reply = call(messages, False)

    def repair(feedback: str) -> str:
        followup: list[Message] = [
            *messages,
            {"role": "assistant", "content": reply},
            {"role": "user", "content": render_repair(cap_feedback(feedback))},
        ]
        return call(followup, True)

    return parse_with_repair(reply, parse, repair)
