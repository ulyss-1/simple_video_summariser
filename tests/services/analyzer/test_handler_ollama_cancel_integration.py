"""A stored cancel aborts a blocked Ollama generation, through a real worker (issue #302).

One video, one analyze job, one worker call. The Ollama transport blocks on an
``Event`` that only the abort hook sets; the cancel is stored, one heartbeat tick
is fired by hand, and the summarizer's poll sees ``ctx.cancelled``.
"""

from __future__ import annotations

import random
import threading
from typing import Any, cast

import psycopg
import pytest

from adapters.summarize.abortable import CallHandle
from adapters.summarize.ollama import OllamaSummarizer
from adapters.summarize.prompt_loader import load_prompt_set
from common.queue import Job, PostgresQueue
from common.repo.jobs import cancel_video
from common.worker import JobContext, Worker
from tests.common.test_cancel_races import DEADLINE_SEC, Call, ManualTimers
from tests.services.analyzer.test_handler import VIDEO, Rig

pytestmark = pytest.mark.integration


class BlockedGeneration:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.released_by_abort = False
        self.thread: threading.Thread | None = None
        self.calls = 0

    def __call__(self, *_args: Any, handle: CallHandle) -> Any:
        self.calls += 1
        self.thread = threading.current_thread()
        handle.register(self.release.set)
        self.started.set()
        self.released_by_abort = self.release.wait(DEADLINE_SEC)
        raise OSError("connection closed")


def test_a_cancel_stored_during_an_ollama_generation_aborts_it_and_saves_nothing(
    conn: psycopg.Connection[Any], head_dsn: str, tmp_path: Any
) -> None:
    generation = BlockedGeneration()
    summarizer = OllamaSummarizer(
        host="http://h:11434",
        model="qwen3.5:4b",
        num_ctx=8192,
        timeout_sec=900,
        prompts=load_prompt_set("v1"),
        transport=lambda *_a: pytest.fail("the plain transport must not run in a job"),
        abortable_transport=generation,
        cancel_poll_sec=0.001,
    )
    rig = Rig(conn, head_dsn, cast(Any, summarizer))
    admin = psycopg.connect(head_dsn, autocommit=True)
    hb = psycopg.connect(head_dsn, autocommit=True)
    queue = PostgresQueue(admin, rng=random.Random(1))
    job_id = queue.enqueue("analyze", VIDEO, dedupe_key="v1:fake")
    assert job_id is not None
    timers = ManualTimers()
    seen_ctx: list[JobContext] = []

    def handler(job: Job, ctx: JobContext) -> None:
        seen_ctx.append(ctx)
        rig.handler(job, ctx)

    worker = Worker(
        "w1",
        ["analyze"],
        handler,
        queue,
        liveness_path=tmp_path / "hb",
        heartbeat_sec=60,
        heartbeat_queue_factory=lambda: PostgresQueue(hb),
        runtime_limit_sec=lambda job: None,
        timer_factory=timers,
        install_signal_handlers=False,
        sleep=lambda seconds: None,
    )
    try:
        runner = Call(lambda: worker.run(max_iterations=1))
        assert generation.started.wait(DEADLINE_SEC), "the generation never started"
        assert [e.outcome for e in cancel_video(admin, VIDEO)] == ["cancel_requested"]
        timers.fire(60.0)  # one heartbeat tick delivers the request
        runner.result("the worker")

        assert generation.released_by_abort, "the abort hook never ran"
        assert generation.thread is not None and not generation.thread.is_alive()
        assert generation.calls == 1  # not retried, not repaired
        stored = admin.execute(
            "SELECT state, error_class, attempts FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        assert stored == ("dead", "CANCELLED", 1)
        assert rig.count("analyses") == 0
        assert (rig.count("topics"), rig.count("claims"), rig.count("quotes")) == (0, 0, 0)
        assert rig.count("llm_batches") == 0
        assert (summarizer.take_usage().calls) == 0
    finally:
        generation.release.set()
        admin.close()
        hb.close()
