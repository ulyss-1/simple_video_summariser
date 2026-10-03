"""``median_whisper_rtf`` (issue #41)."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from common.repo.transcripts import median_whisper_rtf

pytestmark = pytest.mark.integration


def _add(conn: psycopg.Connection, vid: str, source: str, engine_meta: Any) -> None:
    conn.execute("INSERT INTO videos (video_id) VALUES (%s) ON CONFLICT DO NOTHING", (vid,))
    conn.execute(
        "INSERT INTO transcripts (video_id, source, segments, full_text, engine_meta, created_at)"
        " VALUES (%s, %s, '[]', '', %s::jsonb, now() - (%s * interval '1 minute'))",
        (vid, source, _json(engine_meta), int(vid[1:])),
    )


def _json(engine_meta: Any) -> str | None:
    if engine_meta is None or isinstance(engine_meta, _Raw):
        return engine_meta
    return json.dumps(engine_meta)


class _Raw(str):
    pass


def test_no_transcripts_gives_no_rtf(conn: psycopg.Connection) -> None:
    assert median_whisper_rtf(conn) == (None, 0)


def test_one_sample_is_its_own_median(conn: psycopg.Connection) -> None:
    _add(conn, "v0000000001", "whisper", {"rtf": 0.25})

    assert median_whisper_rtf(conn) == (0.25, 1)


def test_an_even_number_of_samples_takes_the_mean_of_the_middle_two(
    conn: psycopg.Connection,
) -> None:
    for i, value in enumerate([0.1, 0.2, 0.4, 0.8], start=1):
        _add(conn, f"v{i:010d}", "whisper", {"rtf": value})

    rtf, samples = median_whisper_rtf(conn)

    assert samples == 4
    assert rtf == pytest.approx(0.3)


def test_unusable_rtf_values_and_other_sources_are_ignored(conn: psycopg.Connection) -> None:
    bad: list[Any] = [
        None,
        {},
        {"rtf": None},
        {"rtf": 0},
        {"rtf": -1.5},
        {"rtf": "NaN"},
        {"rtf": "fast"},
        {"rtf": "0.5"},
        {"rtf": [0.5]},
        {"rtf": True},
        _Raw('{"rtf": 1e400}'),
        _Raw('[0.5]'),
        _Raw('0.5'),
        _Raw('{"rtf": 1e-400}'),
        _Raw('{"rtf": [1]}'),
    ]
    for i, meta in enumerate(bad, start=1):
        _add(conn, f"v{i:010d}", "whisper", meta)
    _add(conn, "v0000000090", "youtube_manual", {"rtf": 9.0})
    _add(conn, "v0000000091", "whisper", {"rtf": 0.5})

    assert median_whisper_rtf(conn) == (0.5, 1)


def test_only_the_newest_20_whisper_transcripts_are_used(conn: psycopg.Connection) -> None:
    for i in range(1, 21):
        _add(conn, f"v{i:010d}", "whisper", {"rtf": 1.0})
    for i in range(21, 31):
        _add(conn, f"v{i:010d}", "whisper", {"rtf": 99.0})

    assert median_whisper_rtf(conn) == (1.0, 20)


def test_unusable_rows_do_not_take_slots_in_the_newest_20(conn: psycopg.Connection) -> None:
    _add(conn, "v0000000001", "whisper", {"rtf": 2.0})
    for i in range(2, 30):
        _add(conn, f"v{i:010d}", "whisper", _Raw('{"rtf": 1e400}') if i % 2 else {"rtf": "x"})

    assert median_whisper_rtf(conn) == (2.0, 1)
