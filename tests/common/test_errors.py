import ast
import errno
import json
import logging
import pathlib
import socket
import traceback
from datetime import UTC, datetime, timedelta

import pytest

import common.errors
from common.errors import (
    MAX_ERROR_BYTES,
    BugError,
    Cancelled,
    Defer,
    ErrorClass,
    JobError,
    LLMInvalidOutputError,
    LLMUnavailableError,
    PermanentSourceError,
    RateLimitedError,
    ResourceError,
    RetryPolicy,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
    classify,
    format_error,
    log_dead_letter,
    policy,
)

# --- taxonomy -------------------------------------------------------------


def test_error_class_has_exactly_the_eight_stored_values() -> None:
    assert [c.value for c in ErrorClass] == [
        "PERMANENT_SOURCE",
        "TRANSIENT_NETWORK",
        "RATE_LIMITED",
        "TOOL_FAILURE",
        "LLM_INVALID_OUTPUT",
        "LLM_UNAVAILABLE",
        "RESOURCE",
        "BUG",
    ]
    # StrEnum: the member is the string jobs.error_class stores.
    assert ErrorClass.TOOL_FAILURE == "TOOL_FAILURE"


def _job_error_for(cls: ErrorClass) -> JobError:
    match cls:
        case ErrorClass.PERMANENT_SOURCE:
            return PermanentSourceError("removed")
        case ErrorClass.TRANSIENT_NETWORK:
            return TransientNetworkError()
        case ErrorClass.RATE_LIMITED:
            return RateLimitedError()
        case ErrorClass.TOOL_FAILURE:
            return ToolFailureError()
        case ErrorClass.LLM_INVALID_OUTPUT:
            return LLMInvalidOutputError()
        case ErrorClass.LLM_UNAVAILABLE:
            return LLMUnavailableError()
        case ErrorClass.RESOURCE:
            return ResourceError()
        case ErrorClass.BUG:
            return BugError()


def test_there_is_exactly_one_job_error_subclass_per_class() -> None:
    subclasses = JobError.__subclasses__()
    assert sorted(s.error_class for s in subclasses) == sorted(ErrorClass)


@pytest.mark.parametrize("cls", list(ErrorClass))
def test_classify_returns_the_class_of_every_job_error(cls: ErrorClass) -> None:
    assert classify(_job_error_for(cls)) is cls


@pytest.mark.parametrize("reason", ["removed", "private", "geoblocked", "agegated"])
def test_permanent_source_error_carries_its_unavailable_reason(reason: str) -> None:
    exc = PermanentSourceError(reason, "Private video")
    assert exc.reason == reason
    assert isinstance(exc.reason, UnavailableReason)
    assert str(exc) == "Private video"


def test_permanent_source_error_rejects_an_unknown_reason() -> None:
    with pytest.raises(ValueError):
        PermanentSourceError("deleted")


def test_rate_limited_error_retry_after_is_optional() -> None:
    assert RateLimitedError("HTTP Error 429").retry_after_sec is None
    assert RateLimitedError(retry_after_sec=0).retry_after_sec == 0
    assert RateLimitedError(retry_after_sec=120).retry_after_sec == 120


def test_rate_limited_error_rejects_a_negative_retry_after() -> None:
    with pytest.raises(ValueError):
        RateLimitedError(retry_after_sec=-1)


# --- policy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (
            ErrorClass.PERMANENT_SOURCE,
            RetryPolicy(retry=False, max_attempts=None, min_backoff=None, alert=False),
        ),
        (
            ErrorClass.TRANSIENT_NETWORK,
            RetryPolicy(retry=True, max_attempts=None, min_backoff=None, alert=False),
        ),
        (
            ErrorClass.RATE_LIMITED,
            RetryPolicy(
                retry=True,
                max_attempts=None,
                min_backoff=timedelta(minutes=30),
                alert=False,
            ),
        ),
        (
            ErrorClass.TOOL_FAILURE,
            RetryPolicy(retry=True, max_attempts=3, min_backoff=None, alert=True),
        ),
        (
            ErrorClass.LLM_INVALID_OUTPUT,
            RetryPolicy(retry=False, max_attempts=None, min_backoff=None, alert=False),
        ),
        (
            ErrorClass.LLM_UNAVAILABLE,
            RetryPolicy(retry=True, max_attempts=None, min_backoff=None, alert=False),
        ),
        (
            ErrorClass.RESOURCE,
            RetryPolicy(retry=False, max_attempts=None, min_backoff=None, alert=True),
        ),
        (
            ErrorClass.BUG,
            RetryPolicy(retry=False, max_attempts=None, min_backoff=None, alert=False),
        ),
    ],
)
def test_policy_matches_the_architecture_table(
    cls: ErrorClass, expected: RetryPolicy
) -> None:
    assert policy(cls) == expected


