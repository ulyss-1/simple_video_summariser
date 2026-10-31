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

* ``upgrade()`` adds the columns, the table and the triggers that move stored batch
  usage to a failed-attempt record when a job goes ``dead`` or a non-done job is deleted.
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
            ADD COLUMN cost_usd           NUMERIC(14,8) NULL,
            ADD COLUMN model              TEXT NULL,
            ADD COLUMN usage_moved        BOOLEAN NOT NULL DEFAULT false
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
    # Stored batch usage moves to a failed-attempt record in the transaction that makes
    # the job dead, or deletes a job that is not done, whichever path does it (queue,
    # reaper, watchdog, cancel, purge). A video being deleted (cascade) has no row left to
    # point at, so nothing is written then.
    op.execute(
        """
        CREATE FUNCTION move_batch_usage(p_job_id BIGINT) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO analyze_failed_usage
                (video_id, job_id, model, outcome, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, calls, cost_usd)
            SELECT j.video_id, b.job_id, COALESCE(b.model, ''), 'failed', b.input_tokens,
                   b.output_tokens, b.cache_read_tokens, b.cache_write_tokens, b.calls, b.cost_usd
            FROM llm_batches b
            JOIN jobs j ON j.id = b.job_id
            WHERE b.job_id = p_job_id AND b.calls IS NOT NULL AND NOT b.usage_moved
              AND (b.calls > 0 OR b.input_tokens > 0 OR b.output_tokens > 0
                   OR b.cache_read_tokens > 0 OR b.cache_write_tokens > 0)
              AND EXISTS (SELECT 1 FROM videos v WHERE v.video_id = j.video_id);
            UPDATE llm_batches SET usage_moved = true
            WHERE job_id = p_job_id AND calls IS NOT NULL AND NOT usage_moved;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION jobs_dead_move_usage() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM move_batch_usage(NEW.id);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER jobs_dead_move_usage AFTER UPDATE OF state ON jobs
        FOR EACH ROW WHEN (NEW.state = 'dead' AND OLD.state IS DISTINCT FROM 'dead')
        EXECUTE FUNCTION jobs_dead_move_usage()
        """
    )
    op.execute(
        """
        CREATE FUNCTION jobs_delete_move_usage() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.state <> 'done' THEN
                PERFORM move_batch_usage(OLD.id);
            END IF;
            RETURN OLD;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER jobs_delete_move_usage BEFORE DELETE ON jobs
        FOR EACH ROW EXECUTE FUNCTION jobs_delete_move_usage()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER jobs_delete_move_usage ON jobs")
    op.execute("DROP TRIGGER jobs_dead_move_usage ON jobs")
    op.execute("DROP FUNCTION jobs_delete_move_usage()")
    op.execute("DROP FUNCTION jobs_dead_move_usage()")
    op.execute("DROP FUNCTION move_batch_usage(BIGINT)")
    op.execute("DROP TABLE analyze_failed_usage")
    op.execute(
        """
        ALTER TABLE llm_batches
            DROP COLUMN input_tokens,
            DROP COLUMN output_tokens,
            DROP COLUMN cache_read_tokens,
            DROP COLUMN cache_write_tokens,
            DROP COLUMN calls,
            DROP COLUMN cost_usd,
            DROP COLUMN model,
            DROP COLUMN usage_moved
        """
    )
