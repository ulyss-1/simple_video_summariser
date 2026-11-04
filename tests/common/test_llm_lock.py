"""Local-LLM / Whisper exclusion, the parts that need no database (issue #393)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.errors import (
    ANALYSIS_WAITING,
    DEFER_REASONS,
    LOCAL_LLM_BUSY,
    WHISPER_BUSY,
    ControlFlow,
    Defer,
    LocalLlmBusy,
    classify,
)
from common.llm_lock import (
    LOCK_BUSY_DEFER,
    NullLocalLlmGuard,
    NullWhisperGate,
    deferred_text,
    jittered_until,
)
from common.models import LocalLlmGuard

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def test_a_plain_defer_carries_no_reason() -> None:
    assert Defer(NOW).reason is None
    assert Defer(NOW).until == NOW


def test_a_defer_keeps_the_reason_it_was_given() -> None:
    assert Defer(NOW, reason=LOCAL_LLM_BUSY).reason == "local_llm_busy"


def test_local_llm_busy_is_a_defer_so_the_queue_resolves_it_as_one() -> None:
    busy = LocalLlmBusy(NOW, WHISPER_BUSY)
    assert isinstance(busy, Defer)
    assert (busy.until, busy.reason) == (NOW, "whisper_busy")


def test_classify_refuses_local_llm_busy_like_any_control_flow() -> None:
    with pytest.raises(TypeError):
        classify(LocalLlmBusy(NOW, WHISPER_BUSY))
    assert issubclass(LocalLlmBusy, ControlFlow)


def test_the_stored_texts_are_the_three_fixed_constants() -> None:
    assert deferred_text(LOCAL_LLM_BUSY) == "deferred: local_llm_busy"
    assert deferred_text(WHISPER_BUSY) == "deferred: whisper_busy"
    assert deferred_text(ANALYSIS_WAITING) == "deferred: analysis_waiting"
    assert DEFER_REASONS == (LOCAL_LLM_BUSY, WHISPER_BUSY, ANALYSIS_WAITING)


def test_the_default_delay_is_30_seconds() -> None:
    assert LOCK_BUSY_DEFER == timedelta(seconds=30)


@pytest.mark.parametrize(("roll", "seconds"), [(0.0, 24.0), (0.5, 30.0), (1.0, 36.0)])
def test_the_delay_is_jittered_by_plus_or_minus_20_percent(roll: float, seconds: float) -> None:
    until = jittered_until(NOW, LOCK_BUSY_DEFER, lambda: roll)
    assert (until - NOW).total_seconds() == pytest.approx(seconds)


def test_the_null_guard_satisfies_the_port_and_never_raises() -> None:
    guard = NullLocalLlmGuard()
    assert isinstance(guard, LocalLlmGuard)
    guard.ensure_held()
    guard.ensure_held()
    guard.release()
    guard.release()


def test_the_null_gate_holds_nothing() -> None:
    entered: list[int] = []
    with NullWhisperGate().hold(None):
        entered.append(1)
    assert entered == [1]
