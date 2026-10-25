"""a user's request to cancel a job

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-25

Adds ``jobs.cancel_requested_at`` (issue #237): a nullable ``TIMESTAMPTZ`` set by
``POST /videos/{id}/cancel`` on each ``running`` job of the video. The database
row is the source of truth: the worker learns of the request on its next
heartbeat, and every outcome write checks the column in its own ``UPDATE``.
``NULL`` means nobody asked. A job that is cancelled ends ``dead`` with
``error_class = 'CANCELLED'`` (no new state, so no constraint changes).

* ``upgrade()`` adds the column; existing rows stay ``NULL``.
* ``downgrade()`` drops only that column.

Raw SQL through ``op.execute`` (convention set by #7). Runs only as the
standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN cancel_requested_at TIMESTAMPTZ NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN cancel_requested_at")
