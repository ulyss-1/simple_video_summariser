"""Database connections (architecture.md §2; issue #11).

``connect()`` is the only place that opens a psycopg connection from
``DATABASE_URL``. Callers (``PostgresQueue``, the repo layer, ...) receive a
connection; per architecture.md §5's "Database connection" acceptance
criterion, none of them create their own.
"""

from __future__ import annotations

from typing import Any

import psycopg

from common.config import get_settings


def connect() -> psycopg.Connection[Any]:
    """Open a new psycopg connection to ``get_settings().DATABASE_URL``.

    psycopg accepts the ``postgresql://`` scheme directly - unlike
    SQLAlchemy, which ``migrations/env.py`` must rewrite to
    ``postgresql+psycopg://`` - so the DSN is passed through unchanged.
    """
    return psycopg.connect(get_settings().DATABASE_URL.get_secret_value())
