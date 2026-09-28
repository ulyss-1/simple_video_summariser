"""Versioned prompt files: eager loading and safe rendering (architecture.md 8.2).

Prompts live under ``prompts/<version>/`` and are selected by the caller
(``get_settings().PROMPT_VERSION``; this module never reads the environment).
Every problem with a version is raised by :func:`load_prompt_set`, so a broken
prompt directory fails when a service starts, not partway through a job.

Values that reach a prompt (title, description, transcript, LLM output) come
from outside. They are substituted exactly once through ``string.Template``,
and any tag that would open or close one of the prompt's data blocks is
neutralised, so a value cannot break out of its block.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template

from common.models import Segment

_VERSION_RE = re.compile(r"[a-z0-9_-]+")

# Every block tag the v1 prompts use to fence an untrusted value.
_BLOCK_TAGS = (
    "title",
    "description",
    "opening",
    "roster",
    "transcript",
    "partials",
    "error",
)
# A "<" that starts a block tag: any case, optional "/" and inner whitespace.
_BLOCK_TAG_START = re.compile(
    r"<(?=\s*/?\s*(?:" + "|".join(_BLOCK_TAGS) + r")(?![\w-]))", re.IGNORECASE
)

_USER_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "roster.user.txt": frozenset({"title", "description", "opening"}),
    "chunk.user.txt": frozenset(
        {"title", "roster", "chunk_start", "chunk_end", "transcript"}
    ),
    "reduce.user.txt": frozenset({"title", "partials"}),
    "repair.user.txt": frozenset({"error"}),
}
_SYSTEM_FILES = ("roster.system.txt", "chunk.system.txt", "reduce.system.txt")

_NO_SPEAKERS_LINE = 'No known speakers. Every speaker must be "unknown".'


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """A system prompt (static, cacheable) and the user message built for one call."""

    system: str
    user: str


@dataclass(frozen=True, slots=True)
class PromptSet:
    """One loaded, validated prompt version."""

    version: str
    roster_system: str
    roster_user: Template
    chunk_system: str
    chunk_user: Template
    reduce_system: str
    reduce_user: Template
    repair_user: Template

    def render_roster(
        self, *, title: str, description: str, opening: str
    ) -> RenderedPrompt:
        user = _render(
            self.roster_user, title=title, description=description, opening=opening
        )
        return RenderedPrompt(system=self.roster_system, user=user)

    def render_chunk(
        self,
        *,
        title: str,
        roster: str,
        chunk_start: float,
        chunk_end: float,
        transcript: str,
    ) -> RenderedPrompt:
        user = _render(
            self.chunk_user,
            title=title,
            roster=roster,
            chunk_start=str(math.floor(chunk_start)),
            chunk_end=str(math.floor(chunk_end)),
            transcript=transcript,
        )
        return RenderedPrompt(system=self.chunk_system, user=user)

    def render_reduce(self, *, title: str, partials: str) -> RenderedPrompt:
        if _is_empty_json_list(partials):
            raise ValueError(
                "render_reduce needs at least one partial; there is nothing to reduce"
            )
        user = _render(self.reduce_user, title=title, partials=partials)
        return RenderedPrompt(system=self.reduce_system, user=user)

    def render_repair(self, *, error: str) -> str:
        return _render(self.repair_user, error=error)


def load_prompt_set(version: str, *, root: Path | None = None) -> PromptSet:
    """Load and validate ``<root>/<version>/``.

    ``root`` defaults to the packaged ``prompts/`` directory. Raises
    ``ValueError`` for a malformed version or template, ``FileNotFoundError``
    for a missing directory or file. Unknown extra files are ignored.
    """
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(
            f"invalid prompt version {version!r}: must match ^[a-z0-9_-]+$"
        )
    base = root if root is not None else files("adapters.summarize").joinpath("prompts")
    vdir = base.joinpath(version)
    if not vdir.is_dir():
        raise FileNotFoundError(f"prompt version {version!r} not found: tried {vdir}")

    texts: dict[str, str] = {}
    for name in (*_SYSTEM_FILES, *_USER_PLACEHOLDERS):
        path = vdir.joinpath(name)
        if not path.is_file():
            raise FileNotFoundError(
                f"prompt file {name} missing for version {version!r}: {path}"
            )
        texts[name] = path.read_bytes().decode("utf-8")

    systems = {name: _static_text(name, texts[name]) for name in _SYSTEM_FILES}
    users = {name: _template(name, texts[name]) for name in _USER_PLACEHOLDERS}
    return PromptSet(
        version=version,
        roster_system=systems["roster.system.txt"],
        roster_user=users["roster.user.txt"],
        chunk_system=systems["chunk.system.txt"],
        chunk_user=users["chunk.user.txt"],
        reduce_system=systems["reduce.system.txt"],
        reduce_user=users["reduce.user.txt"],
        repair_user=users["repair.user.txt"],
    )


def format_roster(speakers: Sequence[tuple[str, str]]) -> str:
    """One ``name (role)`` line per speaker, or an explicit no-speakers line."""
    if not speakers:
        return _NO_SPEAKERS_LINE
    return "\n".join(
        f"{_one_line(name)} ({_one_line(role)})" for name, role in speakers
    )


def format_timestamped(segments: Sequence[Segment]) -> str:
    """``[<seconds>] text`` per segment, with ``speaker:`` when one is set."""
    lines: list[str] = []
    for seg in segments:
        text = _one_line(seg.text)
        if not text:
            continue
        if seg.speaker:
            lines.append(f"[{int(seg.start)}] {_one_line(seg.speaker)}: {text}")
        else:
            lines.append(f"[{int(seg.start)}] {text}")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ", text).strip()


def _is_empty_json_list(partials: str) -> bool:
    stripped = partials.strip()
    if not stripped:
        return True
    try:
        return bool(json.loads(stripped) == [])
    except ValueError:
        return False


def _scan(name: str, text: str) -> set[str]:
    """Identifiers used by a template; raise ValueError on an invalid ``$``."""
    tpl = Template(text)
    found: set[str] = set()
    for match in tpl.pattern.finditer(text):
        if match.group("invalid") is not None:
            snippet = text[match.start() : match.start() + 12].split("\n", 1)[0]
            raise ValueError(
                f"{name}: invalid '$' at offset {match.start()} ({snippet!r}); write $$ for a literal dollar"
            )
        ident = match.group("named") or match.group("braced")
        if ident is not None:
            found.add(ident)
    return found


def _static_text(name: str, text: str) -> str:
    found = _scan(name, text)
    if found:
        raise ValueError(
            f"{name}: system prompts must have no placeholders, found {sorted(found)}"
        )
    return Template(text).substitute()


def _template(name: str, text: str) -> Template:
    found = _scan(name, text)
    expected = _USER_PLACEHOLDERS[name]
    if found != expected:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        raise ValueError(
            f"{name}: placeholders differ from the expected set; missing {missing}, extra {extra}"
        )
    return Template(text)


def _neutralise(value: str) -> str:
    """Defuse block tags in a value so it cannot open or close a block."""
    return _BLOCK_TAG_START.sub("&lt;", value)


def _render(template: Template, **values: str) -> str:
    return template.substitute({k: _neutralise(v) for k, v in values.items()})
