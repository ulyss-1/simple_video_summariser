"""Error taxonomy (architecture.md 8.4).

Every job failure is sorted into one of eight classes, and each class carries a
retry policy. The class string is what ``jobs.error_class`` stores. A ninth,
``CANCELLED`` (#237), is not a failure: it is the outcome of a job a user
cancelled, written by the queue and never returned by ``classify``.

Adapters translate what they know about their tool into a ``JobError``
subclass; anything else is classified by ``classify`` from its exception type
alone. Nothing here imports an adapter or a tool library (AGENTS.md, import
direction).
"""

from __future__ import annotations

import errno
import logging
import socket
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from common.safe_url import redact

#: ``last_error`` of a cancelled job (#237): fixed, so it carries no user input.
CANCELLED_LAST_ERROR = "cancelled on request"

#: ``format_error`` output never exceeds this many UTF-8 bytes.
MAX_ERROR_BYTES = 8 * 1024

_TRUNCATED_MARKER = "[... traceback truncated, showing the end ...]\n"

_logger = logging.getLogger(__name__)


class ErrorClass(StrEnum):
    PERMANENT_SOURCE = "PERMANENT_SOURCE"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    RATE_LIMITED = "RATE_LIMITED"
    TOOL_FAILURE = "TOOL_FAILURE"
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    RESOURCE = "RESOURCE"
    BUG = "BUG"
    #: A user cancelled the job (#237). Written by the queue only; no exception
    #: classifies to it.
    CANCELLED = "CANCELLED"


