"""Tests for common/repo/analyses.py (issue #14, architecture.md §7.4)."""

from __future__ import annotations

import psycopg
import pytest

from common.models import Analysis, Claim, Quote, Topic
from common.repo.analyses import latest_analysis, save_analysis
from common.repo.transcripts import save_transcript

pytestmark = pytest.mark.integration


def make_analysis(video_id: str, transcript_id: int, **overrides: object) -> Analysis:
    defaults: dict[str, object] = {
        "video_id": video_id,
        "transcript_id": transcript_id,
        "chunk_strategy": "time:900:60",
        "model": "test-model",
        "prompt_version": "v1",
        "tldr": "tldr text",
        "topics": (Topic(seq=0, title="Topic A"),),
        "claims": (Claim(text="claim A", speaker="Alice"),),
        "quotes": (Quote(text="quote A", speaker="Alice"),),
    }
    defaults.update(overrides)
    return Analysis(**defaults)  # type: ignore[arg-type]


def test_save_analysis_inserts_analysis_and_children_and_returns_id(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    analysis = make_analysis(video_id, transcript_id)

    analysis_id = save_analysis(conn, analysis)

    row = conn.execute(
        "SELECT tldr, model FROM analyses WHERE id = %s", (analysis_id,)
    ).fetchone()
    assert row == ("tldr text", "test-model")

    topics = conn.execute(
        "SELECT title FROM topics WHERE analysis_id = %s", (analysis_id,)
    ).fetchall()
    assert topics == [("Topic A",)]

    claims = conn.execute(
        "SELECT text, speaker FROM claims WHERE analysis_id = %s", (analysis_id,)
    ).fetchall()
    assert claims == [("claim A", "Alice")]

    quotes = conn.execute(
        "SELECT text FROM quotes WHERE analysis_id = %s", (analysis_id,)
    ).fetchall()
    assert quotes == [("quote A",)]


def test_save_analysis_does_not_commit(head_dsn: str) -> None:
    # A raw connection here, not the `conn` fixture, since this test needs
    # explicit control over rollback.
    with psycopg.connect(head_dsn) as raw_conn:
        raw_conn.execute("INSERT INTO videos (video_id) VALUES ('vid1')")
        transcript_row = raw_conn.execute(
            """
            INSERT INTO transcripts (video_id, source, segments, full_text)
            VALUES ('vid1', 'whisper', '[]', 'x') RETURNING id
            """
        ).fetchone()
        assert transcript_row is not None
        transcript_id = int(transcript_row[0])

        save_analysis(raw_conn, make_analysis("vid1", transcript_id))
        raw_conn.rollback()

    with psycopg.connect(head_dsn) as verify_conn:
        count = verify_conn.execute("SELECT count(*) FROM analyses").fetchone()
        assert count == (0,)


def test_latest_analysis_returns_none_when_no_analysis(
    conn: psycopg.Connection, video_id: str
) -> None:
    assert latest_analysis(conn, video_id) is None


def test_latest_analysis_returns_newest_with_children_ordered(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    save_analysis(conn, make_analysis(video_id, transcript_id, tldr="old"))
    newest_id = save_analysis(
        conn,
        make_analysis(
            video_id,
            transcript_id,
            tldr="new",
            topics=(Topic(seq=1, title="B"), Topic(seq=0, title="A")),
            claims=(
                Claim(text="late", start_sec=10.0),
                Claim(text="early", start_sec=1.0),
            ),
        ),
    )

    result = latest_analysis(conn, video_id)

    assert result is not None
    assert result.id == newest_id
    assert result.tldr == "new"
    assert [t.title for t in result.topics] == ["A", "B"]
    assert [c.text for c in result.claims] == ["early", "late"]


def test_latest_analysis_breaks_created_at_ties_by_id_desc(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    first_id = save_analysis(conn, make_analysis(video_id, transcript_id, tldr="first"))
    second_id = save_analysis(conn, make_analysis(video_id, transcript_id, tldr="second"))
    # force an identical created_at to exercise the tie-break
    conn.execute(
        "UPDATE analyses SET created_at = now() WHERE id IN (%s, %s)",
        (first_id, second_id),
    )

    result = latest_analysis(conn, video_id)

    assert result is not None
    assert result.id == max(first_id, second_id)


def test_save_analysis_strips_nul_from_all_text_and_json_fields(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    analysis = make_analysis(
        video_id,
        transcript_id,
        tldr="bad\x00tldr",
        speaker_roster={"note": "bad\x00note"},
        topics=(Topic(seq=0, title="bad\x00title", summary="bad\x00summary"),),
        claims=(Claim(text="bad\x00claim"),),
        quotes=(Quote(text="bad\x00quote"),),
    )

    analysis_id = save_analysis(conn, analysis)

    row = conn.execute(
        "SELECT tldr, speaker_roster FROM analyses WHERE id = %s", (analysis_id,)
    ).fetchone()
    assert row == ("badtldr", {"note": "badnote"})

    topic_row = conn.execute(
        "SELECT title, summary FROM topics WHERE analysis_id = %s", (analysis_id,)
    ).fetchone()
    assert topic_row == ("badtitle", "badsummary")

    claim_row = conn.execute(
        "SELECT text FROM claims WHERE analysis_id = %s", (analysis_id,)
    ).fetchone()
    assert claim_row == ("badclaim",)

    quote_row = conn.execute(
        "SELECT text FROM quotes WHERE analysis_id = %s", (analysis_id,)
    ).fetchone()
    assert quote_row == ("badquote",)


def test_save_analysis_default_speaker_and_confidence_values(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    analysis = make_analysis(
        video_id,
        transcript_id,
        claims=(Claim(text="unattributed"),),
        quotes=(Quote(text="unattributed"),),
    )

    analysis_id = save_analysis(conn, analysis)

    claim_row = conn.execute(
        "SELECT speaker, confidence FROM claims WHERE analysis_id = %s", (analysis_id,)
    ).fetchone()
    assert claim_row == ("unknown", None)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_save_analysis_defaults_token_counts_to_zero(
    conn: psycopg.Connection, video_id: str, field: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)

    analysis_id = save_analysis(conn, make_analysis(video_id, transcript_id))

    row = conn.execute(
        f"SELECT {field} FROM analyses WHERE id = %s", (analysis_id,)
    ).fetchone()
    assert row == (0,)
