"""The parent side of the faster-whisper child process (issue #324).

The child is ``fake_child.py`` (selected through ``child_command``): no model,
no network. Every wait is a deadline on a file or a pid, never a bare sleep.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from adapters.transcription.faster_whisper import (
    STDERR_TAIL_BYTES,
    FasterWhisperTranscriber,
)
from common.config import get_settings
from common.errors import (
    BugError,
    Cancelled,
    ErrorClass,
    ResourceError,
    ToolFailureError,
    TransientNetworkError,
    classify,
)
from common.models import AudioRef

FAKE_CHILD = Path(__file__).with_name("fake_child.py")
AUDIO = AudioRef(rel_path="ab/abc12345678.opus", bytes=1234, duration_sec=10.0)
DEADLINE_SEC = 5.0
POLL_SEC = 0.01


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Rig:
    """A transcriber wired to the fake child in one mode, with its pid file."""

    def __init__(self, mode: str, tmp_path: Path, **kwargs: Any) -> None:
        self.pidfile = tmp_path / "pids.json"
        kwargs.setdefault("poll_interval", POLL_SEC)
        kwargs.setdefault("reap_timeout", DEADLINE_SEC)
        self.transcriber = FasterWhisperTranscriber(
            "tiny",
            "int8",
            3,
            child_command=[sys.executable, str(FAKE_CHILD), mode, str(self.pidfile)],
            **kwargs,
        )

    def pids(self) -> dict[str, Any]:
        wait_for(self.pidfile.exists, "the child to report")
        data: dict[str, Any] = json.loads(self.pidfile.read_text())
        return data

    def cancel_when_child_is_blocked(self) -> Callable[[], bool]:
        """A ``cancelled`` that turns true once the child reported it is blocking."""
        return lambda: self.pidfile.exists()


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SEC
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def alive(pid: int) -> bool:
    """Whether ``pid`` is a running process (a zombie is not running)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def assert_gone(pid: int) -> None:
    wait_for(lambda: not alive(pid), f"pid {pid} to be gone")


def never() -> bool:
    return False


# --- the happy path -------------------------------------------------------


def test_the_result_and_progress_come_back_and_the_parent_never_loads_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    rig = Rig("ok", tmp_path)
    threads: set[int] = set()
    positions: list[float] = []

    def on_progress(done: float) -> None:
        threads.add(threading.get_ident())
        positions.append(done)

    result = rig.transcriber.transcribe(
        AUDIO, language="en-US", on_progress=on_progress, cancelled=never
    )

    assert positions == [1.0, 2.0, 3.0]
    assert threads == {threading.get_ident()}  # the calling thread, as in-process
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.0, 1.0, "hello"),
        (1.0, 2.0, "world"),
    ]
    assert result.language == "en"
    assert result.engine_meta["load_sec"] == 1.5
    assert rig.transcriber._loaded_model is None
    assert "faster_whisper" not in sys.modules


def test_the_request_carries_model_threads_audio_and_language(tmp_path: Path) -> None:
    rig = Rig("ok", tmp_path, parent_watch_sec=0.25)
    rig.transcriber.transcribe(AUDIO, language="de", cancelled=never)

    request = rig.pids()["request"]
    assert request["model"] == "tiny"
    assert request["compute_type"] == "int8"
    assert request["threads"] == 3
    assert request["audio"] == {"rel_path": AUDIO.rel_path, "bytes": 1234, "duration_sec": 10.0}
    assert request["language"] == "de"
    assert request["parent_watch_sec"] == 0.25
    assert isinstance(request["reply_fd"], int)


def test_the_child_leads_its_own_session_and_inherits_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_INHERITED", "yes")
    rig = Rig("ok", tmp_path)
    rig.transcriber.transcribe(AUDIO, cancelled=never)

    report = rig.pids()
    assert report["sid"] == report["child"] == report["pgid"]  # start_new_session
    assert report["inherited"] == "yes"


def test_library_output_on_stdout_and_stderr_does_not_disturb_the_protocol(
    tmp_path: Path,
) -> None:
    result = Rig("noisy_stdio", tmp_path).transcriber.transcribe(AUDIO, cancelled=never)

    assert result.language == "en"


