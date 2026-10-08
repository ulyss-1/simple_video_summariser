"""``GET /videos/{id}/render`` end to end on Postgres (issue #45)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from services.api.main import create_app
from tests.common.repo import read_seed as seed
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

CH = "UC" + "c" * 22
VID = "-wNyEUrxzFU"


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


def test_unknown_video_and_video_without_analysis_are_404(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    assert api.get(f"/videos/{VID}/render").json() == {"detail": "video not found"}

    seed.channel(db, CH, "Chan")
    seed.video(db, VID, channel_id=CH, title="T")

    assert api.get(f"/videos/{VID}/render").json() == {"detail": "no analysis yet"}


def test_renders_the_newer_analysis_with_tied_claims_in_id_order(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    seed.channel(db, CH, "The Chan")
    seed.video(db, VID, channel_id=CH, title="Real title", duration_sec=90)
    t = seed.transcript(db, VID)
    seed.analysis(
        db, VID, t, created=1, tldr="OLD TLDR",
        claims=[{"text": "old claim", "start_sec": 1.0}],
    )  # fmt: skip
    seed.analysis(
        db, VID, t, created=2, tldr="NEW TLDR",
        claims=[
            {"text": "tie-first", "start_sec": 5.0},
            {"text": "tie-second", "start_sec": 5.0},
            {"text": "tie-third", "start_sec": 5.0},
            {"text": "no-time"},
            {"text": "earlier", "start_sec": 1.0},
        ],
    )  # fmt: skip

    response = api.get(f"/videos/{VID}/render")

    assert response.status_code == 200
    body = response.text
    assert "NEW TLDR" in body and "OLD TLDR" not in body and "old claim" not in body
    assert "The Chan" in body and "Real title" in body and "1:30" in body
    order = [body.index(x) for x in ("earlier", "tie-first", "tie-second", "tie-third", "no-time")]
    assert order == sorted(order)
