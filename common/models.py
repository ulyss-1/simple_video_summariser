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


# Channel, Transcript, Chunk, Analysis, Topic, Claim and Quote, below,
# belong to #14 (the repository layer, common/repo/). They mirror the
# tables in architecture.md §6 one-for-one; common/repo/ functions build
# and return these instead of ever returning a raw row or tuple.


@dataclass(frozen=True, slots=True)
class Channel:
    """A monitored (or once-monitored) YouTube channel."""

    channel_id: str
    title: str | None
    active: bool
    monitor_from: datetime
    last_polled: datetime | None
    last_poll_err: str | None
    added_at: datetime


@dataclass(frozen=True, slots=True)
class Transcript:
    """A saved transcript for one video, from one source (architecture.md §6)."""

    id: int
    video_id: str
    source: str  # youtube_manual | youtube_auto | whisper
    language: str | None
    speaker_source: str  # subtitle_labels | none  (C3)
    segments: tuple[Segment, ...]
    full_text: str
    engine_meta: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Chunk:
    """One window of a transcript under a given ``chunk_strategy`` (§7.3).

    ``id``, ``transcript_id`` and ``chunk_strategy`` default to ``None``
    because ``save_chunks`` (``common/repo/transcripts.py``) only needs
    ``seq``/``start_sec``/``end_sec``/``text`` from its caller - the
    transcript id and strategy are already that function's own arguments.
    ``get_chunks`` always returns all three set.
    """

    seq: int
    start_sec: float
    end_sec: float
    text: str
    chunk_strategy: str | None = None
    transcript_id: int | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Topic:
    """One topic under an analysis, ordered by ``seq``."""

    seq: int
    title: str
    summary: str | None = None
    start_sec: float | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    """One claim under an analysis, ordered by ``start_sec``."""

    text: str
    speaker: str = "unknown"
    start_sec: float | None = None
    confidence: str | None = None  # high | medium | low
    source_chunk_seq: int | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    """One quote under an analysis."""

    text: str
    speaker: str = "unknown"
    start_sec: float | None = None
    source_chunk_seq: int | None = None
    id: int | None = None


# RosterSpeaker, Roster and ChunkAnalysis belong to #22 (LLM output schema,
# adapters/summarize/schema.py). They are what its parsers return, so they
# live here beside Topic/Claim/Quote, keeping pydantic types out of every
# public signature.


@dataclass(frozen=True, slots=True)
class RosterSpeaker:
    """One named speaker from the roster pass (architecture.md 8.1)."""

    name: str
    role: str  # host | guest | panelist | unknown


@dataclass(frozen=True, slots=True)
class Roster:
    """The speakers of one video; every later ``speaker`` is one of these or ``unknown``."""

    speakers: tuple[RosterSpeaker, ...]

    def to_json(self) -> dict[str, object]:
        """The value stored in ``analyses.speaker_roster``."""
        return {"speakers": [{"name": s.name, "role": s.role} for s in self.speakers]}


@dataclass(frozen=True, slots=True)
class ChunkAnalysis:
    """A validated per-chunk analysis plus what validation had to repair.

    ``speaker_coercions`` counts speakers replaced by ``unknown`` because they
    were not in the roster; ``start_sec_clamped`` counts timestamps moved into
    the chunk span; ``items_dropped`` counts items removed for being blank,
    over a length cap or past a list cap.
    """

    topics: tuple[Topic, ...]
    claims: tuple[Claim, ...]
    quotes: tuple[Quote, ...]
    speaker_coercions: int = 0
    start_sec_clamped: int = 0
    items_dropped: int = 0


@dataclass(frozen=True, slots=True)
class Analysis:
    """One LLM analysis pass over a video's transcript, with its children.

    ``id`` and ``created_at`` default to ``None``: they are unset until
    ``save_analysis`` (``common/repo/analyses.py``) persists the row.
    ``latest_analysis`` always returns both populated.
    """

    video_id: str
    transcript_id: int
    chunk_strategy: str
    model: str
    prompt_version: str
    tldr: str
    speaker_roster: dict[str, object] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    topics: tuple[Topic, ...] = ()
    claims: tuple[Claim, ...] = ()
    quotes: tuple[Quote, ...] = ()
    id: int | None = None
    created_at: datetime | None = None


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
