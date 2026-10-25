"""Cancel checkpoints in the transcribe handler, against the real queue (issue #237)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from common.errors import Cancelled
from common.models import AudioRef
from tests.services.transcriber.test_transcribe import (
    REL,
    FakeAudio,
    FakeTranscriber,
    Rig,
    make_rig,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.integration
# --- transcribe checkpoints (real queue) -----------------------------------------------


def _request_cancel(rig: Rig) -> None:
    rig.conn.execute(
        "UPDATE jobs SET cancel_requested_at = now() WHERE kind = 'transcribe'"
    )
    rig.conn.commit()


def test_a_request_seen_during_transcription_stops_at_the_next_progress_call_and_saves_nothing(
    make_rig: Callable[..., Rig],
) -> None:
    holder: list[Rig] = []

    def request_midway(_: AudioRef) -> None:
        holder[
            0
        ].ctx._cancel()  # what the heartbeat tick does once the request is stored

    rig = make_rig(transcriber=FakeTranscriber(on_call=request_midway, progress=(1.0,)))
    holder.append(rig)

    with pytest.raises(Cancelled):
        rig.run()

    assert rig.count("transcripts") == 0
    assert rig.analyze_jobs() == []


def test_a_cancelled_transcribe_with_audio_keep_off_removes_the_file_and_the_media_row(
    make_rig: Callable[..., Rig],
) -> None:
    holder: list[Rig] = []
    rig = make_rig(
        AUDIO_KEEP=False,
        transcriber=FakeTranscriber(
            on_call=lambda _: holder[0].ctx._cancel(), progress=(1.0,)
        ),
    )
    holder.append(rig)

    with pytest.raises(Cancelled):
        rig.run()

    assert not rig.file(REL).exists()
    assert rig.media() == []


def test_a_request_stored_before_the_handoff_enqueues_no_analyze_job(
    make_rig: Callable[..., Rig],
) -> None:
    holder: list[Rig] = []

    def request_stored(_: AudioRef) -> None:
        _request_cancel(
            holder[0]
        )  # in the database, but the heartbeat has not carried it yet

    rig = make_rig(transcriber=FakeTranscriber(on_call=request_stored))
    holder.append(rig)

    with pytest.raises(Cancelled):  # the handoff refuses
        rig.run()

    assert rig.analyze_jobs() == []


def test_a_job_cancelled_before_the_download_makes_no_audio_source_call(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(audio=FakeAudio())
    rig.ctx._cancel()

    with pytest.raises(Cancelled):
        rig.run()

    assert rig.audio.calls == []
    assert rig.file(REL).exists() is False
