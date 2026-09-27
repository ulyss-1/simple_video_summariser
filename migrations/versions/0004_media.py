"""media

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27

Creates the ``media`` table exactly as specified in architecture.md §6
(D6b, task #10). Records each retained audio file's location, size and
expiry so the planner can enforce both the age limit and the disk cap.
Audio bytes never go in the database - only ``path``, ``bytes`` and the
timestamps do.

Storage rule (pinned by a test in tests/migrations/test_media.py): ``path``
is always **relative to ``AUDIO_DIR``** (#5's config var), e.g.
``"ab/abc123def45.opus"`` - never an absolute path. This is the same rule
``common/models.py``'s ``AudioRef.rel_path`` documents and that #19's
``adapters/youtube/audio.py`` builds (``f"{video_id[:2]}/{video_id}.opus"``).
Keeping paths relative means the audio root can move (or differ between a
host run and a container) without rewriting every row.

Writing media rows when audio is fetched, deleting expired files/oldest-
first eviction, and a ``media_repo`` module are all out of scope here (#10)
and move to #29 and #36.

Raw SQL through ``op.execute``, copied from §6 (convention set by #7).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE media (
            id            BIGSERIAL PRIMARY KEY,
            video_id      TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
            path          TEXT NOT NULL,
            bytes         BIGINT NOT NULL,
            format        TEXT NOT NULL DEFAULT 'opus16k',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            UNIQUE (video_id, format)
        )
        """
    )

    op.execute("CREATE INDEX media_expiry_idx ON media (expires_at)")
    op.execute("CREATE INDEX media_lru_idx ON media (created_at)")  # oldest-first eviction


def downgrade() -> None:
    # Dropping the table drops its indexes with it.
    op.execute("DROP TABLE media")
