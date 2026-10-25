"""``common.safe_url.redact`` and its write sites (issue #196).

Secrets are fake sentinels; tests only assert absence and never print them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from common.errors import MAX_ERROR_BYTES, format_error
from common.safe_url import REDACTED, make_redactor, redact
from services.planner.scheduler import MAX_RECORDED_ERROR_CHARS, Scheduler, Task
from tests.services.planner.test_scheduler import FakeLocks

BEARER = "Zq7Xv9Lm2Rt4Yb6Nc8Pd1Fg3Hj5Kw0Sa"
COOKIE = "sessionid-sentinel-8841"
ANT_KEY = "sk-ant-api03-SENTINELkey1234567890abcdef"
OPENAI_KEY = "sk-SENTINELopenaikey1234567890abcd"
URL_PASSWORD = "p4ss-sentinel-77"
BARE = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7dE"

SENTINELS = {
    "bearer": (f"Authorization: Bearer {BEARER}", BEARER),
    "lowercase-header": (f"authorization: basic {BEARER}", BEARER),
    "cookie": (f"Cookie: {COOKIE}; other=1", COOKIE),
    "set-cookie": (f"Set-Cookie: {COOKIE}; HttpOnly", COOKIE),
    "anthropic-key": (f"bad key {ANT_KEY} rejected", ANT_KEY),
    "openai-key": (f"bad key {OPENAI_KEY} rejected", OPENAI_KEY),
    "url": (f"GET https://user:{URL_PASSWORD}@hooks.example.com:8443/x?t=1 failed", URL_PASSWORD),
    "bare-token": (f"token={BARE} rejected", BARE),
}


@pytest.mark.parametrize("kind", SENTINELS)
def test_each_secret_type_is_redacted(kind: str) -> None:
    text, secret = SENTINELS[kind]

    assert secret not in redact(text)


def test_auth_scheme_and_url_host_are_kept() -> None:
    assert redact(f"Authorization: Bearer {BEARER}") == f"Authorization: Bearer {REDACTED}"
    assert redact(f"Set-Cookie: {COOKIE}; Path=/") == f"Set-Cookie: {REDACTED}"
    assert (
        redact(f"see https://u:{URL_PASSWORD}@h.example.com:8443/p?q=1#f now")
        == "see https://h.example.com:8443 now"
    )


def test_literals_use_the_caller_placeholder() -> None:
    out = make_redactor(["hunter2-sentinel", ""], "***")("dsn pw=hunter2-sentinel end")

    assert out == "dsn pw=*** end"


def test_unparseable_credentialed_url_is_replaced_whole() -> None:
    out = redact(f"bad postgresql://u:{URL_PASSWORD}@[bad/db")

    assert URL_PASSWORD not in out


ORDINARY = (
    "Traceback (most recent call last):\n"
    '  File "/home/app/services/transcriber/handlers/ingest_module.py", line 42, in run\n'
    "    client.fetch(video)\n"
    "common.errors.ToolFailureError: yt-dlp exited 1 for 123e4567-e89b-12d3-a456-426614174000\n"
    "<function run at 0x7f3a2c1d4e50> status 503 port 5432 host db:5432\n"
    "commit da39a3ee5e6b4b0d3255bfef95601890afd80709 "
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    "https://www.youtube.com/@somechannel/videos?v=dQw4w9WgXcQ\n"
    "ThreadPoolExecutorWorkerInitializerNotFoundErrorHandlerFactory\n"
)


def test_ordinary_diagnostic_text_survives_unchanged() -> None:
    assert redact(ORDINARY) == ORDINARY


@pytest.mark.parametrize("kind", SENTINELS)
def test_redaction_is_idempotent(kind: str) -> None:
    once = redact(SENTINELS[kind][0])

    assert redact(once) == once


def test_redacted_placeholder_alone_is_unchanged() -> None:
    assert redact(f"Authorization: Bearer {REDACTED} Cookie: {REDACTED}") == (
        f"Authorization: Bearer {REDACTED} Cookie: {REDACTED}"
    )


# --- jobs write site: format_error -------------------------------------------


@pytest.mark.parametrize("kind", SENTINELS)
def test_format_error_redacts_each_secret_type(kind: str) -> None:
    text, secret = SENTINELS[kind]

    assert secret not in format_error(RuntimeError(text))


def test_secret_straddling_the_truncation_cut_does_not_survive() -> None:
    # Sweep the distance from the secret to the end of the text across the
    # whole neighbourhood of the kept-tail budget, so some placement has the
    # cut inside the secret.
    secret = f"Authorization: Bearer {BEARER}"
    for tail_len in range(MAX_ERROR_BYTES - 150, MAX_ERROR_BYTES + 50):
        out = format_error(RuntimeError("x" * 100 + secret + "y" * tail_len))
        assert BEARER not in out
        assert BEARER[:10] not in out
        assert BEARER[-10:] not in out


# --- planner write site: scheduler -> planner_task_runs ----------------------


def _run_failing(tmp_path: Path, recorder: Any, message: str) -> None:
    def boom() -> int:
        raise RuntimeError(message)

    Scheduler(
        [Task("a", 60, boom)], FakeLocks(), clock=lambda: 0.0, sleep=lambda _s: None,
        liveness_path=tmp_path / "hb", install_signal_handlers=False, recorder=recorder,
        wall_clock=lambda: datetime(2026, 10, 10, tzinfo=UTC),
    ).run_once()


class _Capture:
    def __init__(self) -> None:
        self.error: str | None = None

    def task_started(self, name: str, *, interval_sec: float, at: datetime) -> None: ...
    def task_skipped_locked(self, name: str, *, interval_sec: float, at: datetime) -> None: ...

    def task_finished(self, name: str, *, interval_sec: float, at: datetime, outcome: Any,
                      error: str | None, count: int | None) -> None:
        self.error = error


@pytest.mark.parametrize("kind", SENTINELS)
def test_scheduler_redacts_each_secret_type_before_recording(kind: str, tmp_path: Path) -> None:
    text, secret = SENTINELS[kind]
    rec = _Capture()

    _run_failing(tmp_path, rec, text)

    assert rec.error is not None
    assert secret not in rec.error


def test_scheduler_redacts_before_the_500_char_cut(tmp_path: Path) -> None:
    rec = _Capture()
    # RuntimeError: + padding puts the key across character 500.
    pad = "x" * (MAX_RECORDED_ERROR_CHARS - len("RuntimeError: ") - 10)

    _run_failing(tmp_path, rec, f"{pad}{ANT_KEY}")

    assert rec.error is not None
    assert ANT_KEY[:12] not in rec.error
