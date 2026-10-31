"""The two repo reads behind the analysis estimate (issue #153)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from common.repo.analyses import analysis_models
from common.repo.transcripts import analysis_transcript_chars
from tests.common.repo import read_seed as seed

pytestmark = pytest.mark.integration

A = "a0000000001"
B = "b0000000001"
C = "c0000000001"


def _segs(text: str) -> list[dict[str, Any]]:
    return [{"start": 0.0, "end": 1.0, "text": text}]


def test_the_best_ranked_non_blank_transcript_is_measured(conn: psycopg.Connection[Any]) -> None:
    seed.video(conn, A)
    seed.transcript(conn, A, "youtube_auto", segments=_segs("x" * 500))
    seed.transcript(conn, A, "youtube_manual", segments=_segs("y" * 120))
    seed.video(conn, B)
    seed.transcript(conn, B, "youtube_manual", segments=_segs("   "))  # blank: skipped
    seed.transcript(conn, B, "whisper", segments=_segs("z" * 40))
    seed.video(conn, C)  # no transcript

    assert analysis_transcript_chars(conn, [A, B, C]) == {A: 120, B: 40}


def test_no_ids_is_empty_without_a_query(conn: psycopg.Connection[Any]) -> None:
    assert analysis_transcript_chars(conn, []) == {}


def test_analysis_models_are_distinct_and_sorted(conn: psycopg.Connection[Any]) -> None:
    seed.video(conn, A)
    seed.video(conn, B)
    t = seed.transcript(conn, A)
    tb = seed.transcript(conn, B)
    seed.analysis(conn, A, t, created=1, model="zeta")
    seed.analysis(conn, A, t, created=2, model="alpha")
    seed.analysis(conn, A, t, created=3, model="zeta", prompt_version="v2")
    seed.analysis(conn, B, tb, created=4, model="other")

    assert analysis_models(conn, A) == ["alpha", "zeta"]
    assert analysis_models(conn, C) == []
