"""Time-window transcript chunker (architecture.md §6, §7.3, D9/D9c).

Pure and deterministic: the same segments and parameters always give the same
chunks, and the ``time:<chunk_sec>:<overlap_sec>`` label names the parameter
set so a change produces a new chunk set instead of overwriting the old one.
Defaults live in ``Settings``; callers pass them in.
"""

import math
from bisect import bisect_left
from collections.abc import Sequence

from common.models import Chunk, Segment

# Float ``//`` and ``k * chunk_sec`` lose exactness beyond 2**53, which would
# make a window empty. A start past this (~285 million years) is rejected.
MAX_START_SEC = 2.0**53


def _validate_params(chunk_sec: int, overlap_sec: int) -> None:
    for name, value in (("chunk_sec", chunk_sec), ("overlap_sec", overlap_sec)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an int, got {value!r}")  # noqa: TRY004
    if chunk_sec <= 0:
        raise ValueError(f"chunk_sec must be > 0, got {chunk_sec}")
    if overlap_sec < 0:
        raise ValueError(f"overlap_sec must be >= 0, got {overlap_sec}")
    if overlap_sec >= chunk_sec:
        raise ValueError(
            f"overlap_sec ({overlap_sec}) must be < chunk_sec ({chunk_sec})"
        )


def chunk_strategy(chunk_sec: int, overlap_sec: int) -> str:
    """The strategy label stored with a chunk set, e.g. ``time:900:60``."""
    _validate_params(chunk_sec, overlap_sec)
    return f"time:{chunk_sec}:{overlap_sec}"


def chunk_segments(
    segments: Sequence[Segment], *, chunk_sec: int, overlap_sec: int
) -> list[Chunk]:
    """Split segments into windows anchored at video time 0.

    Window ``k`` owns the segments whose start is in
    ``[k*chunk_sec, (k+1)*chunk_sec)`` and additionally carries the segments
    starting up to ``overlap_sec`` before it. Segments are never split; a
    window with no core segment is skipped.

    Raises ``ValueError`` for bad parameters and for a non-blank segment that
    is non-finite, negative, ends before it starts, or starts after
    ``MAX_START_SEC`` (2**53 s, where window arithmetic stops being exact).
    """
    label = chunk_strategy(chunk_sec, overlap_sec)

    kept: list[Segment] = []
    for index, segment in enumerate(segments):
        if not segment.text.strip():
            continue
        start, end = segment.start, segment.end
        if (
            not (math.isfinite(start) and math.isfinite(end))
            or start < 0
            or end < start
            or start > MAX_START_SEC
        ):
            raise ValueError(
                f"segment {index} has an invalid span: start={start!r}, end={end!r}"
            )
        kept.append(segment)

    kept.sort(key=lambda s: (s.start, s.end))  # stable: ties keep input order
    starts = [s.start for s in kept]

    chunks: list[Chunk] = []
    position = 0
    while position < len(kept):
        window = int(starts[position] // chunk_sec)
        core_start = window * chunk_sec
        core_end = core_start + chunk_sec
        first = bisect_left(starts, max(0, core_start - overlap_sec))
        last = bisect_left(starts, core_end, lo=position)
        members = kept[first:last]
        chunks.append(
            Chunk(
                seq=len(chunks),
                start_sec=members[0].start,
                end_sec=max(s.end for s in members),
                text=" ".join(s.text.strip() for s in members),
                chunk_strategy=label,
            )
        )
        position = last
    return chunks
