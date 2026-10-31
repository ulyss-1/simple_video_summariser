"""The child process behind ``FasterWhisperTranscriber`` (issue #324).

``python -m adapters.transcription.faster_whisper_child`` reads one JSON request
line from stdin, runs the in-process transcription path of
``FasterWhisperTranscriber`` (so decoding options and ``engine_meta`` exist in
one place) and writes JSON lines to the pipe whose descriptor number the request
names (``reply_fd``). Its stdout and stderr belong to the libraries it uses and
are not the protocol. The messages are ``progress`` (one per decoded segment),
then exactly one of ``done`` or ``error``.

The parent kills this process's whole group on a cancel. If the parent dies
without a chance to do so (the runtime-limit watchdog uses ``os._exit``), the
child notices its stdin closing, or its parent changing, and exits by itself.
It has no database connection and reads ``AUDIO_DIR`` from its environment.
"""

from __future__ import annotations

import json
import os
import select
import sys
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

from adapters.transcription.faster_whisper import (
    KIND_MEMORY,
    KIND_NETWORK,
    KIND_TOOL,
    KIND_UNEXPECTED,
    MSG_DONE,
    MSG_ERROR,
    MSG_PROGRESS,
    FasterWhisperTranscriber,
)
from common.errors import ToolFailureError, TransientNetworkError
from common.models import AudioRef

#: Exit status of a child whose parent went away.
EXIT_PARENT_GONE = 3


def run_request(request: dict[str, Any], send: Callable[[dict[str, Any]], None]) -> None:
    """Do the work of one request; every outcome is a message through ``send``."""
    try:
        audio = request["audio"]
        transcriber = FasterWhisperTranscriber(
            request["model"], request["compute_type"], request["threads"]
        )
        result = transcriber.transcribe(
            AudioRef(
                rel_path=audio["rel_path"],
                bytes=audio["bytes"],
                duration_sec=audio["duration_sec"],
            ),
            language=request.get("language"),
            on_progress=lambda done_sec: send({"type": MSG_PROGRESS, "done_sec": done_sec}),
        )
    except BaseException as exc:  # every outcome is reported, never raised
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        send({"type": MSG_ERROR, "kind": _kind(exc), "message": _message(exc)})
        return
    send(
        {
            "type": MSG_DONE,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in result.segments],
            "language": result.language,
            "engine_meta": result.engine_meta,
        }
    )


def _kind(exc: BaseException) -> str:
    if isinstance(exc, MemoryError):
        return KIND_MEMORY
    if isinstance(exc, TransientNetworkError):
        return KIND_NETWORK
    if isinstance(exc, ToolFailureError):
        return KIND_TOOL
    return KIND_UNEXPECTED


def _message(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if isinstance(exc, (ToolFailureError, TransientNetworkError)) else (
        f"{type(exc).__name__}: {text}"
    )


def watch_parent(
    stdin_fd: int,
    interval_sec: float,
    on_gone: Callable[[], None],
    *,
    parent_pid: int | None = None,
) -> threading.Thread:
    """Call ``on_gone`` when stdin reaches EOF or the parent pid changes.

    EOF is seen at once; the pid check runs every ``interval_sec`` as a
    backstop. The thread is a daemon.
    """
    original = os.getppid() if parent_pid is None else parent_pid

    def watch() -> None:
        while True:
            try:
                ready, _, _ = select.select([stdin_fd], [], [], interval_sec)
                if ready and not os.read(stdin_fd, 4096):
                    break
            except (OSError, ValueError):
                break
            if os.getppid() != original:
                break
        on_gone()

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def main() -> int:
    stdin = sys.stdin.buffer
    line = stdin.readline()
    if not line:
        return EXIT_PARENT_GONE
    request = json.loads(line)
    reply: BinaryIO = os.fdopen(request["reply_fd"], "wb")
    lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        data = json.dumps(message).encode() + b"\n"
        with lock:
            reply.write(data)
            reply.flush()

    watch_parent(
        stdin.fileno(),
        float(request.get("parent_watch_sec", 2.0)),
        lambda: os._exit(EXIT_PARENT_GONE),
    )
    run_request(request, send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
