"""``GET /videos/{id}/analysis-estimate`` end to end on Postgres (issue #153)."""

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

VID = "dQw4w9WgXcQ"
OLD = "aaaaaaaaaaa"
HAIKU = "claude-haiku-4-5"


@pytest.fixture
def db(head_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("SUMMARIZER", "anthropic")
    get_settings.cache_clear()
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def api(db: psycopg.Connection[Any], logs: LogSink) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def test_the_estimate_is_the_transcript_length_times_the_recorded_median(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    # Two recorded analyses of 1,000 chars at $0.002 and $0.004: median per char 3e-6.
    for i, (vid, cost) in enumerate([(OLD, "0.002"), ("bbbbbbbbbbb", "0.004")]):
        seed.video(db, vid)
        t = seed.transcript(db, vid, segments=[{"start": 0, "end": 1, "text": "x" * 1000}])
        seed.analysis(db, vid, t, created=i, model=HAIKU, cost_usd=cost)
    seed.video(db, VID)
    seed.transcript(db, VID, segments=[{"start": 0, "end": 1, "text": "y" * 10_000}])
    before = db.execute("SELECT count(*) FROM analyses").fetchone()

    response = api.get(f"/videos/{VID}/analysis-estimate")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript_chars"] == 10_000 and body["batch"] is True
    [entry] = body["estimates"]
    assert entry["state"] == "estimated" and entry["samples"] == 2
    assert entry["cost_usd"] == pytest.approx(0.03)
    assert db.execute("SELECT count(*) FROM analyses").fetchone() == before


def test_an_unknown_video_is_404(api: TestClient) -> None:
    assert api.get(f"/videos/{VID}/analysis-estimate").status_code == 404
