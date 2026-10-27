"""tombstones remember the title and channel of the deleted video

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-27

Adds nullable ``deleted_videos.title`` and ``deleted_videos.channel_id`` (issue
#239) so ``GET /deleted-videos`` is readable. No foreign key: the channel may be
deleted (#339). Tombstones from before this revision keep ``NULL``.

* ``upgrade()`` adds the two columns.
* ``downgrade()`` drops only those two columns.

Runs only as the standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE deleted_videos ADD COLUMN title TEXT NULL, ADD COLUMN channel_id TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE deleted_videos DROP COLUMN title, DROP COLUMN channel_id")
