"""Tests for common/repo/jobs.py (issue #28)."""

from __future__ import annotations

import psycopg
import pytest

from common.repo.jobs import latest_job_state

pytestmark = pytest.mark.integration

VID = "abc12345678"


def add_job(conn: psycopg.Connection, kind: str, state: str, *, key: str = "default") -> int:
    row = conn.execute(
        "INSERT INTO jobs (video_id, kind, dedupe_key, state) VALUES (%s, %s, %s, %s) RETURNING id",
        (VID, kind, key, state),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_latest_job_state_is_none_when_the_video_has_no_job_of_that_kind(
    conn: psycopg.Connection,
) -> None:
    add_job(conn, "analyze", "done")

    assert latest_job_state(conn, VID, "transcribe") is None
    assert latest_job_state(conn, "zzzzzzzzzzz", "analyze") is None


@pytest.mark.parametrize("state", ["pending", "running", "done", "dead"])
def test_latest_job_state_reports_each_state(conn: psycopg.Connection, state: str) -> None:
    add_job(conn, "transcribe", state)

    assert latest_job_state(conn, VID, "transcribe") == state


def test_a_pending_job_created_after_a_dead_one_is_the_latest(conn: psycopg.Connection) -> None:
    add_job(conn, "transcribe", "dead", key="a")
    add_job(conn, "transcribe", "pending", key="b")

    assert latest_job_state(conn, VID, "transcribe") == "pending"


def test_a_dead_job_created_after_a_done_one_is_the_latest(conn: psycopg.Connection) -> None:
    add_job(conn, "transcribe", "done", key="a")
    add_job(conn, "transcribe", "dead", key="b")

    assert latest_job_state(conn, VID, "transcribe") == "dead"


def test_ties_on_created_at_are_broken_by_id(conn: psycopg.Connection) -> None:
    # Both rows are inserted in this one transaction, so now() is identical.
    add_job(conn, "transcribe", "pending", key="a")
    add_job(conn, "transcribe", "dead", key="b")
    distinct = conn.execute("SELECT count(DISTINCT created_at) FROM jobs").fetchone()
    assert distinct == (1,)

    assert latest_job_state(conn, VID, "transcribe") == "dead"


def test_only_the_asked_kind_counts(conn: psycopg.Connection) -> None:
    add_job(conn, "transcribe", "dead")
    add_job(conn, "analyze", "pending")

    assert latest_job_state(conn, VID, "transcribe") == "dead"
    assert latest_job_state(conn, VID, "analyze") == "pending"
