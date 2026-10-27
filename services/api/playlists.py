"""Playlist resolution, API side (issue #342; architecture.md 8.3).

``resolve_playlist`` lists a public playlist once (a bounded yt-dlp flat
listing, no per-video metadata) and marks which entries the library already
knows. It stores nothing: no ``videos``, ``jobs`` or ``deleted_videos`` row is
written, and no playlist is remembered. Submitting the picked videos is
``POST /videos/batch`` (#341).

No pooled connection is held while yt-dlp runs (#219): the listing happens first,
then one short read on a connection the provider yields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from common.backfill import ConnectionProvider
from common.models import PlaylistSource
from common.repo.videos import library_statuses
from common.youtube_refs import is_playlist_id, is_video_id

#: The most entries one resolve returns; the rest of a longer playlist is not
#: listed (#342). Read at call time, so a test can lower it.
MAX_PLAYLIST_ENTRIES = 200

#: ``processing_status`` of an entry whose video was deleted from the library.
DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ResolvedEntry:
    position: int
    video_id: str
    title: str | None
    known: bool
    #: The library status of a ``videos`` row, ``deleted`` for a tombstone, else None.
    processing_status: str | None


@dataclass(frozen=True, slots=True)
class ResolvedPlaylist:
    playlist_id: str
    title: str | None
    total_count: int | None
    truncated: bool
    entries: tuple[ResolvedEntry, ...]


def resolve_playlist(
    provider: ConnectionProvider,
    source: PlaylistSource,
    playlist_id: str,
    *,
    limit: int | None = None,
) -> ResolvedPlaylist:
    """List ``playlist_id``'s first ``limit`` videos and mark the known ones.

    ``limit`` defaults to ``MAX_PLAYLIST_ENTRIES``. ``playlist_id`` must already be
    a validated ID (``ValueError`` otherwise, before any listing). The source's
    ``PermanentSourceError`` / ``TransientNetworkError`` / ``RateLimitedError`` /
    ``ToolFailureError`` propagate unchanged. Entries whose ID is not a canonical
    video ID, or that repeat, are dropped; positions count the entries returned,
    from 0. ``truncated`` is true when the playlist holds more than was returned:
    the source's total exceeds the returned entries plus the ones it skipped, or
    it returned more than ``limit``.
    """
    if not is_playlist_id(playlist_id):
        raise ValueError("not a playlist ID")
    cap = MAX_PLAYLIST_ENTRIES if limit is None else limit
    listing = source.list_playlist(playlist_id, limit=cap)  # no connection held

    seen: set[str] = set()
    kept = []
    for entry in listing.entries:
        if is_video_id(entry.video_id) and entry.video_id not in seen:
            seen.add(entry.video_id)
            kept.append(entry)
    cut = len(kept) > cap
    kept = kept[:cap]

    statuses: dict[str, str] = {}
    if kept:
        with provider() as conn:
            _require_idle(conn)
            with conn.transaction():
                statuses = library_statuses(conn, [e.video_id for e in kept])

    total = listing.total_count
    truncated = cut or (total is not None and total > len(kept) + listing.skipped)
    return ResolvedPlaylist(
        playlist_id=playlist_id,
        title=listing.title,
        total_count=total,
        truncated=truncated,
        entries=tuple(
            ResolvedEntry(
                position=position,
                video_id=entry.video_id,
                title=entry.title,
                known=entry.video_id in statuses,
                processing_status=statuses.get(entry.video_id),
            )
            for position, entry in enumerate(kept)
        ),
    )


def _require_idle(conn: psycopg.Connection[Any]) -> None:
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("resolve_playlist expects a connection with no open transaction")
