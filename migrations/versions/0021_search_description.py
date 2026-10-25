"""search_docs: also match the video description

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17

Issue #168. ``videos.description`` joins the materialised search vector
(``search_docs.fts``, 0010) so a query found only in the description returns the
video.

* ``refresh_search_doc`` adds ``setweight(to_tsvector('english', description), 'C')``.
  Only four weight labels exist and all are taken, so the description shares C
  (topics and claims). It must be on the English side (A/B/C): the search filters
  D with the transcript's own-language query, which would miss a description-only
  hit on a non-English video. ``RANK_WEIGHTS`` is unchanged: title (A) outranks the
  description (C), which outranks the transcript (D).
* ``videos_search_docs_trg`` now also fires on ``UPDATE OF description``.
* ``upgrade()`` rebuilds every ``search_docs`` row; ``downgrade()`` restores the
  0010 function and trigger and rebuilds again.

Raw SQL through ``op.execute`` (convention set by #7). Runs only as the
standalone ``migrate`` command.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION refresh_search_doc(vid TEXT) RETURNS void LANGUAGE plpgsql AS
$$
DECLARE
    a_id BIGINT;
BEGIN
    SELECT id INTO a_id FROM analyses
    WHERE video_id = vid ORDER BY created_at DESC, id DESC LIMIT 1;

    INSERT INTO search_docs (video_id, fts, updated_at)
    SELECT v.video_id,
           setweight(to_tsvector('english', coalesce(v.title, '')), 'A')
           || setweight(to_tsvector('english', coalesce(
                  (SELECT tldr FROM analyses WHERE id = a_id), '')), 'B')
           || setweight(to_tsvector('english', coalesce(
                  (SELECT string_agg(
                              coalesce(title, '') || ' ' || coalesce(summary, ''),
                              ' ' ORDER BY seq, id)
                   FROM topics WHERE analysis_id = a_id), '')), 'C')
           || setweight(to_tsvector('english', coalesce(
                  (SELECT string_agg(text, ' ' ORDER BY id)
                   FROM claims WHERE analysis_id = a_id), '')), 'C')
           {description}
           || coalesce(
                  (SELECT t.fts FROM transcripts t WHERE t.video_id = vid
                   ORDER BY transcript_rank(t.source), t.id LIMIT 1),
                  to_tsvector('english', '')),
           now()
    FROM videos v WHERE v.video_id = vid
    ON CONFLICT (video_id) DO UPDATE
        SET fts = EXCLUDED.fts, updated_at = EXCLUDED.updated_at;
END
$$
"""
_DESCRIPTION_TERM = "|| setweight(to_tsvector('english', coalesce(v.description, '')), 'C')"


def _trigger(columns: str) -> None:
    op.execute("DROP TRIGGER videos_search_docs_trg ON videos")
    op.execute(
        f"""
        CREATE TRIGGER videos_search_docs_trg
            AFTER INSERT OR UPDATE OF {columns} ON videos
            FOR EACH ROW EXECUTE FUNCTION search_docs_sync()
        """
    )


def upgrade() -> None:
    op.execute(_FUNCTION.format(description=_DESCRIPTION_TERM))
    _trigger("title, description")
    op.execute("SELECT refresh_search_doc(video_id) FROM videos")


def downgrade() -> None:
    op.execute(_FUNCTION.format(description=""))
    _trigger("title")
    op.execute("SELECT refresh_search_doc(video_id) FROM videos")
