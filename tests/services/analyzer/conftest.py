"""Fixtures for analyzer tests (issue #30).

Reuses the migrated-database fixtures from ``tests/common/repo/conftest.py``.
"""

from __future__ import annotations

from tests.common.repo.conftest import conn, head_dsn

__all__ = ["conn", "head_dsn"]
