"""The child module itself (issue #324): its request handling in-process with the
fake ``faster_whisper`` helpers, and the real module run as a subprocess against
a stub ``faster_whisper`` package on ``PYTHONPATH``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from adapters.transcription.faster_whisper import FasterWhisperTranscriber
from adapters.transcription.faster_whisper_child import run_request, watch_parent
from common.config import get_settings
from common.errors import Cancelled
from tests.adapters.transcription.test_faster_whisper import (
    FakeSegment,
    install_fake_module,
    make_fake_model,
)
from tests.adapters.transcription.test_faster_whisper_child_process import (
    AUDIO,
    DEADLINE_SEC,
    assert_gone,
    wait_for,
)

STUB_DIR = Path(__file__).with_name("stub_whisper")


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "large-v3",
        "compute_type": "int8",
        "threads": 3,
        "audio": {"rel_path": AUDIO.rel_path, "bytes": AUDIO.bytes, "duration_sec": 10.0},
        "language": "en-US",
    }
    return {**base, **overrides}


def run(req: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    run_request(req, sent.append)
    return sent


SEGMENTS = [FakeSegment(0.0, 1.0, " hello "), FakeSegment(1.0, 2.0, "   "), FakeSegment(2.0, 3.0, "bye")]


# --- request in, messages out ---------------------------------------------


def test_progress_per_segment_then_done_matching_the_in_process_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model: Any = make_fake_model(segments=SEGMENTS)
    install_fake_module(monkeypatch, model)

    sent = run(request())

    assert [m["type"] for m in sent] == ["progress", "progress", "progress", "done"]
    assert [m["done_sec"] for m in sent[:3]] == [1.0, 2.0, 3.0]
    done = sent[-1]
    expected = FasterWhisperTranscriber("large-v3", "int8", 3).transcribe(
        AUDIO, language="en-US"
    )
    assert done["segments"] == [
        {"start": s.start, "end": s.end, "text": s.text} for s in expected.segments
    ]
    assert done["language"] == expected.language == "en"
    assert done["engine_meta"].keys() == expected.engine_meta.keys()
    for key in ("engine", "engine_version", "model", "compute_type", "beam_size", "vad", "threads"):
        assert done["engine_meta"][key] == expected.engine_meta[key]
    assert done["engine_meta"]["load_sec"] > 0  # the child really loaded the model
    json.dumps(sent)  # all of it is JSON


def test_threads_and_compute_type_reach_the_one_model_in_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model: Any = make_fake_model(segments=SEGMENTS)
    install_fake_module(monkeypatch, model)

    run(request(threads=2, compute_type="int8_float32"))

    [instance] = model.instances
    assert instance.cpu_threads == 2
    assert instance.compute_type == "int8_float32"
    assert instance.model_size_or_path == "large-v3"
    assert instance.device == "cpu"


def test_decoding_options_and_language_are_the_in_process_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model: Any = make_fake_model(segments=SEGMENTS)
    install_fake_module(monkeypatch, model)

    run(request(language="pt_BR"))

    [call] = model.instances[0].transcribe_calls
    assert (call["beam_size"], call["vad_filter"], call["language"]) == (5, True, "pt")


@pytest.mark.parametrize(
    ("model_kwargs", "kind", "fragment"),
    [
        ({"raise_on_init": OSError("hub unreachable")}, "network", "hub unreachable"),
        ({"raise_on_transcribe": ValueError("bad audio")}, "tool", "bad audio"),
        ({"raise_on_init": MemoryError()}, "memory", "MemoryError"),
    ],
)
def test_each_failure_becomes_one_error_message_with_a_kind(
    monkeypatch: pytest.MonkeyPatch, model_kwargs: dict[str, Any], kind: str, fragment: str
) -> None:
    install_fake_module(monkeypatch, make_fake_model(**model_kwargs))

    sent = run(request())

    assert len(sent) == 1
    assert sent[0]["type"] == "error"
    assert sent[0]["kind"] == kind
    assert fragment in sent[0]["message"]


def test_a_missing_faster_whisper_names_the_requirements_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # makes the import fail

    [message] = run(request())

    assert (message["type"], message["kind"]) == ("error", "tool")
    assert "requirements.whisper.txt" in message["message"]


def test_an_unexpected_exception_is_reported_not_raised() -> None:
    [message] = run({"model": "tiny"})  # no audio: a KeyError inside

    assert (message["type"], message["kind"]) == ("error", "unexpected")


# --- the real module in a subprocess --------------------------------------


def use_stub_whisper(monkeypatch: pytest.MonkeyPatch, mode: str, ready: Path) -> None:
    """Child processes started from now on import the stub ``faster_whisper``."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(STUB_DIR), *sys.path]))
    monkeypatch.setenv("STUB_WHISPER_MODE", mode)
    monkeypatch.setenv("STUB_WHISPER_READY", str(ready))


