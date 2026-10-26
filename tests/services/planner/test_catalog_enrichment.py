"""The ``catalog_enrichment`` task: bounded, failure-isolated, outside transactions (#242).

Needs Postgres. The metadata source is a fake; no yt-dlp, no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from pydantic import SecretStr

from common.config import Settings
from common.errors import RateLimitedError, ToolFailureError
from common.models import CatalogEntry, ChannelCatalog, VideoMeta
from common.repo.channel_catalog import replace_catalog_snapshot
from services.planner import main as planner_main
from services.planner.catalog_enrichment import run_catalog_enrichment

pytestmark = pytest.mark.integration

CHANNEL = "UC" + "c" * 22
OTHER = "UC" + "d" * 22
A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
PUBLISHED = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _meta(video_id: str, *, channel_id: str = CHANNEL, description: str = "About") -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        channel_id=channel_id,
        title=f"Full {video_id}",
        description=description,
        duration_sec=10,
        published_at=PUBLISHED,
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


class FakeMetadata:
    def __init__(
        self, by_id: dict[str, Any] | None = None, *, conn: psycopg.Connection[Any] | None = None
    ) -> None:
        self.by_id = by_id or {}
        self.conn = conn
        self.fetched: list[str] = []
        self.statuses: list[TransactionStatus] = []

    def fetch(self, video_id: str) -> VideoMeta:
        self.fetched.append(video_id)
        if self.conn is not None:
            self.statuses.append(self.conn.info.transaction_status)
        result = self.by_id[video_id]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, VideoMeta)
        return result


@pytest.fixture
def db(head_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO channels (channel_id) VALUES (%s)", (CHANNEL,))
        yield conn


def _seed(db: psycopg.Connection[Any], *ids: str) -> None:
    entries = tuple(CatalogEntry(i, f"Flat {i}", None) for i in ids)
    replace_catalog_snapshot(db, CHANNEL, ChannelCatalog(CHANNEL, entries, len(ids)), limit=50)


def _states(db: psycopg.Connection[Any]) -> dict[str, tuple[str, str | None]]:
    rows = db.execute(
        "SELECT video_id, enrichment_state, enrichment_error FROM channel_catalog_entries"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_a_tick_enriches_rows_and_returns_the_completed_count(db: psycopg.Connection[Any]) -> None:
    _seed(db, A, B)
    metadata = FakeMetadata({A: _meta(A, description="First"), B: _meta(B)}, conn=db)

    assert run_catalog_enrichment(db, metadata, batch_size=5) == 2

    assert _states(db) == {A: ("done", None), B: ("done", None)}
    assert db.execute(
        "SELECT description, published_at, title FROM channel_catalog_entries WHERE video_id = %s",
        (A,),
    ).fetchone() == ("First", PUBLISHED, f"Full {A}")


def test_no_transaction_is_open_while_the_metadata_is_fetched(db: psycopg.Connection[Any]) -> None:
    _seed(db, A, B)
    metadata = FakeMetadata({A: _meta(A), B: ToolFailureError("x")}, conn=db)

    run_catalog_enrichment(db, metadata, batch_size=5)

    assert metadata.statuses == [TransactionStatus.IDLE, TransactionStatus.IDLE]


def test_a_tick_is_bounded_by_the_injected_batch_size(db: psycopg.Connection[Any]) -> None:
    _seed(db, A, B, C)
    metadata = FakeMetadata({A: _meta(A), B: _meta(B), C: _meta(C)})

    assert run_catalog_enrichment(db, metadata, batch_size=2) == 2

    assert metadata.fetched == [A, B]
    assert _states(db)[C] == ("pending", None)
    assert run_catalog_enrichment(db, metadata, batch_size=2) == 1
    assert _states(db)[C] == ("done", None)


def test_a_tick_starts_no_new_row_once_the_time_budget_is_spent(
    db: psycopg.Connection[Any],
) -> None:
    """Tasks share one thread, so a slow source must not hold the others behind it."""
    _seed(db, A, B, C)
    now = [0.0]
    metadata = FakeMetadata({A: _meta(A), B: _meta(B), C: _meta(C)})
    real_fetch = metadata.fetch

    def slow_fetch(video_id: str) -> VideoMeta:
        now[0] += 40.0  # each lookup takes 40 s of the injected clock
        return real_fetch(video_id)

    metadata.fetch = slow_fetch  # type: ignore[method-assign]

    done = run_catalog_enrichment(db, metadata, batch_size=10, budget_sec=60, clock=lambda: now[0])

    assert done == 2  # A at 0 s, B at 40 s; at 80 s the 60 s budget is spent
    assert metadata.fetched == [A, B]
    assert _states(db) == {A: ("done", None), B: ("done", None), C: ("pending", None)}


def test_a_failure_is_isolated_to_its_row_and_stores_only_the_class(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A, B, C)
    secret = "yt-dlp: ERROR https://secret.example/x"
    metadata = FakeMetadata(
        {A: ToolFailureError(secret), B: _meta(B), C: RateLimitedError(secret)}
    )

    assert run_catalog_enrichment(db, metadata, batch_size=5) == 1

    assert _states(db) == {
        A: ("failed", "ToolFailureError"),
        B: ("done", None),
        C: ("failed", "RateLimitedError"),
    }
    assert secret not in repr(db.execute("SELECT * FROM channel_catalog_entries").fetchall())


def test_an_unexpected_error_class_is_isolated_too(db: psycopg.Connection[Any]) -> None:
    _seed(db, A, B)
    metadata = FakeMetadata({A: KeyError("secret"), B: _meta(B)})

    assert run_catalog_enrichment(db, metadata, batch_size=5) == 1

    assert _states(db) == {A: ("failed", "KeyError"), B: ("done", None)}


def test_a_result_for_another_channel_fails_only_that_row_and_leaks_nothing(
    db: psycopg.Connection[Any],
) -> None:
    _seed(db, A, B)
    metadata = FakeMetadata(
        {A: _meta(A, channel_id=OTHER, description="not ours"), B: _meta(B, description="ready")}
    )

    assert run_catalog_enrichment(db, metadata, batch_size=2) == 1

    assert _states(db) == {A: ("failed", "ChannelMismatch"), B: ("done", None)}
    assert db.execute(
        "SELECT description FROM channel_catalog_entries WHERE video_id = %s", (A,)
    ).fetchone() == (None,)
    assert OTHER not in repr(db.execute("SELECT * FROM channel_catalog_entries").fetchall())


def test_a_result_for_another_video_fails_the_row(db: psycopg.Connection[Any]) -> None:
    _seed(db, A)

    run_catalog_enrichment(db, FakeMetadata({A: _meta(B)}), batch_size=1)

    assert _states(db) == {A: ("failed", "ChannelMismatch")}


def test_failed_rows_are_not_retried_automatically(db: psycopg.Connection[Any]) -> None:
    _seed(db, A)
    metadata = FakeMetadata({A: ToolFailureError("x")})

    run_catalog_enrichment(db, metadata, batch_size=5)
    assert run_catalog_enrichment(db, metadata, batch_size=5) == 0

    assert metadata.fetched == [A]


def test_pending_rows_persist_across_a_planner_restart(
    db: psycopg.Connection[Any], head_dsn: str
) -> None:
    _seed(db, A)

    with psycopg.connect(head_dsn, autocommit=True) as restarted:
        assert run_catalog_enrichment(restarted, FakeMetadata({A: _meta(A)}), batch_size=1) == 1


def test_an_empty_catalog_is_a_quiet_no_op(db: psycopg.Connection[Any]) -> None:
    assert run_catalog_enrichment(db, FakeMetadata(), batch_size=5) == 0


def test_an_open_transaction_is_refused(db: psycopg.Connection[Any]) -> None:
    with db.transaction(), pytest.raises(RuntimeError, match="no open transaction"):
        run_catalog_enrichment(db, FakeMetadata(), batch_size=5)


# --- wiring: the real registry closure ----------------------------------------


class RecordingMetadata:
    instances: ClassVar[list[RecordingMetadata]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingMetadata.instances.append(self)

    def fetch(self, video_id: str) -> VideoMeta:
        return _meta(video_id)


def test_the_registered_task_uses_the_configured_cookie_file_and_batch_size(
    db: psycopg.Connection[Any], head_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    RecordingMetadata.instances = []
    monkeypatch.setattr(planner_main, "YouTubeMetadata", RecordingMetadata)
    cookies = tmp_path / "cookies.txt"
    settings = Settings(
        DATABASE_URL=SecretStr(head_dsn),
        YTDLP_COOKIE_FILE=cookies,
        CATALOG_ENRICH_BATCH_SIZE=2,
        CATALOG_ENRICH_INTERVAL_SEC=45,
    )
    _seed(db, A, B, C)
    tasks = {
        t.name: t
        for t in planner_main.build_registry(settings, lambda: psycopg.connect(head_dsn))
    }
    task = tasks["catalog_enrichment"]

    assert task.interval_sec == 45
    assert task.fn() == 2

    assert [i.kwargs["cookie_file"] for i in RecordingMetadata.instances] == [cookies]
    assert [i.kwargs["timeout"] for i in RecordingMetadata.instances] == [45.0]
    assert _states(db) == {A: ("done", None), B: ("done", None), C: ("pending", None)}


def test_the_task_is_registered_after_reconcile_and_with_its_own_lock() -> None:
    settings = Settings(DATABASE_URL=SecretStr("postgresql://u:p@127.0.0.1:1/db"))

    names = [t.name for t in planner_main.build_registry(settings, lambda: None)]  # type: ignore[arg-type, return-value]

    assert names.index("catalog_enrichment") > names.index("reconcile_channels")
    assert planner_main.LOCK_KEYS["catalog_enrichment"] == (planner_main.LOCK_NAMESPACE, 10)
    assert len(set(planner_main.LOCK_KEYS.values())) == len(planner_main.LOCK_KEYS)
