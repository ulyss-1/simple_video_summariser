"""channel catalog snapshots

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-25

``channel_catalog_entries`` (#242) holds the latest flat listing of a channel's
uploads plus per-row metadata enrichment state. A catalog row is not a
``videos`` row: browsing an upload must not make it known. Only the confirmed
bulk action inserts into ``videos``.

Raw SQL through ``op.execute`` (convention set by #7). Runs only as the
standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE channel_catalog_entries (
            channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
            video_id TEXT NOT NULL CHECK (video_id ~ '^[A-Za-z0-9_-]{11}$'),
            position INTEGER NOT NULL CHECK (position >= 0),
            snapshot_limit INTEGER NOT NULL CHECK (snapshot_limit IN (50, 200, 500, 5000)),
            total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0),
            title TEXT,
            description TEXT,
            published_at TIMESTAMPTZ,
            enrichment_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (enrichment_state IN ('pending', 'running', 'done', 'failed')),
            enrichment_error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (channel_id, video_id),
            CHECK ((enrichment_state = 'failed') = (enrichment_error IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX channel_catalog_claim_idx ON channel_catalog_entries "
        "(updated_at, channel_id, position) WHERE enrichment_state = 'pending'"
    )
    op.execute(
        "CREATE INDEX channel_catalog_snapshot_idx ON channel_catalog_entries "
        "(channel_id, position, video_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE channel_catalog_entries")