def _spawn_real_child(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch, *, parent_watch_sec: float = 0.05
) -> tuple[subprocess.Popen[bytes], int, Path]:
    read_fd, write_fd = os.pipe()
    ready = tmp_path / "stub-ready"
    use_stub_whisper(monkeypatch, mode, ready)
    proc = subprocess.Popen(
        [sys.executable, "-m", "adapters.transcription.faster_whisper_child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(write_fd)
    assert proc.stdin is not None
    line = json.dumps({**request(model="tiny", threads=1), "reply_fd": write_fd,
                       "parent_watch_sec": parent_watch_sec})
    proc.stdin.write(line.encode() + b"\n")
    proc.stdin.flush()
    return proc, read_fd, ready


def test_the_real_child_module_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub_whisper(monkeypatch, "ok", tmp_path / "unused")
    transcriber = FasterWhisperTranscriber("tiny", "int8", 1)
    positions: list[float] = []

    result = transcriber.transcribe(AUDIO, on_progress=positions.append, cancelled=lambda: False)

    assert positions == [1.0, 2.0]
    # The stub puts what it was constructed with in the text: 1 thread, int8.
    assert [s.text for s in result.segments] == ["tiny/cpu/int8/1"]
    assert result.engine_meta["engine_version"] == "0.0-stub"
    assert result.engine_meta["load_sec"] >= 0
    assert transcriber._loaded_model is None


def test_the_real_child_exits_by_itself_when_its_stdin_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc, read_fd, ready = _spawn_real_child(tmp_path, "block", monkeypatch)
    try:
        wait_for(ready.exists, "the stub model to be inside transcribe()")
        assert proc.stdin is not None
        proc.stdin.close()  # the parent died without killing anyone

        assert proc.wait(timeout=DEADLINE_SEC) == 3
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(read_fd)


def test_the_real_child_is_killed_by_a_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = tmp_path / "ready"
    use_stub_whisper(monkeypatch, "block", ready)
    transcriber = FasterWhisperTranscriber("tiny", "int8", 1, poll_interval=0.01)

    with pytest.raises(Cancelled):
        transcriber.transcribe(AUDIO, cancelled=ready.exists)

    assert_gone(int(ready.read_text()))


# --- the parent watch -----------------------------------------------------


def test_watch_parent_fires_when_stdin_reaches_eof() -> None:
    read_fd, write_fd = os.pipe()
    gone = threading.Event()
    watch_parent(read_fd, 5.0, gone.set)
    assert not gone.wait(0.05)

    os.close(write_fd)

    assert gone.wait(DEADLINE_SEC)
    os.close(read_fd)


def test_watch_parent_fires_when_the_parent_pid_changes() -> None:
    read_fd, write_fd = os.pipe()  # stays open: only the pid check can fire
    gone = threading.Event()

    watch_parent(read_fd, 0.01, gone.set, parent_pid=os.getppid() + 1)

    assert gone.wait(DEADLINE_SEC)
    os.close(read_fd)
    os.close(write_fd)
