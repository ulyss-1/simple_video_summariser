"""Tests for ``common.logging`` (issue #6, architecture.md 12).

Output goes to an in-memory stream passed to ``configure_logging``; the
autouse fixture restores the root logger, structlog's defaults and the bound
context after every test so nothing leaks between tests.
"""

import contextvars
import io
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog

from common.config import Settings
from common.errors import ToolFailureError, log_dead_letter
from common.logging import bound_context, configure_logging

JOB_FIELDS = ("job_id", "video_id", "kind", "attempt")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def setup(level: str = "INFO", fmt: str = "json") -> io.StringIO:
    """Configure logging into a fresh in-memory stream and return it."""
    stream = io.StringIO()
    settings = Settings(
        DATABASE_URL="postgresql://u:p@h/db",  # type: ignore[arg-type]
        LOG_LEVEL=level,  # type: ignore[arg-type]
        LOG_FORMAT=fmt,  # type: ignore[arg-type]
    )
    configure_logging(settings, stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    """Every output line parsed as JSON; fails if any line is not JSON."""
    parsed = [json.loads(line) for line in lines(stream)]
    assert all(isinstance(r, dict) for r in parsed)
    return parsed


def log() -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger("test")


# --- JSON shape ---------------------------------------------------------------


def test_json_record_is_one_line_with_required_fields() -> None:
    stream = setup()
    log().info("hello", answer=42)

    [line] = lines(stream)
    record = json.loads(line)
    assert record["event"] == "hello"
    assert record["level"] == "info"
    assert record["logger"] == "test"
    assert record["answer"] == 42


def test_json_timestamp_is_iso8601_utc() -> None:
    stream = setup()
    log().info("hello")

    [record] = records(stream)
    ts = datetime.fromisoformat(record["timestamp"])
    assert ts.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - ts) < timedelta(minutes=5)


def test_console_format_is_human_readable_not_json() -> None:
    stream = setup(fmt="console")
    log().info("hello world", answer=42)

    [line] = lines(stream)
    assert "hello world" in line
    assert "answer" in line and "42" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


# --- Levels -------------------------------------------------------------------


def test_info_level_drops_debug_from_structlog_and_stdlib() -> None:
    stream = setup(level="INFO")
    log().debug("hidden")
    logging.getLogger("lib").debug("hidden too")
    log().info("shown")

    assert [r["event"] for r in records(stream)] == ["shown"]


def test_debug_level_emits_debug() -> None:
    stream = setup(level="DEBUG")
    log().debug("shown")
    logging.getLogger("lib").debug("shown too")

    assert [r["event"] for r in records(stream)] == ["shown", "shown too"]


def test_warning_level_drops_info() -> None:
    stream = setup(level="WARNING")
    log().info("hidden")
    log().warning("shown")

    assert [r["event"] for r in records(stream)] == ["shown"]


# --- bound_context ------------------------------------------------------------


def test_bound_context_fields_appear_on_every_line() -> None:
    stream = setup()
    with bound_context(job_id=1, video_id="abc", kind="ingest", attempt=2):
        log().info("one")
        log().warning("two")
        logging.getLogger("lib").error("three")

    output = records(stream)
    assert [r["event"] for r in output] == ["one", "two", "three"]
    for record in output:
        assert record["job_id"] == 1
        assert record["video_id"] == "abc"
        assert record["kind"] == "ingest"
        assert record["attempt"] == 2


def test_context_does_not_leak_into_the_next_job() -> None:
    stream = setup()
    with bound_context(job_id=1, video_id="job1video", kind="ingest", attempt=1):
        log().info("job 1 running")
    with bound_context(job_id=2, video_id="job2video", kind="analyze", attempt=1):
        log().info("job 2 running")

    job1, job2 = records(stream)
    assert job1["video_id"] == "job1video"
    assert job2["video_id"] == "job2video"
    assert "job1video" not in lines(stream)[1]


def test_context_is_removed_after_normal_exit() -> None:
    stream = setup()
    with bound_context(job_id=1, video_id="abc", kind="ingest", attempt=2):
        pass
    log().info("after")

    [record] = records(stream)
    for field in JOB_FIELDS:
        assert field not in record


def test_context_is_removed_after_exception() -> None:
    stream = setup()
    with (
        pytest.raises(RuntimeError),
        bound_context(job_id=1, video_id="abc", kind="ingest", attempt=2),
    ):
        raise RuntimeError("handler blew up")
    log().info("after")

    [record] = records(stream)
    for field in JOB_FIELDS:
        assert field not in record


def test_nested_context_adds_fields_and_restores_outer() -> None:
    stream = setup()
    with bound_context(job_id=1, video_id="abc", kind="ingest", attempt=2):
        with bound_context(chunk=3, attempt=5):
            log().info("inner")
        log().info("outer again")
    log().info("outside")

    inner, outer, outside = records(stream)
    assert inner["job_id"] == 1 and inner["video_id"] == "abc"
    assert inner["chunk"] == 3 and inner["attempt"] == 5
    assert outer["attempt"] == 2
    assert "chunk" not in outer
    assert "attempt" not in outside and "chunk" not in outside


