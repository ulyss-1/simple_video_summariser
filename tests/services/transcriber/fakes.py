"""Test doubles and builders for the ingest handler tests (issue #28).

Metadata comes from the recorded #16 ``--dump-json`` fixtures, parsed by the
real ``YouTubeMetadata`` through a ``FakeRunner``; subtitle segments come from
the recorded #17 VTT fixtures. Nothing here touches the network or yt-dlp.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.testing import CapturingLogger

from adapters.youtube.metadata import YouTubeMetadata
from adapters.youtube.subtitles import SubtitleAvailability, YouTubeSubtitles
from adapters.youtube.vtt import parse_vtt
from common.config import Settings
from common.models import Segment, VideoMeta
from common.queue import Job
from common.worker import JobContext
from tests.adapters.youtube.fakes import FakeRunner, completed

TESTS = Path(__file__).resolve().parents[2]
METADATA_FIXTURES = TESTS / "adapters" / "youtube" / "fixtures" / "metadata"
VTT_FIXTURES = TESTS / "fixtures" / "vtt"

MANUAL_ID = "iG9CE55wbtY"  # manual_en.json
AUTO_ID = "zjkBMFhNj_g"  # auto_only.json
NO_CAPTIONS_ID = "BZiu46G4ukc"  # no_captions.json
TRANSLATED_ID = "pnMM6MwpaTc"  # non_english_translated_en.json

_NOW = datetime(2026, 9, 28, tzinfo=UTC)


def fixture_meta(name: str, video_id: str) -> VideoMeta:
    text = (METADATA_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    return YouTubeMetadata(runner=FakeRunner(completed(stdout=text))).fetch(video_id)


def manual_segments() -> list[Segment]:
    return parse_vtt((VTT_FIXTURES / "youtube_manual_iG9CE55wbtY.en.vtt").read_text("utf-8"))


def auto_segments() -> list[Segment]:
    return parse_vtt((VTT_FIXTURES / "youtube_auto_zjkBMFhNj_g.en.vtt").read_text("utf-8"))


def make_settings(**overrides: Any) -> Settings:
    return Settings(DATABASE_URL="postgresql://u:p@localhost/db", **overrides)  # type: ignore[arg-type]


def make_job(
    video_id: str, *, payload: dict[str, Any] | None = None, priority: int = 0
) -> Job:
    return Job(
        id=1,
        video_id=video_id,
        kind="ingest",
        dedupe_key="default",
        state="running",
        priority=priority,
        payload=payload if payload is not None else {},
        attempts=1,
        last_error=None,
        error_class=None,
        run_after=_NOW,
        locked_by="w",
        locked_at=_NOW,
        heartbeat_at=_NOW,
        finished_at=None,
        created_at=_NOW,
    )


class FakeMetadata:
    """Returns ``result`` (or raises it) and records every ``fetch``."""

    def __init__(self, result: VideoMeta | BaseException) -> None:
        self.result = result
        self.calls: list[str] = []

    def fetch(self, video_id: str) -> VideoMeta:
        self.calls.append(video_id)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeSubtitles:
    """``available`` is the real, pure #18 logic; ``fetch`` returns canned segments.

    ``manual`` / ``auto`` are the segments (or the exception) a download of that
    kind gives. ``on_fetch`` runs before each download (used to cancel mid-way).
    """

    def __init__(
        self,
        *,
        manual: list[Segment] | BaseException | None = None,
        auto: list[Segment] | BaseException | None = None,
        on_fetch: Callable[[str], None] | None = None,
    ) -> None:
        self.manual = manual if manual is not None else []
        self.auto = auto if auto is not None else []
        self.on_fetch = on_fetch
        self.fetch_calls: list[tuple[str, str, str]] = []
        self._real = YouTubeSubtitles()

    def available(self, meta: VideoMeta) -> SubtitleAvailability:
        return self._real.available(meta)

    def fetch(self, video_id: str, lang: str, kind: str) -> list[Segment]:
        self.fetch_calls.append((video_id, lang, kind))
        if self.on_fetch is not None:
            self.on_fetch(kind)
        result = self.manual if kind == "manual" else self.auto
        if isinstance(result, BaseException):
            raise result
        return list(result)


def _to_kwargs(_logger: object, _method: str, event_dict: Any) -> tuple[tuple[()], Any]:
    return (), event_dict


class RecordedLogs:
    """A structlog logger whose calls can be read back, independent of global config."""

    def __init__(self) -> None:
        self._capture = CapturingLogger()
        self.logger = structlog.wrap_logger(
            self._capture,
            processors=[_to_kwargs],
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        )

    def records(self, level: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(call.kwargs)
            for call in self._capture.calls
            if level is None or call.method_name == level
        ]


def make_ctx() -> tuple[JobContext, RecordedLogs]:
    logs = RecordedLogs()
    return JobContext(logs.logger), logs
