"""Domain dataclasses shared across services and adapters."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Segment:
    """A timed piece of transcript text; times are seconds from video start."""

    start: float
    end: float
    text: str
    speaker: str | None = None
