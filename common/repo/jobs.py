"""Job read helpers (issue #28, architecture.md §2).

Writes go through the ``JobQueue`` port; this module only reads what a
handler needs to decide what to do next.
"""

from __future__ import annotations

from typing import Any

import psycopg


def latest_job_state(conn: psycopg.Connection[Any], video_id: str, kind: str) -> str | None:
    """State of the newest ``kind`` job for ``video_id``, or ``None`` if there is none.

    "Newest" is by ``created_at`` then ``id``, so a pending job created after a
    dead one (an operator retry) is the latest.
    """
    row = conn.execute(
        """
        SELECT state FROM jobs
        WHERE video_id = %s AND kind = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (video_id, kind),
    ).fetchone()
    return None if row is None else str(row[0])
