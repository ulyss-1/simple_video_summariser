"""Operational routes (issue #39; architecture.md 8.3)."""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from common.repo.jobs import queue_depth
from services.api.deps import UNAVAILABLE_BODY, get_conn

router = APIRouter()
_log = structlog.get_logger(__name__)

KINDS = ("ingest", "transcribe", "analyze")
STATES = ("pending", "running", "done", "dead")

type QueueDepth = dict[str, dict[str, int]]


def _zero_filled(depth: dict[tuple[str, str], int]) -> QueueDepth:
    queue: QueueDepth = {kind: dict.fromkeys(STATES, 0) for kind in KINDS}
    for (kind, state), count in depth.items():
        queue.setdefault(kind, {})[state] = count
    return queue


@router.get("/healthz", response_model=None)
def healthz(
    conn: Annotated[psycopg.Connection[Any], Depends(get_conn)],
) -> dict[str, object] | JSONResponse:
    """Liveness plus queue depth by kind and state; 503 when the database is not usable."""
    try:
        conn.read_only = True
        depth = queue_depth(conn)
    except Exception:  # noqa: BLE001 - any database failure is "unavailable", never a 500
        _log.exception("healthz database check failed")
        return JSONResponse(status_code=503, content=UNAVAILABLE_BODY)
    return {"status": "ok", "queue": _zero_filled(depth)}
