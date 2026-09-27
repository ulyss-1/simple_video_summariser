"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-09-27

No-op revision (task #7). It exists so ``alembic upgrade head`` has a real
revision to land on and ``alembic_version`` reads ``0001`` on a fresh
database, giving #8, #9 and #10 a single, concrete base to branch from.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
