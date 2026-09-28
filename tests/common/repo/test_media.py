"""Tests for the retention queries in common/repo/media.py (issue #36)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from common.repo.media import (
    LockOutcome,
    delete_row,
    list_expired,
    list_oldest_first,
    list_referenced_paths,
    lock_for_deletion,
    total_bytes,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 1, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def add(
    conn: psycopg.Connection,
    video_id: str,
    path: str,
    *,
    size: int = 10,
    created_at: datetime = T0,
    expires_at: datetime = NOW + timedelta(days=30),
) -> int:
    row = conn.execute(
        "INSERT INTO media (video_id, path, bytes, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (video_id, path, size, created_at, expires_at),
    ).fetchone()
    assert row is not None
    return int(row[0])


def new_video(conn: psycopg.Connection, vid: str) -> str:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s)", (vid,))
    return vid


def test_total_bytes_is_zero_for_an_empty_table(conn: psycopg.Connection) -> None:
    assert total_bytes(conn) == 0


def test_total_bytes_sums_all_rows(conn: psycopg.Connection) -> None:
    add(conn, new_video(conn, "v1"), "a", size=3)
    add(conn, new_video(conn, "v2"), "b", size=4)

    assert total_bytes(conn) == 7


def test_list_expired_includes_the_boundary_and_excludes_later_rows(
    conn: psycopg.Connection,
) -> None:
    exact = add(conn, new_video(conn, "v1"), "a", expires_at=NOW)
    add(conn, new_video(conn, "v2"), "b", expires_at=NOW + timedelta(microseconds=1))
    before = add(conn, new_video(conn, "v3"), "c", expires_at=NOW - timedelta(days=1))

    rows = list_expired(conn, NOW)

    assert sorted(r.id for r in rows) == sorted([exact, before])


def test_list_oldest_first_orders_by_created_at_then_id(
    conn: psycopg.Connection,
) -> None:
    newer = add(conn, new_video(conn, "v1"), "a", created_at=T0 + timedelta(days=1))
    tie_a = add(conn, new_video(conn, "v2"), "b", created_at=T0)
    tie_b = add(conn, new_video(conn, "v3"), "c", created_at=T0)

    assert [r.id for r in list_oldest_first(conn)] == [tie_a, tie_b, newer]


def test_list_referenced_paths_returns_every_path(conn: psycopg.Connection) -> None:
    add(conn, new_video(conn, "v1"), "ab/a.opus")
    add(conn, new_video(conn, "v2"), "cd/b.opus")

    assert list_referenced_paths(conn) == {"ab/a.opus", "cd/b.opus"}


def test_lock_for_deletion_expired_row_is_locked_and_returned(
    conn: psycopg.Connection,
) -> None:
    mid = add(conn, new_video(conn, "v1"), "a", size=5, expires_at=NOW)

    outcome, row = lock_for_deletion(conn, mid, expired_at=NOW)

    assert outcome is LockOutcome.LOCKED
    assert row is not None
    assert (row.id, row.video_id, row.path, row.bytes) == (mid, "v1", "a", 5)


def test_lock_for_deletion_rechecks_expiry(conn: psycopg.Connection) -> None:
    mid = add(conn, new_video(conn, "v1"), "a", expires_at=NOW + timedelta(seconds=1))

    outcome, row = lock_for_deletion(conn, mid, expired_at=NOW)

    assert outcome is LockOutcome.NOT_ELIGIBLE
    assert row is None


def test_lock_for_deletion_rechecks_the_cap(conn: psycopg.Connection) -> None:
    mid = add(conn, new_video(conn, "v1"), "a", size=10)

    assert lock_for_deletion(conn, mid, max_bytes=10)[0] is LockOutcome.NOT_ELIGIBLE
    assert lock_for_deletion(conn, mid, max_bytes=9)[0] is LockOutcome.LOCKED


def test_lock_for_deletion_reports_running_jobs(conn: psycopg.Connection) -> None:
    vid = new_video(conn, "v1")
    mid = add(conn, vid, "a", expires_at=NOW)
    conn.execute(
        "INSERT INTO jobs (video_id, kind, state) VALUES (%s, 'transcribe', 'running')",
        (vid,),
    )

    outcome, row = lock_for_deletion(conn, mid, expired_at=NOW)

    assert outcome is LockOutcome.RUNNING
    assert row is None


def test_lock_for_deletion_missing_row_is_unavailable(conn: psycopg.Connection) -> None:
    assert lock_for_deletion(conn, 999, expired_at=NOW) == (
        LockOutcome.UNAVAILABLE,
        None,
    )


def test_lock_for_deletion_skips_a_row_locked_elsewhere(
    conn: psycopg.Connection, head_dsn: str
) -> None:
    mid = add(conn, new_video(conn, "v1"), "a", expires_at=NOW)
    conn.commit()

    with psycopg.connect(head_dsn) as other:
        other.execute("SELECT id FROM media WHERE id = %s FOR UPDATE", (mid,))
        outcome, row = lock_for_deletion(conn, mid, expired_at=NOW)
        other.rollback()

    assert (outcome, row) == (LockOutcome.UNAVAILABLE, None)


def test_lock_for_deletion_requires_exactly_one_criterion(
    conn: psycopg.Connection,
) -> None:
    with pytest.raises(ValueError):
        lock_for_deletion(conn, 1)
    with pytest.raises(ValueError):
        lock_for_deletion(conn, 1, expired_at=NOW, max_bytes=1)


def test_delete_row_removes_only_that_row_and_does_not_commit(
    conn: psycopg.Connection, head_dsn: str
) -> None:
    keep = add(conn, new_video(conn, "v1"), "a")
    gone = add(conn, new_video(conn, "v2"), "b")
    conn.commit()

    delete_row(conn, gone)

    with psycopg.connect(head_dsn) as other:
        seen = {r[0] for r in other.execute("SELECT id FROM media").fetchall()}
    assert seen == {keep, gone}  # uncommitted
    conn.rollback()
    assert list_referenced_paths(conn) == {"a", "b"}
