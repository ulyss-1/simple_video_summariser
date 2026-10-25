"""keep llm_batches rows when their job is deleted

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-24

``llm_batches.job_id`` becomes nullable and its foreign key to ``jobs (id)``
changes from ``ON DELETE CASCADE`` to ``ON DELETE SET NULL`` (#251). Deleting a
video (#231) deletes its jobs; a ``pending`` analyze job in retry backoff can
hold a row for a batch that is still running at Anthropic. With CASCADE the row
went and nothing knew the batch id. Now the row stays with ``job_id`` NULL and
the analyzer's orphan pass (#160) cancels the batch and deletes the row.
``UNIQUE (job_id, step_key)`` is unchanged; Postgres treats NULLs as distinct,
so several NULL rows may share a ``step_key``.

The foreign key's name is looked up in ``pg_constraint``, not hardcoded.

The downgrade is lossy by design: it deletes the ``job_id IS NULL`` rows (a
NOT NULL column cannot hold them) before restoring ``NOT NULL`` and ``ON DELETE
CASCADE``. Anthropic expires those batches within 24 h anyway.

Raw SQL through ``op.execute`` (convention set by #7). Runs only as the
standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROP_JOB_FK = """
DO $$
DECLARE fk text;
BEGIN
    FOR fk IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'llm_batches'::regclass
          AND confrelid = 'jobs'::regclass
          AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE llm_batches DROP CONSTRAINT %I', fk);
    END LOOP;
END
$$
"""


def upgrade() -> None:
    op.execute(_DROP_JOB_FK)
    op.execute("ALTER TABLE llm_batches ALTER COLUMN job_id DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE llm_batches
            ADD CONSTRAINT llm_batches_job_id_fkey
            FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM llm_batches WHERE job_id IS NULL")
    op.execute(_DROP_JOB_FK)
    op.execute("ALTER TABLE llm_batches ALTER COLUMN job_id SET NOT NULL")
    op.execute(
        """
        ALTER TABLE llm_batches
            ADD CONSTRAINT llm_batches_job_id_fkey
            FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
        """
    )
