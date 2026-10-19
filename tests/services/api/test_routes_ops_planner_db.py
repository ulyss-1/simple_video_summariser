"""``GET /ops/planner`` end to end on Postgres (issues #98/#154)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.repo.planner_runs import record_finished
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

OK_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FAIL_AT = datetime(2026, 1, 2, 4, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(head_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    get_settings.cache_clear()
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def api(db: psycopg.Connection[Any], logs: LogSink) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def test_an_empty_registry_returns_no_items(api: TestClient) -> None:
    response = api.get("/ops/planner")

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_returns_each_task_ordered_by_name_with_its_last_run(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    record_finished(db, "reconcile", interval_sec=21600, at=OK_AT, outcome="ok", error=None, count=3)
    record_finished(
        db, "poll", interval_sec=3600, at=FAIL_AT, outcome="failed", error="boom", count=None
    )

    items = api.get("/ops/planner").json()["items"]

    assert [i["task_name"] for i in items] == ["poll", "reconcile"]  # ordered by name
    poll, reconcile = items
    assert poll["last_outcome"] == "failed"
    assert poll["last_error"] == "boom"
    assert poll["last_ok_at"] is None  # a failed run never sets last_ok_at
    assert reconcile["last_outcome"] == "ok"
    assert reconcile["last_count"] == 3
    assert reconcile["last_ok_at"] == "2026-01-02T03:04:05Z"


def test_the_metrics_gauge_exposes_last_success_only_for_tasks_that_succeeded(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    record_finished(db, "reconcile", interval_sec=21600, at=OK_AT, outcome="ok", error=None, count=3)
    record_finished(
        db, "poll", interval_sec=3600, at=FAIL_AT, outcome="failed", error="boom", count=None
    )

    body = api.get("/metrics").text
    name = "planner_task_last_success_timestamp_seconds"

    samples = [ln for ln in body.splitlines() if ln.startswith(name + "{")]
    assert samples == [f'{name}{{task="reconcile"}} {OK_AT.timestamp()!r}']
    # poll never succeeded: no sample, so time()-metric is absent (alert on that too).
    assert 'task="poll"' not in body