def test_nested_context_restores_outer_after_inner_exception() -> None:
    stream = setup()
    with bound_context(job_id=1, attempt=2):
        with pytest.raises(ValueError), bound_context(attempt=9, chunk=1):
            raise ValueError("x")
        log().info("outer")

    [record] = records(stream)
    assert record["attempt"] == 2 and record["job_id"] == 1
    assert "chunk" not in record


def test_explicit_kwargs_on_a_call_are_kept_alongside_context() -> None:
    stream = setup()
    with bound_context(job_id=1):
        log().info("x", duration_ms=12)

    [record] = records(stream)
    assert record["job_id"] == 1 and record["duration_ms"] == 12


def test_context_reaches_thread_run_under_copy_context() -> None:
    stream = setup()

    def heartbeat() -> None:
        log().info("heartbeat")
        logging.getLogger("worker").info("stdlib heartbeat")

    with bound_context(job_id=7, video_id="abc", kind="transcribe", attempt=1):
        ctx = contextvars.copy_context()
        thread = threading.Thread(target=ctx.run, args=(heartbeat,))
        thread.start()
        thread.join()

    beat, stdlib_beat = records(stream)
    for record in (beat, stdlib_beat):
        assert record["job_id"] == 7
        assert record["video_id"] == "abc"
        assert record["kind"] == "transcribe"
        assert record["attempt"] == 1


# --- stdlib bridging ----------------------------------------------------------


def test_stdlib_logger_gets_json_shape_and_bound_fields() -> None:
    stream = setup()
    with bound_context(job_id=1, video_id="abc", kind="ingest", attempt=2):
        logging.getLogger("alembic").info("running %s", "upgrade")

    [record] = records(stream)
    assert record["event"] == "running upgrade"
    assert record["logger"] == "alembic"
    assert record["level"] == "info"
    datetime.fromisoformat(record["timestamp"])
    assert record["job_id"] == 1
    assert record["video_id"] == "abc"
    assert record["kind"] == "ingest"
    assert record["attempt"] == 2


def test_stdlib_extra_fields_are_rendered() -> None:
    stream = setup()
    logging.getLogger("lib").warning("with extra", extra={"size": 3})

    [record] = records(stream)
    assert record["size"] == 3


def test_dead_letter_log_from_errors_module_renders_as_json() -> None:
    stream = setup()
    with bound_context(job_id=5, video_id="abc", kind="ingest", attempt=3):
        log_dead_letter(ToolFailureError("extractor broke"))

    [record] = records(stream)
    assert record["level"] == "critical"
    assert record["logger"] == "common.errors"
    assert record["error_class"] == "TOOL_FAILURE"
    assert record["alert"] is True
    assert record["job_id"] == 5


# --- Exceptions ---------------------------------------------------------------


def test_structlog_exception_is_one_json_line_with_traceback() -> None:
    stream = setup()
    try:
        raise ValueError("boom")
    except ValueError:
        log().exception("failed")

    [line] = lines(stream)
    record = json.loads(line)
    assert record["event"] == "failed"
    assert record["level"] == "error"
    assert "Traceback" in record["exception"]
    assert "ValueError: boom" in record["exception"]


def test_stdlib_exception_is_one_json_line_with_traceback() -> None:
    stream = setup()
    try:
        raise KeyError("missing")
    except KeyError:
        logging.getLogger("lib").exception("lib failed")

    [line] = lines(stream)
    record = json.loads(line)
    assert record["event"] == "lib failed"
    assert "Traceback" in record["exception"]
    assert "KeyError" in record["exception"]


# --- Serialization ------------------------------------------------------------


def test_non_json_values_are_logged_as_strings() -> None:
    stream = setup()
    when = datetime(2026, 9, 27, 10, 30, tzinfo=UTC)
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    log().info(
        "values",
        path=Path("/data/audio/x.opus"),
        when=when,
        amount=Decimal("1.10"),
        ident=ident,
    )

    [record] = records(stream)
    assert record["path"] == "/data/audio/x.opus"
    assert record["when"] == when.isoformat()
    assert record["amount"] == "1.10"
    assert record["ident"] == str(ident)


def test_non_json_values_in_context_and_stdlib_extra_are_strings() -> None:
    stream = setup()
    with bound_context(audio=Path("/a.opus")):
        logging.getLogger("lib").info("x", extra={"cost": Decimal("0.5")})

    [record] = records(stream)
    assert record["audio"] == "/a.opus"
    assert record["cost"] == "0.5"


def test_non_json_values_do_not_raise_in_console_format() -> None:
    stream = setup(fmt="console")
    log().info("values", path=Path("/x"), amount=Decimal("2.5"))

    assert "/x" in stream.getvalue()


# --- Reconfiguration ----------------------------------------------------------


def test_configuring_twice_does_not_duplicate_lines() -> None:
    setup()
    stream = setup()
    log().info("once")
    logging.getLogger("lib").info("once too")

    assert [r["event"] for r in records(stream)] == ["once", "once too"]


def test_reconfiguring_applies_the_new_level_and_format() -> None:
    first = setup(level="INFO", fmt="json")
    second = setup(level="DEBUG", fmt="console")
    log().debug("dbg")

    assert first.getvalue() == ""
    [line] = lines(second)
    assert "dbg" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
