"""The batches of a deleted video end to end (issue #251): real Postgres, the real
``delete_video``, the real ``AnthropicSummarizer`` on a ``FakeTransport``.
No network, no real key."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from adapters.summarize.anthropic import AnthropicSummarizer
from common.config import get_settings
from common.repo.deleted_videos import delete_video
from common.repo.llm_batches import insert_checkpoint
from services.analyzer.orphans import ORPHAN_EXPIRED_AFTER, PassCounts, run_pass
from services.api.main import create_app
from tests.adapters.summarize.test_anthropic import (
    API,
    FakeTransport,
    fixture_response,
    settings,
)

pytestmark = pytest.mark.integration

VIDEO_A = "AAAAAAAAAAA"
VIDEO_B = "BBBBBBBBBBB"
B1, B2, B3 = "msgbatch_B1", "msgbatch_B2", "msgbatch_B3"


def seed_two_videos(conn: psycopg.Connection[Any]) -> tuple[int, int, int]:
    """A: a pending job holding B1 and a dead job holding B2. B: a pending job holding B3."""
    for video in (VIDEO_A, VIDEO_B):
        conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (video,))
    ids = []
    for video, state, key in (
        (VIDEO_A, "pending", "v1:pending"),
        (VIDEO_A, "dead", "v1:dead"),
        (VIDEO_B, "pending", "v1:pending"),
    ):
        row = conn.execute(
            "INSERT INTO jobs (kind, video_id, dedupe_key, state) VALUES ('analyze', %s, %s, %s) "
            "RETURNING id",
            (video, key, state),
        ).fetchone()
        assert row is not None
        ids.append(int(row[0]))
    for job, batch, hours in zip(ids, (B1, B2, B3), (3, 2, 1), strict=True):
        insert_checkpoint(conn, job, "roster", batch, "sha")
        conn.execute(
            "UPDATE llm_batches SET submitted_at = now() - make_interval(hours => %s) "
            "WHERE job_id = %s",
            (hours, job),
        )
    conn.commit()
    return ids[0], ids[1], ids[2]


def canceller_and_transport() -> tuple[AnthropicSummarizer, FakeTransport]:
    transport = FakeTransport(
        fixture_response("batch_in_progress.json"), fixture_response("batch_in_progress.json")
    )
    return AnthropicSummarizer(settings(), batch=True, transport=transport), transport


def one_pass(head_dsn: str, canceller: AnthropicSummarizer) -> PassCounts | None:
    return run_pass(
        lambda: psycopg.connect(head_dsn),
        canceller,
        limit=5,
        expired_after=ORPHAN_EXPIRED_AFTER,
        stop=lambda: False,
    )


def requests(transport: FakeTransport) -> list[tuple[str, str]]:
    return [(r.method, r.url) for r in transport.requests]


def test_the_batches_of_a_deleted_video_are_cancelled_once_and_another_videos_is_not(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    _, _, other_job = seed_two_videos(conn)
    delete_video(conn, VIDEO_A)
    canceller, transport = canceller_and_transport()

    counts = one_pass(head_dsn, canceller)

    assert counts is not None and counts.cancelled == 2
    assert requests(transport) == [
        ("POST", f"{API}/v1/messages/batches/{B1}/cancel"),
        ("POST", f"{API}/v1/messages/batches/{B2}/cancel"),
    ]
    conn.rollback()
    assert conn.execute("SELECT job_id, batch_id FROM llm_batches").fetchall() == [(other_job, B3)]

    again = one_pass(head_dsn, canceller)

    assert again is not None and again.total == 0
    assert len(transport.requests) == 2


def test_a_video_added_again_after_the_delete_does_not_adopt_the_old_batches(
    conn: psycopg.Connection[Any], head_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_pending, old_dead, other_job = seed_two_videos(conn)
    delete_video(conn, VIDEO_A)
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as api:
            response = api.post("/videos", json={"url": VIDEO_A})
    finally:
        get_settings.cache_clear()
    assert response.status_code == 202
    assert response.json()["job_id"] not in (old_pending, old_dead)
    conn.rollback()
    assert conn.execute("SELECT job_id, batch_id FROM llm_batches ORDER BY batch_id").fetchall() == [
        (None, B1),
        (None, B2),
        (other_job, B3),
    ]
    canceller, transport = canceller_and_transport()

    counts = one_pass(head_dsn, canceller)

    assert counts is not None and counts.cancelled == 2
    assert requests(transport) == [
        ("POST", f"{API}/v1/messages/batches/{B1}/cancel"),
        ("POST", f"{API}/v1/messages/batches/{B2}/cancel"),
    ]
