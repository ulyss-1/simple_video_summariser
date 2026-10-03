"""Tests for common/repo/jobs.py (issues #28, #39)."""

from __future__ import annotations

import psycopg
import pytest

from common.repo.jobs import latest_job, latest_job_state, queue_depth

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


def test_queue_depth_of_an_empty_jobs_table_is_empty(conn: psycopg.Connection) -> None:
    assert queue_depth(conn) == {}


def test_queue_depth_counts_each_kind_and_state_group(conn: psycopg.Connection) -> None:
    add_job(conn, "ingest", "done", key="a")
    add_job(conn, "ingest", "done", key="b")
    add_job(conn, "ingest", "done", key="c")
    add_job(conn, "ingest", "pending", key="d")
    add_job(conn, "transcribe", "running", key="e")
    add_job(conn, "transcribe", "dead", key="f")
    add_job(conn, "transcribe", "dead", key="g")
    add_job(conn, "analyze", "pending", key="h")
    add_job(conn, "notify", "pending", key="i")
    add_job(conn, "analyze", "paused", key="j")

    assert queue_depth(conn) == {
        ("ingest", "done"): 3,
        ("ingest", "pending"): 1,
        ("transcribe", "running"): 1,
        ("transcribe", "dead"): 2,
        ("analyze", "pending"): 1,
        ("notify", "pending"): 1,
        ("analyze", "paused"): 1,
    }


def test_queue_depth_runs_inside_a_read_only_transaction(conn: psycopg.Connection) -> None:
    add_job(conn, "ingest", "pending")
    conn.commit()
    conn.read_only = True

    assert queue_depth(conn) == {("ingest", "pending"): 1}


def test_latest_job_is_none_without_a_job_of_that_kind(conn: psycopg.Connection) -> None:
    add_job(conn, "analyze", "done")

    assert latest_job(conn, VID, "ingest") is None


def test_latest_job_returns_the_newest_jobs_id_and_state(conn: psycopg.Connection) -> None:
    add_job(conn, "ingest", "dead")
    newest = add_job(conn, "ingest", "pending")
    add_job(conn, "analyze", "running")

    assert latest_job(conn, VID, "ingest") == (newest, "pending")
