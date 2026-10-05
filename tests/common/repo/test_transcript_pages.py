"""``best_transcript_info`` / ``best_transcript_page`` (issue #42)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from common.models import IndexedSegment
from common.repo.transcripts import best_transcript_info, best_transcript_page
from tests.common.repo import read_seed as seed

pytestmark = pytest.mark.integration

VID = "tr000000001"


def _segs(n: int) -> list[dict[str, Any]]:
    return [{"start": float(i), "end": i + 0.5, "text": f"s{i}"} for i in range(n)]


@pytest.fixture
def video(conn: psycopg.Connection[Any]) -> str:
    seed.video(conn, VID)
    return VID


def test_no_transcript(conn: psycopg.Connection[Any], video: str) -> None:
    assert best_transcript_info(conn, VID) is None
    assert best_transcript_page(conn, VID, offset=0, limit=10) is None


@pytest.mark.parametrize(
    ("sources", "best"),
    [
        (["youtube_auto", "whisper"], "whisper"),
        (["whisper", "youtube_auto", "youtube_manual"], "youtube_manual"),
        (["youtube_auto"], "youtube_auto"),
    ],
)
def test_the_best_transcript_by_rank_is_used(
    conn: psycopg.Connection[Any], video: str, sources: list[str], best: str
) -> None:
    ids = {s: seed.transcript(conn, VID, s, segments=_segs(3)) for s in sources}

    info = best_transcript_info(conn, VID)
    page = best_transcript_page(conn, VID, offset=0, limit=10)

    assert info is not None and page is not None
    assert (info.id, info.source, info.segment_count) == (ids[best], best, 3)
    assert page.info == info


def test_info_fields(conn: psycopg.Connection[Any], video: str) -> None:
    seed.transcript(conn, VID, "youtube_manual", segments=_segs(2), language=None,
                    speaker_source="subtitle_labels")

    info = best_transcript_info(conn, VID)

    assert info is not None
    assert (info.language, info.speaker_source) == (None, "subtitle_labels")


def test_segments_carry_absolute_index_and_optional_speaker(
    conn: psycopg.Connection[Any], video: str
) -> None:
    seed.transcript(conn, VID, segments=[
        {"start": 0, "end": 1.25, "text": "a"},
        {"start": 1.25, "end": 2, "text": "b", "speaker": "Host"},
        {"start": 2, "end": 3, "text": "c"},
    ])

    page = best_transcript_page(conn, VID, offset=1, limit=5)

    assert page is not None
    assert page.segments == (
        IndexedSegment(1, 1.25, 2.0, "b", "Host"),
        IndexedSegment(2, 2.0, 3.0, "c", None),
    )
    assert page.info.segment_count == 3


def test_zero_segments(conn: psycopg.Connection[Any], video: str) -> None:
    seed.transcript(conn, VID, segments=[])

    page = best_transcript_page(conn, VID, offset=0, limit=10)

    assert page is not None
    assert (page.info.segment_count, page.segments) == (0, ())


def test_last_segment_and_beyond(conn: psycopg.Connection[Any], video: str) -> None:
    seed.transcript(conn, VID, segments=_segs(5))

    last = best_transcript_page(conn, VID, offset=4, limit=10)
    beyond = best_transcript_page(conn, VID, offset=5, limit=10)

    assert last is not None and beyond is not None
    assert [s.index for s in last.segments] == [4]
    assert (beyond.segments, beyond.info.segment_count) == ((), 5)


def test_2500_segments_page_contiguously(conn: psycopg.Connection[Any], video: str) -> None:
    seed.transcript(conn, VID, segments=_segs(2500))

    pages = [best_transcript_page(conn, VID, offset=o, limit=1000) for o in (0, 1000, 2000)]

    sizes = [len(p.segments) for p in pages if p is not None]
    indexes = [s.index for p in pages if p is not None for s in p.segments]
    assert sizes == [1000, 1000, 500]
    assert indexes == list(range(2500))
    assert pages[2] is not None and pages[2].segments[-1].text == "s2499"


def test_the_page_never_fetches_the_full_segments_or_full_text(
    conn: psycopg.Connection[Any], video: str
) -> None:
    seed.transcript(conn, VID, segments=_segs(50))
    rows: list[Any] = []

    class Spy:
        def execute(self, query: Any, params: Any = None) -> Any:
            cur = conn.execute(query, params)

            class Cur:
                def fetchone(self) -> Any:
                    row = cur.fetchone()
                    rows.append(row)
                    return row

                def fetchall(self) -> Any:
                    got = cur.fetchall()
                    rows.extend(got)
                    return got

            return Cur()

    best_transcript_page(Spy(), VID, offset=0, limit=2)  # type: ignore[arg-type]

    flat = [v for row in rows if row is not None for v in row]
    assert not any(isinstance(v, list) for v in flat)
    assert not any(isinstance(v, str) and "s49" in v for v in flat)
