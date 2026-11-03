"""Migration ``0026_seed_followed_channels`` (issue #386).

The first tests read the revision only. The integration tests need Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from common.config import Settings, get_settings
from common.models import FeedEntry
from common.queue import PostgresQueue
from common.repo.channels import list_active_channels
from common.youtube_refs import CHANNEL_ID_PATTERN
from services.api.main import create_app
from services.planner.poll import poll_channels

REPO_ROOT = Path(__file__).resolve().parents[2]
integration = pytest.mark.integration
SEED = (
    ("UCbta0n8i6Rljh0obO7HzG9A", "Joseph Carlson"),
    ("UCfCT7SSFEWyG4th9ZmaGYqQ", "Joseph Carlson After Hours"),
    ("UC3w193M5tYPJqF0Hi-7U-2g", "Dr. Eric Berg DC"),
    ("UCyqlbzLoYtpqDXwRI9Yh5LA", "BWB - Business With Brian"),
    ("UCrTTBSUr0zhPU56UQljag5A", "Value Investing with Sven Carlin, Ph.D."),
)
IDS = [cid for cid, _ in SEED]
TITLES_ASC = [
    "BWB - Business With Brian",
    "Dr. Eric Berg DC",
    "Joseph Carlson",
    "Joseph Carlson After Hours",
    "Value Investing with Sven Carlin, Ph.D.",
]


def _script() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _config_for(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


def _channels(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT channel_id, title, active, monitor_from, added_at, last_polled, "
            "poll_failures, last_poll_err, poll_failing_since FROM channels ORDER BY channel_id"
        ).fetchall()


def _count(dsn: str, table: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_0026_is_chained_to_0025() -> None:
    revision = _script().get_revision("0026")
    assert revision is not None
    assert revision.down_revision == "0025"


def test_constant_has_five_unique_well_formed_ids() -> None:
    revision = _script().get_revision("0026")
    assert revision is not None
    seed = revision.module.FOLLOWED_CHANNELS
    assert tuple(seed) == SEED
    ids = [cid for cid, _ in seed]
    assert len(ids) == 5 == len(set(ids))
    assert all(CHANNEL_ID_PATTERN.fullmatch(cid) for cid in ids)


@integration
def test_head_dsn_database_has_no_channels(head_dsn: str) -> None:
    assert _count(head_dsn, "channels") == 0


@integration
def test_upgrade_seeds_five_followed_channels(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0025")
    before = datetime.now(UTC)
    command.upgrade(config, "0026")

    rows = _channels(postgres_dsn)
    assert [(r[0], r[1]) for r in rows] == sorted(SEED)
    assert all(r[2] is True and r[5] is None and r[6] == 0 for r in rows)
    with psycopg.connect(postgres_dsn) as conn:
        now = conn.execute("SELECT now()").fetchone()
    assert now is not None
    assert all(before <= r[3] <= now[0] for r in rows)
    for table in ("videos", "jobs", "backfill_requests", "channel_catalog_entries"):
        assert _count(postgres_dsn, table) == 0


@integration
def test_second_run_changes_nothing(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0026")
    first = _channels(postgres_dsn)
    module = _script().get_revision("0026").module
    engine = create_engine(postgres_dsn.replace("postgresql://", "postgresql+psycopg://"))
    with engine.begin() as conn:
        module.seed_followed_channels(conn)
    engine.dispose()
    assert _channels(postgres_dsn) == first


@integration
def test_existing_rows_are_left_unchanged(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0025")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO channels (channel_id, title, active, monitor_from) "
            "VALUES (%s, 'Mine', false, %s)",
            (IDS[0], old),
        )
        conn.execute(
            "INSERT INTO channels (channel_id, title, active, monitor_from) "
            "VALUES (%s, NULL, true, %s)",
            (IDS[1], old),
        )
        conn.commit()
    existing = _channels(postgres_dsn)

    command.upgrade(config, "0026")

    after = {r[0]: r for r in _channels(postgres_dsn)}
    assert len(after) == 5
    for row in existing:
        assert after[row[0]] == row
    assert after[IDS[0]][1] == "Mine" and after[IDS[0]][2] is False
    assert after[IDS[1]][1] is None


@integration
def test_downgrade_removes_unused_rows_and_keeps_used_ones(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0026")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute("INSERT INTO channels (channel_id, title) VALUES ('UCother', 'Other')")
        conn.execute(
            "INSERT INTO videos (video_id, channel_id) VALUES ('aaaaaaaaaaa', %s)", (IDS[0],)
        )
        conn.execute(
            "INSERT INTO backfill_requests (channel_id, limit_n, dry_run, state) "
            "VALUES (%s, 50, false, 'done')",
            (IDS[1],),
        )
        conn.execute(
            "INSERT INTO channel_catalog_entries (channel_id, video_id, position, snapshot_limit) "
            "VALUES (%s, 'bbbbbbbbbbb', 0, 50)",
            (IDS[2],),
        )
        conn.commit()

    command.downgrade(config, "0025")

    kept = sorted(r[0] for r in _channels(postgres_dsn))
    assert kept == sorted([*IDS[:3], "UCother"])


@integration
def test_downgrade_without_dependants_and_round_trip(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_for(postgres_dsn, monkeypatch)
    command.upgrade(config, "0026")
    command.downgrade(config, "0025")
    assert _count(postgres_dsn, "channels") == 0
    command.upgrade(config, "0026")
    assert _count(postgres_dsn, "channels") == 5


class _Feed:
    def __init__(self, entries: list[FeedEntry]) -> None:
        self.entries = entries

    def fetch(self, channel_id: str) -> list[FeedEntry]:
        return [e for e in self.entries if e.channel_id == channel_id]


@integration
def test_seeded_channels_are_polled_like_followed_ones(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "0026")
    cid = IDS[0]
    with psycopg.connect(postgres_dsn) as conn:
        assert sorted(c.channel_id for c in list_active_channels(conn)) == sorted(IDS)
        monitor_from = conn.execute(
            "SELECT monitor_from FROM channels WHERE channel_id = %s", (cid,)
        ).fetchone()
        assert monitor_from is not None
        mf = monitor_from[0]
        # Only one seeded channel gets entries; the other four have empty feeds.
        feed = _Feed(
            [
                FeedEntry("oldvideo0001", cid, "Old", mf - timedelta(hours=1)),
                FeedEntry("newvideo0001", cid, "New", mf + timedelta(hours=1)),
            ]
        )
        queue = PostgresQueue(conn, settings=Settings(DATABASE_URL=postgres_dsn))  # type: ignore[arg-type]
        result = poll_channels(conn, feed, queue)
        conn.commit()
        assert (result.channels_polled, result.channels_failed) == (5, 0)
        videos = conn.execute("SELECT video_id, origin FROM videos").fetchall()
        jobs = conn.execute("SELECT video_id, kind FROM jobs").fetchall()
        polled = conn.execute(
            "SELECT count(*) FROM channels WHERE last_polled IS NOT NULL AND poll_failures = 0"
        ).fetchone()
    assert videos == [("newvideo0001", "rss")]
    assert jobs == [("newvideo0001", "ingest")]
    assert polled == (5,)


@integration
def test_read_side_shows_seeded_channels(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(_config_for(postgres_dsn, monkeypatch), "0026")
    with TestClient(create_app()) as client:
        listing = client.get("/channels")
        detail = client.get(f"/channels/{IDS[0]}")
        ops = client.get("/ops/channels")
        failing = client.get("/ops/channels", params={"failing": "true"})
        attention = client.get("/ops/channels", params={"attention": "true"})

    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [i["title"] for i in items] == TITLES_ASC
    assert all(i["active"] is True and i["video_count"] == 0 for i in items)
    assert detail.status_code == 200
    assert detail.json()["active"] is True and detail.json()["monitor_from"]
    ops_items = ops.json()["items"]
    assert sorted(i["channel_id"] for i in ops_items) == sorted(IDS)
    assert all(
        i["poll_failing_since"] is None and i["feed_gap_reason"] is None for i in ops_items
    )
    assert failing.json()["items"] == []
    assert attention.json()["items"] == []