def test_a_child_flooding_stderr_is_not_stalled(tmp_path: Path) -> None:
    result = Rig("stderr_flood", tmp_path).transcriber.transcribe(AUDIO, cancelled=never)

    assert len(result.segments) == 2


def test_only_the_last_2kb_of_stderr_are_kept_and_they_reach_the_error(tmp_path: Path) -> None:
    with pytest.raises(ToolFailureError) as caught:
        Rig("flood_then_exit1", tmp_path).transcriber.transcribe(AUDIO, cancelled=never)

    message = str(caught.value)
    assert "LAST LINE" in message
    assert "status 1" in message
    assert len(message) < STDERR_TAIL_BYTES + 200


def test_a_cancelled_none_call_never_spawns_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    fake = types.ModuleType("faster_whisper")

    class Model:
        def __init__(self, *_: object, **__: object) -> None: ...

        def transcribe(self, *_: object, **__: object) -> tuple[Any, Any]:
            return iter(()), types.SimpleNamespace(language="en")

    fake.WhisperModel = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))

    result = Rig("ok", tmp_path).transcriber.transcribe(AUDIO)

    assert spawned == []
    assert result.segments == ()


# --- one child at a time --------------------------------------------------


def test_a_second_call_never_starts_a_child_while_one_is_alive(tmp_path: Path) -> None:
    rig = Rig("ok", tmp_path)
    inner: list[BaseException] = []

    def on_progress(_done: float) -> None:
        try:
            rig.transcriber.transcribe(AUDIO, cancelled=never)
        except BaseException as exc:  # noqa: BLE001
            inner.append(exc)

    rig.transcriber.transcribe(AUDIO, on_progress=on_progress, cancelled=never)

    assert len(inner) == 3
    assert all(isinstance(exc, BugError) for exc in inner)


def test_the_slot_is_free_again_after_a_cancel(tmp_path: Path) -> None:
    rig = Rig("block_before_output", tmp_path)
    with pytest.raises(Cancelled):
        rig.transcriber.transcribe(
            AUDIO, cancelled=rig.cancel_when_child_is_blocked()
        )
    rig.pidfile.unlink()

    again = Rig("ok", tmp_path)
    assert again.transcriber.transcribe(AUDIO, cancelled=never).language == "en"


# --- cancel kills the child and its group ---------------------------------


@pytest.mark.parametrize(
    "mode", ["block_before_output", "block_after_progress", "block_with_grandchild", "ignore_sigterm"]
)
def test_a_cancel_kills_the_whole_group_within_a_bounded_time(mode: str, tmp_path: Path) -> None:
    rig = Rig(mode, tmp_path)
    flagged = threading.Event()

    def on_progress(_done: float) -> None:
        flagged.set()

    cancelled = (
        flagged.is_set if mode == "block_after_progress" else rig.cancel_when_child_is_blocked()
    )
    started = time.monotonic()
    with pytest.raises(Cancelled):
        rig.transcriber.transcribe(AUDIO, on_progress=on_progress, cancelled=cancelled)
    elapsed = time.monotonic() - started

    pids = rig.pids()
    assert elapsed < DEADLINE_SEC
    assert_gone(pids["child"])
    if "grandchild" in pids:
        assert_gone(pids["grandchild"])
    with pytest.raises(ChildProcessError):
        os.waitpid(pids["child"], os.WNOHANG)  # already reaped by the parent


# --- every other way out kills the child too ------------------------------


class Boom(Exception):
    pass


@pytest.mark.parametrize("raised", [Cancelled(), KeyboardInterrupt(), Boom("callback failed")])
def test_an_exception_from_on_progress_kills_and_reaps_the_child(
    raised: BaseException, tmp_path: Path
) -> None:
    rig = Rig("block_after_progress", tmp_path)

    def on_progress(_done: float) -> None:
        raise raised

    with pytest.raises(type(raised)):
        rig.transcriber.transcribe(AUDIO, on_progress=on_progress, cancelled=never)

    assert_gone(rig.pids()["child"])


