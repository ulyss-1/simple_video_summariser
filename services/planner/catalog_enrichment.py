"""The ``catalog_enrichment`` planner task (issue #242).

A channel catalog (``common.channel_catalog``) is stored with titles only. This
task fills in each row's description and publication date through the same
``MetadataSource`` ingest uses, one yt-dlp call per row, in bounded batches, so
a 5000-row catalog never makes anyone wait and never floods YouTube.

One tick claims up to ``batch_size`` rows (``pending``, or ``running`` and
abandoned by a crashed planner) in one short transaction, then looks each up
with no transaction open, recording the outcome of every row in its own short
transaction. A failure is isolated to its row: the row ends ``failed`` with the
exception class name only (never upstream text), the rest of the batch goes on,
and a person retries it from the catalog page. Nothing is retried
automatically, so a persistent failure cannot hot-loop.
"""

from __future__ import annotations

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


class ChannelMismatch(Exception):
    """The metadata names a different channel than the catalog row's."""


def run_catalog_enrichment(
    conn: psycopg.Connection[Any], metadata: MetadataSource, *, batch_size: int
) -> int:
    """Enrich up to ``batch_size`` catalog rows; return how many completed.

    ``conn`` must have no open transaction, so no lock is held while yt-dlp runs.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("run_catalog_enrichment expects a connection with no open transaction")
    with conn.transaction():
        claims = claim_enrichment(conn, batch_size=batch_size)
    completed = 0
    for claim in claims:
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
    if claims:
        _log.info("catalog.enrich", claimed=len(claims), completed=completed)
    return completed
