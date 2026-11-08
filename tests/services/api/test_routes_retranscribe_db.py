"""``POST /videos/{id}/retranscribe`` against Postgres (issue #415)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.models import Segment
from common.repo.transcripts import save_transcript
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

VID = "dQw4w9WgXcQ"
SEGMENTS = (Segment(0, 10, "Alpha opening."), Segment(100, 110, "Beta middle."))


@pytest.fixture
def dsn(head_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()
    return head_dsn


@pytest.fixture
def db(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def api(dsn: str, logs: LogSink) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def seed(db: psycopg.Connection[Any], *sources: str) -> None:
    db.execute(
        "INSERT INTO videos (video_id, origin, duration_sec) VALUES (%s, 'adhoc', 600)",
        (VID,),
    )
    for source in sources:
        save_transcript(db, VID, source, "en", "none", SEGMENTS, None)


def jobs(db: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db.execute("SELECT kind, state, priority, payload FROM jobs ORDER BY id").fetchall()


def test_an_unknown_video_is_404_and_writes_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    assert api.post(f"/videos/{VID}/retranscribe").status_code == 404
    assert jobs(db) == []


def test_a_malformed_id_is_422(api: TestClient) -> None:
    assert api.post("/videos/short/retranscribe").status_code == 422


def test_a_video_without_a_transcript_is_409(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db)
    assert api.post(f"/videos/{VID}/retranscribe").status_code == 409
    assert jobs(db) == []


@pytest.mark.parametrize("sources", [("youtube_manual",), ("youtube_manual", "youtube_auto"), ("whisper",)])
def test_manual_or_whisper_best_transcripts_are_refused(
    api: TestClient, db: psycopg.Connection[Any], sources: tuple[str, ...]
) -> None:
    seed(db, *sources)
    assert api.post(f"/videos/{VID}/retranscribe").status_code == 409
    assert jobs(db) == []


@pytest.mark.parametrize("state", ["pending", "running"])
def test_an_active_job_of_any_kind_is_409(
    api: TestClient, db: psycopg.Connection[Any], state: str
) -> None:
    seed(db, "youtube_auto")
    db.execute(
        "INSERT INTO jobs (video_id, kind, state) VALUES (%s, 'analyze', %s)", (VID, state)
    )
    assert api.post(f"/videos/{VID}/retranscribe").status_code == 409
    assert len(jobs(db)) == 1


def test_an_auto_caption_video_gets_one_forced_interactive_transcribe_job(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, "youtube_auto")

    response = api.post(f"/videos/{VID}/retranscribe")

    assert response.status_code == 202
    body = response.json()
    assert body["video_id"] == VID and body["created"] is True and body["state"] == "pending"
    assert jobs(db) == [
        (
            "transcribe",
            "pending",
            10,
            {"force_whisper": True, "language": "en", "duration_sec": 600},
        )
    ]
    # nothing is deleted
    assert db.execute("SELECT source FROM transcripts").fetchall() == [("youtube_auto",)]
    # a repeat is refused while the job is active
    assert api.post(f"/videos/{VID}/retranscribe").status_code == 409
    assert len(jobs(db)) == 1
