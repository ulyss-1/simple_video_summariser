"""Tests for ``analysis_exists`` in common/repo/analyses.py (issue #30)."""

from __future__ import annotations

import psycopg
import pytest

from common.models import Analysis
from common.repo import analyses as analyses_repo
from common.repo.analyses import save_analysis
from common.repo.transcripts import save_transcript

pytestmark = pytest.mark.integration


def _save(conn: psycopg.Connection, video_id: str, transcript_id: int) -> None:
    save_analysis(
        conn,
        Analysis(
            video_id=video_id,
            transcript_id=transcript_id,
            chunk_strategy="time:900:60",
            model="m1",
            prompt_version="v1",
            tldr="t",
        ),
    )


def test_analysis_exists_matches_only_the_exact_five_part_key(
    conn: psycopg.Connection, video_id: str
) -> None:
    transcript_id = save_transcript(conn, video_id, "whisper", "en", "none", (), None)
    other_id = save_transcript(conn, video_id, "youtube_auto", "en", "none", (), None)
    _save(conn, video_id, transcript_id)

    def exists(
        *,
        vid: str = video_id,
        tid: int = transcript_id,
        strategy: str = "time:900:60",
        model: str = "m1",
        version: str = "v1",
    ) -> bool:
        return analyses_repo.analysis_exists(conn, vid, tid, strategy, model, version)

    assert exists() is True
    assert exists(tid=other_id) is False
    assert exists(strategy="time:600:60") is False
    assert exists(model="m2") is False
    assert exists(version="v2") is False
    assert exists(vid="someothervid") is False
