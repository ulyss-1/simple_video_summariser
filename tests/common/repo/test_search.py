"""``search_transcripts`` against Postgres (issue #43).

Seeded through ``common.repo`` (``upsert_video``, ``save_transcript``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from common.models import Segment, VideoMeta
from common.repo.search import SEARCH_SQL, SearchPage, Span, search_transcripts
from common.repo.transcripts import save_transcript
from common.repo.videos import record_unavailable, upsert_video

pytestmark = pytest.mark.integration

CH = "UC" + "s" * 22


def _video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    published: datetime | None = datetime(2026, 1, 1, tzinfo=UTC),
    title: str = "T",
    channel_id: str = CH,
) -> None:
    upsert_video(
        conn,
        VideoMeta(
            video_id=video_id,
            channel_id=channel_id,
            title=title,
            description="",
            duration_sec=60,
            published_at=published,
            language="en",
            live_status=None,
            manual_subtitle_langs=(),
            auto_caption_langs=(),
        ),
        "adhoc",
    )


def _transcript(
    conn: psycopg.Connection[Any], video_id: str, text: str, source: str = "whisper"
) -> int:
    segments = (Segment(0.0, 1.0, text),) if text else ()
    return save_transcript(conn, video_id, source, "en", "none", segments, None)


def _seed(conn: psycopg.Connection[Any], video_id: str, text: str, **kw: Any) -> None:
    _video(conn, video_id, **kw)
    _transcript(conn, video_id, text)


def _ids(page: SearchPage) -> list[str]:
    return [hit.video_id for hit in page.results]


def _search(conn: psycopg.Connection[Any], q: str, limit: int = 50, offset: int = 0) -> SearchPage:
    return search_transcripts(conn, q, limit, offset)


# --- matching -----------------------------------------------------------------


def test_stemming_and_case(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "stem0000001", "she was running late")
    _seed(conn, "case0000001", "nasa launched a rocket")

    assert _ids(_search(conn, "runs")) == ["stem0000001"]
    assert _ids(_search(conn, "NASA")) == ["case0000001"]


def test_websearch_syntax(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "phrase00001", "we study machine learning daily")
    _seed(conn, "phrase00002", "learning about a machine is fun")
    _seed(conn, "cats0000001", "cats are great")
    _seed(conn, "catsdogs001", "cats and dogs together")
    _seed(conn, "dogs0000001", "dogs bark")

    assert _ids(_search(conn, '"machine learning"')) == ["phrase00001"]
    assert sorted(_ids(_search(conn, "cats -dogs"))) == ["cats0000001"]
    assert sorted(_ids(_search(conn, "cats or dogs"))) == [
        "cats0000001", "catsdogs001", "dogs0000001",
    ]


@pytest.mark.parametrize("q", ["the", "and of", "-dogs", "-dogs -cats", "or", "'", "😀"])
def test_a_query_without_a_positive_term_returns_nothing(
    conn: psycopg.Connection[Any], q: str
) -> None:
    _seed(conn, "cats0000001", "cats are great")
    _seed(conn, "the00000001", "the and of")

    page = _search(conn, q)

    assert page == SearchPage((), False)


def test_a_query_without_a_positive_term_never_runs_the_search(
    conn: psycopg.Connection[Any],
) -> None:
    queries: list[str] = []

    class Spy:
        def execute(self, query: Any, params: Any = None) -> Any:
            queries.append(str(query))
            return conn.execute(query, params)

    search_transcripts(Spy(), "-dogs", 10, 0)  # type: ignore[arg-type]

    assert SEARCH_SQL not in queries
    assert not any("ts_headline" in q or "FROM transcripts" in q for q in queries)


def test_only_the_preferred_transcript_is_searched(conn: psycopg.Connection[Any]) -> None:
    _video(conn, "pref0000001")
    _transcript(conn, "pref0000001", "a zebra appears", "youtube_auto")
    _transcript(conn, "pref0000001", "a horse appears", "whisper")
    _video(conn, "pref0000002")
    _transcript(conn, "pref0000002", "a zebra appears", "youtube_auto")

    page = _search(conn, "zebra")

    assert _ids(page) == ["pref0000002"]
    assert page.results[0].transcript_source == "youtube_auto"
    assert _ids(_search(conn, "horse")) == ["pref0000001"]
    assert _search(conn, "horse").results[0].transcript_source == "whisper"


def test_manual_beats_whisper(conn: psycopg.Connection[Any]) -> None:
    _video(conn, "pref0000003")
    _transcript(conn, "pref0000003", "zebra", "whisper")
    _transcript(conn, "pref0000003", "zebra too", "youtube_manual")

    [hit] = _search(conn, "zebra").results

    assert hit.transcript_source == "youtube_manual"


def test_a_video_appears_once_even_if_several_transcripts_match(
    conn: psycopg.Connection[Any],
) -> None:
    _video(conn, "once0000001")
    for source in ("youtube_auto", "whisper", "youtube_manual"):
        _transcript(conn, "once0000001", "zebra zebra", source)

    assert _ids(_search(conn, "zebra")) == ["once0000001"]


def test_empty_transcripts_and_videos_without_one_never_match(
    conn: psycopg.Connection[Any],
) -> None:
    _seed(conn, "empty000001", "")
    _video(conn, "nottrans001", title="zebra")

    assert _search(conn, "zebra").results == ()


def test_unavailable_videos_are_returned_with_the_field_set(
    conn: psycopg.Connection[Any],
) -> None:
    _seed(conn, "unav0000001", "zebra")
    record_unavailable(conn, "unav0000001", "removed", origin="adhoc")

    [hit] = _search(conn, "zebra").results

    assert hit.unavailable == "removed"


def test_result_metadata_and_nullable_columns(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "meta0000001", "zebra", published=datetime(2026, 3, 1, tzinfo=UTC))
    conn.execute("UPDATE channels SET title = 'Chan' WHERE channel_id = %s", (CH,))
    conn.execute("INSERT INTO videos (video_id) VALUES ('stub0000001')")
    _transcript(conn, "stub0000001", "zebra")

    hits = {h.video_id: h for h in _search(conn, "zebra").results}

    meta = hits["meta0000001"]
    assert (meta.title, meta.channel_id, meta.channel_title, meta.published_at,
            meta.duration_sec, meta.unavailable, meta.transcript_source) == (
        "T", CH, "Chan", datetime(2026, 3, 1, tzinfo=UTC), 60, None, "whisper",
    )
    stub = hits["stub0000001"]
    assert (stub.title, stub.channel_id, stub.channel_title, stub.published_at,
            stub.duration_sec) == (None, None, None, None, None)


@pytest.mark.parametrize(
    "q",
    [
        "'", '"', '"unbalanced', "' OR 1=1 --", "foo & bar | !(baz:*) <-> qux", "\\", "%", "_",
        "-", "--", "or", "<script>alert(1)</script>", "café", "🦓", "!@#$%^&*()-+=[]{};:,.<>/?"
        * 8,
    ],
)
def test_hostile_queries_do_not_raise(conn: psycopg.Connection[Any], q: str) -> None:
    _seed(conn, "host0000001", "café zebra foo bar baz qux script alert")

    page = _search(conn, q[:200])

    assert isinstance(page, SearchPage)


def test_the_search_uses_the_gin_index(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "idx00000001", "zebra")
    conn.execute("SET enable_seqscan = off")

    plan = conn.execute(
        "EXPLAIN " + SEARCH_SQL, {"q": "zebra", "limit": 21, "offset": 0}
    ).fetchall()

    assert "transcripts_fts_idx" in "\n".join(row[0] for row in plan)
    assert "to_tsvector" not in SEARCH_SQL
    assert "websearch_to_tsquery('english'" in SEARCH_SQL


def test_a_nul_byte_in_the_query_is_rejected_without_touching_sql(
    conn: psycopg.Connection[Any],
) -> None:
    class Spy:
        def execute(self, query: Any, params: Any = None) -> Any:
            raise AssertionError("must not run SQL")

    with pytest.raises(ValueError, match="NUL"):
        search_transcripts(Spy(), "zebra\x00", 10, 0)  # type: ignore[arg-type]


def test_two_transcripts_of_the_same_unrecognised_rank_still_give_one_result(
    conn: psycopg.Connection[Any],
) -> None:
    # transcript_rank() falls back to the same rank (3) for any source it
    # does not recognise; save_transcript's own _SOURCES check keeps this
    # from happening through the application, but the SQL still has to
    # produce one row per video, not two, if it ever does.
    _video(conn, "tie0000001")
    conn.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text) VALUES"
        " (%s, 'bogus_a', '[]', 'zebra one'), (%s, 'bogus_b', '[]', 'zebra two')",
        ("tie0000001", "tie0000001"),
    )

    page = _search(conn, "zebra")

    assert len(page.results) == 1


def test_the_search_writes_nothing(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "rw000000001", "zebra")

    def counts() -> Any:
        return conn.execute(
            "SELECT (SELECT count(*) FROM videos), (SELECT count(*) FROM transcripts),"
            " (SELECT count(*) FROM channels), (SELECT count(*) FROM jobs)"
        ).fetchone()

    before = counts()
    _search(conn, "zebra")
    assert counts() == before


# --- ranking and paging -------------------------------------------------------


def test_relevance_then_published_then_video_id(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "rank_low001", "zebra " + "filler " * 40, published=datetime(2026, 9, 1,
                                                                            tzinfo=UTC))
    _seed(conn, "rank_hi0001", "zebra zebra zebra", published=datetime(2020, 1, 1, tzinfo=UTC))
    _seed(conn, "tie_b000001", "zebra horse", published=datetime(2025, 1, 1, tzinfo=UTC))
    _seed(conn, "tie_a000001", "zebra horse", published=datetime(2025, 1, 1, tzinfo=UTC))
    _seed(conn, "tie_new0001", "zebra horse", published=datetime(2025, 6, 1, tzinfo=UTC))
    _seed(conn, "tie_null001", "zebra horse", published=None)

    assert _ids(_search(conn, "zebra")) == [
        "rank_hi0001", "tie_new0001", "tie_a000001", "tie_b000001", "tie_null001",
        "rank_low001",
    ]


def test_a_short_transcript_outranks_a_long_one_with_the_same_hits(
    conn: psycopg.Connection[Any],
) -> None:
    short = "zebra " + "word " * 48 + "zebra " + "word " * 49
    long = "zebra " + "word " * 2498 + "zebra " + "word " * 2499
    _seed(conn, "short000001", short, published=datetime(2020, 1, 1, tzinfo=UTC))
    _seed(conn, "long0000001", long, published=datetime(2026, 1, 1, tzinfo=UTC))

    assert _ids(_search(conn, "zebra")) == ["short000001", "long0000001"]


def test_has_more_boundaries(conn: psycopg.Connection[Any]) -> None:
    for i in range(3):
        _seed(conn, f"more{i:07d}", "zebra")

    assert _search(conn, "zebra", limit=3).has_more is False
    assert _search(conn, "zebra", limit=2).has_more is True
    assert _search(conn, "zebra", limit=2, offset=1).has_more is False
    last = _search(conn, "zebra", limit=2, offset=2)
    assert (len(last.results), last.has_more) == (1, False)
    beyond = _search(conn, "zebra", limit=2, offset=3)
    assert beyond == SearchPage((), False)


def test_walking_pages_of_one_matches_a_single_page(conn: psycopg.Connection[Any]) -> None:
    for i in range(7):
        _seed(conn, f"walk{i:07d}", "zebra " * (i % 3 + 1) + "x " * i,
              published=datetime(2026, 1, 1 + i % 2, tzinfo=UTC))

    whole = _ids(_search(conn, "zebra", limit=50))
    walked: list[str] = []
    for offset in range(20):
        page = _search(conn, "zebra", limit=1, offset=offset)
        walked.extend(_ids(page))
        if not page.has_more:
            break
    else:
        raise AssertionError("has_more never became false")

    assert walked == whole
    assert len(set(walked)) == 7


# --- excerpts -----------------------------------------------------------------


def test_matched_words_are_marked(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "exc00000001", "the zebra ran")

    [hit] = _search(conn, "zebra").results

    assert 1 <= len(hit.excerpts) <= 3
    spans = [s for fragment in hit.excerpts for s in fragment]
    assert Span("zebra", True) in spans
    assert all(s.text for s in spans)
    assert "".join(s.text for s in hit.excerpts[0]) == "the zebra ran"


def test_html_in_a_transcript_is_literal_unmarked_text(conn: psycopg.Connection[Any]) -> None:
    _seed(conn, "html0000001", "zebra <script>alert(1)</script> and <b>fake</b> & more")

    [hit] = _search(conn, "zebra").results

    spans = [s for fragment in hit.excerpts for s in fragment]
    text = "".join(s.text for s in spans)
    assert "<script>alert(1)</script>" in text
    assert "<b>fake</b>" in text
    assert [s.text for s in spans if s.match] == ["zebra"]


def test_marker_characters_in_a_transcript_cannot_forge_matches(
    conn: psycopg.Connection[Any],
) -> None:
    forged = "zebra \x01fake\x02 \x03 split \ue000x\ue001 \x01dangling"
    _seed(conn, "forge000001", forged)

    [hit] = _search(conn, "zebra").results

    spans = [s for fragment in hit.excerpts for s in fragment]
    assert [s.text for s in spans if s.match] == ["zebra"]
    assert len(hit.excerpts) == 1
    for span in spans:
        assert not any(c in span.text for c in "\x01\x02\x03\ue000\ue001")


def test_long_transcripts_give_at_most_three_short_fragments(
    conn: psycopg.Connection[Any],
) -> None:
    filler = " ".join(f"w{i}" for i in range(200))
    _seed(conn, "frag0000001", f"zebra {filler} zebra {filler} zebra {filler} zebra {filler}")

    [hit] = _search(conn, "zebra").results

    assert 1 <= len(hit.excerpts) <= 3
    for fragment in hit.excerpts:
        words = "".join(s.text for s in fragment).split()
        assert len(words) <= 36
        assert any(s.match for s in fragment)


def test_headline_is_computed_only_for_the_page(conn: psycopg.Connection[Any]) -> None:
    head, _, tail = SEARCH_SQL.partition("ts_headline")
    assert tail, "SEARCH_SQL must call ts_headline"
    assert "LIMIT" in head and "OFFSET" in head
    assert "LIMIT" not in tail and "OFFSET" not in tail
