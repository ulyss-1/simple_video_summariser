"""Cancel checkpoints in the ingest and transcribe handlers (issue #237).

Unit tests (no Postgres) for the ingest checkpoints and the decision table; the
integration tests run the real queue so the successor guard is exercised too.
"""

from __future__ import annotations

import pytest

from adapters.youtube.subtitles import SubtitleAvailability
from common.errors import Cancelled
from services.transcriber import ingest as ingest_module
from services.transcriber.ingest import TranscriptPath, choose_transcript_path
from tests.services.transcriber.fakes import MANUAL_ID, fixture_meta
from tests.services.transcriber.test_ingest_shorts import UnitRig

# --- ingest checkpoints ---------------------------------------------------------------


def test_a_request_seen_after_the_metadata_fetch_writes_nothing_and_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = UnitRig(monkeypatch, fixture_meta("manual_en", MANUAL_ID))
    rig.ctx._cancel()

    with pytest.raises(Cancelled):
        rig.run(MANUAL_ID)

    assert rig.events == []  # no videos upsert, no unavailable mark
    assert rig.subtitles.fetch_calls == []
    assert rig.queue.calls == []


def test_a_request_seen_before_a_subtitle_download_makes_no_subtitle_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = UnitRig(monkeypatch, fixture_meta("manual_en", MANUAL_ID))
    monkeypatch.setattr(
        ingest_module, "clear_unavailable", lambda conn, vid: rig.ctx._cancel()
    )

    with pytest.raises(Cancelled):
        rig.run(MANUAL_ID)

    assert rig.subtitles.fetch_calls == []
    assert rig.queue.calls == []


# --- the decision table ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_class", "expected"),
    [
        (None, TranscriptPath.AUTO_FALLBACK),
        ("TOOL_FAILURE", TranscriptPath.AUTO_FALLBACK),
        ("CANCELLED", TranscriptPath.TRANSCRIBE),
    ],
)
def test_a_cancelled_transcribe_is_not_a_dead_letter_so_ingest_transcribes_again(
    error_class: str | None, expected: TranscriptPath
) -> None:
    path = choose_transcript_path(
        SubtitleAvailability(manual=None, auto="en"),
        prefer_whisper=False,
        auto_caption_fallback=True,
        existing_source=None,
        transcribe_state="dead",
        transcribe_error_class=error_class,
    )

    assert path is expected
