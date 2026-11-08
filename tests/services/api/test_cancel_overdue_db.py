"""``cancel_overdue`` on the active job and the Ops rows (#411).

The server decides with the database clock. Requests are seeded in the past
(no waiting) and the threshold is injected into the repo functions, so the
29 s / 31 s boundary needs no 30 s clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.repo.jobs import get_job, list_jobs
from common.repo.videos import get_video, list_videos
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

VID = "dQw4w9WgXcQ"
THRESHOLD = 30
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def dsn(head_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
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


@pytest.fixture
def video(db: psycopg.Connection[Any]) -> None:
    db.execute("INSERT INTO channels (channel_id, title) VALUES ('UCkeep', 'Keep')")
    db.execute(
        "INSERT INTO videos (video_id, channel_id, title, origin) VALUES (%s, 'UCkeep', 'T', 'adhoc')",
        (VID,),
    )


def add_job(
    db: psycopg.Connection[Any],
    state: str,
    age_sec: int | None,
    *,
    kind: str = "ingest",
    error_class: str | None = None,
) -> int:
    """A job whose cancel request is ``age_sec`` old by the database clock (None: no request)."""
    row = db.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, locked_by, attempts, error_class,"
        " cancel_requested_at, finished_at)"
        " VALUES (%s, %s, 'default', %s, %s, 1, %s,"
        " now() - make_interval(secs => %s::float8),"
        " CASE WHEN %s IN ('done', 'dead') THEN now() END) RETURNING id",
        (
            VID,
            kind,
            state,
            "w1" if state == "running" else None,
            error_class,
            age_sec,  # NULL stays NULL
            state,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def detail_flag(db: psycopg.Connection[Any], threshold: int = THRESHOLD) -> bool | None:
    video = get_video(db, VID, cancel_overdue_sec=threshold)
    assert video is not None
    return None if video.active_job is None else video.active_job.cancel_overdue


@pytest.mark.parametrize(("age", "expected"), [(29, False), (31, True)])
def test_a_running_job_is_overdue_only_past_the_injected_threshold(
    db: psycopg.Connection[Any], video: None, age: int, expected: bool
) -> None:
    job_id = add_job(db, "running", age)

    assert detail_flag(db) is expected
    assert [r.cancel_overdue for r in list_jobs(db, limit=5, cancel_overdue_sec=THRESHOLD)] == [expected]
    row = get_job(db, job_id, cancel_overdue_sec=THRESHOLD)
    assert row is not None
    assert row.cancel_overdue is expected
    page = list_videos(db, cancel_overdue_sec=THRESHOLD)
    assert page.items[0].active_job is not None
    assert page.items[0].active_job.cancel_overdue is expected


def test_no_request_is_never_overdue(db: psycopg.Connection[Any], video: None) -> None:
    add_job(db, "running", None)

    assert detail_flag(db, 0) is False


def test_a_pending_job_is_never_overdue_even_with_an_old_request(
    db: psycopg.Connection[Any], video: None
) -> None:
    add_job(db, "pending", 3600)

    assert detail_flag(db) is False


def test_without_a_threshold_nothing_is_overdue(db: psycopg.Connection[Any], video: None) -> None:
    add_job(db, "running", 3600)

    video_row = get_video(db, VID)
    assert video_row is not None
    assert video_row.active_job is not None
    assert video_row.active_job.cancel_overdue is False


def test_the_api_uses_the_computed_threshold(api: TestClient, db: psycopg.Connection[Any], video: None) -> None:
    add_job(db, "running", get_settings().CANCEL_OVERDUE_SEC + 1)

    assert api.get(f"/videos/{VID}").json()["active_job"]["cancel_overdue"] is True
    assert api.get("/videos").json()["items"][0]["active_job"]["cancel_overdue"] is True
    assert [i["cancel_overdue"] for i in api.get("/ops/jobs").json()["items"]] == [True]


def test_the_api_says_false_for_a_fresh_request(
    api: TestClient, db: psycopg.Connection[Any], video: None
) -> None:
    add_job(db, "running", 1)

    assert api.get(f"/videos/{VID}").json()["active_job"]["cancel_overdue"] is False
    assert [i["cancel_overdue"] for i in api.get("/ops/jobs").json()["items"]] == [False]


def test_ops_rows_that_are_not_running_are_never_overdue(
    api: TestClient, db: psycopg.Connection[Any], video: None
) -> None:
    done = add_job(db, "done", 3600)  # a stale request left on a finished job
    dead = add_job(db, "dead", 3600, kind="analyze", error_class="CANCELLED")
    pending = add_job(db, "pending", 3600, kind="transcribe")

    items = {i["id"]: i for i in api.get("/ops/jobs").json()["items"]}

    assert {k: v["cancel_overdue"] for k, v in items.items()} == {done: False, dead: False, pending: False}


def test_a_retried_job_is_not_overdue(api: TestClient, db: psycopg.Connection[Any], video: None) -> None:
    job_id = add_job(db, "dead", 3600, error_class="CANCELLED")

    response = api.post(f"/ops/jobs/{job_id}/retry", json={}, headers=JSON)

    assert response.status_code == 200
    assert response.json()["cancel_requested_at"] is None
    assert response.json()["cancel_overdue"] is False
