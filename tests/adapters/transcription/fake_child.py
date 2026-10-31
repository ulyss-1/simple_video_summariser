"""A fake faster-whisper child for the parent-side tests (issue #324).

Run as ``python fake_child.py MODE PIDFILE``. It reads the request line from
stdin, then behaves as MODE says, speaking the reply protocol on the descriptor
the request names. Before it blocks or dies it writes its own pid (and its
grandchild's, if any) to PIDFILE as JSON, so a test knows the child has reached
the point it wants to cancel at.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

DONE = {
    "type": "done",
    "segments": [
        {"start": 0.0, "end": 1.0, "text": "hello"},
        {"start": 1.0, "end": 2.0, "text": "world"},
    ],
    "language": "en",
    "engine_meta": {"engine": "fake", "load_sec": 1.5, "transcribe_sec": 2.0, "rtf": 0.2},
}


def main() -> None:
    mode, pidfile = sys.argv[1], sys.argv[2]
    request = json.loads(sys.stdin.buffer.readline())
    reply = os.fdopen(request["reply_fd"], "wb")

    def send(message: object) -> None:
        reply.write(json.dumps(message).encode() + b"\n")
        reply.flush()

    def record(**extra: object) -> None:
        tmp = pidfile + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(
                {
                    "child": os.getpid(),
                    "pgid": os.getpgrp(),
                    "sid": os.getsid(0),
                    "request": request,
                    "inherited": os.getenv("FAKE_INHERITED"),  # noqa: TID251 - a test double
                    **extra,
                },
                handle,
            )
        os.replace(tmp, pidfile)

    def block() -> None:
        while True:
            time.sleep(60)

    if mode == "ok":
        record()
        for position in (1.0, 2.0, 3.0):
            send({"type": "progress", "done_sec": position})
        send(DONE)
    elif mode == "block_before_output":
        record()
        block()
    elif mode == "block_after_progress":
        record()
        send({"type": "progress", "done_sec": 1.0})
        block()
    elif mode == "block_with_grandchild":
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(60)"],
        )
        record(grandchild=grandchild.pid)
        block()
    elif mode == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        record()
        block()
    elif mode == "err_tool":
        send({"type": "error", "kind": "tool", "message": "boom: transcribe failed"})
    elif mode == "err_network":
        send({"type": "error", "kind": "network", "message": "no route to hub"})
    elif mode == "err_memory":
        send({"type": "error", "kind": "memory", "message": "MemoryError: out of memory"})
    elif mode == "err_unexpected":
        send({"type": "error", "kind": "unexpected", "message": "KeyError: 'x'"})
    elif mode == "err_unknown_kind":
        send({"type": "error", "kind": "weird", "message": "?"})
    elif mode == "kill_self":
        os.kill(os.getpid(), signal.SIGKILL)
    elif mode == "segv_self":
        os.kill(os.getpid(), signal.SIGSEGV)
    elif mode == "exit1":
        sys.stderr.write("trace line one\nOSError: disk gone token=SECRETVALUE\n")
        sys.stderr.flush()
        sys.exit(1)
    elif mode == "exit0":
        sys.exit(0)
    elif mode == "malformed":
        reply.write(b"this is not json\n")
        reply.flush()
        block()
    elif mode == "unknown_type":
        send({"type": "banana"})
        block()
    elif mode == "missing_field":
        send({"type": "done", "segments": []})
        block()
    elif mode == "progress_without_position":
        send({"type": "progress"})
        block()
    elif mode == "message_after_done":
        send(DONE)
        send({"type": "progress", "done_sec": 9.0})
        block()
    elif mode == "done_then_hang":
        send(DONE)
        record()
        block()
    elif mode == "noisy_stdio":
        # Library chatter on stdout and stderr must not disturb the protocol.
        sys.stdout.write("tqdm 50%\n{not json\n")
        sys.stdout.flush()
        sys.stderr.write("ctranslate2 says hi\n")
        sys.stderr.flush()
        send(DONE)
    elif mode in ("stderr_flood", "flood_then_exit1"):
        # Far more than a pipe buffer: a parent that does not drain stalls us.
        for _ in range(400):
            sys.stderr.write("x" * 1000 + "\n")
        sys.stderr.write("LAST LINE\n")
        sys.stderr.flush()
        if mode == "flood_then_exit1":
            sys.exit(1)
        send(DONE)
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
