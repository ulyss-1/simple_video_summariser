"""prompt-cache tokens and model-call count per analysis

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-24

``analyses.cache_read_tokens``, ``cache_write_tokens`` and ``calls`` (#139): what
the analyze job's summarizer reported in ``Usage`` besides input and output
tokens. All three are nullable with no default and no backfill, so an analysis
stored before the upgrade reads ``NULL`` (unknown), never ``0``. Raw SQL through
``op.execute`` (convention set by #7).

The issue text called this ``0017``; ``0017_deleted_videos`` landed first.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE analyses ADD COLUMN cache_read_tokens INTEGER NULL")
    op.execute("ALTER TABLE analyses ADD COLUMN cache_write_tokens INTEGER NULL")
    op.execute("ALTER TABLE analyses ADD COLUMN calls INTEGER NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE analyses DROP COLUMN calls")
    op.execute("ALTER TABLE analyses DROP COLUMN cache_write_tokens")
    op.execute("ALTER TABLE analyses DROP COLUMN cache_read_tokens")
