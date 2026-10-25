"""The ``channel_backfill`` wiring, pinned without Postgres (issue #217).

``test_backfill_wiring.py`` asserts the same values but is integration-marked, so
the fast suite never ran it. Here the real ``build_registry`` closure (through the
real ``with_conn``) runs on a fake connection: a recording stand-in for
``YouTubeCatalog`` captures the catalog kwargs, and a spy replaces ``backfill`` in
``services.planner.backfill`` to capture what ``run_channel_backfill`` forwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from psycopg.pq import TransactionStatus
from pydantic import SecretStr

from common.config import Settings
from common.repo import backfill_requests
from services.planner import backfill as planner_backfill
from services.planner import main as planner_main
from services.planner.backfill import BACKFILL_REQUEST_LIST_TIMEOUT_SEC


class _Stop(Exception):
    """Raised by the spy so the run ends right after ``backfill`` was called."""


class _FakeConn:
    info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    @contextmanager
    def transaction(self) -> Any:
        yield

    def close(self) -> None:
        pass


class RecordingCatalog:
    """Stands in for ``YouTubeCatalog``: remembers its constructor kwargs, no network."""

    instances: ClassVar[list[RecordingCatalog]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingCatalog.instances.append(self)


class _Wired:
    def __init__(self) -> None:
        self.catalog_kwargs: dict[str, Any] = {}
        self.backfill_kwargs: dict[str, Any] = {}


def _run_channel_backfill_task(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> _Wired:
    RecordingCatalog.instances = []
    wired = _Wired()
    request = SimpleNamespace(id=1, channel_id="UCx", limit_n=5, dry_run=True, enqueued=0)

    def spy(*args: Any, **kwargs: Any) -> Any:
        wired.backfill_kwargs = kwargs
        raise _Stop

    monkeypatch.setattr(planner_main, "YouTubeCatalog", RecordingCatalog)
    monkeypatch.setattr(planner_backfill, "backfill", spy)
    monkeypatch.setattr(backfill_requests, "claim_next", lambda conn: request)
    tasks = {t.name: t for t in planner_main.build_registry(settings, lambda: _FakeConn())}  # type: ignore[arg-type,return-value]
    built_by_registry = len(RecordingCatalog.instances)  # the reconcile task's catalog

    with pytest.raises(_Stop):
        tasks["channel_backfill"].fn()

    (ours,) = RecordingCatalog.instances[built_by_registry:]
    wired.catalog_kwargs = ours.kwargs
    return wired


def _settings(**overrides: Any) -> Settings:
    return Settings(DATABASE_URL=SecretStr("postgresql://u:p@127.0.0.1:1/db"), **overrides)


def test_the_closure_builds_the_catalog_with_the_backfill_timeout_and_cookie_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"

    wired = _run_channel_backfill_task(monkeypatch, _settings(YTDLP_COOKIE_FILE=cookies))

    assert wired.catalog_kwargs == {
        "timeout": BACKFILL_REQUEST_LIST_TIMEOUT_SEC,
        "cookie_file": cookies,
    }
    assert wired.catalog_kwargs["timeout"] != 90  # the synchronous route's timeout


def test_the_ollama_model_and_prefer_whisper_reach_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _run_channel_backfill_task(
        monkeypatch,
        _settings(
            SUMMARIZER="ollama",
            OLLAMA_MODEL="ollama-sentinel",
            ANTHROPIC_MODEL="anthropic-sentinel",
            PREFER_WHISPER=True,
        ),
    )

    assert wired.backfill_kwargs["model"] == "ollama-sentinel"
    assert wired.backfill_kwargs["summarizer"] == "ollama"
    assert wired.backfill_kwargs["prefer_whisper"] is True


def test_the_anthropic_model_reaches_backfill_and_prefer_whisper_stays_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _run_channel_backfill_task(
        monkeypatch,
        _settings(
            SUMMARIZER="anthropic",
            OLLAMA_MODEL="ollama-sentinel",
            ANTHROPIC_MODEL="anthropic-sentinel",
            PREFER_WHISPER=False,
        ),
    )

    assert wired.backfill_kwargs["model"] == "anthropic-sentinel"
    assert wired.backfill_kwargs["prefer_whisper"] is False
