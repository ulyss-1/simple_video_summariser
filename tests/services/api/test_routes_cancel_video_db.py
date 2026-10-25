"""``POST /videos/{id}/cancel`` and what reads and deletes make of a cancel (issue #237)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

VID = "dQw4w9WgXcQ"
OTHER = "abcdefghijk"
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


def seed_video(db: psycopg.Connection[Any], video_id: str = VID) -> None:
    db.execute(
        "INSERT INTO channels (channel_id, title) VALUES ('UCkeep', 'Keep') ON CONFLICT DO NOTHING"
    )
    db.execute(
        "INSERT INTO videos (video_id, channel_id, title, origin) VALUES (%s, 'UCkeep', 'T', 'adhoc')",
        (video_id,),
    )


def add_job(
    db: psycopg.Connection[Any],
    state: str,
    *,
    kind: str = "ingest",
    key: str = "default",
    requested: bool = False,
    video_id: str = VID,
    error_class: str | None = None,
) -> int:
    row = db.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state, locked_by, attempts, error_class,"
        " cancel_requested_at, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END,"
        " CASE WHEN %s IN ('done', 'dead') THEN now() END) RETURNING id",
        (
            video_id,
            kind,
            key,
            state,
            "w1" if state == "running" else None,
            1 if state != "pending" else 0,
            error_class,
            requested,
            state,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def job_row(db: psycopg.Connection[Any], job_id: int) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT state, error_class, last_error, locked_by, attempts, cancel_requested_at"
        " FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------


def test_malformed_and_percent_encoded_ids_get_the_fixed_422_without_echo(
    api: TestClient,
) -> None:
    for path in ("short", "%64Qw4w9WgXcQ"):
        response = api.post(f"/videos/{path}/cancel", json={})
        assert response.status_code == 422
        assert response.json() == {
            "detail": "video id must be 11 characters of A-Z, a-z, 0-9, '_' or '-'"
        }
        assert path not in response.text


def test_a_non_json_request_gets_415_and_changes_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job_id = add_job(db, "pending")

    response = api.post(
        f"/videos/{VID}/cancel", content=b"{}", headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 415
    assert job_row(db, job_id)[0] == "pending"


@pytest.mark.parametrize(
    "body", [{"x": 1}, [], "yes"], ids=["extra-field", "list", "string"]
)
def test_the_body_must_be_an_empty_object(
    api: TestClient, db: psycopg.Connection[Any], body: Any
) -> None:
    seed_video(db)
    job_id = add_job(db, "pending")

    response = api.post(f"/videos/{VID}/cancel", json=body)

    assert response.status_code == 422
    assert job_row(db, job_id)[0] == "pending"


def test_an_unknown_or_deleted_video_is_a_fixed_404_and_nothing_is_written(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    missing = api.post(f"/videos/{VID}/cancel", json={})
    assert missing.status_code == 404
    assert missing.json() == {"detail": "video not found"}

    seed_video(db)
    assert api.delete(f"/videos/{VID}").status_code == 204
    tombstoned = api.post(f"/videos/{VID}/cancel", json={})
    assert tombstoned.status_code == 404
    assert tombstoned.json() == {"detail": "video not found"}


# ---------------------------------------------------------------------------
# what the request does
# ---------------------------------------------------------------------------


def test_pending_jobs_end_at_once_and_the_answer_is_200(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    first = add_job(db, "pending")
    second = add_job(db, "pending", kind="analyze", key="v1:fake")

    response = api.post(f"/videos/{VID}/cancel", json={})

    assert response.status_code == 200
    assert response.json() == {
        "video_id": VID,
        "jobs": [
            {"job_id": first, "kind": "ingest", "outcome": "cancelled"},
            {"job_id": second, "kind": "analyze", "outcome": "cancelled"},
        ],
    }
    for job_id in (first, second):
        state, error_class, last_error, locked_by, attempts, requested = job_row(
            db, job_id
        )
        assert (state, error_class, last_error, locked_by, attempts) == (
            "dead",
            "CANCELLED",
            "cancelled on request",
            None,
            0,
        )
        assert requested is None


def test_a_running_job_is_asked_not_ended_and_the_answer_is_202_every_time(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    running = add_job(db, "running")
    pending = add_job(db, "pending", kind="analyze", key="v1:fake")
    done = add_job(db, "done", kind="transcribe")
    dead = add_job(db, "dead", kind="transcribe", key="x", error_class="BUG")
    untouched = (job_row(db, done), job_row(db, dead))

    first = api.post(f"/videos/{VID}/cancel", json={})
    stamp = job_row(db, running)[5]
    second = api.post(f"/videos/{VID}/cancel", json={})

    assert first.status_code == 202
    assert first.json()["jobs"] == [
        {"job_id": running, "kind": "ingest", "outcome": "cancel_requested"},
        {"job_id": pending, "kind": "analyze", "outcome": "cancelled"},
    ]
    assert second.status_code == 202
    assert second.json()["jobs"] == [
        {"job_id": running, "kind": "ingest", "outcome": "cancel_requested"}
    ]
    assert stamp is not None
    assert job_row(db, running)[5] == stamp
    assert job_row(db, running)[0] == "running"
    assert (job_row(db, done), job_row(db, dead)) == untouched


def test_a_video_with_nothing_active_is_200_with_no_jobs(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "done")

    response = api.post(f"/videos/{VID}/cancel", json={})

    assert response.status_code == 200
    assert response.json() == {"video_id": VID, "jobs": []}


def test_another_videos_jobs_are_left_alone(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    seed_video(db, OTHER)
    add_job(db, "pending")
    other = add_job(db, "pending", video_id=OTHER)

    api.post(f"/videos/{VID}/cancel", json={})

    assert job_row(db, other)[0] == "pending"


def test_a_cancel_does_not_touch_the_video_or_tombstone_it(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "pending")

    api.post(f"/videos/{VID}/cancel", json={})

    assert db.execute(
        "SELECT count(*) FROM videos WHERE video_id = %s", (VID,)
    ).fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM deleted_videos").fetchone() == (0,)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_the_active_job_and_the_ops_item_expose_the_request(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    running = add_job(db, "running")
    before = api.get(f"/videos/{VID}").json()["active_job"]
    assert before["cancel_requested_at"] is None

    api.post(f"/videos/{VID}/cancel", json={})

    detail = api.get(f"/videos/{VID}").json()
    assert detail["active_job"]["id"] == running
    assert detail["active_job"]["cancel_requested_at"] is not None
    items = api.get("/ops/jobs", params={"state": "running"}).json()["items"]
    assert [i["cancel_requested_at"] is not None for i in items] == [True]


def test_a_cancelled_video_reads_idle_not_failed_and_has_no_last_failure(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "pending")

    api.post(f"/videos/{VID}/cancel", json={})

    detail = api.get(f"/videos/{VID}").json()
    assert detail["status"] == "idle"
    assert detail["last_failure"] is None
    assert detail["active_job"] is None
    assert [
        v["video_id"]
        for v in api.get("/videos", params={"status": "idle"}).json()["items"]
    ] == [VID]
    assert api.get("/videos", params={"status": "failed"}).json()["items"] == []


def test_a_genuinely_failed_video_still_reads_failed(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "dead", error_class="BUG")

    detail = api.get(f"/videos/{VID}").json()

    assert detail["status"] == "failed"
    assert detail["last_failure"]["error_class"] == "BUG"


def test_a_cancelled_video_with_an_analysis_still_reads_done(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    transcript = db.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text) VALUES (%s, 'whisper', '[]', 'x')"
        " RETURNING id",
        (VID,),
    ).fetchone()
    assert transcript is not None
    db.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version, tldr)"
        " VALUES (%s, %s, 'time:1:0', 'm', 'v1', 'x')",
        (VID, transcript[0]),
    )
    add_job(db, "pending", kind="analyze", key="v2:fake")

    api.post(f"/videos/{VID}/cancel", json={})

    assert api.get(f"/videos/{VID}").json()["status"] == "done"


def test_the_ops_view_accepts_the_cancelled_class_and_shows_the_job(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job_id = add_job(db, "pending")
    api.post(f"/videos/{VID}/cancel", json={})

    response = api.get("/ops/jobs", params={"error_class": "CANCELLED"})

    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == [job_id]


def test_ops_retry_resumes_a_cancelled_job_and_clears_the_request(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    job_id = add_job(db, "running", requested=True, error_class=None)
    db.execute(
        "UPDATE jobs SET state = 'dead', error_class = 'CANCELLED', locked_by = NULL, finished_at = now()"
        " WHERE id = %s",
        (job_id,),
    )

    response = api.post(f"/ops/jobs/{job_id}/retry", json={}, headers=JSON)

    assert response.status_code == 200
    assert response.json()["state"] == "pending"
    assert response.json()["cancel_requested_at"] is None
    assert job_row(db, job_id)[5] is None


# ---------------------------------------------------------------------------
# DELETE after a cancel
# ---------------------------------------------------------------------------


def test_delete_says_cancellation_in_progress_while_every_running_job_has_a_request(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "running")
    add_job(db, "running", kind="transcribe", key="a")
    api.post(f"/videos/{VID}/cancel", json={})

    response = api.delete(f"/videos/{VID}")

    assert response.status_code == 409
    assert response.json() == {"detail": "cancellation in progress"}


def test_delete_keeps_the_processing_message_while_a_running_job_has_no_request(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "running", requested=True)
    add_job(db, "running", kind="transcribe", key="a")

    response = api.delete(f"/videos/{VID}")

    assert response.status_code == 409
    assert response.json() == {"detail": "video is currently processing"}


def test_delete_succeeds_once_the_cancelled_job_has_stopped(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    running = add_job(db, "running")
    api.post(f"/videos/{VID}/cancel", json={})
    assert api.delete(f"/videos/{VID}").status_code == 409
    db.execute(
        "UPDATE jobs SET state = 'dead', error_class = 'CANCELLED', locked_by = NULL WHERE id = %s",
        (running,),
    )

    assert api.delete(f"/videos/{VID}").status_code == 204


def test_delete_after_a_cancel_of_only_pending_jobs_is_204(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed_video(db)
    add_job(db, "pending")
    assert api.post(f"/videos/{VID}/cancel", json={}).status_code == 200

    assert api.delete(f"/videos/{VID}").status_code == 204
