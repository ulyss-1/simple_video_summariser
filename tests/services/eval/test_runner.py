"""Unit tests for ``services.eval.runner`` helpers (issue #62, hardened by #195).

Pure, no database: only the synthetic job the handler is called with.
"""

from __future__ import annotations

import types

from common.config import Settings
from common.models import Summarizer
from common.queue import PRIORITY_INTERACTIVE, analyze_dedupe_key
from services.eval.runner import synthetic_job

VID = "dQw4w9WgXcQ"


def _summarizer(name: str) -> Summarizer:
    return types.SimpleNamespace(name=name)


def test_synthetic_job_dedupe_key_is_prompt_version_and_summarizer_name() -> None:
    # The value, not merely its presence: a job built for a different
    # prompt/summarizer must not collide with a real analyze job's key, so the
    # key has to be exactly analyze_dedupe_key(PROMPT_VERSION, summarizer.name).
    settings = Settings(DATABASE_URL="postgresql://u:p@h/db", PROMPT_VERSION="v3")  # type: ignore[arg-type]

    job = synthetic_job(VID, settings, _summarizer("fakemodel"))

    assert job.dedupe_key == "v3:fakemodel"
    assert job.dedupe_key == analyze_dedupe_key("v3", "fakemodel")
    assert job.kind == "analyze"
    assert job.video_id == VID
    assert job.priority == PRIORITY_INTERACTIVE
