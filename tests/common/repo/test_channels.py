"""Tests for common/repo/channels.py (issue #14)."""

from __future__ import annotations

import psycopg
import pytest

from common.repo.channels import add_channel, list_active_channels, record_poll

pytestmark = pytest.mark.integration


def test_add_channel_creates_an_active_channel_with_monitor_from_set(
    conn: psycopg.Connection,
) -> None:
    add_channel(conn, "UC123", "Some Channel")

    [channel] = list_active_channels(conn)
    assert channel.channel_id == "UC123"
    assert channel.title == "Some Channel"
    assert channel.active is True
    assert channel.monitor_from is not None


def test_add_channel_again_for_an_active_channel_changes_nothing(
    conn: psycopg.Connection,
) -> None:
    add_channel(conn, "UC123", "Some Channel")
    [before] = list_active_channels(conn)

    add_channel(conn, "UC123", "Renamed")

    [after] = list_active_channels(conn)
    assert after == before  # not even monitor_from changed


def test_list_active_channels_excludes_inactive_channels(conn: psycopg.Connection) -> None:
    add_channel(conn, "UC1", "One")
    conn.execute("UPDATE channels SET active = false WHERE channel_id = %s", ("UC1",))
    add_channel(conn, "UC2", "Two")

    active_ids = {c.channel_id for c in list_active_channels(conn)}

    assert active_ids == {"UC2"}


def test_list_active_channels_returns_empty_list_when_none_exist(
    conn: psycopg.Connection,
) -> None:
    assert list_active_channels(conn) == []


def test_record_poll_sets_last_polled_and_error(conn: psycopg.Connection) -> None:
    add_channel(conn, "UC1", "One")

    record_poll(conn, "UC1", error="boom")

    row = conn.execute(
        "SELECT last_polled, last_poll_err FROM channels WHERE channel_id = %s", ("UC1",)
    ).fetchone()
    assert row is not None
    last_polled, last_poll_err = row
    assert last_polled is not None
    assert last_poll_err == "boom"


def test_record_poll_with_no_error_clears_the_old_one(conn: psycopg.Connection) -> None:
    add_channel(conn, "UC1", "One")
    record_poll(conn, "UC1", error="boom")

    record_poll(conn, "UC1")

    row = conn.execute(
        "SELECT last_poll_err FROM channels WHERE channel_id = %s", ("UC1",)
    ).fetchone()
    assert row == (None,)


def test_record_poll_strips_nul_from_error(conn: psycopg.Connection) -> None:
    add_channel(conn, "UC1", "One")

    record_poll(conn, "UC1", error="bad\x00error")

    row = conn.execute(
        "SELECT last_poll_err FROM channels WHERE channel_id = %s", ("UC1",)
    ).fetchone()
    assert row == ("baderror",)


def test_add_channel_strips_nul_from_title(conn: psycopg.Connection) -> None:
    add_channel(conn, "UC1", "bad\x00title")

    [channel] = list_active_channels(conn)
    assert channel.title == "badtitle"
