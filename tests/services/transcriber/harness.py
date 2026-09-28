"""Subprocess harness for the transcriber entrypoint tests (issue #32).

``python -m tests.services.transcriber.harness <mode>`` runs the *real*
``services.transcriber.main.main()`` against the database named by
``DATABASE_URL``, with fake handlers for both kinds instead of the production
ones (so nothing here needs faster-whisper, yt-dlp or a network).

The handler prints ``started <job id> <locked_by>`` when it begins a job and
``finished <job id>`` when it returns, flushing each line, so the test can wait
on those lines instead of sleeping. Modes:

``block``
    Wait for a line on stdin ("release"), then return.
``gate``
    Like ``block`` for the first job only; every later job returns at once.
``instant``
    Return at once.
``cooperative``
    Loop on ``ctx.check_cancelled()`` until the worker cancels the job.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

from common.queue import Job
from common.worker import JobContext
from services.transcriber.main import main


def make_handler(mode: str) -> Callable[[Job, JobContext], None]:
    handled = 0

    def handler(job: Job, ctx: JobContext) -> None:
        nonlocal handled
        handled += 1
        print(f"started {job.id} {job.locked_by}", flush=True)
        if mode == "block" or (mode == "gate" and handled == 1):
            sys.stdin.readline()
        elif mode == "cooperative":
            tick = threading.Event()
            while True:
                ctx.check_cancelled()
                tick.wait(0.05)
        print(f"finished {job.id}", flush=True)

    return handler


if __name__ == "__main__":
    handler = make_handler(sys.argv[1])
    sys.exit(main([], handlers={"ingest": handler, "transcribe": handler}))
