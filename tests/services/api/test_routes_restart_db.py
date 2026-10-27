"""``POST /videos`` restarts a cancelled video's newest job in place (issue #304)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_INTERACTIVE,
    PostgresQueue,
    RetryOutcome,
)
from common.repo.deleted_videos import delete_video
from services.api.main import create_app
from services.api.routes_write import submit_video
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

VID = "dQw4w9WgXcQ"
ANALYZE_KEY = "v1:fake"


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


def seed_video(db: psycopg.Connection[Any], *, unavailable: bool = False) -> None:
    db.execute(
        "INSERT INTO videos (video_id, origin, unavailable) VALUES (%s, 'adhoc', %s)",
        (VID, "private" if unavailable else None),
    )


def add_job(
    db: psycopg.Connection[Any],
    state: str,
    *,
    kind: str = "ingest",
    key: str = "default",
    error_class: str | None = None,
    priority: int = 0,
    requested: bool = False,
) -> int:
    row = db.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, priority, attempts, error_class,"
        " last_error, cancel_requested_at, locked_by, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, 2, %s, %s, CASE WHEN %s THEN now() END,"
        " CASE WHEN %s = 'running' THEN 'w1' END, CASE WHEN %s IN ('done', 'dead') THEN now() END)"
        " RETURNING id",
        (
            VID,
            kind,
            key,
            state,
            priority,
            error_class,
            "cancelled on request" if error_class == "CANCELLED" else None,
            requested,
            state,
            state,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def job_row(db: psycopg.Connection[Any], job_id: int) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT state, attempts, cancel_requested_at, priority, error_class FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def job_count(db: psycopg.Connection[Any]) -> int:
    row = db.execute("SELECT count(*) FROM jobs WHERE video_id = %s", (VID,)).fetchone()
    assert row is not None
    return int(row[0])


def seed_analysis(db: psycopg.Connection[Any]) -> None:
    row = db.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text)"
        " VALUES (%s, 'whisper', '[]', 't') RETURNING id",
        (VID,),
    ).fetchone()
    assert row is not None
    db.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version, tldr)"
        " VALUES (%s, %s, 's', 'm', 'v1', 't')",
        (VID, row[0]),
    )


def wait_until_blocked(db: psycopg.Connection[Any], app: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = db.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE application_name = %s AND wait_event_type = 'Lock'",
            (app,),
        ).fetchone()
        if row is not None and row[0] > 0:
            return
    raise AssertionError(f"{app} never blocked on a lock")


def restarted_body(job_id: int) -> dict[str, Any]:
    return {
        "video_id": VID,
        "job_id": job_id,
        "state": "pending",
        "created": False,
        "restarted": True,
    }


# --- when it restarts ---------------------------------------------------------


def test_a_cancelled_ingest_is_revived_in_place_at_interactive_priority(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED", priority=PRIORITY_BACKFILL, requested=True)

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 202
    assert response.json() == restarted_body(job)
    assert job_row(db, job) == ("pending", 0, None, PRIORITY_INTERACTIVE, "CANCELLED")
    assert job_count(db) == 1


def test_a_cancelled_transcribe_is_revived_and_the_finished_ingest_is_untouched(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    ingest = add_job(db, "done")
    transcribe = add_job(db, "dead", kind="transcribe", error_class="CANCELLED")
    before = job_row(db, ingest)

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 202
    assert response.json() == restarted_body(transcribe)
    assert job_row(db, transcribe)[0] == "pending"
    assert job_row(db, ingest) == before
    assert job_count(db) == 2


def test_a_cancelled_analyze_is_revived_under_its_own_key(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "done")
    add_job(db, "done", kind="transcribe")
    analyze = add_job(db, "dead", kind="analyze", key=ANALYZE_KEY, error_class="CANCELLED")

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 202
    assert response.json() == restarted_body(analyze)
    assert db.execute("SELECT state, dedupe_key FROM jobs WHERE id = %s", (analyze,)).fetchone() == (
        "pending",
        ANALYZE_KEY,
    )


def test_a_second_post_answers_with_the_revived_job_and_restarts_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED")

    first = api.post("/videos", json={"url": VID})
    second = api.post("/videos", json={"url": VID})

    assert first.status_code == 202
    assert first.json()["restarted"] is True
    assert second.status_code == 200
    assert second.json() == {**restarted_body(job), "restarted": False}
    assert job_count(db) == 1


def test_cancel_restart_cancel_restart_round_trips(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED")

    for _ in range(2):
        assert api.post("/videos", json={"url": VID}).json()["restarted"] is True
        assert job_row(db, job)[0] == "pending"
        cancel = api.post(f"/videos/{VID}/cancel", json={})
        assert cancel.status_code in (200, 202)
        assert job_row(db, job)[0] == "dead"
        assert job_row(db, job)[4] == "CANCELLED"

    assert api.post("/videos", json={"url": VID}).json()["job_id"] == job
    assert job_count(db) == 1


def test_a_worker_claims_the_revived_job_and_finishes_it(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED", requested=True)
    api.post("/videos", json={"url": VID})

    with PostgresQueue(db).claim(["ingest"], worker="w1") as claimed:
        assert claimed is not None
        assert claimed.id == job

    assert job_row(db, job)[:3] == ("done", 1, None)


# --- when it does not restart -------------------------------------------------


@pytest.mark.parametrize(
    ("state", "error_class"),
    [
        ("dead", "PermanentSourceError"),
        ("dead", "TransientNetworkError"),
        ("dead", "ToolFailureError"),
        ("dead", "RateLimitedError"),
        ("dead", None),
        ("done", None),
    ],
)
def test_other_terminal_jobs_are_not_restarted(
    api: TestClient, db: psycopg.Connection[Any], state: str, error_class: str | None
) -> None:
    seed_video(db)
    job = add_job(db, state, error_class=error_class)
    before = job_row(db, job)

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json() == {**restarted_body(job), "state": state, "restarted": False}
    assert job_row(db, job) == before
    assert job_count(db) == 1


def test_only_the_newest_job_counts(api: TestClient, db: psycopg.Connection[Any]) -> None:
    seed_video(db)
    old = add_job(db, "dead", error_class="CANCELLED")
    add_job(db, "done", kind="transcribe")
    before = job_row(db, old)

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json()["restarted"] is False
    assert job_row(db, old) == before


def test_a_video_with_an_analysis_is_not_restarted(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    seed_analysis(db)
    add_job(db, "done")
    analyze = add_job(db, "dead", kind="analyze", key=ANALYZE_KEY, error_class="CANCELLED")

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json()["restarted"] is False
    assert job_row(db, analyze)[0] == "dead"


def test_an_unavailable_video_is_not_restarted(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, unavailable=True)
    job = add_job(db, "dead", error_class="CANCELLED")

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json()["restarted"] is False
    assert job_row(db, job)[0] == "dead"


def test_a_running_job_with_a_cancel_request_is_active_not_terminal(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job = add_job(db, "running", requested=True)
    before = job_row(db, job)[:3]

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json() == {**restarted_body(job), "state": "running", "restarted": False}
    assert job_row(db, job)[:3] == before


def test_an_active_sibling_means_no_restart(api: TestClient, db: psycopg.Connection[Any]) -> None:
    seed_video(db)
    add_job(db, "done")
    cancelled = add_job(db, "dead", kind="transcribe", error_class="CANCELLED")
    add_job(db, "pending", kind="analyze", key=ANALYZE_KEY)

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 200
    assert response.json()["restarted"] is False
    assert job_row(db, cancelled)[0] == "dead"


def test_a_superseded_cancelled_job_falls_through_to_the_existing_job(
    dsn: str, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED")

    def superseded(self: PostgresQueue, job_id: int) -> RetryOutcome:
        return RetryOutcome("superseded", newer_job_id=job_id + 1)

    monkeypatch.setattr(PostgresQueue, "retry_dead", superseded)
    with psycopg.connect(dsn) as conn:
        result = submit_video(conn, VID, queue=PostgresQueue(conn))

    assert (result.job_id, result.state, result.created, result.restarted) == (
        job,
        "dead",
        False,
        False,
    )


# --- tombstone and delete -----------------------------------------------------


def test_a_deleted_cancelled_video_is_restored_with_a_fresh_ingest(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "dead", error_class="CANCELLED")
    assert api.delete(f"/videos/{VID}").status_code == 204

    response = api.post("/videos", json={"url": VID})

    assert response.status_code == 202
    body = response.json()
    assert (body["created"], body["restarted"], body["state"]) == (True, False, "pending")
    assert db.execute("SELECT count(*) FROM deleted_videos").fetchone() == (0,)
    assert db.execute("SELECT kind, state FROM jobs WHERE video_id = %s", (VID,)).fetchall() == [
        ("ingest", "pending")
    ]


def test_delete_wins_over_a_restart_blocked_behind_it(
    dsn: str, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "dead", error_class="CANCELLED")
    result: dict[str, Any] = {}

    with psycopg.connect(dsn, application_name="deleter") as deleting:
        with deleting.transaction():
            delete_video(deleting, VID)

            def restart() -> None:
                with psycopg.connect(dsn, application_name="restarter") as conn:
                    result["value"] = submit_video(conn, VID, queue=PostgresQueue(conn))

            thread = threading.Thread(target=restart)
            thread.start()
            wait_until_blocked(db, "restarter")
        thread.join(timeout=10)
        assert not thread.is_alive()

    outcome = result["value"]
    assert (outcome.created, outcome.restarted) == (True, False)
    assert db.execute("SELECT kind, state FROM jobs WHERE video_id = %s", (VID,)).fetchall() == [
        ("ingest", "pending")
    ]


def test_a_delete_after_a_restart_removes_the_revived_job(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "dead", error_class="CANCELLED")
    assert api.post("/videos", json={"url": VID}).json()["restarted"] is True

    assert api.delete(f"/videos/{VID}").status_code == 204

    assert job_count(db) == 0


def test_two_concurrent_restarts_revive_once(dsn: str, db: psycopg.Connection[Any]) -> None:
    seed_video(db)
    job = add_job(db, "dead", error_class="CANCELLED")
    result: dict[str, Any] = {}

    with psycopg.connect(dsn) as first:
        with first.transaction():
            first_result = submit_video(first, VID, queue=PostgresQueue(first))

            def second() -> None:
                with psycopg.connect(dsn, application_name="second") as conn:
                    result["value"] = submit_video(conn, VID, queue=PostgresQueue(conn))

            thread = threading.Thread(target=second)
            thread.start()
            wait_until_blocked(db, "second")
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert (first_result.job_id, first_result.restarted) == (job, True)
    assert (result["value"].job_id, result["value"].restarted) == (job, False)
    assert job_count(db) == 1
