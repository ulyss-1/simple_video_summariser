"""``POST /videos/batch`` against a real database (issue #341).

The races are forced, not hoped for: one transaction is held open, the competing
request is started, and the test waits (bounded) until Postgres reports it blocked
on a lock before the holder commits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_INTERACTIVE,
    PRIORITY_NORMAL,
    PostgresQueue,
)
from common.repo.deleted_videos import delete_video, delete_videos
from services.api import routes_write
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

A = "dQw4w9WgXcQ"
B = "abcdefghijk"
C = "ABCDEFGHIJK"


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


def seed_video(db: psycopg.Connection[Any], video_id: str) -> None:
    db.execute("INSERT INTO videos (video_id, origin) VALUES (%s, 'adhoc')", (video_id,))


def add_job(
    db: psycopg.Connection[Any],
    video_id: str,
    state: str,
    *,
    priority: int = 0,
    error_class: str | None = None,
    requested: bool = False,
) -> int:
    row = db.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, priority, attempts, error_class,"
        " cancel_requested_at, locked_by, finished_at)"
        " VALUES (%s, 'ingest', 'default', %s, %s, 1, %s, CASE WHEN %s THEN now() END,"
        " CASE WHEN %s = 'running' THEN 'w1' END, CASE WHEN %s IN ('done', 'dead') THEN now() END)"
        " RETURNING id",
        (video_id, state, priority, error_class, requested, state, state),
    ).fetchone()
    assert row is not None
    return int(row[0])


def jobs_of(db: psycopg.Connection[Any], video_id: str) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in db.execute(
            "SELECT id, kind, state, priority FROM jobs WHERE video_id = %s ORDER BY id", (video_id,)
        ).fetchall()
    ]


def count(db: psycopg.Connection[Any], table: str, video_id: str) -> int:
    row = db.execute(f"SELECT count(*) FROM {table} WHERE video_id = %s", (video_id,)).fetchone()
    assert row is not None
    return int(row[0])


def wait_until_a_backend_is_blocked(db: psycopg.Connection[Any]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = db.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0"
        ).fetchone()
        if row is not None and row[0] > 0:
            return
        time.sleep(0.01)
    raise AssertionError("the competing request never blocked on a lock")


def run_in_thread(fn: Callable[[], Any], out: dict[str, Any]) -> threading.Thread:
    def target() -> None:
        out["value"] = fn()

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def outcomes(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["outcome"] for item in response.json()["results"]]


# --- outcomes and priority ----------------------------------------------------


def test_a_new_video_is_queued_as_an_adhoc_ingest_at_normal_priority(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    response = api.post("/videos/batch", json={"urls": [A]})

    assert outcomes(response) == ["queued"]
    item = response.json()["results"][0]
    assert item["video_id"] == A
    rows = jobs_of(db, A)
    assert [(r[1], r[2], r[3]) for r in rows] == [("ingest", "pending", PRIORITY_NORMAL)]
    assert item["job_id"] == rows[0][0]
    assert db.execute("SELECT origin FROM videos WHERE video_id = %s", (A,)).fetchone() == ("adhoc",)


def test_a_pending_backfill_job_is_already_submitted_and_raised_to_normal(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, A)
    job = add_job(db, A, "pending", priority=PRIORITY_BACKFILL)

    response = api.post("/videos/batch", json={"urls": [A]})

    assert outcomes(response) == ["already_submitted"]
    item = response.json()["results"][0]
    assert (item["job_id"], item["state"]) == (job, "pending")
    assert jobs_of(db, A) == [(job, "ingest", "pending", PRIORITY_NORMAL)]


def test_a_pending_interactive_job_keeps_its_priority(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, A)
    job = add_job(db, A, "pending", priority=PRIORITY_INTERACTIVE)

    assert outcomes(api.post("/videos/batch", json={"urls": [A]})) == ["already_submitted"]

    assert jobs_of(db, A) == [(job, "ingest", "pending", PRIORITY_INTERACTIVE)]


def test_post_videos_still_enqueues_at_interactive_priority(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    assert api.post("/videos", json={"url": A}).status_code == 202

    assert [r[3] for r in jobs_of(db, A)] == [PRIORITY_INTERACTIVE]


def test_a_tombstoned_video_is_restored_and_queued(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, A)
    add_job(db, A, "done")
    assert api.delete(f"/videos/{A}").status_code == 204

    assert outcomes(api.post("/videos/batch", json={"urls": [A]})) == ["queued"]

    assert count(db, "deleted_videos", A) == 0
    assert [(r[1], r[2], r[3]) for r in jobs_of(db, A)] == [("ingest", "pending", PRIORITY_NORMAL)]


def test_a_tombstoned_video_with_a_cancelling_running_job_is_cancellation_in_progress(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, A)
    job = add_job(db, A, "running")
    assert api.delete(f"/videos/{A}").status_code == 204
    before = jobs_of(db, A)
    assert before == [(job, "ingest", "running", 0)]

    response = api.post("/videos/batch", json={"urls": [A, B]})

    assert outcomes(response) == ["cancellation_in_progress", "queued"]
    assert response.json()["results"][0] == {
        "index": 0, "outcome": "cancellation_in_progress", "video_id": A,
    }
    assert count(db, "deleted_videos", A) == 1
    assert count(db, "videos", A) == 0
    assert jobs_of(db, A) == before


def test_a_cancelled_video_is_restarted_in_place_at_max_of_current_and_normal(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db, A)
    seed_video(db, B)
    low = add_job(db, A, "dead", priority=PRIORITY_BACKFILL, error_class="CANCELLED")
    high = add_job(db, B, "dead", priority=PRIORITY_INTERACTIVE, error_class="CANCELLED")

    response = api.post("/videos/batch", json={"urls": [A, B]})

    assert outcomes(response) == ["restarted", "restarted"]
    assert [r["job_id"] for r in response.json()["results"]] == [low, high]
    assert response.json()["counts"]["restarted"] == 2
    assert jobs_of(db, A) == [(low, "ingest", "pending", PRIORITY_NORMAL)]
    assert jobs_of(db, B) == [(high, "ingest", "pending", PRIORITY_INTERACTIVE)]


# --- a database error on one item ---------------------------------------------


def test_a_database_error_on_the_second_item_fails_only_that_item(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = routes_write.submit_video
    calls: list[str] = []

    def flaky(conn: Any, video_id: str, **kwargs: Any) -> Any:
        calls.append(video_id)
        if len(calls) == 2:
            raise psycopg.errors.QueryCanceled("injected")
        return real(conn, video_id, **kwargs)

    monkeypatch.setattr(routes_write, "submit_video", flaky)

    first = api.post("/videos/batch", json={"urls": [A, B, C]})

    assert outcomes(first) == ["queued", "failed", "queued"]
    assert first.json()["counts"]["failed"] == 1
    assert first.json()["results"][1] == {"index": 1, "outcome": "failed", "video_id": B}
    assert count(db, "jobs", A) == 1 and count(db, "jobs", C) == 1
    assert count(db, "jobs", B) == 0 and count(db, "videos", B) == 0

    monkeypatch.setattr(routes_write, "submit_video", real)
    again = api.post("/videos/batch", json={"urls": [A, B, C]})

    assert outcomes(again) == ["already_submitted", "queued", "already_submitted"]
    assert count(db, "jobs", A) == 1 and count(db, "jobs", C) == 1


# --- forced interleavings -----------------------------------------------------


def test_delete_first_then_the_blocked_batch_item_restores_the_video(
    dsn: str, db: psycopg.Connection[Any], api: TestClient
) -> None:
    seed_video(db, A)
    add_job(db, A, "done")
    out: dict[str, Any] = {}

    with psycopg.connect(dsn) as deleting:
        with deleting.transaction():
            delete_video(deleting, A)
            thread = run_in_thread(lambda: api.post("/videos/batch", json={"urls": [A]}), out)
            wait_until_a_backend_is_blocked(db)
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert outcomes(out["value"]) == ["queued"]
    assert count(db, "videos", A) == 1
    assert count(db, "deleted_videos", A) == 0


def test_batch_item_first_then_the_blocked_delete_removes_the_video(
    dsn: str, db: psycopg.Connection[Any], api: TestClient
) -> None:
    seed_video(db, A)
    out: dict[str, Any] = {}

    with psycopg.connect(dsn) as submitting:
        with submitting.transaction():
            routes_write.submit_video(
                submitting, A, queue=PostgresQueue(submitting), priority=PRIORITY_NORMAL
            )
            thread = run_in_thread(lambda: api.delete(f"/videos/{A}"), out)
            wait_until_a_backend_is_blocked(db)
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert out["value"].status_code == 204
    assert count(db, "videos", A) == 0
    assert count(db, "jobs", A) == 0
    assert count(db, "deleted_videos", A) == 1


def test_a_batch_and_a_bulk_delete_naming_the_same_videos_in_opposite_order_do_not_deadlock(
    dsn: str, db: psycopg.Connection[Any], api: TestClient
) -> None:
    for video_id in (A, B):
        seed_video(db, video_id)
        add_job(db, video_id, "done")
    out: dict[str, Any] = {}

    with psycopg.connect(dsn) as deleting:
        with deleting.transaction():
            delete_videos(deleting, [B, A])
            thread = run_in_thread(lambda: api.post("/videos/batch", json={"urls": [A, B]}), out)
            wait_until_a_backend_is_blocked(db)
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert outcomes(out["value"]) == ["queued", "queued"]
    for video_id in (A, B):
        assert count(db, "videos", video_id) == 1
        assert count(db, "deleted_videos", video_id) == 0


def test_a_bulk_delete_blocked_behind_a_batch_item_waits_then_deletes_both(
    dsn: str, db: psycopg.Connection[Any], api: TestClient
) -> None:
    for video_id in (A, B):
        seed_video(db, video_id)
        add_job(db, video_id, "done")
    out: dict[str, Any] = {}

    with psycopg.connect(dsn) as submitting:
        with submitting.transaction():
            routes_write.submit_video(
                submitting, A, queue=PostgresQueue(submitting), priority=PRIORITY_NORMAL
            )
            thread = run_in_thread(
                lambda: api.post("/videos/delete", json={"video_ids": [B, A]}), out
            )
            wait_until_a_backend_is_blocked(db)
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert out["value"].status_code == 200
    for video_id in (A, B):
        assert count(db, "videos", video_id) == 0
        assert count(db, "deleted_videos", video_id) == 1
