"""Stale job reaper (issue #35; architecture.md §0 C2, §5 "Reaping").

``reap_stale_jobs`` is one pass, called by the planner loop (#38) on each
tick: it hands back to the queue every ``running`` job whose worker stopped
heartbeating. Staleness is judged by ``heartbeat_at`` alone, never by total
runtime, so a long transcription with a live heartbeat is never touched.

There is no loop and no sleep here, and a database error propagates
unchanged: scheduling and keeping the planner alive belong to #38.
"""

from __future__ import annotations

import logging

from common.config import Settings
from common.queue import JobQueue

_logger = logging.getLogger(__name__)


def reap_stale_jobs(queue: JobQueue, settings: Settings) -> int:
    """Run one reaper pass and return how many jobs were reaped."""
    reaped = queue.reap_stale()
    # An idle system reaps nothing on every tick, so that line is DEBUG.
    _logger.log(
        logging.INFO if reaped else logging.DEBUG,
        "planner.reap",
        extra={"reaped": reaped, "threshold_sec": settings.REAP_AFTER_SEC},
    )
    return reaped
