"""Internal text/JSON hygiene helpers shared by ``common/repo/`` modules.

Postgres rejects a NUL byte (``\\x00``) inside a ``TEXT`` or ``JSONB``
value outright - transcripts and LLM output can contain one (issue #14's
"Text hygiene" criterion) - so every write path strips it before the value
reaches the database, instead of letting the write fail.

Private to ``common/repo/``: not part of the public repository API.
"""

from __future__ import annotations


def clean_text(value: str | None) -> str | None:
    """Remove NUL bytes from ``value``; ``None`` stays ``None``."""
    if value is None:
        return None
    return value.replace("\x00", "")


def clean_json(value: object) -> object:
    """Recursively remove NUL bytes from every string inside ``value``.

    ``value`` is whatever is about to be stored in a ``JSONB`` column: a
    dict, a list, a scalar, or ``None``. Tuples are normalized to lists,
    which is how they would round-trip through JSON anyway.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [clean_json(item) for item in value]
    return value
