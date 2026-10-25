"""The ``catalog_enrichment`` planner task (issue #242).

A channel catalog (``common.channel_catalog``) is stored with titles only. This
task fills in each row's description and publication date through the same
``MetadataSource`` ingest uses, one yt-dlp call per row, in bounded batches, so
a 5000-row catalog never makes anyone wait and never floods YouTube.

One tick enriches at most ``batch_size`` rows and stops starting new ones once
``CATALOG_ENRICH_BUDGET_SEC`` has passed on the injected monotonic ``clock``: the
planner runs its tasks one after another on one thread, so a slow YouTube must
not hold ``reap`` and the others behind it. Each row is claimed alone (``pending``,
or ``running`` and abandoned by a crashed planner) in one short transaction,
looked up with no transaction open, and its outcome recorded in another. A
failure is isolated to its row: the row ends ``failed`` with the exception class
name only (never upstream text), the rest of the batch goes on, and a person
retries it from the catalog page. Nothing is retried automatically, so a
persistent failure cannot hot-loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
import structlog
from psycopg.pq import TransactionStatus

from common.models import MetadataSource
from common.repo.channel_catalog import (
    claim_enrichment,
    complete_enrichment,
    fail_enrichment,
)

_log = structlog.get_logger(__name__)

#: One tick starts no new row after this long; below the 60 s REAP interval's patience.
CATALOG_ENRICH_BUDGET_SEC = 90.0
#: Per-row yt-dlp timeout: a row that hangs fails (``failed``, retryable by hand).
CATALOG_ENRICH_FETCH_TIMEOUT_SEC = 45.0


class ChannelMismatch(Exception):
    """The metadata names a different channel than the catalog row's."""


def run_catalog_enrichment(
    conn: psycopg.Connection[Any],
    metadata: MetadataSource,
    *,
    batch_size: int,
    budget_sec: float = CATALOG_ENRICH_BUDGET_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Enrich up to ``batch_size`` catalog rows; return how many completed.

    ``conn`` must have no open transaction, so no lock is held while yt-dlp runs.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("run_catalog_enrichment expects a connection with no open transaction")
    started = clock()
    completed = 0
    claimed = 0
    while claimed < batch_size and clock() - started < budget_sec:
        with conn.transaction():
            claims = claim_enrichment(conn, batch_size=1)
        if not claims:
            break
        claimed += 1
        claim = claims[0]
        try:
            meta = metadata.fetch(claim.video_id)
            if meta.channel_id != claim.channel_id or meta.video_id != claim.video_id:
                raise ChannelMismatch
        except Exception as exc:  # noqa: BLE001 - per-row isolation; SystemExit passes
            error_class = type(exc).__name__
            _log.warning(
                "catalog.enrich.failed",
                channel_id=claim.channel_id,
                video_id=claim.video_id,
                error_class=error_class,
            )
            with conn.transaction():
                fail_enrichment(conn, claim.channel_id, claim.video_id, error_class=error_class)
        else:
            with conn.transaction():
                completed += int(complete_enrichment(conn, meta))
    if claimed:
        _log.info("catalog.enrich", claimed=claimed, completed=completed)
    return completed
