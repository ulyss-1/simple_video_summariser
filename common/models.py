"""Domain dataclasses shared across services and adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Segment:
    """A timed piece of transcript text; times are seconds from video start."""

    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class VideoMeta:
    """What the metadata source reports about one video."""

    video_id: str
    channel_id: str
    title: str
    description: str
    duration_sec: int | None
    published_at: datetime | None
    language: str | None
    live_status: str | None
    manual_subtitle_langs: tuple[str, ...]
    auto_caption_langs: tuple[str, ...]


class MetadataSource(Protocol):
    def fetch(self, video_id: str) -> VideoMeta: ...


# AudioRef and AudioSource, below, belong to #19 (audio acquisition and
# normalization). #19's own file list names only adapters/youtube/audio.py
# and its tests, but the port and its return type live in common/models.py
# per architecture.md §3 - the same place #16 put MetadataSource beside
# VideoMeta. #20's Transcriber port also consumes AudioRef, so it cannot
# live in adapters/youtube/ (services/ -> adapters/ -> common/, AGENTS.md).


@dataclass(frozen=True, slots=True)
class AudioRef:
    """Where normalized audio ended up, and its real duration.

    ``rel_path`` is relative to ``AUDIO_DIR`` (#10's storage rule), e.g.
    ``"ab/abc123def45.opus"`` - never an absolute path. ``bytes`` is the
    size of the file on disk. ``duration_sec`` is measured by ``ffprobe``
    on the normalized audio itself (#19), never taken from video metadata.
    """

    rel_path: str
    bytes: int
    duration_sec: float


class AudioSource(Protocol):
    # returns 16 kHz mono opus, relative to `dest` (architecture.md 3, D6b)
    def fetch_normalized(self, video_id: str, dest: Path) -> AudioRef: ...


# TranscriptResult/Transcriber belong to #20 (faster-whisper adapter). Same
# reasoning as AudioRef/AudioSource above: the port and its return type live
# here per architecture.md 3, next to what they consume (AudioRef, Segment).


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """What a ``Transcriber`` returns for one ``AudioRef``.

    ``segments`` are in time order, whitespace-trimmed, with empty segments
    already dropped. ``language`` is the language actually used for decoding
    - the caller's request if one was given, otherwise the detected one.
    ``engine_meta`` is a JSON-safe dict persisted verbatim to
    ``transcripts.engine_meta`` (architecture.md 7.2), e.g. engine name and
    version, model, compute_type, beam_size, vad, threads, audio_sec,
    load_sec, transcribe_sec and rtf.
    """

    segments: tuple[Segment, ...]
    language: str
    engine_meta: dict[str, Any]


class Transcriber(Protocol):
    def transcribe(
        self,
        audio: AudioRef,
        *,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> TranscriptResult: ...