def test_a_removed_video_dead_letters_on_its_first_attempt() -> None:
    exc = PermanentSourceError("removed", "This video has been removed")
    assert policy(classify(exc)).retry is False


# --- classify fallbacks for raw exceptions ---------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionError("reset"),
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        OSError(errno.ETIMEDOUT, "Connection timed out"),  # constructs a TimeoutError
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
    ],
    ids=repr,
)
def test_raw_network_errors_classify_as_transient_network(exc: BaseException) -> None:
    assert classify(exc) is ErrorClass.TRANSIENT_NETWORK


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.ENOSPC, "No space left on device"),
        OSError(errno.EDQUOT, "Disk quota exceeded"),
        MemoryError(),
    ],
    ids=repr,
)
def test_raw_resource_exhaustion_classifies_as_resource(exc: BaseException) -> None:
    assert classify(exc) is ErrorClass.RESOURCE


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad"),
        KeyError("x"),
        RuntimeError("boom"),
        AssertionError(),
        OSError(errno.EIO, "Input/output error"),  # an OSError, but not ENOSPC/EDQUOT
        OSError("no errno at all"),
        FileNotFoundError(errno.ENOENT, "No such file or directory"),
        # Raw decode errors are BUG; the yt-dlp adapter wraps them as TOOL_FAILURE.
        json.JSONDecodeError("Expecting value", "", 0),
    ],
    ids=repr,
)
def test_any_other_raw_exception_classifies_as_bug(exc: BaseException) -> None:
    assert classify(exc) is ErrorClass.BUG


def test_raw_pydantic_validation_error_classifies_as_bug() -> None:
    # pydantic is not a dependency yet (it arrives with pydantic-settings, #5).
    # This runs once it is installed; classify itself must not import pydantic.
    pydantic = pytest.importorskip("pydantic")

    with pytest.raises(pydantic.ValidationError) as info:
        pydantic.TypeAdapter(int).validate_python("not a number")
    assert classify(info.value) is ErrorClass.BUG


def test_classify_uses_the_outer_exception_not_its_cause() -> None:
    with pytest.raises(ValueError) as outer:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            raise ValueError("parse failed") from inner
    assert classify(outer.value) is ErrorClass.BUG

    with pytest.raises(TransientNetworkError) as wrapped:
        try:
            raise ValueError("bad bytes")
        except ValueError as inner:
            raise TransientNetworkError("connection dropped") from inner
    assert classify(wrapped.value) is ErrorClass.TRANSIENT_NETWORK


@pytest.mark.parametrize(
    "exc", [Defer(datetime(2026, 1, 1, tzinfo=UTC)), Cancelled()], ids=repr
)
def test_classify_refuses_control_flow_exceptions(exc: BaseException) -> None:
    with pytest.raises(TypeError):
        classify(exc)


# --- control flow ---------------------------------------------------------


def test_defer_carries_the_time_to_run_again() -> None:
    until = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    exc = Defer(until)
    assert exc.until == until
    assert not isinstance(exc, JobError)


def test_cancelled_is_not_a_job_error() -> None:
    assert not isinstance(Cancelled(), JobError)


# --- format_error ---------------------------------------------------------


def _unraised(message_len: int) -> ValueError:
    # An exception that was never raised formats as "ValueError: <msg>\n".
    return ValueError("x" * message_len)


def _formatted_len(message_len: int) -> int:
    return len(f"ValueError: {'x' * message_len}\n".encode())