def test_a_cancel_in_the_slice_of_the_done_message_uses_the_finished_result(
    tmp_path: Path,
) -> None:
    rig = Rig("done_then_hang", tmp_path, reap_timeout=0.2)

    # True only after the child sent "done" (it reports its pid right after).
    result = rig.transcriber.transcribe(
        AUDIO, cancelled=rig.cancel_when_child_is_blocked()
    )

    assert [s.text for s in result.segments] == ["hello", "world"]
    assert_gone(rig.pids()["child"])


def test_a_child_that_sent_done_but_will_not_exit_is_killed(tmp_path: Path) -> None:
    rig = Rig("done_then_hang", tmp_path, reap_timeout=0.2)

    result = rig.transcriber.transcribe(AUDIO, cancelled=never)

    assert result.language == "en"
    assert_gone(rig.pids()["child"])


# --- failures keep the error classes of the in-process path ---------------


@pytest.mark.parametrize(
    ("mode", "error_type", "error_class", "fragment"),
    [
        ("err_tool", ToolFailureError, ErrorClass.TOOL_FAILURE, "boom: transcribe failed"),
        ("err_network", TransientNetworkError, ErrorClass.TRANSIENT_NETWORK, "no route"),
        ("err_memory", ResourceError, ErrorClass.RESOURCE, "out of memory"),
        ("err_unexpected", BugError, ErrorClass.BUG, "KeyError"),
        ("err_unknown_kind", ToolFailureError, ErrorClass.TOOL_FAILURE, "unknown error kind"),
        ("kill_self", ResourceError, ErrorClass.RESOURCE, "killed (SIGKILL), probably out of memory"),
        ("segv_self", ToolFailureError, ErrorClass.TOOL_FAILURE, "SIGSEGV"),
        ("exit1", ToolFailureError, ErrorClass.TOOL_FAILURE, "status 1"),
        ("exit0", ToolFailureError, ErrorClass.TOOL_FAILURE, "exited 0 without a result"),
        ("malformed", ToolFailureError, ErrorClass.TOOL_FAILURE, "malformed line"),
        ("unknown_type", ToolFailureError, ErrorClass.TOOL_FAILURE, "unknown message"),
        ("missing_field", ToolFailureError, ErrorClass.TOOL_FAILURE, "bad 'done'"),
        ("progress_without_position", ToolFailureError, ErrorClass.TOOL_FAILURE, "done_sec"),
        ("message_after_done", ToolFailureError, ErrorClass.TOOL_FAILURE, "after 'done'"),
    ],
)
def test_child_failures_map_to_the_classes_of_the_in_process_path(
    mode: str, error_type: type[Exception], error_class: ErrorClass, fragment: str, tmp_path: Path
) -> None:
    # A long poll: a failure must be reported as itself, not lose a race with
    # the (always true) cancel flag below.
    rig = Rig(mode, tmp_path, poll_interval=DEADLINE_SEC)

    with pytest.raises(Exception) as caught:
        rig.transcriber.transcribe(AUDIO, cancelled=lambda: True)

    assert type(caught.value) is error_type
    assert classify(caught.value) is error_class
    assert fragment in str(caught.value)
    assert not isinstance(caught.value, Cancelled)
    if rig.pidfile.exists():
        assert_gone(rig.pids()["child"])


def test_the_exit_status_message_carries_redacted_stderr(tmp_path: Path) -> None:
    with pytest.raises(ToolFailureError) as caught:
        Rig("exit1", tmp_path).transcriber.transcribe(AUDIO, cancelled=never)

    assert "OSError: disk gone" in str(caught.value)


def test_a_child_that_cannot_be_started_is_a_tool_failure(tmp_path: Path) -> None:
    transcriber = FasterWhisperTranscriber(
        "tiny", "int8", 1, child_command=[str(tmp_path / "no-such-program")]
    )

    with pytest.raises(ToolFailureError, match="could not start"):
        transcriber.transcribe(AUDIO, cancelled=never)


def test_a_poll_interval_of_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        FasterWhisperTranscriber("tiny", "int8", 1, poll_interval=0)
