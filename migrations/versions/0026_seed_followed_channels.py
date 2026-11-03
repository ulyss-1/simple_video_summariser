"""seed the owner's five followed YouTube channels

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-03

Issue #386. The owner's five followed channels (``FOLLOWED_CHANNELS``,
documented in ``_docs/followed-channels.md``) are inserted into ``channels``
exactly as ``POST /channels`` would follow them: ``active = true`` and
``monitor_from = now()``, the other columns keep their defaults. Following is
forward-only, so no job, backfill request, catalog entry or video is written.

* ``upgrade()`` runs one parameterised ``INSERT ... ON CONFLICT (channel_id) DO
  NOTHING`` per channel. A row that already exists (unfollowed, or added earlier
  without a title) is left exactly as it is.
* ``downgrade()`` deletes the five rows that have no ``videos``,
  ``backfill_requests`` or ``channel_catalog_entries`` row (the three tables
  with a foreign key to ``channels``), so it never fails on a used channel and
  never touches another channel. Known limit: a row the owner added by hand
  before this migration, with none of those dependants, is indistinguishable from
  a seeded one and is removed too.

Runs only as the standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (channel_id, title), the single source of truth for the seed.
FOLLOWED_CHANNELS: tuple[tuple[str, str], ...] = (
    ("UCbta0n8i6Rljh0obO7HzG9A", "Joseph Carlson"),
    ("UCfCT7SSFEWyG4th9ZmaGYqQ", "Joseph Carlson After Hours"),
    ("UC3w193M5tYPJqF0Hi-7U-2g", "Dr. Eric Berg DC"),
    ("UCyqlbzLoYtpqDXwRI9Yh5LA", "BWB - Business With Brian"),
    ("UCrTTBSUr0zhPU56UQljag5A", "Value Investing with Sven Carlin, Ph.D."),
)

INSERT_SQL = sa.text(
    "INSERT INTO channels (channel_id, title, active, monitor_from) "
    "VALUES (:channel_id, :title, true, now()) ON CONFLICT (channel_id) DO NOTHING"
)

_IDS = ", ".join(f":id{i}" for i in range(len(FOLLOWED_CHANNELS)))
DELETE_SQL = sa.text(
    f"DELETE FROM channels c WHERE c.channel_id IN ({_IDS}) "
    "AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.channel_id = c.channel_id) "
    "AND NOT EXISTS (SELECT 1 FROM backfill_requests b WHERE b.channel_id = c.channel_id) "
    "AND NOT EXISTS (SELECT 1 FROM channel_catalog_entries e WHERE e.channel_id = c.channel_id)"
)


def seed_followed_channels(conn: Connection) -> None:
    """The upgrade statement, callable twice to show it is idempotent."""
    for channel_id, title in FOLLOWED_CHANNELS:
        conn.execute(INSERT_SQL, {"channel_id": channel_id, "title": title})


def upgrade() -> None:
    seed_followed_channels(op.get_bind())


def downgrade() -> None:
    params = {f"id{i}": cid for i, (cid, _) in enumerate(FOLLOWED_CHANNELS)}
    op.get_bind().execute(DELETE_SQL, params)
