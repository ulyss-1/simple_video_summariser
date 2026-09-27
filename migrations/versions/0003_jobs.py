"""jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27

Creates the ``jobs`` table exactly as specified in architecture.md §6 (task
#9), including correction C1: uniqueness is a **partial** unique index over
only the active states (``pending``, ``running``), keyed on
``(video_id, kind, dedupe_key)``, so a finished (``done``) or dead job never
blocks enqueueing the same work again.

``video_id`` deliberately has **no** foreign key to ``videos`` — job history
must survive a video's deletion (§6). Queue logic (enqueue, claim, backoff,
reap) and any CHECK constraint on ``state``/``kind`` are out of scope here
and move to #11.

Raw SQL through ``op.execute``, copied from §6 (convention set by #7).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
            id           BIGSERIAL PRIMARY KEY,
            video_id     TEXT NOT NULL,
            kind         TEXT NOT NULL,
            dedupe_key   TEXT NOT NULL DEFAULT 'default',
            state        TEXT NOT NULL DEFAULT 'pending',
            priority     INTEGER NOT NULL DEFAULT 0,
            payload      JSONB NOT NULL DEFAULT '{}',
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT,
            error_class  TEXT,
            run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_by    TEXT,
            locked_at    TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX jobs_active_uniq ON jobs (video_id, kind, dedupe_key)
            WHERE state IN ('pending', 'running')
        """
    )
    op.execute(
        "CREATE INDEX jobs_claim_idx ON jobs (kind, priority DESC, run_after) "
        "WHERE state = 'pending'"
    )
    op.execute("CREATE INDEX jobs_reap_idx ON jobs (heartbeat_at) WHERE state = 'running'")
    op.execute("CREATE INDEX jobs_dead_idx ON jobs (created_at DESC) WHERE state = 'dead'")


def downgrade() -> None:
    # Dropping the table drops its indexes with it.
    op.execute("DROP TABLE jobs")
