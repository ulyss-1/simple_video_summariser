"""``GET /videos/{id}/analysis-estimate`` without Postgres (issue #153).

The repo reads are replaced with fakes; the numbers are made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from common.config import get_settings
from services.api import deps, routes_read, schemas

VID = "dQw4w9WgXcQ"
HAIKU = "claude-haiku-4-5"


class Fake:
    def __init__(self) -> None:
        self.chars: int | None = 10_000
        self.models: list[str] = []
        self.medians: dict[str, tuple[float | None, int]] = {}
        self.exists = True
        self.median_calls: list[str] = []


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake()
    monkeypatch.setattr(routes_read, "video_exists", lambda conn, vid: f.exists)
    monkeypatch.setattr(
        routes_read,
        "analysis_transcript_chars",
        lambda conn, ids: {} if f.chars is None else {i: f.chars for i in ids},
    )
    monkeypatch.setattr(routes_read, "analysis_models", lambda conn, vid: f.models)

    def median(conn: Any, model: str) -> tuple[float | None, int]:
        f.median_calls.append(model)
        return f.medians.get(model, (None, 0))

    monkeypatch.setattr(routes_read, "median_cost_per_char", median)
    return f


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    app.dependency_overrides[deps.get_conn] = lambda: object()
    app.dependency_overrides[deps.get_read_conn] = lambda: object()
    with TestClient(app) as client:
        yield client


def _get(api: TestClient) -> dict[str, Any]:
    response = api.get(f"/videos/{VID}/analysis-estimate")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_estimated_is_chars_times_the_median_and_batch_is_not_applied_again(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic", ANTHROPIC_BATCH="true")
    fake.medians[HAIKU] = (2e-6, 7)

    body = _get(api)

    assert body["batch"] is True
    assert body["summarizer"] == "anthropic"
    assert body["transcript_chars"] == 10_000
    [entry] = body["estimates"]
    assert entry["model"] == HAIKU and entry["state"] == "estimated"
    assert entry["cost_usd"] == pytest.approx(0.02)  # not 0.01: the median has the 0.5 in it
    assert entry["reason"] is None and entry["samples"] == 7


def test_batch_off_is_reported(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic", ANTHROPIC_BATCH="false")
    fake.medians[HAIKU] = (2e-6, 1)

    body = _get(api)

    assert body["batch"] is False
    assert body["estimates"][0]["cost_usd"] == pytest.approx(0.02)


@pytest.mark.parametrize("summarizer", ["ollama", "free"])
def test_a_local_or_free_summarizer_is_free_and_runs_no_cost_query(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch, summarizer: str
) -> None:
    _settings(monkeypatch, SUMMARIZER=summarizer)
    fake.models = ["other"]

    body = _get(api)

    [entry] = body["estimates"]
    assert entry["state"] == "free"
    assert entry["cost_usd"] is None and entry["reason"] is None
    assert body["summarizer"] == summarizer
    assert fake.median_calls == []


def test_no_cost_samples_is_unknown(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic")

    [entry] = _get(api)["estimates"]

    assert entry["state"] == "unknown" and entry["reason"] == "no_cost_samples"
    assert entry["cost_usd"] is None


def test_no_transcript_is_unknown_and_chars_are_null(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic")
    fake.chars = None
    fake.medians[HAIKU] = (2e-6, 3)

    body = _get(api)

    assert body["transcript_chars"] is None
    assert body["estimates"][0]["reason"] == "no_transcript"


def test_an_unpriced_model_is_no_price_even_without_a_transcript(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic", ANTHROPIC_MODEL="claude-future-9")
    fake.chars = None

    [entry] = _get(api)["estimates"]

    assert entry["state"] == "unknown" and entry["reason"] == "no_price"
    assert "claude-future-9" not in fake.median_calls


def test_other_analysed_models_follow_the_configured_one_by_name_capped_at_five(
    api: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, SUMMARIZER="anthropic")
    fake.models = [f"z-model-{i}" for i in range(8)] + [HAIKU, "a-model"]
    fake.medians[HAIKU] = (1e-6, 2)

    names = [e["model"] for e in _get(api)["estimates"]]

    assert names == [HAIKU, "a-model", "z-model-0", "z-model-1", "z-model-2"]


@pytest.mark.parametrize(
    "entry",
    [
        {"state": "estimated", "cost_usd": 0.0, "reason": None, "samples": 1},
        {"state": "unknown", "cost_usd": None, "reason": "no_transcript", "samples": 0},
        {"state": "unknown", "cost_usd": None, "reason": "no_cost_samples", "samples": 0},
        {"state": "unknown", "cost_usd": None, "reason": "no_price", "samples": 0},
        {"state": "free", "cost_usd": None, "reason": None, "samples": 0},
    ],
    ids=["estimated", "no_transcript", "no_cost_samples", "no_price", "free"],
)
def test_the_encoding_invariants_hold_for_each_combination(entry: dict[str, Any]) -> None:
    model = schemas.ModelEstimate.model_validate({"model": "m", **entry})

    assert (model.cost_usd is not None) == (model.state == "estimated")
    assert (model.reason is not None) == (model.state == "unknown")


def test_an_unknown_video_is_404(api: TestClient, fake: Fake) -> None:
    fake.exists = False

    assert api.get(f"/videos/{VID}/analysis-estimate").status_code == 404


def test_a_malformed_id_is_422_before_the_database(app: FastAPI) -> None:
    def no_db() -> Iterator[Any]:
        raise AssertionError("the database must not be touched")
        yield  # pragma: no cover

    app.dependency_overrides[deps.get_conn] = no_db
    with TestClient(app) as client:
        assert client.get("/videos/bad/analysis-estimate").status_code == 422


@pytest.mark.parametrize(
    "entry",
    [
        {"state": "estimated", "cost_usd": None, "reason": None, "samples": 1},
        {"state": "estimated", "cost_usd": -0.01, "reason": None, "samples": 1},
        {"state": "estimated", "cost_usd": 0.1, "reason": "no_price", "samples": 1},
        {"state": "unknown", "cost_usd": 0.1, "reason": "no_price", "samples": 0},
        {"state": "unknown", "cost_usd": None, "reason": None, "samples": 0},
        {"state": "free", "cost_usd": 0.0, "reason": None, "samples": 0},
        {"state": "free", "cost_usd": None, "reason": "no_price", "samples": 0},
    ],
    ids=[
        "estimated-no-number",
        "estimated-negative",
        "estimated-with-reason",
        "unknown-with-number",
        "unknown-no-reason",
        "free-with-number",
        "free-with-reason",
    ],
)
def test_a_combination_that_breaks_the_encoding_is_rejected(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        schemas.ModelEstimate.model_validate({"model": "m", **entry})
