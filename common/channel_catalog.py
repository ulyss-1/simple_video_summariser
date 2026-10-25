"""Channel catalog discovery and selected processing (issue #242).

``discover_catalog`` lists a registered channel's newest uploads once (a yt-dlp
flat listing, no per-video metadata) and persists the result as a snapshot in
``channel_catalog_entries``. Browsing never creates ``videos`` or ``jobs`` rows.

``process_catalog_selection`` is the one write: for each selected ID it checks
the ID is in this channel's persisted catalog and, if the video is new, inserts
it as ``origin='backfill'`` and enqueues a low-priority ``ingest`` job, exactly
as a channel backfill does (``common.backfill``). Every requested ID gets its
own result, so a partial outcome is always visible. Downstream subtitle and
transcription policy is untouched: it runs in the existing ingest handler.

Neither function holds a transaction while yt-dlp runs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.pq import TransactionStatus

from common.backfill import ChannelNotRegistered
from common.errors import PermanentSourceError
from common.models import CatalogEntry, CatalogPage, CatalogSource, ChannelCatalog
from common.queue import PRIORITY_BACKFILL, JobQueue
from common.repo.channel_catalog import (
    CATALOG_LIMITS,
    get_catalog_page,
    replace_catalog_snapshot,
)
from common.repo.channels import channel_exists
from common.repo.jobs import active_job, latest_job
from common.repo.videos import insert_backfill_video
from common.youtube_refs import is_video_id

_log = logging.getLogger(__name__)

#: The most IDs one bulk call accepts: the largest catalog range.
MAX_SELECTED = max(CATALOG_LIMITS)

__all__ = [
    "MAX_SELECTED",
    "BulkCatalogResult",
    "ChannelNotRegistered",
    "discover_catalog",
    "process_catalog_selection",
]


def check_limit(limit: object) -> int:
    """``limit`` if it is one of the named ranges, else ``ValueError``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit not in CATALOG_LIMITS:
        raise ValueError("catalog limit must be one of 50, 200, 500, 5000")
    return limit


def discover_catalog(
    conn: psycopg.Connection[Any], catalog: CatalogSource, channel_id: str, *, limit: int
) -> CatalogPage:
    """List ``channel_id``'s newest ``limit`` uploads, persist them, return the page.

    Raises ``ValueError`` for a limit outside ``CATALOG_LIMITS`` (before any
    listing), ``ChannelNotRegistered`` for an unknown channel (before any
    listing) and the adapter's ``TransientNetworkError`` / ``RateLimitedError`` /
    ``ToolFailureError`` unchanged. ``PermanentSourceError`` is an empty
    listing (architecture.md 8.3.1). Entries whose ID is not a canonical video
    ID are dropped. ``conn`` must have no open transaction.
    """
    check_limit(limit)
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("discover_catalog expects a connection with no open transaction")
    with conn.transaction():
        registered = channel_exists(conn, channel_id)
    if not registered:
        raise ChannelNotRegistered(channel_id)
    try:
        listing = catalog.list_uploads(channel_id, limit=limit)
    except PermanentSourceError as exc:
        _log.info(
            "catalog listing empty channel_id=%s reason=%s", channel_id, exc.reason.value
        )
        listing = ChannelCatalog(channel_id, (), None)
    entries = _canonical(listing.entries)[:limit]
    listing = ChannelCatalog(channel_id, entries, listing.total_count)
    with conn.transaction():
        replace_catalog_snapshot(conn, channel_id, listing, limit=limit)
        page = get_catalog_page(conn, channel_id, limit=limit)
    if page is None:  # an empty listing leaves no rows
        return CatalogPage(channel_id, limit, listing.total_count, ())
    return page


def _canonical(entries: Sequence[CatalogEntry]) -> tuple[CatalogEntry, ...]:
    seen: set[str] = set()
    kept: list[CatalogEntry] = []
    for entry in entries:
        if is_video_id(entry.video_id) and entry.video_id not in seen:
            seen.add(entry.video_id)
            kept.append(entry)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class BulkCatalogResult:
    video_id: str
    outcome: Literal["queued", "already_known", "rejected"]
    job_id: int | None
    reason: Literal["not_in_catalog"] | None


def process_catalog_selection(
    conn: psycopg.Connection[Any],
    queue: JobQueue,
    channel_id: str,
    video_ids: Sequence[str],
    *,
    max_selected: int = MAX_SELECTED,
) -> tuple[BulkCatalogResult, ...]:
    """Queue the selected catalog videos for processing; one result per ID, in order.

    The whole input is validated before any write: non-empty, unique, at most
    ``max_selected`` and every ID a canonical video ID (``ValueError``
    otherwise). An unregistered channel raises ``ChannelNotRegistered``. Each ID
    is then handled in its own short transaction:

    - not in this channel's persisted catalog -> ``rejected`` / ``not_in_catalog``;
    - already has a ``videos`` row, a deletion tombstone, or lost an insert race
      -> ``already_known`` (nothing is written);
    - otherwise a ``origin='backfill'`` row and an ``ingest`` job at
      ``PRIORITY_BACKFILL`` -> ``queued`` with the job's ID.

    ``conn`` must have no open transaction.
    """
    ids = list(video_ids)
    if not ids:
        raise ValueError("video_ids must not be empty")
    if len(ids) > max_selected:
        raise ValueError(f"at most {max_selected} video_ids may be selected")
    if len(set(ids)) != len(ids):
        raise ValueError("video_ids must be unique")
    if not all(is_video_id(v) for v in ids):
        raise ValueError("video_ids must be canonical YouTube video IDs")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("process_catalog_selection expects a connection with no open transaction")
    with conn.transaction():
        registered = channel_exists(conn, channel_id)
    if not registered:
        raise ChannelNotRegistered(channel_id)
    return tuple(_process_one(conn, queue, channel_id, video_id) for video_id in ids)


def _process_one(
    conn: psycopg.Connection[Any], queue: JobQueue, channel_id: str, video_id: str
) -> BulkCatalogResult:
    with conn.transaction():
        member = conn.execute(
            """
            SELECT title FROM channel_catalog_entries
            WHERE channel_id = %s AND video_id = %s
            FOR SHARE
            """,
            (channel_id, video_id),
        ).fetchone()
        if member is None:
            return BulkCatalogResult(video_id, "rejected", None, "not_in_catalog")
        entry = CatalogEntry(video_id, member[0], None)
        if not insert_backfill_video(conn, entry, channel_id=channel_id):
            return BulkCatalogResult(video_id, "already_known", None, None)
        job_id = queue.enqueue(
            "ingest", video_id, payload={"origin": "backfill"}, priority=PRIORITY_BACKFILL
        )
        if job_id is None:  # a job already exists for the row this call just made
            existing = active_job(conn, video_id, "ingest", "default")
            if existing is not None:
                job_id = existing.id
            else:
                latest = latest_job(conn, video_id, "ingest")
                if latest is None:
                    raise RuntimeError("ingest job neither created nor found")
                job_id = latest[0]
    return BulkCatalogResult(video_id, "queued", job_id, None)
