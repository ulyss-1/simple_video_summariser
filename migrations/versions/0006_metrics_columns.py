"""job start time and coerced-speaker count for /metrics

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09

Two numbers ``GET /metrics`` (#60) needs and the database did not hold:
``jobs.started_at`` (set by the claim query; job duration is
``finished_at - started_at`` of the last attempt) and
``analyses.speakers_coerced`` (the analyzer's per-analysis count of speakers
coerced to ``unknown``). Both are nullable and existing rows stay ``NULL``
(unknown), never ``0``. Raw SQL through ``op.execute`` (convention set by #7).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN started_at TIMESTAMPTZ NULL")
    op.execute("ALTER TABLE analyses ADD COLUMN speakers_coerced INTEGER NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE analyses DROP COLUMN speakers_coerced")
    op.execute("ALTER TABLE jobs DROP COLUMN started_at")
