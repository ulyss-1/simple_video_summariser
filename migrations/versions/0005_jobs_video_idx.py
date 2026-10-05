"""jobs per-video index

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05

Adds ``jobs_video_idx`` so the per-video job lookups behind a video's
processing status (#42: active job, newest job, newest dead job) do not scan
the whole ``jobs`` table. Raw SQL through ``op.execute`` (convention set by #7).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE INDEX jobs_video_idx ON jobs (video_id, created_at DESC, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX jobs_video_idx")
