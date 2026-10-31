"""``build_registry`` hands ``SWEEP_MAX_JOBS_ANTHROPIC`` to the sweep (issue #99).

The cap is a spend control on a paid summarizer. ``run_reanalysis_sweep`` falls
back to a default cap when the kwarg is missing, so dropping the wiring in
``build_registry`` would leave every sweep test green and silently ignore the
operator's setting. Needs Postgres.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from common.config import Settings
from services.planner.main import build_registry
from tests.services.planner.test_sweep import VERSION, _ready

pytestmark = pytest.mark.integration


def test_the_registered_sweep_task_respects_the_configured_cap(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    for i in range(6):
        _ready(conn, f"c{i:03d}", age_days=i)
    conn.commit()
    # A cap below the sweep's own default (100), so only the wiring can produce 2.
    settings = Settings(
        DATABASE_URL=SecretStr(head_dsn),
        PROMPT_VERSION=VERSION,
        SUMMARIZER="anthropic",
        SWEEP_MAX_JOBS_ANTHROPIC=2,
    )
    task = {t.name: t for t in build_registry(settings, lambda: psycopg.connect(head_dsn))}[
        "reanalysis_sweep"
    ]

    first = task.fn()
    second = task.fn()

    assert (first, second) == (2, 0)


def test_the_registered_sweep_task_respects_the_configured_usd_budget(
    conn: psycopg.Connection[Any], head_dsn: str
) -> None:
    for i in range(3):
        _ready(conn, f"c{i:03d}", age_days=i)
    conn.commit()
    # Budget 0 enqueues nothing whatever the job cap; only the wiring can produce that (#153).
    settings = Settings(
        DATABASE_URL=SecretStr(head_dsn),
        PROMPT_VERSION=VERSION,
        SUMMARIZER="anthropic",
        SWEEP_MAX_JOBS_ANTHROPIC=50,
        SWEEP_MAX_USD_ANTHROPIC=0,
    )
    task = {t.name: t for t in build_registry(settings, lambda: psycopg.connect(head_dsn))}[
        "reanalysis_sweep"
    ]

    assert task.fn() == 0
