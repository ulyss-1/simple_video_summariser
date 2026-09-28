"""Fake ports and a rig for driving ``main()`` without network, LLM or Whisper (issue #31)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import psycopg

from common.config import Settings
from common.models import (
    AudioRef,
    ChunkAnalysis,
    Claim,
    Segment,
    Topic,
    TranscriptResult,
    VideoMeta,
)
from services.cli.main import Deps
from tests.services.analyzer.fakes import FakeSummarizer
from tests.services.transcriber.fakes import (
    MANUAL_ID,
    NO_CAPTIONS_ID,
    FakeMetadata,
    FakeSubtitles,
    fixture_meta,
    make_settings,
    manual_segments,
)

__all__ = [
    "MANUAL_ID",
    "NO_CAPTIONS_ID",
    "FakeAudio",
    "FakeTranscriber",
    "Rig",
    "make_rig",
    "manual_rig",
    "no_captions_rig",
]

REL = "ab/audio.opus"


class FakeAudio:
    """Writes a real file under ``dest`` and records calls."""

    def __init__(self, *, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls: list[str] = []

    def fetch_normalized(self, video_id: str, dest: Path) -> AudioRef:
        self.calls.append(video_id)
        if self.exc is not None:
            raise self.exc
        target = dest / REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"opus")
        return AudioRef(REL, 4, 120.0)


class FakeTranscriber:
    def __init__(self, outcome: TranscriptResult | BaseException | None = None) -> None:
        if outcome is None:
            outcome = TranscriptResult(
                segments=tuple(Segment(float(i), float(i + 1), f"spoken word {i}") for i in range(3)),
                language="en",
                engine_meta={"engine": "fake"},
            )
        self.outcome = outcome
        self.calls = 0

    def transcribe(
        self,
        audio: AudioRef,
        *,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> TranscriptResult:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@dataclass
class Rig:
    deps: Deps
    metadata: FakeMetadata
    subtitles: FakeSubtitles
    audio: FakeAudio
    transcriber: Any
    summarizer: FakeSummarizer
    settings: Settings
    dsn: str


def make_rig(
    dsn: str,
    audio_dir: Path,
    *,
    meta: VideoMeta | BaseException,
    subtitles: FakeSubtitles | None = None,
    audio: FakeAudio | None = None,
    transcriber: Any = None,
    summarizer: FakeSummarizer | None = None,
    **settings_overrides: Any,
) -> Rig:
    settings = make_settings(AUDIO_DIR=audio_dir, **settings_overrides)
    metadata = FakeMetadata(meta)
    subs = subtitles if subtitles is not None else FakeSubtitles()
    aud = audio if audio is not None else FakeAudio()
    trans = transcriber if transcriber is not None else FakeTranscriber()
    summ = (
        summarizer
        if summarizer is not None
        else FakeSummarizer(
            name="ollama",
            default_chunk=ChunkAnalysis(
                topics=(Topic(seq=0, title="Opening remarks", start_sec=5.0),),
                claims=(Claim(text="Creativity matters."),),
                quotes=(),
            ),
        )
    )
    deps = Deps(
        settings=settings,
        connect=lambda: psycopg.connect(dsn),
        metadata=metadata,
        subtitles=subs,
        audio=aud,
        transcriber=trans,
        summarizer=summ,
    )
    return Rig(deps, metadata, subs, aud, trans, summ, settings, dsn)


def manual_rig(dsn: str, audio_dir: Path, **kwargs: Any) -> Rig:
    kwargs.setdefault("meta", fixture_meta("manual_en", MANUAL_ID))
    kwargs.setdefault("subtitles", FakeSubtitles(manual=manual_segments()))
    return make_rig(dsn, audio_dir, **kwargs)


def no_captions_rig(dsn: str, audio_dir: Path, **kwargs: Any) -> Rig:
    kwargs.setdefault("meta", fixture_meta("no_captions", NO_CAPTIONS_ID))
    return make_rig(dsn, audio_dir, **kwargs)


def with_video_id(meta: VideoMeta, video_id: str) -> VideoMeta:
    return replace(meta, video_id=video_id)
