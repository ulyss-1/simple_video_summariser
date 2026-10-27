"""Video repository functions (issue #14, architecture.md §6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from common.errors import Cancelled, PermanentSourceError, UnavailableReason
from common.models import (
    ActiveJob,
    CatalogEntry,
    FeedEntry,
    JobCancelled,
    JobFailure,
    VideoMeta,
    VideoPage,
    VideoSummary,
)
from common.repo._hygiene import clean_text

_UNAVAILABLE_REASONS = {"removed", "private", "geoblocked", "agegated", "short"}

_SORT_ORDERS = {
    ("title", "asc"): 'lower(title) COLLATE "C" ASC NULLS LAST',
    ("title", "desc"): 'lower(title) COLLATE "C" DESC NULLS LAST',
    ("channel", "asc"): 'lower(channel_title) COLLATE "C" ASC NULLS LAST',
    ("channel", "desc"): 'lower(channel_title) COLLATE "C" DESC NULLS LAST',
    ("published", "asc"): "published_at ASC NULLS LAST",
    ("published", "desc"): "published_at DESC NULLS LAST",
    ("status", "asc"): (
        "CASE status WHEN 'idle' THEN 0 WHEN 'processing' THEN 1 WHEN 'done' THEN 2 "
        "WHEN 'unavailable' THEN 3 WHEN 'failed' THEN 4 END ASC"
    ),
    ("status", "desc"): (
        "CASE status WHEN 'idle' THEN 0 WHEN 'processing' THEN 1 WHEN 'done' THEN 2 "
        "WHEN 'unavailable' THEN 3 WHEN 'failed' THEN 4 END DESC"
    ),
}


@dataclass(frozen=True, slots=True)
class JobGuard:
    """The queue job a ``videos`` write is made for (#303): its id and the worker holding it."""

    job_id: int
    worker: str | None


def guard_video_write(conn: psycopg.Connection[Any], video_id: str, guard: JobGuard) -> None:
    """Lock the caller's own job row, then refuse to write for a cancelled job or a deleted video.

    ``upsert_video`` and ``record_unavailable`` are ``INSERT ... ON CONFLICT DO UPDATE``:
    after a concurrent ``delete_video`` they would insert a new row and bring the video
    back. ``delete_video`` locks the video's jobs before it deletes anything, so this
    transaction either commits first (the delete then cascades what it wrote) or waits
    for the delete and reads the cancel request it stored. The lock is kept until the
    caller's commit.

    Raises ``Cancelled`` for a job with a cancel request, or one this worker no longer
    holds (reaped), and ``PermanentSourceError`` (non-retryable, raises no alert) when
    the job has no request but its video is gone (a job enqueued after the delete).
    The open transaction is rolled back first, so the lock is not held while the
    exception travels to the queue's outcome write.
    """
    row = conn.execute(
        "SELECT cancel_requested_at IS NOT NULL FROM jobs"
        " WHERE id = %s AND locked_by IS NOT DISTINCT FROM %s AND state = 'running'"
        " FOR UPDATE",
        (guard.job_id, guard.worker),
    ).fetchone()
    if row is None or row[0]:
        conn.rollback()
        raise Cancelled()
    if not video_exists(conn, video_id):
        conn.rollback()
        raise PermanentSourceError(UnavailableReason.REMOVED, f"video {video_id} was deleted")


def upsert_video(
    conn: psycopg.Connection[Any],
    meta: VideoMeta,
    origin: str,
    *,
    guard: JobGuard | None = None,
) -> None:
    """Insert ``meta`` as a video, or update its mutable fields on a later call.

    ``title``, ``duration_sec``, ``published_at`` and ``description`` are
    refreshed on every call, and a missing ``channel_id`` (a stub row from
    ``record_unavailable``, or a submitted video from
    ``insert_submitted_video``) is filled in; a known ``channel_id`` is never
    overwritten. ``origin`` and ``discovered_at`` are set only
    on the first insert - they record the video's *first* discovery, so a
    later call, even with a different ``origin``, never changes them.

    When ``meta.channel_id`` has no ``channels`` row yet, one is created
    with ``active = false`` so the foreign key holds without starting
    monitoring for an ad-hoc submission. An existing channel row, active or
    not, is left untouched.

    With ``guard`` (a queue job, #303) the write first passes ``guard_video_write``;
    without it (the CLI path) nothing changes.
    """
    if guard is not None:
        guard_video_write(conn, meta.video_id, guard)
    conn.execute(
        """
        INSERT INTO channels (channel_id, active)
        VALUES (%s, false)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (meta.channel_id,),
    )
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, duration_sec,
                             published_at, description, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (video_id) DO UPDATE SET
            channel_id   = COALESCE(videos.channel_id, EXCLUDED.channel_id),
            title        = EXCLUDED.title,
            duration_sec = EXCLUDED.duration_sec,
            published_at = EXCLUDED.published_at,
            description  = EXCLUDED.description
        """,
        (
            meta.video_id,
            meta.channel_id,
            clean_text(meta.title),
            meta.duration_sec,
            meta.published_at,
            clean_text(meta.description),
            origin,
        ),
    )


def _check_reason(reason: str) -> None:
    if reason not in _UNAVAILABLE_REASONS:
        raise ValueError(
            f"unavailable reason must be one of {sorted(_UNAVAILABLE_REASONS)}, got {reason!r}"
        )


def mark_unavailable(conn: psycopg.Connection[Any], video_id: str, reason: str) -> None:
    """Record why ``video_id`` can no longer be fetched.

    ``reason`` must be one of ``removed``, ``private``, ``geoblocked``,
    ``agegated`` or ``short``; anything else raises ``ValueError``.
    """
    _check_reason(reason)
    conn.execute(
        "UPDATE videos SET unavailable = %s WHERE video_id = %s",
        (reason, video_id),
    )


def get_discovered_at(conn: psycopg.Connection[Any], video_id: str) -> datetime | None:
    """When ``video_id`` was first discovered, or ``None`` if it has no row (#65)."""
    row = conn.execute(
        "SELECT discovered_at FROM videos WHERE video_id = %s", (video_id,)
    ).fetchone()
    return row[0] if row is not None else None


def clear_unavailable(conn: psycopg.Connection[Any], video_id: str) -> None:
    """Reset ``videos.unavailable`` to NULL, e.g. a private video went public.

    A no-op when ``video_id`` has no row.
    """
    conn.execute("UPDATE videos SET unavailable = NULL WHERE video_id = %s", (video_id,))


def record_unavailable(
    conn: psycopg.Connection[Any],
    video_id: str,
    reason: str,
    *,
    origin: str,
    guard: JobGuard | None = None,
) -> None:
    """Mark ``video_id`` unavailable, creating a stub row if it has none.

    The stub has a NULL ``channel_id`` and the given ``origin`` (first
    ingest of an already-removed video). An existing row keeps its
    ``origin`` and metadata; only ``unavailable`` changes. ``reason`` is
    validated like ``mark_unavailable``. ``guard`` as for ``upsert_video`` (#303).
    """
    _check_reason(reason)
    if guard is not None:
        guard_video_write(conn, video_id, guard)
    conn.execute(
        """
        INSERT INTO videos (video_id, origin, unavailable)
        VALUES (%s, %s, %s)
        ON CONFLICT (video_id) DO UPDATE SET unavailable = EXCLUDED.unavailable
        """,
        (video_id, origin, reason),
    )


def insert_discovered_video(
    conn: psycopg.Connection[Any],
    entry: FeedEntry,
    origin: str,
    *,
    unavailable: str | None = None,
) -> bool:
    """Insert the video ``entry`` lists, unless it already has a row.

    Returns whether a row was inserted. An existing row is left exactly as it is
    (``origin``, ``title``, ``description``, ``discovered_at`` and the rest), which is
    what makes polling the same feed twice harmless. ``upsert_video`` is the wrong tool
    here: it needs full metadata and overwrites ``title`` and ``description``.

    A missing ``channels`` row is created inactive, as ``upsert_video`` does.
    ``unavailable``, when given, is stored on the new row (the poll records a
    skipped Short as ``short``, #80) and is validated like ``mark_unavailable``.
    """
    if unavailable is not None:
        _check_reason(unavailable)
    conn.execute(
        """
        INSERT INTO channels (channel_id, active)
        VALUES (%s, false)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (entry.channel_id,),
    )
    cur = conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, published_at, origin, unavailable)
        SELECT %s, %s, %s, %s, %s, %s
        WHERE NOT EXISTS (SELECT 1 FROM deleted_videos WHERE video_id = %s)
        ON CONFLICT (video_id) DO NOTHING
        """,
        (
            entry.video_id,
            entry.channel_id,
            clean_text(entry.title),
            entry.published_at,
            origin,
            unavailable,
            entry.video_id,
        ),
    )
    return cur.rowcount == 1


def insert_submitted_video(conn: psycopg.Connection[Any], video_id: str) -> bool:
    """Insert a bare ``origin = 'adhoc'`` row for a submitted video (#40).

    Only ``video_id`` and ``origin`` are set; ``channel_id``, ``title`` and the
    rest stay NULL until ingest (#28) runs ``upsert_video``. Returns whether a
    row was inserted. An existing row (from RSS, backfill or an earlier
    submission) is left exactly as it is, including ``origin`` and
    ``discovered_at``. No ``channels`` row is created.
    """
    conn.execute("DELETE FROM deleted_videos WHERE video_id = %s", (video_id,))
    cur = conn.execute(
        """
        INSERT INTO videos (video_id, origin)
        VALUES (%s, 'adhoc')
        ON CONFLICT (video_id) DO NOTHING
        """,
        (video_id,),
    )
    return cur.rowcount == 1


def video_exists(conn: psycopg.Connection[Any], video_id: str) -> bool:
    """Whether ``videos`` has a row for ``video_id`` (issue #29)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM videos WHERE video_id = %s)", (video_id,)
    ).fetchone()
    assert row is not None
    return bool(row[0])


def get_video_meta(conn: psycopg.Connection[Any], video_id: str) -> VideoMeta | None:
    """The stored fields of ``video_id`` as a ``VideoMeta``, or ``None`` without a row.

    Fields the ``videos`` table does not store (``language``, ``live_status``, the
    subtitle language lists) are ``None`` or ``()``. NULL text columns (a stub row
    from ``record_unavailable``) come back as empty strings.
    """
    row = conn.execute(
        """
        SELECT video_id, channel_id, title, description, duration_sec, published_at
        FROM videos
        WHERE video_id = %s
        """,
        (video_id,),
    ).fetchone()
    if row is None:
        return None
    found_id, channel_id, title, description, duration_sec, published_at = row
    return VideoMeta(
        video_id=found_id,
        channel_id=channel_id or "",
        title=title or "",
        description=description or "",
        duration_sec=duration_sec,
        published_at=published_at,
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )


@dataclass(frozen=True, slots=True)
class KnownVideo:
    """What a ``videos`` row says about its owner and date (issue #93)."""

    channel_id: str | None  # NULL for a stub row (a submission, an unavailable video)
    published_at: datetime | None  # NULL until ingest (#28) fills it in


def known_videos(
    conn: psycopg.Connection[Any], video_ids: Sequence[str]
) -> dict[str, KnownVideo]:
    """Map each of ``video_ids`` that already has a ``videos`` row to its facts.

    Any row counts, whatever its origin, job state or ``unavailable`` flag. One
    query for the whole list; unknown IDs are simply absent from the result.
    """
    if not video_ids:
        return {}
    rows = conn.execute(
        """
        SELECT video_id, channel_id, published_at FROM videos WHERE video_id = ANY(%s)
        UNION ALL
        SELECT video_id, NULL, NULL FROM deleted_videos WHERE video_id = ANY(%s)
        """,
        (list(video_ids), list(video_ids)),
    ).fetchall()
    return {row[0]: KnownVideo(channel_id=row[1], published_at=row[2]) for row in rows}


def known_video_ids(conn: psycopg.Connection[Any], video_ids: Sequence[str]) -> set[str]:
    """Return the subset of ``video_ids`` that already has a ``videos`` row.

    The keys of ``known_videos``: one query for the whole list.
    """
    return set(known_videos(conn, video_ids))


def insert_catalog_video(
    conn: psycopg.Connection[Any], entry: CatalogEntry, *, channel_id: str, origin: str
) -> bool:
    """Insert a row for a catalog-listed video with the given ``origin`` (#41, #93).

    Shared by channel backfill (``origin='backfill'``) and RSS-gap
    reconciliation (``origin='rss'``). The listing has no publish date, so
    ``published_at`` stays NULL until ingest fills it in; that keeps a row made
    here from ever acting as a reconciliation anchor. The title is cleaned. A
    missing ``channels`` row is created inactive, as ``insert_discovered_video``
    does. An existing video row is left untouched. Returns whether this call
    inserted the row.
    """
    conn.execute(
        """
        INSERT INTO channels (channel_id, active)
        VALUES (%s, false)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        (channel_id,),
    )
    row = conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, duration_sec, origin)
        SELECT %s, %s, %s, %s, %s
        WHERE NOT EXISTS (SELECT 1 FROM deleted_videos WHERE video_id = %s)
        ON CONFLICT (video_id) DO NOTHING
        RETURNING video_id
        """,
        (
            entry.video_id,
            channel_id,
            clean_text(entry.title),
            entry.duration_sec,
            origin,
            entry.video_id,
        ),
    ).fetchone()
    return row is not None


def insert_backfill_video(
    conn: psycopg.Connection[Any], entry: CatalogEntry, *, channel_id: str
) -> bool:
    """Insert a channel-backfill row from a catalog entry (issue #41).

    ``insert_catalog_video`` with ``origin='backfill'``.
    """
    return insert_catalog_video(conn, entry, channel_id=channel_id, origin="backfill")


def delete_unused_backfill_video(conn: psycopg.Connection[Any], video_id: str) -> bool:
    """Delete a backfill-origin ``videos`` row nothing else depends on (#116).

    Deleted only when ``origin = 'backfill'`` and no ``transcripts``,
    ``analyses``, ``media`` or ``jobs`` row (any kind or state) refers to it,
    so ``ON DELETE CASCADE`` never removes anything. ``jobs.video_id`` has no
    foreign key, so a job another transaction inserts for the video after this
    statement's snapshot cannot be waited for; the window is one statement
    wide. Returns whether the row was deleted. Never commits.
    """
    row = conn.execute(
        """
        DELETE FROM videos v
        WHERE v.video_id = %s AND v.origin = 'backfill'
          AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.video_id = v.video_id)
          AND NOT EXISTS (SELECT 1 FROM analyses a WHERE a.video_id = v.video_id)
          AND NOT EXISTS (SELECT 1 FROM media m WHERE m.video_id = v.video_id)
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.video_id)
        RETURNING 1
        """,
        (video_id,),
    ).fetchone()
    return row is not None


# The one definition of a video's processing status (#42). Both list_videos and
# get_video select from this, so the rules cannot drift apart. ``v`` is the
# videos row; the LATERAL joins see only that video's jobs and analyses.
_VIDEO_STATUS_SQL = """
    CASE
        WHEN la.created_at IS NOT NULL THEN 'done'
        WHEN aj.id IS NOT NULL THEN 'processing'
        WHEN v.unavailable IS NOT NULL THEN 'unavailable'
        WHEN nj.state = 'dead' AND nj.error_class IS DISTINCT FROM 'CANCELLED' THEN 'failed'
        ELSE 'idle'
    END
"""

_VIDEO_STATUS_JOINS = """
    LEFT JOIN LATERAL (
        SELECT a.created_at FROM analyses a
        WHERE a.video_id = v.video_id
        ORDER BY
            COALESCE(a.model = %(prefer_model)s
                     AND a.prompt_version = %(prefer_prompt_version)s, FALSE) DESC,
            a.created_at DESC, a.id DESC
        LIMIT 1
    ) la ON true
    LEFT JOIN LATERAL (
        SELECT j.id, j.kind, j.state, j.cancel_requested_at FROM jobs j
        WHERE j.video_id = v.video_id AND j.state IN ('pending', 'running')
        ORDER BY CASE j.kind WHEN 'ingest' THEN 0 WHEN 'transcribe' THEN 1
                             WHEN 'analyze' THEN 2 ELSE 3 END,
                 j.created_at, j.id
        LIMIT 1
    ) aj ON true
    LEFT JOIN LATERAL (
        SELECT j.id, j.kind, j.state, j.error_class, j.finished_at FROM jobs j
        WHERE j.video_id = v.video_id
        ORDER BY j.created_at DESC, j.id DESC LIMIT 1
    ) nj ON true
    LEFT JOIN LATERAL (
        SELECT j.id, j.kind, j.error_class, j.finished_at FROM jobs j
        WHERE j.video_id = v.video_id AND j.state = 'dead'
          AND j.error_class IS DISTINCT FROM 'CANCELLED'
        ORDER BY j.created_at DESC, j.id DESC LIMIT 1
    ) lf ON true
"""

_VIDEO_READ = f"""
    SELECT v.video_id, v.title, v.channel_id, c.title AS channel_title,
           v.published_at, v.duration_sec, v.origin, v.unavailable,
           {_VIDEO_STATUS_SQL} AS status,
           aj.id AS aj_id, aj.kind AS aj_kind, aj.state AS aj_state,
           aj.cancel_requested_at AS aj_cancel_requested_at,
           lf.id AS lf_id, lf.kind AS lf_kind, lf.error_class AS lf_error_class,
           lf.finished_at AS lf_finished_at,
           la.created_at AS latest_analysis_at,
           v.discovered_at,
           EXISTS (SELECT 1 FROM transcripts t WHERE t.video_id = v.video_id) AS has_transcript,
           CASE WHEN nj.state = 'dead' AND nj.error_class = 'CANCELLED' THEN nj.id END
               AS nc_id,
           CASE WHEN nj.state = 'dead' AND nj.error_class = 'CANCELLED' THEN nj.kind END
               AS nc_kind,
           CASE WHEN nj.state = 'dead' AND nj.error_class = 'CANCELLED' THEN nj.finished_at END
               AS nc_finished_at
    FROM videos v
    LEFT JOIN channels c ON c.channel_id = v.channel_id
    {_VIDEO_STATUS_JOINS}
"""


def library_statuses(
    conn: psycopg.Connection[Any], video_ids: Sequence[str]
) -> dict[str, str]:
    """Map each of ``video_ids`` the library knows to its processing status (#342).

    A ``videos`` row gives the shared status (``done``, ``processing``,
    ``unavailable``, ``failed`` or ``idle``); an ID with only a deletion tombstone
    gives ``deleted``. Unknown IDs are absent. One read-only query for the whole
    list, writing nothing.
    """
    ids = list(video_ids)
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT v.video_id, {_VIDEO_STATUS_SQL} AS status
        FROM videos v
        {_VIDEO_STATUS_JOINS}
        WHERE v.video_id = ANY(%(ids)s)
        UNION ALL
        SELECT d.video_id, 'deleted'
        FROM deleted_videos d
        WHERE d.video_id = ANY(%(ids)s)
          AND NOT EXISTS (SELECT 1 FROM videos x WHERE x.video_id = d.video_id)
        """,
        {"ids": ids, "prefer_model": None, "prefer_prompt_version": None},
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def list_videos(
    conn: psycopg.Connection[Any],
    *,
    channel_id: str | None = None,
    status: str | None = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    sort: str = "published",
    order: str = "desc",
    sorts: Sequence[tuple[str, str]] | None = None,
    offset: int = 0,
    limit: int = 50,
    prefer_model: str | None = None,
    prefer_prompt_version: str | None = None,
) -> VideoPage:
    """One page of the library plus the total match count (#42).

    Filters combine with AND; any date filter excludes videos with no
    ``published_at``. ``published_after`` is inclusive, ``published_before``
    exclusive. Sort keys and directions must be validated values selected by
    the API. One statement, however large ``limit`` is.
    """
    sequence = tuple(sorts) if sorts is not None else ((sort, order),)
    fragments = [_SORT_ORDERS[pair] for pair in sequence]
    if any(key == "published" for key, _direction in sequence):
        fragments.append("discovered_at DESC")
    fragments.append("video_id ASC")
    order_by = ", ".join(fragments)
    rows = conn.execute(
        f"""
        WITH m AS (
            SELECT * FROM ({_VIDEO_READ}) r
            WHERE (%(channel)s::text IS NULL OR channel_id = %(channel)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
              AND (%(after)s::timestamptz IS NULL OR published_at >= %(after)s)
              AND (%(before)s::timestamptz IS NULL OR published_at < %(before)s)
        )
        SELECT t.total, p.*
        FROM (SELECT count(*) AS total FROM m) t
        LEFT JOIN LATERAL (
            SELECT * FROM m
            ORDER BY {order_by}
            OFFSET %(offset)s LIMIT %(limit)s
        ) p ON true
        """,
        {
            "channel": channel_id,
            "status": status,
            "after": published_after,
            "before": published_before,
            "offset": offset,
            "limit": limit,
            "prefer_model": prefer_model,
            "prefer_prompt_version": prefer_prompt_version,
        },
    ).fetchall()
    total = int(rows[0][0]) if rows else 0
    items = tuple(_summary(row[1:]) for row in rows if row[1] is not None)
    return VideoPage(items=items, total=total)


def get_video(
    conn: psycopg.Connection[Any],
    video_id: str,
    *,
    prefer_model: str | None = None,
    prefer_prompt_version: str | None = None,
) -> VideoSummary | None:
    """One video with its derived status, or ``None`` without a ``videos`` row (#42)."""
    row = conn.execute(
        f"{_VIDEO_READ} WHERE v.video_id = %(video_id)s",
        {
            "video_id": video_id,
            "prefer_model": prefer_model,
            "prefer_prompt_version": prefer_prompt_version,
        },
    ).fetchone()
    return None if row is None else _summary(row)


def _summary(row: Sequence[Any]) -> VideoSummary:
    (
        video_id, title, channel_id, channel_title, published_at, duration_sec, origin,
        unavailable, status, aj_id, aj_kind, aj_state, aj_cancel_requested_at, lf_id, lf_kind,
        lf_class, lf_finished, latest_analysis_at, _discovered_at, has_transcript,
        nc_id, nc_kind, nc_finished,
    ) = row
    return VideoSummary(
        video_id=video_id,
        title=title,
        channel_id=channel_id,
        channel_title=channel_title,
        published_at=published_at,
        duration_sec=duration_sec,
        origin=origin,
        unavailable=unavailable,
        status=status,
        active_job=(
            None
            if aj_id is None
            else ActiveJob(int(aj_id), aj_kind, aj_state, aj_cancel_requested_at)
        ),
        last_failure=(
            None if lf_id is None else JobFailure(int(lf_id), lf_kind, lf_class, lf_finished)
        ),
        latest_analysis_at=latest_analysis_at,
        has_transcript=has_transcript,
        last_cancelled=(
            None if nc_id is None else JobCancelled(int(nc_id), nc_kind, nc_finished)
        ),
    )

