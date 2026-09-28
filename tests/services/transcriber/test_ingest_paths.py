"""Decision table and trust-boundary checks for the ingest handler (issue #28).

Nothing here needs Postgres: the decision function is pure, and invalid input
must be rejected before the handler touches an adapter or the database, so a
placeholder connection is enough to prove it.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from adapters.youtube.subtitles import SubtitleAvailability
from services.transcriber.ingest import (
    TranscriptPath,
    choose_transcript_path,
    make_ingest_handler,
)
from tests.services.transcriber.fakes import (
    MANUAL_ID,
    FakeMetadata,
    FakeSubtitles,
    fixture_meta,
    make_ctx,
    make_job,
    make_settings,
)

M, T, A, H, N = (
    TranscriptPath.MANUAL,
    TranscriptPath.TRANSCRIBE,
    TranscriptPath.AUTO_FALLBACK,
    TranscriptPath.HAVE_TRANSCRIPT,
    TranscriptPath.NONE,
)

# (existing source, transcribe state, manual, prefer_whisper, auto, fallback) -> path
TABLE = [
    # an existing manual or whisper transcript always wins, whatever else is true
    ("youtube_manual", None, "en", False, "en", True, H),
    ("youtube_manual", "dead", None, True, None, False, H),
    ("whisper", None, None, False, None, False, H),
    ("whisper", "dead", "en", True, "en", True, H),
    ("whisper", "pending", "en", False, "en", True, H),
    # manual subtitles and PREFER_WHISPER=0
    (None, None, "en", False, None, False, M),
    (None, "dead", "en", False, "en", True, M),
    (None, "running", "en", False, "en", False, M),
    ("youtube_auto", "done", "en-GB", False, "en", True, M),
    # no manual, or PREFER_WHISPER=1, and transcription not dead
    (None, None, None, False, None, False, T),
    (None, None, None, False, "en", True, T),
    (None, None, "en", True, "en", True, T),
    (None, "pending", None, False, "en", True, T),
    (None, "running", "en", True, None, False, T),
    (None, "done", None, False, "en", True, T),
    ("youtube_auto", None, None, False, "en", True, T),
    # transcription dead, auto captions present, fallback on
    (None, "dead", None, False, "en", True, A),
    (None, "dead", "en", True, "en", True, A),
    ("youtube_auto", "dead", None, False, "en-US", True, A),
    # transcription dead and no usable auto captions or fallback off
    (None, "dead", None, False, None, True, N),
    (None, "dead", None, False, "en", False, N),
    (None, "dead", "en", True, None, False, N),
    ("youtube_auto", "dead", None, True, "en", False, N),
]


@pytest.mark.parametrize(("existing", "state", "manual", "prefer", "auto", "fallback", "expected"), TABLE)
def test_choose_transcript_path_follows_the_decision_table(
    existing: str | None,
    state: str | None,
    manual: str | None,
    prefer: bool,
    auto: str | None,
    fallback: bool,
    expected: TranscriptPath,
) -> None:
    path = choose_transcript_path(
        SubtitleAvailability(manual=manual, auto=auto),
        prefer_whisper=prefer,
        auto_caption_fallback=fallback,
        existing_source=existing,
        transcribe_state=state,
    )

    assert path is expected


def test_the_table_covers_every_path() -> None:
    assert {row[-1] for row in TABLE} == set(TranscriptPath)


def test_path_values_are_stable_strings() -> None:
    assert {p.name: p.value for p in TranscriptPath} == {
        "MANUAL": "manual",
        "TRANSCRIBE": "transcribe",
        "AUTO_FALLBACK": "auto_fallback",
        "HAVE_TRANSCRIPT": "have_transcript",
        "NONE": "none",
    }


# --- trust boundary ---------------------------------------------------------------


def run_with_placeholder_connection(
    video_id: str, payload: dict[str, Any] | None = None
) -> tuple[FakeMetadata, FakeSubtitles, list[tuple[Any, ...]]]:
    """Run the handler with a connection and queue that fail if touched."""
    metadata = FakeMetadata(fixture_meta("manual_en", MANUAL_ID))
    subtitles = FakeSubtitles()
    touched: list[tuple[Any, ...]] = []

    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            touched.append((name,))
            raise AssertionError(f"touched {name}")

    handler = make_ingest_handler(
        conn=cast(Any, Untouchable()),
        queue=cast(Any, Untouchable()),
        metadata=metadata,
        subtitles=subtitles,
        settings=make_settings(),
    )
    ctx, _ = make_ctx()
    try:
        handler(make_job(video_id, payload=payload), ctx)
    finally:
        assert metadata.calls == []
        assert subtitles.fetch_calls == []
        assert touched == []
    return metadata, subtitles, touched


@pytest.mark.parametrize(
    "video_id",
    [
        "--exec=x",
        "https://www.youtube.com/watch?v=iG9CE55wbtY",
        "iG9CE55wbt",  # 10 characters
        "iG9CE55wbtYY",  # 12 characters
        "",
        " ",
        "iG9CE55wbtY\n",
        "iG9CE55wbt\n",
        "iG9CE55wb Y",
        "iG9CE55wbt;",
        "iG9CE55wbtÿ",
    ],
)
def test_a_malformed_video_id_is_rejected_before_any_call(video_id: str) -> None:
    with pytest.raises(ValueError, match="video"):
        run_with_placeholder_connection(video_id)


@pytest.mark.parametrize("origin", ["", "manual", "RSS", "rss ", None, 1, ["rss"], {"a": 1}, True])
def test_an_invalid_origin_is_rejected_before_any_call(origin: object) -> None:
    with pytest.raises(ValueError, match="origin"):
        run_with_placeholder_connection(MANUAL_ID, {"origin": origin})
