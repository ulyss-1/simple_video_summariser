"""Domain dataclasses shared across services and adapters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