def test_format_error_returns_the_full_traceback_when_short() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        text = format_error(exc)
    assert text.startswith("Traceback (most recent call last):")
    assert "test_format_error_returns_the_full_traceback_when_short" in text
    assert text.endswith("RuntimeError: boom\n")


def test_format_error_is_unchanged_at_exactly_the_cap() -> None:
    n = MAX_ERROR_BYTES - _formatted_len(0)
    assert _formatted_len(n) == MAX_ERROR_BYTES
    assert format_error(_unraised(n)) == f"ValueError: {'x' * n}\n"


def test_format_error_truncates_one_byte_past_the_cap_keeping_the_end() -> None:
    n = MAX_ERROR_BYTES - _formatted_len(0) + 1
    text = format_error(_unraised(n))
    assert len(text.encode()) <= MAX_ERROR_BYTES
    assert text.endswith("x\n")
    assert "ValueError: " not in text  # the head is what was dropped


def _fail_after_a_long_cause() -> None:
    try:
        raise ValueError("noise " * 5000)
    except ValueError as cause:
        raise RuntimeError("the failing frame") from cause


def test_format_error_keeps_the_failing_frame_of_a_long_traceback() -> None:
    try:
        _fail_after_a_long_cause()
    except RuntimeError as exc:
        full = "".join(traceback.format_exception(exc))
        text = format_error(exc)
    assert len(full.encode()) > MAX_ERROR_BYTES
    assert len(text.encode()) <= MAX_ERROR_BYTES
    assert text.endswith("RuntimeError: the failing frame\n")
    assert 'raise RuntimeError("the failing frame")' in text
    assert not text.startswith("Traceback")


def test_format_error_does_not_split_a_multibyte_character() -> None:
    exc = ValueError("é" * MAX_ERROR_BYTES)  # 2 bytes each, far past the cap
    text = format_error(exc)
    assert len(text.encode()) <= MAX_ERROR_BYTES
    assert text.endswith("é\n")
    assert "�" not in text


# --- dead-letter logging --------------------------------------------------


@pytest.mark.parametrize("cls", [c for c in ErrorClass if policy(c).alert], ids=str)
def test_dead_letter_of_an_alerting_class_logs_critical_with_alert(
    cls: ErrorClass, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("test.dead_letter")
    with caplog.at_level(logging.DEBUG, logger="test.dead_letter"):
        log_dead_letter(_job_error_for(cls), logger=logger, job_id=7)
    [record] = caplog.records
    assert record.levelno == logging.CRITICAL
    assert record.__dict__["alert"] is True
    assert record.__dict__["error_class"] == cls
    assert record.__dict__["job_id"] == 7


@pytest.mark.parametrize("cls", [c for c in ErrorClass if not policy(c).alert], ids=str)
def test_dead_letter_of_a_quiet_class_does_not_alert(
    cls: ErrorClass, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("test.dead_letter")
    with caplog.at_level(logging.DEBUG, logger="test.dead_letter"):
        log_dead_letter(_job_error_for(cls), logger=logger)
    [record] = caplog.records
    assert record.levelno < logging.CRITICAL
    assert record.__dict__["alert"] is False


def test_the_alerting_classes_are_tool_failure_and_resource() -> None:
    assert {c for c in ErrorClass if policy(c).alert} == {
        ErrorClass.TOOL_FAILURE,
        ErrorClass.RESOURCE,
    }


def test_dead_letter_of_a_raw_disk_full_error_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.dead_letter")
    with caplog.at_level(logging.DEBUG, logger="test.dead_letter"):
        log_dead_letter(OSError(errno.ENOSPC, "No space left on device"), logger=logger)
    [record] = caplog.records
    assert record.levelno == logging.CRITICAL
    assert record.__dict__["alert"] is True


# --- import direction -----------------------------------------------------


def _imported_modules(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_common_errors_imports_no_adapter_yt_dlp_or_pydantic() -> None:
    imported = _imported_modules(pathlib.Path(common.errors.__file__))
    top_level = {name.split(".")[0] for name in imported}
    assert not top_level & {"adapters", "services", "yt_dlp", "pydantic"}
