"""The ingest handler hands the cancel signal to the metadata and subtitle calls (issue #302).

The fakes behave like the real adapters: while their ``cancelled()`` signal is true
they raise ``Cancelled``. The signal must read ``ctx.cancelled`` live, so the flag
is set from inside the call, as a heartbeat tick would while yt-dlp runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from common.errors import Cancelled
from common.models import Segment, VideoMeta
from services.transcriber.ingest import make_ingest_handler
from tests.services.transcriber.fakes import (
    MANUAL_ID,
    FakeMetadata,
    FakeSubtitles,
    fixture_meta,
    make_settings,
)
from tests.services.transcriber.test_ingest_shorts import UnitRig


class AwareMetadata(FakeMetadata):
    def __init__(self, meta: VideoMeta, on_call: Callable[[], None]) -> None:
        super().__init__(meta)
        self.on_call = on_call

    def fetch(
        self,
        video_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        **_kw: Any,
    ) -> VideoMeta:
        self.on_call()
        assert cancelled is not None, "ingest did not pass the cancel signal"
        if cancelled():
            raise Cancelled()
        return super().fetch(video_id)


class AwareSubtitles(FakeSubtitles):
    def __init__(self, on_call: Callable[[], None]) -> None:
        super().__init__()
        self.on_call = on_call

    def fetch(
        self,
        video_id: str,
        lang: str,
        kind: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        **_kw: Any,
    ) -> list[Segment]:
        self.on_call()
        assert cancelled is not None, "ingest did not pass the cancel signal"
        if cancelled():
            raise Cancelled()
        return super().fetch(video_id, lang, kind)


def rewire(rig: UnitRig, metadata: FakeMetadata, subtitles: FakeSubtitles) -> None:
    rig.subtitles = subtitles
    rig.handler = make_ingest_handler(
        conn=rig.conn,  # type: ignore[arg-type]
        queue=rig.queue,  # type: ignore[arg-type]
        metadata=metadata,
        subtitles=subtitles,
        settings=make_settings(),
    )


def test_a_cancel_during_the_metadata_fetch_writes_nothing_and_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = fixture_meta("manual_en", MANUAL_ID)
    rig = UnitRig(monkeypatch, meta)
    metadata = AwareMetadata(meta, rig.ctx._cancel)
    rewire(rig, metadata, FakeSubtitles())

    with pytest.raises(Cancelled):
        rig.run(MANUAL_ID)

    assert rig.events == []  # no videos upsert, no unavailable mark
    assert rig.subtitles.fetch_calls == []
    assert rig.queue.calls == []  # no successor job


def test_a_cancel_during_the_subtitle_download_stores_no_transcript_and_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = fixture_meta("manual_en", MANUAL_ID)
    rig = UnitRig(monkeypatch, meta)
    rewire(rig, FakeMetadata(meta), AwareSubtitles(rig.ctx._cancel))

    with pytest.raises(Cancelled):
        rig.run(MANUAL_ID)

    assert rig.queue.calls == []
    assert rig.subtitles.fetch_calls == []  # the fake raised before recording a result
