"""Fixtures for planner tests (issue #36).

Reuses the migrated-database fixtures from ``tests/common/repo/conftest.py``
so there is one place that knows how to bring a fresh Postgres to head.
"""

from __future__ import annotations

from tests.common.repo.conftest import conn, head_dsn

__all__ = ["conn", "head_dsn"]
