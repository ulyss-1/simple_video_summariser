"""usage of failed analyze attempts and of checkpointed batches

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-31

Issue #252. Two changes:

* ``llm_batches`` gets nullable usage columns (input, output, cache read and
  cache write tokens, ``calls``, ``cost_usd``). All NULL means the batch's
  results were not fetched yet. The usage of a batch is stored on its row the
  first time its results are fetched, so a resumed attempt reads it instead of
  counting the results a second time.
* ``analyze_failed_usage`` holds the spend of failed and cancelled attempts
  that no ``analyses`` row carries. ``ON DELETE CASCADE`` from ``videos`` (it
  goes with the video, like ``analyses``); ``job_id`` is ``SET NULL`` when the
  job is deleted.

Existing rows are untouched: the new columns are NULL.

* ``upgrade()`` adds the columns and the table.
* ``downgrade()`` drops them.

Runs only as the standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_batches
            ADD COLUMN input_tokens       INTEGER NULL,
            ADD COLUMN output_tokens      INTEGER NULL,
            ADD COLUMN cache_read_tokens  INTEGER NULL,
            ADD COLUMN cache_write_tokens INTEGER NULL,
            ADD COLUMN calls              INTEGER NULL,
            ADD COLUMN cost_usd           NUMERIC(14,8) NULL
        """
    )
    op.execute(
        """
        CREATE TABLE analyze_failed_usage (
            id                 BIGSERIAL PRIMARY KEY,
            video_id           TEXT NOT NULL REFERENCES videos (video_id) ON DELETE CASCADE,
            job_id             BIGINT NULL REFERENCES jobs (id) ON DELETE SET NULL,
            model              TEXT NOT NULL,
            outcome            TEXT NOT NULL CHECK (outcome IN ('failed', 'cancelled')),
            input_tokens       INTEGER NOT NULL DEFAULT 0,
            output_tokens      INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            calls              INTEGER NOT NULL DEFAULT 0,
            cost_usd           NUMERIC(14,8) NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX analyze_failed_usage_video_idx ON analyze_failed_usage (video_id)")


def downgrade() -> None:
    op.execute("DROP TABLE analyze_failed_usage")
    op.execute(
        """
        ALTER TABLE llm_batches
            DROP COLUMN input_tokens,
            DROP COLUMN output_tokens,
            DROP COLUMN cache_read_tokens,
            DROP COLUMN cache_write_tokens,
            DROP COLUMN calls,
            DROP COLUMN cost_usd
        """
    )
