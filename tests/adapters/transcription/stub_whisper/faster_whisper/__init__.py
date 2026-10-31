"""A stand-in ``faster_whisper`` for running the real child module (issue #324).

Put its parent directory on ``PYTHONPATH`` of a child process. ``STUB_WHISPER_MODE``
picks the behaviour: ``ok`` yields two segments whose text records the
constructor arguments; ``block`` writes ``STUB_WHISPER_READY`` and then sleeps
forever inside ``transcribe``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

__version__ = "0.0-stub"


@dataclass
class _Segment:
    start: float
    end: float
    text: str


@dataclass
class _Info:
    language: str = "en"


class WhisperModel:
    def __init__(self, name: str, *, device: str, compute_type: str, cpu_threads: int) -> None:
        self.label = f"{name}/{device}/{compute_type}/{cpu_threads}"

    def transcribe(self, path: str, **_: object) -> tuple[object, _Info]:
        # A stand-in for a third-party module: it reads its knobs from the environment.
        if os.getenv("STUB_WHISPER_MODE") == "block":  # noqa: TID251
            with open(os.getenv("STUB_WHISPER_READY", "ready"), "w") as handle:  # noqa: TID251
                handle.write(str(os.getpid()))
            while True:
                time.sleep(60)
        segments = iter([_Segment(0.0, 1.0, f" {self.label} "), _Segment(1.0, 2.0, "  ")])
        return segments, _Info()
