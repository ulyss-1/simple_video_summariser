"""A backfill dry run does not starve the API pool (#204), on real Postgres.

``DB_POOL_MAX_SIZE=1``: the dry run's long phase blocks on an ``Event`` the
test controls, and while it is parked other requests must still get the one
pooled connection.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.models import CatalogEntry, ChannelCatalog
from services.api import deps, routes_write
from services.api.backfill import SubtitleProbe
from services.api.main import create_app
from tests.services.api.conftest import LogSink

pytestmark = pytest.mark.integration

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
ENTRIES = (CatalogEntry("pd0000000001", "t", 100),)
DEADLINE = 15.0


class _Gate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def park(self) -> None:
        self.entered.set()
        assert self.release.wait(DEADLINE), "test never released the gate"


class _GatedCatalog:
    def __init__(self, gate: _Gate | None) -> None:
        self.gate = gate

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        if self.gate is not None:
            self.gate.park()
        return ChannelCatalog(channel_id, ENTRIES, None)


class _GatedMeta:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def fetch(self, video_id: str) -> str:
        self.gate.park()
        return video_id


@pytest.fixture
def make_client(
    head_dsn: str, monkeypatch: pytest.MonkeyPatch, logs: LogSink
) -> Iterator[Callable[[_Gate, str], TestClient]]:
    monkeypatch.setenv("DATABASE_URL", head_dsn)
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SEC", "2")
    get_settings.cache_clear()
    with psycopg.connect(head_dsn, autocommit=True) as admin:
        admin.execute("INSERT INTO channels (channel_id) VALUES (%s)", (CHANNEL,))
    clients: list[TestClient] = []

    def make(gate: _Gate, phase: str) -> TestClient:
        app = create_app()
        app.dependency_overrides[deps.get_catalog] = lambda: _GatedCatalog(
            gate if phase == "list" else None
        )
        probe = SubtitleProbe(
            metadata_factory=lambda timeout: _GatedMeta(gate),  # type: ignore[arg-type,return-value]
            has_manual=lambda meta: False,
        )
        app.dependency_overrides[routes_write.get_subtitle_probe] = lambda: probe
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        client.app.state.pool.wait(timeout=10)  # type: ignore[attr-defined]
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("phase", ["list", "probe"])
def test_a_parked_dry_run_leaves_the_single_pooled_connection_free(
    make_client: Callable[[_Gate, str], TestClient], phase: str
) -> None:
    gate = _Gate()
    client = make_client(gate, phase)
    outcome: dict[str, Any] = {}

    def dry_run() -> None:
        outcome["response"] = client.post(
            f"/channels/{CHANNEL}/backfill", json={"limit": 5, "dry_run": True}
        )

    thread = threading.Thread(target=dry_run)
    thread.start()
    try:
        assert gate.entered.wait(DEADLINE), "the dry run never reached its long phase"
        health = client.get("/healthz")
        read = client.get("/ops/jobs")
    finally:
        gate.release.set()
        thread.join(DEADLINE)

    assert health.status_code == 200
    assert read.status_code == 200
    assert not thread.is_alive()
    assert outcome["response"].status_code == 200
    assert outcome["response"].json()["new_videos"] == 1
