"""search_docs: materialise the analysis text for highlighted excerpts

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17

Issue #169. ``ts_headline`` needs the original text, which the ``fts`` tsvector
does not hold. ``search_docs.analysis_text`` is the plain text of the same
sources ``fts`` tokenises at weights B and C: the latest analysis' TL;DR, topic
titles and summaries, claim texts, and the video description (#168), one per line.
``refresh_search_doc`` writes it next to ``fts``, so the "latest analysis" selection
stays in this one function and the existing triggers keep it in sync. (The title
needs no column: the search reads ``videos.title``.)

* ``upgrade()`` adds the column, replaces the function, rebuilds every row.
* ``downgrade()`` restores the 0020 function, drops the column, rebuilds.

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

    INSERT INTO search_docs (video_id, fts, {text_column}updated_at)
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
           || setweight(to_tsvector('english', coalesce(v.description, '')), 'C')
           || coalesce(
                  (SELECT t.fts FROM transcripts t WHERE t.video_id = vid
                   ORDER BY transcript_rank(t.source), t.id LIMIT 1),
                  to_tsvector('english', '')),
           {text_value}now()
    FROM videos v WHERE v.video_id = vid
    ON CONFLICT (video_id) DO UPDATE
        SET fts = EXCLUDED.fts, {text_update}updated_at = EXCLUDED.updated_at;
END
$$
"""
_TEXT_VALUE = """concat_ws(E'\\n',
                  (SELECT tldr FROM analyses WHERE id = a_id),
                  (SELECT string_agg(
                              coalesce(title, '') || ' ' || coalesce(summary, ''),
                              E'\\n' ORDER BY seq, id)
                   FROM topics WHERE analysis_id = a_id),
                  (SELECT string_agg(text, E'\\n' ORDER BY id)
                   FROM claims WHERE analysis_id = a_id),
                  v.description),
           """


def upgrade() -> None:
    op.execute("ALTER TABLE search_docs ADD COLUMN analysis_text TEXT NOT NULL DEFAULT ''")
    op.execute(
        _FUNCTION.format(
            text_column="analysis_text, ",
            text_value=_TEXT_VALUE,
            text_update="analysis_text = EXCLUDED.analysis_text, ",
        )
    )
    op.execute("SELECT refresh_search_doc(video_id) FROM videos")


def downgrade() -> None:
    op.execute(_FUNCTION.format(text_column="", text_value="", text_update=""))
    op.execute("ALTER TABLE search_docs DROP COLUMN analysis_text")
    op.execute("SELECT refresh_search_doc(video_id) FROM videos")
