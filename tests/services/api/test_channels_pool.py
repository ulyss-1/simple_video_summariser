"""``POST /channels`` holds no pooled connection across the resolve (#219).

No Postgres: a ``FakePool`` counts checkouts and the repo writes are stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Response

from adapters.youtube.channel_resolver import ResolvedChannel
from common.errors import (
    PermanentSourceError,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
)
from common.models import Channel
from common.repo.channels import RegisteredChannel
from services.api import deps, routes_write
from services.api.routes_write import SubmitRef, post_channels
from tests.services.api.conftest import FakePool, request_with
from tests.services.api.test_backfill_pool import _checked_out, _Conn

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"


@pytest.fixture(autouse=True)
def _stub_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = Channel(
        CHANNEL,
        None,
        True,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        None,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        0,
    )
    monkeypatch.setattr(
        routes_write, "register_channel", lambda c, ch: RegisteredChannel(channel, True)
    )
    monkeypatch.setattr(routes_write, "fill_channel_title", lambda c, ch, t: None)


def _post(pool: FakePool, url: str, resolve: Any) -> Any:
    provider = deps.get_conn_provider(request_with(pool))
    return post_channels(SubmitRef(url=url), Response(), provider, resolve)


def test_no_connection_is_checked_out_during_the_resolve() -> None:
    pool = FakePool([_Conn()])
    during: list[int] = []

    def resolve(kind: str, name: str) -> ResolvedChannel:
        during.append(_checked_out(pool))
        return ResolvedChannel(CHANNEL, "Title")

    result = _post(pool, "https://www.youtube.com/@handle", resolve)

    assert during == [0]
    assert result.resolved_from == "handle"
    assert pool.getconn_calls == 1
    assert _checked_out(pool) == 0


@pytest.mark.parametrize(
    "error",
    [
        PermanentSourceError(UnavailableReason.REMOVED, "x"),
        TransientNetworkError("x"),
        ToolFailureError("x"),
    ],
)
def test_a_failed_resolve_borrows_nothing(error: Exception) -> None:
    pool = FakePool([_Conn()])

    def resolve(kind: str, name: str) -> ResolvedChannel:
        raise error

    with pytest.raises(Exception) as info:
        _post(pool, "https://www.youtube.com/@handle", resolve)

    assert getattr(info.value, "status_code", None) in {404, 502, 503}
    assert pool.getconn_calls == 0


def test_canonical_input_borrows_once_and_never_resolves() -> None:
    pool = FakePool([_Conn()])

    def resolve(kind: str, name: str) -> ResolvedChannel:
        raise AssertionError("resolver reached")

    result = _post(pool, CHANNEL, resolve)

    assert result.resolved_from is None
    assert pool.getconn_calls == 1
    assert _checked_out(pool) == 0
