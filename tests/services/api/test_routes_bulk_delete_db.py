"""``POST /videos/delete`` against a real database (issue #238).

The forced interleavings against handoff, ``DELETE /videos/{id}`` and ``POST /videos``
are in ``tests/common/test_cancel_races.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.repo import deleted_videos
from services.api import routes_write
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

A = "dQw4w9WgXcQ"
B = "abcdefghijk"
C = "ABCDEFGHIJK"
X = "-wNyEUrxzFU"  # never known
D = "zzzzzzzzzzz"  # already deleted


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


def seed(db: psycopg.Connection[Any], video_id: str, *states: str) -> None:
    db.execute("INSERT INTO channels (channel_id, title) VALUES ('UCkeep', 'Keep') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO videos (video_id, channel_id, title, origin) VALUES (%s, 'UCkeep', 'Title', 'adhoc')",
        (video_id,),
    )
    kinds = ("ingest", "transcribe")
    for kind, state in zip(kinds, states, strict=False):
        db.execute("INSERT INTO jobs (video_id, kind, state) VALUES (%s, %s, %s)", (video_id, kind, state))


def seed_derived(db: psycopg.Connection[Any], video_id: str) -> None:
    transcript = db.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text) VALUES (%s, 'whisper', '[]', 'x') RETURNING id",
        (video_id,),
    ).fetchone()
    assert transcript is not None
    db.execute(
        "INSERT INTO transcript_chunks (transcript_id, seq, start_sec, end_sec, text, chunk_strategy)"
        " VALUES (%s, 0, 0, 1, 'x', 'time:1:0')",
        (transcript[0],),
    )
    analysis = db.execute(
        "INSERT INTO analyses (video_id, transcript_id, chunk_strategy, model, prompt_version, tldr)"
        " VALUES (%s, %s, 'time:1:0', 'm', 'v1', 'x') RETURNING id",
        (video_id, transcript[0]),
    ).fetchone()
    assert analysis is not None
    db.execute("INSERT INTO topics (analysis_id, seq, title) VALUES (%s, 0, 't')", (analysis[0],))
    db.execute("INSERT INTO claims (analysis_id, text) VALUES (%s, 'c')", (analysis[0],))
    db.execute("INSERT INTO quotes (analysis_id, text) VALUES (%s, 'q')", (analysis[0],))


def seed_media(db: psycopg.Connection[Any], video_id: str, rel_path: str) -> None:
    db.execute(
        "INSERT INTO media (video_id, path, bytes, expires_at) VALUES (%s, %s, 5, now())",
        (video_id, rel_path),
    )


_PER_VIDEO = ("videos", "transcripts", "analyses", "media", "jobs")


def rows_of(db: psycopg.Connection[Any], video_id: str) -> dict[str, int]:
    counts = {
        table: int(db.execute(f"SELECT count(*) FROM {table} WHERE video_id = %s", (video_id,)).fetchone()[0])  # type: ignore[index]
        for table in _PER_VIDEO
    }
    counts["deleted_videos"] = int(
        db.execute("SELECT count(*) FROM deleted_videos WHERE video_id = %s", (video_id,)).fetchone()[0]  # type: ignore[index]
    )
    counts["search_docs"] = int(
        db.execute("SELECT count(*) FROM search_docs WHERE video_id = %s", (video_id,)).fetchone()[0]  # type: ignore[index]
    )
    return counts


def test_idle_videos_are_deleted_in_request_order_and_the_others_are_untouched(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    for video_id in (A, B, C):
        seed(db, video_id)
        seed_derived(db, video_id)
        seed_media(db, video_id, f"{video_id}/audio.opus")
    before_b = rows_of(db, B)

    response = api.post("/videos/delete", json={"video_ids": [C, A]})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"video_id": C, "outcome": "deleted"},
            {"video_id": A, "outcome": "deleted"},
        ],
        "counts": {"deleted": 2, "not_found": 0},
    }
    for video_id in (A, C):
        assert rows_of(db, video_id) == {
            "videos": 0, "transcripts": 0, "analyses": 0, "media": 0, "jobs": 0,
            "deleted_videos": 1, "search_docs": 0,
        }
    for table in ("transcript_chunks", "topics", "claims", "quotes"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,), table  # only B's
    assert rows_of(db, B) == before_b
    assert before_b["videos"] == 1


def test_a_mixed_request_reports_not_found_for_unknown_and_deleted_and_writes_no_tombstone_for_them(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A)
    db.execute("INSERT INTO deleted_videos (video_id, deleted_at) VALUES (%s, '2020-01-01')", (D,))
    tombstone_before = db.execute("SELECT * FROM deleted_videos WHERE video_id = %s", (D,)).fetchone()

    response = api.post("/videos/delete", json={"video_ids": [A, X, D]})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"video_id": A, "outcome": "deleted"},
            {"video_id": X, "outcome": "not_found"},
            {"video_id": D, "outcome": "not_found"},
        ],
        "counts": {"deleted": 1, "not_found": 2},
    }
    assert db.execute("SELECT count(*) FROM deleted_videos WHERE video_id = %s", (X,)).fetchone() == (0,)
    assert db.execute("SELECT * FROM deleted_videos WHERE video_id = %s", (D,)).fetchone() == tombstone_before


def test_a_running_job_is_kept_with_its_request_a_pending_one_goes_and_each_cancelled_job_is_logged(
    api: TestClient, db: psycopg.Connection[Any], logs: LogSink
) -> None:
    seed(db, A, "running", "pending")
    seed(db, B)
    seed(db, C, "pending")

    response = api.post("/videos/delete", json={"video_ids": [A, B, C]})

    assert response.status_code == 200
    assert response.json()["counts"] == {"deleted": 3, "not_found": 0}
    assert db.execute(
        "SELECT video_id, kind, state, cancel_requested_at IS NOT NULL FROM jobs ORDER BY id"
    ).fetchall() == [(A, "ingest", "running", True)]
    # The pending jobs (A transcribe, C ingest) ended CANCELLED at once and are logged; A running one is not.
    cancelled = [r for r in logs.records() if r.get("event") == "job.cancelled"]
    assert [(r["video_id"], r["kind"], r["level"]) for r in cancelled] == [
        (A, "transcribe", "info"),
        (C, "ingest", "info"),
    ]


def test_a_database_failure_before_commit_is_the_write_503_and_changes_nothing(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db, A, "running", "pending")
    seed(db, B, "pending")
    seed_derived(db, A)
    snapshot = (
        db.execute("SELECT * FROM videos ORDER BY video_id").fetchall(),
        db.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
        rows_of(db, A),
        rows_of(db, B),
    )
    real = deleted_videos.delete_videos

    def failing(conn: Any, ids: Any) -> Any:
        def forced() -> None:
            raise psycopg.OperationalError("forced")

        return real(conn, ids, before_commit=forced)

    monkeypatch.setattr(routes_write, "delete_videos", failing)

    response = api.post("/videos/delete", json={"video_ids": [A, B]})

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert (
        db.execute("SELECT * FROM videos ORDER BY video_id").fetchall(),
        db.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
        rows_of(db, A),
        rows_of(db, B),
    ) == snapshot
    assert db.execute("SELECT count(*) FROM deleted_videos").fetchone() == (0,)


def test_media_files_of_every_deleted_video_are_unlinked(
    api: TestClient, db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    files = []
    for video_id in (A, B):
        seed(db, video_id)
        seed_media(db, video_id, f"{video_id}.opus")
        path = tmp_path / f"{video_id}.opus"
        path.write_bytes(b"audio")
        files.append(path)
    seed(db, C)
    kept = tmp_path / f"{C}.opus"
    kept.write_bytes(b"audio")
    seed_media(db, C, f"{C}.opus")

    response = api.post("/videos/delete", json={"video_ids": [A, B]})

    assert response.status_code == 200
    assert not any(path.exists() for path in files)
    assert kept.exists()


def test_a_path_outside_the_audio_dir_is_not_unlinked_and_is_logged(
    api: TestClient, db: psycopg.Connection[Any], tmp_path: Path, logs: LogSink
) -> None:
    seed(db, A)
    seed_media(db, A, "../outside.opus")
    outside = tmp_path.parent / "outside.opus"
    outside.write_bytes(b"keep")

    response = api.post("/videos/delete", json={"video_ids": [A]})

    assert response.status_code == 200
    assert outside.read_bytes() == b"keep"
    [line] = [r for r in logs.records() if r.get("event") == "video.delete.unlink_failed"]
    assert (line["video_id"], line["error_class"]) == (A, "UnsafeMediaPath")


def test_an_unlink_error_is_logged_and_the_answer_stays_200(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> None:
    seed(db, A)
    seed_media(db, A, "safe/audio.opus")

    class Failing:
        def unlink(self, *, missing_ok: bool) -> None:
            raise PermissionError("forced")

    monkeypatch.setattr(routes_write, "resolve_media_path", lambda *_args: Failing())

    response = api.post("/videos/delete", json={"video_ids": [A]})

    assert response.status_code == 200
    assert response.json()["counts"] == {"deleted": 1, "not_found": 0}
    [line] = [r for r in logs.records() if r.get("event") == "video.delete.unlink_failed"]
    assert (line["video_id"], line["error_class"]) == (A, "PermissionError")
    assert db.execute("SELECT count(*) FROM media").fetchone() == (0,)


def test_deleted_videos_vanish_from_get_search_and_ops_except_a_kept_running_row(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A, "running")
    seed(db, B, "pending")
    seed(db, C)

    assert api.post("/videos/delete", json={"video_ids": [A, B]}).status_code == 200

    for video_id in (A, B):
        assert api.get(f"/videos/{video_id}").status_code == 404
    assert {i["video_id"] for i in api.get("/videos").json()["items"]} == {C}
    found = {r["video_id"] for r in api.get("/search", params={"q": "Title"}).json()["results"]}
    assert found <= {C}
    ops = [(i["video_id"], i["state"]) for i in api.get("/ops/jobs").json()["items"]]
    assert ops == [(A, "running")]  # #303 shows the kept running row in Ops


def test_single_delete_still_shares_the_implementation(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed(db, A, "running", "pending")

    assert api.delete(f"/videos/{A}").status_code == 204
    assert api.delete(f"/videos/{A}").status_code == 404
    assert db.execute("SELECT kind, state FROM jobs").fetchall() == [("ingest", "running")]
