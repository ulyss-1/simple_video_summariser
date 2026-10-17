"""``backfill`` returns its pooled connection across the long phases (#204).

No Postgres: a ``FakePool`` counts checkouts and the repo reads are stubbed.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
from psycopg.pq import TransactionStatus

from common import backfill as common_backfill
from common.models import CatalogEntry, ChannelCatalog
from common.queue import PostgresQueue
from services.api import deps
from services.api.backfill import SubtitleProbe, backfill
from tests.services.api.conftest import FakePool, request_with

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
ENTRIES = tuple(CatalogEntry(f"pl{i:09d}", f"t{i}", 100) for i in range(2))


class _Info:
    transaction_status = TransactionStatus.IDLE


class _Conn:
    info = _Info()

    def transaction(self) -> Any:
        return nullcontext()


def _checked_out(pool: FakePool) -> int:
    return pool.getconn_calls - len(pool.returned)


@pytest.fixture(autouse=True)
def _stub_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common_backfill, "channel_exists", lambda c, ch: True)
    monkeypatch.setattr(common_backfill, "known_video_ids", lambda c, ids: set())
    monkeypatch.setattr(common_backfill, "median_whisper_rtf", lambda c: (None, 0))
    monkeypatch.setattr(common_backfill, "median_chars_per_audio_sec", lambda c: (None, 0))


def test_no_connection_is_checked_out_while_listing_and_probing() -> None:
    pool = FakePool([_Conn(), _Conn()])
    during: dict[str, int] = {}

    class Catalog:
        def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
            during["list"] = _checked_out(pool)
            return ChannelCatalog(channel_id, ENTRIES, None)

    class Meta:
        def fetch(self, video_id: str) -> str:
            during["probe"] = _checked_out(pool)
            return video_id

    probe = SubtitleProbe(lambda timeout: Meta(), lambda meta: False)  # type: ignore[arg-type,return-value]
    provider = deps.get_conn_provider(request_with(pool))

    result = backfill(provider, PostgresQueue, Catalog(), CHANNEL, limit=5, dry_run=True, probe=probe)

    assert result.new_videos == 2
    assert during == {"list": 0, "probe": 0}
    assert pool.getconn_calls == 2  # existence check, then the reads: re-borrowed
    assert _checked_out(pool) == 0
    assert len(set(map(id, pool.returned))) == 2


def test_the_connection_is_returned_when_the_listing_fails() -> None:
    pool = FakePool([_Conn()])

    class Catalog:
        def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
            raise RuntimeError("boom")

    provider = deps.get_conn_provider(request_with(pool))
    with pytest.raises(RuntimeError, match="boom"):
        backfill(provider, PostgresQueue, Catalog(), CHANNEL, limit=5, dry_run=True)

    assert _checked_out(pool) == 0


def test_a_plain_connection_is_used_for_every_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn()
    seen: list[Any] = []

    def spy(c: Any, channel_id: str) -> bool:
        seen.append(c)
        return True

    monkeypatch.setattr(common_backfill, "channel_exists", spy)

    class Catalog:
        def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
            return ChannelCatalog(channel_id, ENTRIES, None)

    backfill(conn, object(), Catalog(), CHANNEL, limit=5, dry_run=True)  # type: ignore[arg-type]

    assert seen == [conn]
