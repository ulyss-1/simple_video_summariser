"""``latest_analysis_run`` / ``list_analysis_runs`` and the claim tie-break (issue #42)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from common.repo.analyses import (
    latest_analysis,
    latest_analysis_run,
    list_analysis_runs,
)
from tests.common.repo import read_seed as seed

pytestmark = pytest.mark.integration

VID = "an000000001"


@pytest.fixture
def transcript_id(conn: psycopg.Connection[Any]) -> int:
    seed.video(conn, VID)
    return seed.transcript(conn, VID, "youtube_auto")


def test_no_analysis(conn: psycopg.Connection[Any], transcript_id: int) -> None:
    assert latest_analysis_run(conn, VID) is None
    assert list_analysis_runs(conn, VID, offset=0, limit=10) == ((), 0)


def test_latest_run_has_every_field_and_ordered_children(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    seed.analysis(conn, VID, transcript_id, created=1, tldr="old")
    newest = seed.analysis(
        conn, VID, transcript_id, created=2, tldr="<script>alert(1)</script>",
        cost_usd="0.12345", speaker_roster={"speakers": [{"name": "A", "role": "host"}]},
        topics=[(1, "second"), (0, "first")],
        claims=[
            {"text": "c-null", "start_sec": None},
            {"text": "c-late", "start_sec": 9.5, "speaker": "A", "confidence": "high",
             "source_chunk_seq": 2},
            {"text": "c-tie-1", "start_sec": 1.0},
            {"text": "c-tie-2", "start_sec": 1.0},
        ],
        quotes=[{"text": "q2", "start_sec": 3}, {"text": "q1", "start_sec": 2,
                                                  "source_chunk_seq": 0}],
    )

    run = latest_analysis_run(conn, VID)

    assert run is not None
    a = run.analysis
    assert run.transcript_source == "youtube_auto"
    assert (a.id, a.tldr, a.model, a.prompt_version, a.chunk_strategy) == (
        newest, "<script>alert(1)</script>", "m1", "v1", "time:900:60",
    )
    assert (a.transcript_id, a.input_tokens, a.output_tokens, a.duration_ms) == (
        transcript_id, 11, 22, 333,
    )
    assert a.cost_usd == pytest.approx(0.12345)
    assert isinstance(a.cost_usd, float)
    assert a.speaker_roster == {"speakers": [{"name": "A", "role": "host"}]}
    assert a.created_at == seed.at(2)
    assert [t.title for t in a.topics] == ["first", "second"]
    assert [c.text for c in a.claims] == ["c-tie-1", "c-tie-2", "c-late", "c-null"]
    late = a.claims[2]
    assert (late.speaker, late.start_sec, late.confidence, late.source_chunk_seq) == (
        "A", 9.5, "high", 2,
    )
    assert (a.claims[3].speaker, a.claims[3].confidence, a.claims[3].start_sec) == (
        "unknown", None, None,
    )
    assert [q.text for q in a.quotes] == ["q1", "q2"]
    assert a.quotes[0].source_chunk_seq == 0


def test_an_analysis_with_no_children_has_empty_tuples(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    seed.analysis(conn, VID, transcript_id, created=1)

    run = latest_analysis_run(conn, VID)

    assert run is not None
    assert (run.analysis.topics, run.analysis.claims, run.analysis.quotes) == ((), (), ())
    assert run.analysis.cost_usd is None
    assert run.analysis.speaker_roster is None


def test_runs_are_newest_first_paged_and_children_never_mix(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    ids = [
        seed.analysis(
            conn, VID, transcript_id, created=i, model=f"m{i}", prompt_version=f"p{i}",
            topics=[(0, f"topic-{i}")], claims=[{"text": f"claim-{i}"}],
            quotes=[{"text": f"quote-{i}"}],
        )
        for i in range(5)
    ]
    tie = seed.analysis(conn, VID, transcript_id, created=4, model="tie")

    first, total = list_analysis_runs(conn, VID, offset=0, limit=3)
    rest, _ = list_analysis_runs(conn, VID, offset=3, limit=3)
    beyond, total_beyond = list_analysis_runs(conn, VID, offset=6, limit=3)

    assert total == total_beyond == 6
    assert beyond == ()
    assert [r.analysis.id for r in first + rest] == [tie, ids[4], ids[3], ids[2], ids[1], ids[0]]
    for run in first + rest:
        a = run.analysis
        if a.model == "tie":
            continue
        i = a.model[1:]
        assert [t.title for t in a.topics] == [f"topic-{i}"]
        assert [c.text for c in a.claims] == [f"claim-{i}"]
        assert [q.text for q in a.quotes] == [f"quote-{i}"]


def test_runs_of_another_video_are_not_included(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    seed.video(conn, "other000001")
    other_t = seed.transcript(conn, "other000001")
    seed.analysis(conn, "other000001", other_t, created=1)

    assert list_analysis_runs(conn, VID, offset=0, limit=10) == ((), 0)


def test_a_page_of_runs_uses_a_fixed_number_of_statements(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    def statements() -> int:
        queries: list[str] = []

        class Spy:
            def execute(self, query: Any, params: Any = None) -> Any:
                queries.append(str(query))
                return conn.execute(query, params)

        list_analysis_runs(Spy(), VID, offset=0, limit=50)  # type: ignore[arg-type]
        return len(queries)

    seed.analysis(conn, VID, transcript_id, created=0, claims=[{"text": "x"}])
    one = statements()
    for i in range(1, 20):
        seed.analysis(conn, VID, transcript_id, created=i, claims=[{"text": "x"}],
                      topics=[(0, "t")], quotes=[{"text": "q"}])

    assert statements() == one


def test_latest_analysis_breaks_start_sec_ties_by_id(
    conn: psycopg.Connection[Any], transcript_id: int
) -> None:
    analysis_id = seed.analysis(conn, VID, transcript_id, created=1)
    for text in ("b", "a", "c"):
        conn.execute(
            "INSERT INTO claims (analysis_id, text, start_sec) VALUES (%s, %s, 5)",
            (analysis_id, text),
        )
        conn.execute(
            "INSERT INTO quotes (analysis_id, text, start_sec) VALUES (%s, %s, 5)",
            (analysis_id, text),
        )
    conn.execute("UPDATE claims SET text = text WHERE text = 'b'")
    conn.execute("UPDATE quotes SET text = text WHERE text = 'b'")

    result = latest_analysis(conn, VID)

    assert result is not None
    assert [c.text for c in result.claims] == ["b", "a", "c"]
    assert [q.text for q in result.quotes] == ["b", "a", "c"]
