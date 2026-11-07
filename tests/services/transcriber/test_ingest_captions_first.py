"""Captions-first ingest (issue #416): the 30 s caption cap, then Whisper at once.

The cap's clock is injected and the fake downloads advance it, so nothing sleeps. A 429,
an error, an empty track or no answer never defers or dead-letters the job: the video
goes to Whisper straight away, and the reason is recorded.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import structlog

from common.errors import RateLimitedError, ToolFailureError, TransientNetworkError
from services.transcriber.ingest import warn_if_prefer_whisper
from tests.services.transcriber.fakes import (
    AUTO_ID,
    MANUAL_ID,
    NO_CAPTIONS_ID,
    FakeMetadata,
    FakeSubtitles,
    auto_segments,
    fixture_meta,
    make_settings,
    manual_segments,
)
from tests.services.transcriber.test_ingest import Rig


class Clock:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


class TimedSubtitles(FakeSubtitles):
    """Records the ``timeout`` each download is given and advances the clock per call."""

    def __init__(self, clock: Clock, step: float = 0.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.step = step
        self.timeouts: list[float | None] = []

    def fetch(self, video_id: str, lang: str, kind: str, **kw: Any) -> Any:
        self.timeouts.append(kw.get("timeout"))
        self.clock.at += self.step
        return super().fetch(video_id, lang, kind, **kw)


def _whisper_reason(rig: Rig) -> str:
    [(state, _, _, payload)] = rig.jobs("transcribe")
    assert state == "pending"
    return str(payload["captions_unavailable"])


# --- the cap ---------------------------------------------------------------------


def test_each_download_gets_the_remaining_cap_as_its_timeout(
    conn: psycopg.Connection[Any],
) -> None:
    clock = Clock()
    subs = TimedSubtitles(clock, step=10.0, manual=[], auto=auto_segments())
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("manual_en", MANUAL_ID)),
        subs,
        monotonic=clock,
        CAPTIONS_CHECK_TIMEOUT_SEC=30,
    )

    rig.run(MANUAL_ID)

    assert subs.timeouts == [30.0, 20.0]  # manual (empty), then auto with what is left
    assert [row[0] for row in rig.transcripts()] == ["youtube_auto"]


def test_when_the_cap_is_spent_the_next_track_is_not_tried_and_whisper_starts(
    conn: psycopg.Connection[Any],
) -> None:
    clock = Clock()
    subs = TimedSubtitles(clock, step=30.0, manual=[], auto=auto_segments())
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("manual_en", MANUAL_ID)),
        subs,
        monotonic=clock,
        CAPTIONS_CHECK_TIMEOUT_SEC=30,
    )

    rig.run(MANUAL_ID)

    assert [c[2] for c in subs.fetch_calls] == ["manual"]  # no time left for auto
    assert _whisper_reason(rig) == "timeout"


def test_a_download_killed_at_the_cap_goes_to_whisper_at_once(
    conn: psycopg.Connection[Any],
) -> None:
    error = TransientNetworkError("yt-dlp timed out after 30.0s")
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("auto_only", AUTO_ID)),
        FakeSubtitles(auto=error),
    )

    rig.run(AUTO_ID)  # returns normally: no Defer, no failure, no attempt spent

    assert rig.transcripts() == []
    assert rig.jobs("analyze") == []
    assert _whisper_reason(rig) == "timeout"
    assert rig.logs.records("warning")


# --- every way of not getting captions ends in Whisper ----------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RateLimitedError("HTTP Error 429"), "rate_limited"),
        (ToolFailureError("exit 0 but no subtitle file"), "tool_failure"),
    ],
    ids=["429", "tool_failure"],
)
@pytest.mark.parametrize("kind", ["manual", "auto"])
def test_a_429_or_tool_failure_goes_straight_to_whisper(
    conn: psycopg.Connection[Any], error: Exception, reason: str, kind: str
) -> None:
    if kind == "manual":
        meta, subs, video_id = (
            fixture_meta("manual_en", MANUAL_ID),
            FakeSubtitles(manual=error, auto=[]),
            MANUAL_ID,
        )
    else:
        meta, subs, video_id = (
            fixture_meta("auto_only", AUTO_ID),
            FakeSubtitles(auto=error),
            AUTO_ID,
        )
    rig = Rig(conn, FakeMetadata(meta), subs)

    rig.run(video_id)

    assert rig.transcripts() == []
    assert _whisper_reason(rig) == reason
    if kind == "manual" and reason == "rate_limited":
        assert [c[2] for c in subs.fetch_calls] == ["manual"]  # no second request into a 429


def test_a_video_with_no_english_tracks_goes_to_whisper_with_reason_none(
    conn: psycopg.Connection[Any],
) -> None:
    rig = Rig(conn, FakeMetadata(fixture_meta("no_captions", NO_CAPTIONS_ID)), FakeSubtitles())

    rig.run(NO_CAPTIONS_ID)

    assert rig.subtitles.fetch_calls == []
    assert _whisper_reason(rig) == "none"


def test_an_empty_track_goes_to_whisper_with_reason_empty(
    conn: psycopg.Connection[Any],
) -> None:
    rig = Rig(conn, FakeMetadata(fixture_meta("auto_only", AUTO_ID)), FakeSubtitles(auto=[]))

    rig.run(AUTO_ID)

    assert _whisper_reason(rig) == "empty"


def test_the_ingest_log_line_names_why_whisper_was_chosen(
    conn: psycopg.Connection[Any],
) -> None:
    rig = Rig(conn, FakeMetadata(fixture_meta("auto_only", AUTO_ID)), FakeSubtitles(auto=[]))

    rig.run(AUTO_ID)

    events = [str(r["event"]) for r in rig.logs.records()]
    assert "whisper chosen: captions could not be acquired" in events
    assert rig.logs.records("info")[-1]["whisper_reason"] == "empty"


# --- success paths: manual first, then auto, no Whisper ---------------------------


def test_manual_captions_win_over_auto_when_both_exist(conn: psycopg.Connection[Any]) -> None:
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("manual_en", MANUAL_ID)),
        FakeSubtitles(manual=manual_segments(), auto=auto_segments()),
    )

    rig.run(MANUAL_ID)

    assert [row[0] for row in rig.transcripts()] == ["youtube_manual"]
    assert [c[2] for c in rig.subtitles.fetch_calls] == ["manual"]
    assert rig.jobs("transcribe") == []


# --- one test per origin ----------------------------------------------------------


@pytest.mark.parametrize("origin", ["adhoc", "rss", "backfill"])
def test_every_origin_uses_auto_captions_for_an_auto_only_video(
    conn: psycopg.Connection[Any], origin: str
) -> None:
    rig = Rig(
        conn, FakeMetadata(fixture_meta("auto_only", AUTO_ID)), FakeSubtitles(auto=auto_segments())
    )

    rig.run(AUTO_ID, payload={"origin": origin})

    assert [row[0] for row in rig.transcripts()] == ["youtube_auto"]
    assert rig.jobs("transcribe") == []
    assert len(rig.jobs("analyze")) == 1


@pytest.mark.parametrize("origin", ["adhoc", "rss", "backfill"])
def test_every_origin_prefers_manual_captions(conn: psycopg.Connection[Any], origin: str) -> None:
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("manual_en", MANUAL_ID)),
        FakeSubtitles(manual=manual_segments(), auto=auto_segments()),
    )

    rig.run(MANUAL_ID, payload={"origin": origin})

    assert [row[0] for row in rig.transcripts()] == ["youtube_manual"]
    assert rig.jobs("transcribe") == []


@pytest.mark.parametrize("origin", ["adhoc", "rss", "backfill"])
def test_every_origin_sends_a_429_to_whisper(conn: psycopg.Connection[Any], origin: str) -> None:
    rig = Rig(
        conn,
        FakeMetadata(fixture_meta("auto_only", AUTO_ID)),
        FakeSubtitles(auto=RateLimitedError("429")),
    )

    rig.run(AUTO_ID, payload={"origin": origin})

    assert _whisper_reason(rig) == "rate_limited"


# --- PREFER_WHISPER startup warning -----------------------------------------------


def test_prefer_whisper_logs_one_startup_warning() -> None:
    with structlog.testing.capture_logs() as logs:
        warn_if_prefer_whisper(make_settings(PREFER_WHISPER=True))

    [entry] = logs
    assert entry["log_level"] == "warning"
    assert "captions are ignored" in entry["event"].lower()


def test_without_prefer_whisper_there_is_no_startup_warning() -> None:
    with structlog.testing.capture_logs() as logs:
        warn_if_prefer_whisper(make_settings())

    assert logs == []
