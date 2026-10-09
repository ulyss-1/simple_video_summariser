"""The aggregate queries behind ``GET /metrics`` (issue #60), on real Postgres.

Timestamps are seeded explicitly, so no test depends on the wall clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from common.metrics import DURATION_BUCKETS
from common.models import Analysis
from common.repo.analyses import (
    latest_analysis,
    latest_analysis_run,
    reanalysis_backlog_count,
    save_analysis,
    token_usage_by_model,
    total_cost_usd,
    total_speaker_coercions,
)
from common.repo.jobs import duration_histograms
from common.repo.transcripts import latest_whisper_rtf

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _video(conn: psycopg.Connection[Any], vid: str) -> None:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING", (vid,))


def _transcript(
    conn: psycopg.Connection[Any],
    vid: str,
    source: str = "whisper",
    *,
    text: str = "hello",
    engine_meta: object = None,
    minutes: int = 0,
) -> int:
    _video(conn, vid)
    row = conn.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text, engine_meta, created_at)"
        " VALUES (%s, %s, '[]', %s, %s::jsonb, %s) RETURNING id",
        (
            vid,
            source,
            text,
            None if engine_meta is None else json.dumps(engine_meta),
            T0 + timedelta(minutes=minutes),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _analysis(
    conn: psycopg.Connection[Any],
    vid: str,
    transcript_id: int,
    *,
    model: str = "m",
    prompt_version: str = "v1",
    tokens: tuple[int, int] = (0, 0),
    cost: float | None = None,
    coerced: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version,"
        " tldr, input_tokens, output_tokens, cost_usd, speakers_coerced)"
        " VALUES (%s, %s, 's', %s, %s, 't', %s, %s, %s, %s)",
        (vid, transcript_id, model, prompt_version, tokens[0], tokens[1], cost, coerced),
    )


def _job(
    conn: psycopg.Connection[Any],
    vid: str,
    kind: str = "ingest",
    state: str = "done",
    *,
    seconds: float | None = None,
    started: bool = True,
    key: str = "default",
) -> None:
    started_at = T0 if (started and seconds is not None) else None
    finished_at = None if seconds is None else T0 + timedelta(seconds=seconds)
    conn.execute(
        "INSERT INTO jobs (video_id, kind, state, dedupe_key, started_at, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (vid, kind, state, key, started_at, finished_at),
    )


# -- job_duration_seconds ---------------------------------------------------------------


def test_no_jobs_give_no_histograms(conn: psycopg.Connection[Any]) -> None:
    assert duration_histograms(conn) == {}


def test_exactly_sixty_seconds_is_in_le_60_and_sixty_point_001_is_not(
    conn: psycopg.Connection[Any],
) -> None:
    _job(conn, "a", seconds=60.0)
    _job(conn, "b", seconds=60.001)

    hist = duration_histograms(conn)["ingest"]

    by_bound = dict(zip(DURATION_BUCKETS, hist.cumulative, strict=True))
    assert by_bound[15] == 0
    assert by_bound[60] == 1
    assert by_bound[300] == 2
    assert hist.count == 2
    assert hist.sum_seconds == pytest.approx(120.001)


def test_every_bucket_edge_is_inclusive_and_buckets_are_cumulative(
    conn: psycopg.Connection[Any],
) -> None:
    for i, bound in enumerate(DURATION_BUCKETS):
        _job(conn, f"e{i}", seconds=float(bound))
    _job(conn, "over", seconds=DURATION_BUCKETS[-1] + 1)

    hist = duration_histograms(conn)["ingest"]

    assert hist.cumulative == tuple(range(1, len(DURATION_BUCKETS) + 1))
    assert hist.count == len(DURATION_BUCKETS) + 1


def test_only_done_jobs_with_a_recorded_start_count(conn: psycopg.Connection[Any]) -> None:
    _job(conn, "ok", "transcribe", seconds=10.0)
    _job(conn, "legacy", "transcribe", seconds=10.0, started=False)  # no started_at
    _job(conn, "dead", "transcribe", "dead", seconds=10.0)
    _job(conn, "pending", "transcribe", "pending")
    _job(conn, "running", "transcribe", "running")

    hist = duration_histograms(conn)

    assert set(hist) == {"transcribe"}
    assert hist["transcribe"].count == 1
    assert hist["transcribe"].sum_seconds == pytest.approx(10.0)


def test_histograms_are_per_kind_and_unknown_kinds_are_returned_for_the_renderer_to_fold(
    conn: psycopg.Connection[Any],
) -> None:
    _job(conn, "a", "ingest", seconds=1.0)
    _job(conn, "b", "analyze", seconds=100.0)
    _job(conn, "c", "mystery", seconds=1.0)

    hist = duration_histograms(conn)

    assert {k: h.count for k, h in hist.items()} == {"ingest": 1, "analyze": 1, "mystery": 1}


# -- whisper_rtf -----------------------------------------------------------------------------


def test_no_whisper_transcript_gives_no_rtf(conn: psycopg.Connection[Any]) -> None:
    _transcript(conn, "a", "youtube_manual", engine_meta={"rtf": 0.5})

    assert latest_whisper_rtf(conn) is None


def test_the_newest_usable_whisper_rtf_wins_and_other_sources_are_ignored(
    conn: psycopg.Connection[Any],
) -> None:
    _transcript(conn, "old", engine_meta={"rtf": 0.5}, minutes=1)
    _transcript(conn, "new", engine_meta={"rtf": 0.25}, minutes=2)
    _transcript(conn, "manual", "youtube_manual", engine_meta={"rtf": 9.0}, minutes=3)

    assert latest_whisper_rtf(conn) == 0.25


@pytest.mark.parametrize(
    "bad",
    [None, {}, {"rtf": None}, {"rtf": "fast"}, {"rtf": "0.9"}, {"rtf": 0}, {"rtf": -1.5},
     {"rtf": [1]}, {"rtf": True}, {"rtf": 1e301}, [0.5], "x"],
)
def test_an_unusable_rtf_is_skipped_in_favour_of_an_older_usable_one(
    conn: psycopg.Connection[Any], bad: object
) -> None:
    _transcript(conn, "good", engine_meta={"rtf": 0.5}, minutes=1)
    _transcript(conn, "bad", engine_meta=bad, minutes=2)

    assert latest_whisper_rtf(conn) == 0.5


# -- llm_tokens_total / cost / coercions -----------------------------------------------------------


def test_tokens_are_summed_per_model(conn: psycopg.Connection[Any]) -> None:
    t = _transcript(conn, "a")
    _analysis(conn, "a", t, model="qwen", tokens=(10, 1))
    _analysis(conn, "a", t, model="qwen", prompt_version="v2", tokens=(5, 2))
    _analysis(conn, "a", t, model="claude", tokens=(7, 3))

    assert token_usage_by_model(conn) == {"qwen": (15, 3), "claude": (7, 3)}


def test_empty_analyses_give_no_usage_no_cost_and_zero_coercions(
    conn: psycopg.Connection[Any],
) -> None:
    assert token_usage_by_model(conn) == {}
    assert total_cost_usd(conn) is None
    assert total_speaker_coercions(conn) == 0


def test_cost_and_coercions_ignore_null_rows(conn: psycopg.Connection[Any]) -> None:
    t = _transcript(conn, "a")
    _analysis(conn, "a", t, prompt_version="v1", cost=0.5, coerced=2)
    _analysis(conn, "a", t, prompt_version="v2", cost=None, coerced=None)
    _analysis(conn, "a", t, prompt_version="v3", cost=0.25, coerced=0)

    assert total_cost_usd(conn) == pytest.approx(0.75)
    assert total_speaker_coercions(conn) == 2


def test_all_null_cost_is_none_and_all_null_coercions_are_zero(
    conn: psycopg.Connection[Any],
) -> None:
    t = _transcript(conn, "a")
    _analysis(conn, "a", t)

    assert total_cost_usd(conn) is None
    assert total_speaker_coercions(conn) == 0


# -- reanalysis_backlog_videos ------------------------------------------------------------------------


def test_backlog_counts_videos_with_text_and_no_analysis_at_this_version(
    conn: psycopg.Connection[Any],
) -> None:
    _transcript(conn, "pending1")  # no analysis at all
    t2 = _transcript(conn, "oldver")
    _analysis(conn, "oldver", t2, prompt_version="v0")  # analysed at another version
    t3 = _transcript(conn, "done")
    _analysis(conn, "done", t3, prompt_version="v1")
    _transcript(conn, "blank", text="  \t\n ")  # no non-whitespace text
    _video(conn, "notranscript")

    assert reanalysis_backlog_count(conn, "v1") == 2
    assert reanalysis_backlog_count(conn, "v0") == 2  # pending1 and done
    assert reanalysis_backlog_count(conn, "v9") == 3


def test_backlog_is_zero_when_everything_is_current_and_on_empty_tables(
    conn: psycopg.Connection[Any],
) -> None:
    assert reanalysis_backlog_count(conn, "v1") == 0
    t = _transcript(conn, "a")
    _analysis(conn, "a", t, prompt_version="v1")
    assert reanalysis_backlog_count(conn, "v1") == 0


def test_backlog_counts_a_video_once_however_many_transcripts_it_has(
    conn: psycopg.Connection[Any],
) -> None:
    _transcript(conn, "a", "whisper")
    _transcript(conn, "a", "youtube_manual")

    assert reanalysis_backlog_count(conn, "v1") == 1


# -- speakers_coerced round trip ---------------------------------------------------------------------------


def _model(video_id: str, transcript_id: int, coerced: int | None) -> Analysis:
    return Analysis(
        video_id=video_id,
        transcript_id=transcript_id,
        chunk_strategy="s",
        model="m",
        prompt_version="v1",
        tldr="t",
        speakers_coerced=coerced,
    )


@pytest.mark.parametrize("coerced", [None, 0, 5])
def test_speakers_coerced_is_saved_and_read_back(
    conn: psycopg.Connection[Any], coerced: int | None
) -> None:
    t = _transcript(conn, "a")
    save_analysis(conn, _model("a", t, coerced))

    latest = latest_analysis(conn, "a")
    run = latest_analysis_run(conn, "a")

    assert latest is not None and latest.speakers_coerced == coerced
    assert run is not None and run.analysis.speakers_coerced == coerced
    assert run.transcript_source == "whisper"