class UnavailableReason(StrEnum):
    """Why a source video cannot be processed; stored in ``videos.unavailable``."""

    REMOVED = "removed"
    PRIVATE = "private"
    GEOBLOCKED = "geoblocked"
    AGEGATED = "agegated"
    #: A policy skip (SKIP_SHORTS, #80), not a source failure: never raised as a
    #: ``PermanentSourceError``.
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How the queue treats a failure of one class.

    ``max_attempts`` counts the first try. ``None`` means the job kind's own
    default (architecture.md 5, Backoff) when ``retry`` is true, and is
    meaningless when it is false: the job dead-letters on this attempt.
    ``min_backoff`` is a floor under the kind's exponential backoff.
    """

    retry: bool
    max_attempts: int | None
    min_backoff: timedelta | None
    alert: bool


_NO_RETRY = RetryPolicy(retry=False, max_attempts=None, min_backoff=None, alert=False)
_KIND_DEFAULT_RETRY = RetryPolicy(
    retry=True, max_attempts=None, min_backoff=None, alert=False
)

_POLICIES: dict[ErrorClass, RetryPolicy] = {
    ErrorClass.PERMANENT_SOURCE: _NO_RETRY,
    ErrorClass.TRANSIENT_NETWORK: _KIND_DEFAULT_RETRY,
    ErrorClass.RATE_LIMITED: RetryPolicy(
        retry=True, max_attempts=None, min_backoff=timedelta(minutes=30), alert=False
    ),
    # First try plus two retries, whatever the kind: a broken extractor means
    # "go update yt-dlp" (architecture.md 16.11).
    ErrorClass.TOOL_FAILURE: RetryPolicy(
        retry=True, max_attempts=3, min_backoff=None, alert=True
    ),
    # The summarizer adapter has already made its one repair attempt (#22).
    ErrorClass.LLM_INVALID_OUTPUT: _NO_RETRY,
    ErrorClass.LLM_UNAVAILABLE: _KIND_DEFAULT_RETRY,
    # Retrying a full disk or an OOM makes it worse.
    ErrorClass.RESOURCE: RetryPolicy(
        retry=False, max_attempts=None, min_backoff=None, alert=True
    ),
    ErrorClass.BUG: _NO_RETRY,
    ErrorClass.CANCELLED: _NO_RETRY,
}


def policy(cls: ErrorClass) -> RetryPolicy:
    return _POLICIES[cls]


class JobError(Exception):
    """A failure whose class is known. Subclass per ``ErrorClass``, one each."""

    error_class: ClassVar[ErrorClass]


class PermanentSourceError(JobError):
    """The source video cannot be fetched and never will be: no retry."""

    error_class = ErrorClass.PERMANENT_SOURCE

    def __init__(self, reason: UnavailableReason | str, message: str = "") -> None:
        super().__init__(message)
        self.reason = UnavailableReason(reason)


class TransientNetworkError(JobError):
    error_class = ErrorClass.TRANSIENT_NETWORK


class RateLimitedError(JobError):
    error_class = ErrorClass.RATE_LIMITED

    def __init__(
        self, message: str = "", *, retry_after_sec: int | None = None
    ) -> None:
        if retry_after_sec is not None and retry_after_sec < 0:
            raise ValueError(f"retry_after_sec must be >= 0, got {retry_after_sec}")
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class ToolFailureError(JobError):
    error_class = ErrorClass.TOOL_FAILURE


class LLMInvalidOutputError(JobError):
    """Raised only by the summarizer adapter, after its repair attempt failed."""

    error_class = ErrorClass.LLM_INVALID_OUTPUT


class LLMUnavailableError(JobError):
    error_class = ErrorClass.LLM_UNAVAILABLE


class ResourceError(JobError):
    error_class = ErrorClass.RESOURCE


class BugError(JobError):
    error_class = ErrorClass.BUG


class ControlFlow(Exception):
    """Raised by a handler to steer the queue (#11). Not a failure."""


class Defer(ControlFlow):
    """Put the job back to run again at ``until``, without spending an attempt."""

    def __init__(self, until: datetime) -> None:
        super().__init__(until)
        self.until = until


class Cancelled(ControlFlow):
    """The job was cancelled while running."""


_RESOURCE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


def classify(exc: BaseException) -> ErrorClass:
    """Return the class of ``exc`` from its own type only.

    ``__cause__`` is not searched: ``raise X from Y`` is classified by ``X``,
    because whoever raised ``X`` decided what the failure means.
    """
    if isinstance(exc, ControlFlow):
        raise TypeError(
            f"{type(exc).__name__} is queue control flow, not a failure; handle it before classify"
        )
    if isinstance(exc, JobError):
        return exc.error_class
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return ErrorClass.TRANSIENT_NETWORK
    if isinstance(exc, MemoryError):
        return ErrorClass.RESOURCE
    if isinstance(exc, OSError) and exc.errno in _RESOURCE_ERRNOS:
        return ErrorClass.RESOURCE
    return ErrorClass.BUG


def format_error(exc: BaseException) -> str:
    """Redacted traceback text for ``jobs.last_error``, at most ``MAX_ERROR_BYTES``.

    Secrets are redacted (``common.safe_url.redact``, issue #196) before the
    text is truncated.

    When it is longer, the start is dropped: the end holds the failing frame
    and the exception message.
    """
    # Redact the whole text first: a secret straddling the cut must not survive.
    text = redact("".join(traceback.format_exception(exc)))
    data = text.encode("utf-8", "backslashreplace")
    if len(data) <= MAX_ERROR_BYTES:
        return text
    budget = MAX_ERROR_BYTES - len(_TRUNCATED_MARKER.encode())
    # A cut inside a multi-byte character leaves a partial lead; drop it.
    tail = data[-budget:].decode("utf-8", "ignore")
    return _TRUNCATED_MARKER + tail


def log_dead_letter(
    *,
    job_id: int,
    kind: str,
    error_class: ErrorClass | str | None,
    last_error: str,
    logger: logging.Logger | None = None,
    **context: object,
) -> None:
    """Log that a job dead-lettered (issue #193; architecture.md 8.4 / 15).

    The one dead-letter logging path, for every site that sets ``state='dead'``.
    Takes the fields stored on the row, not an exception: two sites have none.
    ``error_class`` is the row value (``None`` for a reaped job, #114). Alerting
    classes (``policy(cls).alert``) log at CRITICAL with ``alert=True``; the
    rest, including ``None``, log at ERROR with ``alert=False``. ``last_error``
    is redacted again here (``common.safe_url.redact``, idempotent), and is
    attached to the record only, never interpolated into the message.
    ``context`` (video id ...) is attached to the record.
    """
    cls = ErrorClass(error_class) if error_class is not None else None
    alert = policy(cls).alert if cls is not None else False
    (logger or _logger).log(
        logging.CRITICAL if alert else logging.ERROR,
        "job dead-lettered",
        extra={
            **context,
            "job_id": job_id,
            "kind": kind,
            "error_class": cls,
            "last_error": redact(last_error),
            "alert": alert,
        },
    )


def log_job_cancelled(*, job_id: int, video_id: str, kind: str) -> None:
    """The one ``job.cancelled`` line (#237): INFO, never through ``log_dead_letter``.

    A user's cancel is not a failure, so it is neither an ERROR nor an alert.
    """
    _logger.info("job.cancelled", extra={"job_id": job_id, "video_id": video_id, "kind": kind})
