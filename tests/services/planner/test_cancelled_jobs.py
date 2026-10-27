"""How the planner treats a ``CANCELLED`` dead job (issue #237). Needs Postgres.

A user's cancel ends a job ``dead`` with ``error_class = 'CANCELLED'``. None of the
planner's passes may announce it, re-queue it or revive it.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from common.config import Settings
from common.queue import PostgresQueue, analyze_dedupe_key
from services.planner.alerts import send_dead_letter_alerts
from services.planner.fallback import run_auto_caption_fallback_sweep
from services.planner.sweep import run_reanalysis_sweep

pytestmark = pytest.mark.integration

V = "aaaaaaaaaaa"


class Sink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> None:
        self.sent.append((title, message))


def _settings() -> Settings:
    return Settings(DATABASE_URL=SecretStr("postgresql://u:p@h/db"))


def _cancelled_job(conn: psycopg.Connection[Any], kind: str, dedupe_key: str = "default") -> int:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING", (V,))
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, error_class, last_error, "
        "attempts, finished_at, cancel_requested_at) "
        "VALUES (%s, %s, %s, 'dead', 'CANCELLED', 'cancelled on request', 1, now(), now()) "
        "RETURNING id",
        (V, kind, dedupe_key),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_dead_letter_alert_never_announces_a_cancelled_job(
    conn: psycopg.Connection[Any],
) -> None:
    job = _cancelled_job(conn, "ingest")
    sink = Sink()

    sent = send_dead_letter_alerts(conn, sink, min_interval_sec=3600)

    assert sent == 0
    assert sink.sent == []
    row = conn.execute("SELECT alerted_at FROM jobs WHERE id = %s", (job,)).fetchone()
    assert row is not None and row[0] is None


def test_the_auto_caption_fallback_does_not_ingest_a_video_whose_newest_transcribe_was_cancelled(
    conn: psycopg.Connection[Any],
) -> None:
    _cancelled_job(conn, "transcribe")
    settings = _settings()

    enqueued = run_auto_caption_fallback_sweep(
        conn, PostgresQueue(conn, settings=settings), settings=settings
    )

    assert enqueued == 0
    row = conn.execute("SELECT count(*) FROM jobs WHERE kind = 'ingest'").fetchone()
    assert row is not None and row[0] == 0


def test_the_reanalysis_sweep_does_not_revive_a_cancelled_analyze(
    conn: psycopg.Connection[Any],
) -> None:
    key = analyze_dedupe_key("v2", "fake")
    _cancelled_job(conn, "analyze", key)
    conn.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text) "
        "VALUES (%s, 'whisper', '[]', 'hello')",
        (V,),
    )
    settings = _settings()

    enqueued = run_reanalysis_sweep(
        conn, PostgresQueue(conn, settings=settings), prompt_version="v2", summarizer_name="fake"
    )

    assert enqueued == 0
    row = conn.execute(
        "SELECT state, error_class FROM jobs WHERE kind = 'analyze' AND dedupe_key = %s", (key,)
    ).fetchall()
    assert row == [("dead", "CANCELLED")]


def test_stale_analyze_leaves_a_cancelled_analyze_alone(conn: psycopg.Connection[Any]) -> None:
    job = _cancelled_job(conn, "analyze", analyze_dedupe_key("v1", "fake"))

    result = PostgresQueue(conn, settings=_settings()).resolve_stale_analyze("v2")

    assert (result.rekeyed, result.superseded, result.dead) == (0, 0, 0)
    row = conn.execute(
        "SELECT state, error_class, dedupe_key FROM jobs WHERE id = %s", (job,)
    ).fetchone()
    assert row == ("dead", "CANCELLED", analyze_dedupe_key("v1", "fake"))


def test_the_auto_caption_fallback_ignores_a_dead_transcribe_of_a_deleted_video(
    conn: psycopg.Connection[Any],
) -> None:
    """#303: the leftover record of a deleted video has no ``videos`` row to join."""
    conn.execute(
        "INSERT INTO jobs (video_id, kind, state, error_class, attempts, finished_at) "
        "VALUES (%s, 'transcribe', 'dead', 'TOOL_FAILURE', 3, now())",
        (V,),
    )
    settings = _settings()

    enqueued = run_auto_caption_fallback_sweep(
        conn, PostgresQueue(conn, settings=settings), settings=settings
    )

    assert enqueued == 0
    row = conn.execute("SELECT count(*) FROM jobs WHERE kind = 'ingest'").fetchone()
    assert row is not None and row[0] == 0
