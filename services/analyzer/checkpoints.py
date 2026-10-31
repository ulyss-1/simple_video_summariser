"""The analyze job's ``JobScope``: its Anthropic batch checkpoints (issue #89).

Built from the job's own connection, so the adapter never touches the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg

from common.errors import BugError
from common.models import BatchCheckpoint, Usage
from common.repo.llm_batches import (
    delete_checkpoint,
    delete_job_checkpoints,
    get_checkpoint,
    insert_checkpoint,
    record_usage,
)


class JobCheckpoints:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        job_id: int,
        cancelled: Callable[[], bool],
        *,
        video_id: str,
        model: str,
    ) -> None:
        self._conn = conn
        self.job_id = job_id
        self._cancelled = cancelled
        self._video_id = video_id
        self._model = model
        #: Batch usage this attempt counted in memory (#252): what ``record_usage`` returned.
        self.counted: list[Usage] = []

    def cancelled(self) -> bool:
        return self._cancelled()

    def now(self) -> datetime:
        with self._conn.transaction():
            row = self._conn.execute("SELECT now()").fetchone()
        if row is None:
            raise BugError("SELECT now() returned no row")
        value: datetime = row[0]
        return value

    def checkpoint(self, step_key: str) -> BatchCheckpoint | None:
        return get_checkpoint(self._conn, self.job_id, step_key)

    def record(self, step_key: str, batch_id: str, request_sha256: str) -> BatchCheckpoint:
        return insert_checkpoint(self._conn, self.job_id, step_key, batch_id, request_sha256)

    def forget(self, step_key: str) -> None:
        delete_checkpoint(
            self._conn, self.job_id, step_key, video_id=self._video_id, model=self._model
        )

    def record_usage(self, step_key: str, usage: Usage) -> Usage:
        stored = record_usage(self._conn, self.job_id, step_key, usage)
        self.counted.append(stored)
        return stored

    def clear(self) -> None:
        delete_job_checkpoints(self._conn, self.job_id)
