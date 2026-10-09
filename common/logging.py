"""Structured logging (architecture.md 12).

Every service calls ``configure_logging(get_settings())`` once at startup.
After that, each log line is one JSON object on stdout (``LOG_FORMAT=json``)
or a human-readable line (``LOG_FORMAT=console``, local dev). Docker's
``json-file`` driver collects stdout; nothing here ships logs elsewhere.

structlog loggers and plain stdlib loggers (alembic, httpx, yt-dlp's
callers ...) share one root handler whose ``ProcessorFormatter`` renders
both, so library records come out in the same shape instead of as plain text.

Per-job context lives in ``contextvars``: wrap a job in
``bound_context(job_id=..., video_id=..., kind=..., attempt=...)`` and every
line emitted inside it, from any logger, carries those fields. A thread only
sees the context if it runs under ``contextvars.copy_context()``, e.g.
``Thread(target=contextvars.copy_context().run, args=(fn,))``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Iterator
from datetime import date, datetime, time
from typing import Any, TextIO

import structlog
from structlog.typing import Processor

from common.config import Settings


class _Handler(logging.StreamHandler[TextIO]):
    """The root handler this module installs; its type marks it for replacement."""


def _json_default(value: object) -> str:
    """Fallback for values ``json`` cannot encode: log them as strings."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _json_dumps(obj: Any, **kwargs: Any) -> str:
    kwargs["default"] = _json_default
    return json.dumps(obj, **kwargs)


def _adopt_uvicorn_loggers() -> None:
    """Make uvicorn's records flow to the root JSON handler.

    uvicorn's own logging config gives ``uvicorn`` and ``uvicorn.access``
    plain-text handlers with ``propagate = False``, so without this the API
    container prints non-JSON lines. This touches only logger objects by
    name: it imports nothing, so it is a no-op where uvicorn is absent
    (workers, migrate) and ``common`` keeps no dependency on a web framework.

    uvicorn applies its config before it imports the app, so this must run
    after that, at app import time or later; the API calls it on import.

    The access log is switched off: ``RequestContextMiddleware`` already logs
    one ``request`` line per request with its request id, and the compose
    healthcheck would otherwise add an access line every 30 seconds.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Route structlog and stdlib logging to ``stream`` (default stdout).

    Level and format come from ``settings.LOG_LEVEL`` and
    ``settings.LOG_FORMAT``. Safe to call again: the previous handler is
    replaced, never duplicated.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Run on stdlib records before the shared processors below.
    foreign_pre_chain: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        timestamper,
    ]

    target = stream if stream is not None else sys.stdout
    render: list[Processor]
    if settings.LOG_FORMAT == "json":
        # The traceback becomes a string field, which JSON keeps on one line.
        render = [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=_json_dumps),
        ]
    else:
        render = [structlog.dev.ConsoleRenderer(colors=target.isatty())]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *render,
        ],
    )

    handler = _Handler(target)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in root.handlers[:]:
        if isinstance(old, _Handler):
            root.removeHandler(old)
            old.close()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL)
    _adopt_uvicorn_loggers()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            # stdlib records arrive with stack_info already formatted.
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # A logger first used before configure_logging would otherwise keep
        # structlog's default config for the life of the process.
        cache_logger_on_first_use=False,
    )


@contextlib.contextmanager
def bound_context(**fields: Any) -> Iterator[None]:
    """Attach ``fields`` to every log line emitted inside the block.

    On exit, normal or by exception, each field returns to its previous
    value (or is removed), so nested blocks restore the outer context and
    nothing leaks onto the next job's lines.
    """
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
