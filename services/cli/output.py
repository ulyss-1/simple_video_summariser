"""What the CLI prints to stdout, and the scrubbing of untrusted text (issue #31).

Titles come from YouTube and the ``tldr`` and topic titles from an LLM that has
read a transcript anyone can write. All of it is untrusted, and a terminal
treats some bytes as commands (clear the screen, set the window title, rewrite
earlier lines), so ``sanitize_text`` runs on every such string before it is
printed.
"""

from __future__ import annotations

import math
import re

from common.models import Analysis

# Order matters: strings (OSC, DCS, SOS, PM, APC) first, then CSI, then any
# other two-byte escape. An unterminated string swallows the rest of the text.
_ESCAPE_STRING = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?")
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?")
_ESCAPE_OTHER = re.compile(r"\x1b[ -/]*[0-~]?")
# C0 and C1 controls and DEL, except tab (\x09) and newline (\x0a).
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_text(text: str) -> str:
    """``text`` without ANSI escape sequences and C0/C1 control characters.

    ``\\n`` and ``\\t`` stay.
    """
    text = _ESCAPE_STRING.sub("", text)
    text = _CSI.sub("", text)
    text = _ESCAPE_OTHER.sub("", text)
    return _CONTROLS.sub("", text)


def format_timestamp(seconds: float) -> str:
    """``m:ss``, or ``h:mm:ss`` from one hour on; fractions are dropped."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_result(
    *, title: str, video_id: str, transcript_source: str, analysis: Analysis
) -> str:
    """The analysis as text for stdout. Every free-form field is sanitized here."""
    lines = [
        f"{sanitize_text(title)} ({sanitize_text(video_id)})",
        f"Transcript source: {sanitize_text(transcript_source)}",
        f"Model: {sanitize_text(analysis.model)}",
        f"Prompt version: {sanitize_text(analysis.prompt_version)}",
        "",
        "TL;DR",
        sanitize_text(analysis.tldr),
        "",
        "Topics",
    ]
    for topic in analysis.topics:
        start = topic.start_sec
        prefix = (
            f"[{format_timestamp(start)}] "
            if start is not None and math.isfinite(start) and start >= 0
            else ""
        )
        lines.append(f"  {prefix}{sanitize_text(topic.title)}")
    lines += ["", f"Claims: {len(analysis.claims)}", f"Quotes: {len(analysis.quotes)}"]
    return "\n".join(lines) + "\n"
