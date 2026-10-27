"""YouTube playlist listing, ``YouTubeCatalog.list_playlist`` (issue #342).

Fixtures under fixtures/playlist/ are trimmed yt-dlp ``--flat-playlist
--dump-single-json`` recordings (README.md there). yt-dlp itself never runs: a
``FakeRunner`` stands in.
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import sys
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapters.youtube.catalog import MAX_CATALOG_LIMIT, YouTubeCatalog
from common.errors import (
    PermanentSourceError,
    RateLimitedError,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
)
from common.models import PlaylistListing, PlaylistSource
from tests.adapters.youtube.fakes import FakeRunner, completed

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "playlist"
CASES = pathlib.Path(__file__).parents[2] / "fixtures" / "ytdlp_errors" / "cases.toml"
PLAYLIST = "PLAqhIrjkxbuWI23v9cThsA9GvCAUhRvKZ"
URL = f"https://www.youtube.com/playlist?list={PLAYLIST}"
YTDLP = [sys.executable, "-m", "yt_dlp"]
FIRST, SECOND, THIRD = "VMj-3S1tku0", "PaCmpygFfXo", "TCH_1BHY58I"


def fixture(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return doc


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 characters


def listing_doc(entries: list[Any], **top: Any) -> dict[str, Any]:
    return {
        "id": PLAYLIST,
        "title": "T",
        "playlist_count": len(entries),
        "entries": entries,
    } | top


def list_json(
    info: str | dict[str, Any], *, limit: int = 200, playlist: str = PLAYLIST
) -> PlaylistListing:
    stdout = info if isinstance(info, str) else json.dumps(info)
    return YouTubeCatalog(runner=FakeRunner(completed(stdout=stdout))).list_playlist(
        playlist, limit=limit
    )


def failing(stderr: str) -> YouTubeCatalog:
    return YouTubeCatalog(runner=FakeRunner(completed(stderr=stderr, returncode=1)))


def test_youtube_catalog_is_a_playlist_source() -> None:
    source: PlaylistSource = YouTubeCatalog(runner=FakeRunner())
    assert callable(source.list_playlist)


# --- the command ------------------------------------------------------------------


def test_the_command_is_the_flat_listing_of_the_canonical_url() -> None:
    runner = FakeRunner(completed(stdout=fixture_text("normal_playlist")))
    YouTubeCatalog(runner=runner, timeout=90).list_playlist(PLAYLIST, limit=200)

    [(argv, timeout)] = runner.calls
    assert argv == [
        *YTDLP,
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        "200",
        "--no-warnings",
        URL,
    ]
    assert timeout == 90


@pytest.mark.parametrize("bad", ["", "RD" + "a" * 20, "LL", "--help", PLAYLIST + "\n", "UC" + "a" * 22])
def test_an_invalid_playlist_id_never_reaches_a_process(bad: str) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        YouTubeCatalog(runner=runner).list_playlist(bad, limit=5)
    assert runner.calls == []


@pytest.mark.parametrize("limit", [0, -1, MAX_CATALOG_LIMIT + 1, True, 2.5, "5", None])
def test_a_bad_limit_never_reaches_a_process(limit: Any) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        YouTubeCatalog(runner=runner).list_playlist(PLAYLIST, limit=limit)
    assert runner.calls == []


# --- recorded fixtures --------------------------------------------------------------


def test_a_normal_playlist_keeps_order_and_title() -> None:
    result = list_json(fixture_text("normal_playlist"))

    assert result.playlist_id == PLAYLIST
    assert result.title == "Neural Networks: Zero to Hero"
    assert result.total_count == 10
    assert result.skipped == 0
    assert [e.video_id for e in result.entries] == [e["id"] for e in fixture("normal_playlist")["entries"]]
    assert [e.video_id for e in result.entries][:3] == [FIRST, SECOND, THIRD]
    assert result.entries[0].title == (
        "The spelled-out intro to neural networks and backpropagation: building micrograd"
    )
    assert result.entries[0].duration_sec == 8752


def test_a_playlist_longer_than_the_limit_reports_the_total_and_returns_the_limit() -> None:
    # Real recording with --playlist-end 3: 3 entries while playlist_count still says 10.
    result = list_json(fixture_text("normal_playlist_end3"), limit=3)
    assert [e.video_id for e in result.entries] == [FIRST, SECOND, THIRD]
    assert result.total_count == 10


def test_a_listing_longer_than_the_injected_limit_is_cut_at_the_limit() -> None:
    result = list_json(fixture_text("normal_playlist"), limit=4)
    assert len(result.entries) == 4
    assert result.total_count == 10


def test_private_and_deleted_entries_are_returned_as_listed() -> None:
    doc = fixture("private_and_deleted")
    result = list_json(json.dumps(doc), limit=200, playlist=doc["id"])

    titles = {e.video_id: e.title for e in result.entries}
    assert titles["ETbOX50t0kw"] == "[Private video]"
    assert titles["RcUnmaanrNM"] == "[Deleted video]"
    assert [e.video_id for e in result.entries] == [e["id"] for e in doc["entries"]]
    private = next(e for e in result.entries if e.video_id == "ETbOX50t0kw")
    assert private.duration_sec is None
    assert result.skipped == 0


def test_a_repeated_video_is_kept_once_at_its_first_position() -> None:
    doc = fixture("duplicate_entries")
    ids = [e["id"] for e in doc["entries"]]
    repeated = {i for i in ids if ids.count(i) > 1}
    assert repeated  # the recording does contain a repeat

    result = list_json(json.dumps(doc), limit=200, playlist=doc["id"])

    got = [e.video_id for e in result.entries]
    assert got == list(dict.fromkeys(ids))
    assert result.skipped == len(ids) - len(got) == len(repeated)


def test_an_empty_playlist_has_no_entries() -> None:
    # SYNTHETIC fixture (README): no real empty playlist could be recorded.
    result = list_json(fixture_text("empty_playlist"))
    assert result.entries == ()
    assert result.total_count == 0
    assert result.title == "Empty playlist"


# --- the untrusted listing -----------------------------------------------------------


def test_entries_that_are_not_canonical_video_ids_are_dropped_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SECRET-RAW-VALUE"
    entries: list[Any] = [
        {"id": vid(1), "title": "ok"},
        {"id": "short"},
        {"id": vid(2) + "x"},
        {"id": None},
        {"id": 7},
        {"title": "no id"},
        "string",
        None,
        {"id": secret + "!!"},
        {"id": vid(3), "title": "also ok"},
    ]
    with caplog.at_level(logging.WARNING):
        result = list_json(listing_doc(entries))

    assert [e.video_id for e in result.entries] == [vid(1), vid(3)]
    assert result.skipped == 8
    assert "dropped 8" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("T", "T"), ("", None), (None, None), (5, None)],
)
def test_the_playlist_title_must_be_a_non_empty_string(raw: Any, expected: str | None) -> None:
    assert list_json(listing_doc([], title=raw)).title == expected


def test_the_title_and_duration_of_an_entry_are_sanitised() -> None:
    entries = [
        {"id": vid(1), "title": "", "duration": -5},
        {"id": vid(2), "title": 3, "duration": True},
        {"id": vid(3), "title": "x", "duration": float("inf")},
        {"id": vid(4), "title": "x", "duration": 61.9},
    ]
    result = list_json(json.dumps(listing_doc(entries)))
    assert [(e.title, e.duration_sec) for e in result.entries] == [
        (None, None),
        (None, None),
        ("x", None),
        ("x", 61),
    ]


@pytest.mark.parametrize("count", [None, -1, True, "5", 2.5])
def test_an_unusable_playlist_count_is_none(count: Any) -> None:
    assert list_json(listing_doc([], playlist_count=count)).total_count is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        '"PL"',
        json.dumps({"id": PLAYLIST}),
        json.dumps({"id": PLAYLIST, "entries": None}),
        json.dumps({"id": PLAYLIST, "entries": {"a": 1}}),
        json.dumps({"entries": []}),
        json.dumps({"id": None, "entries": []}),
        json.dumps({"id": "PL" + "b" * 20, "entries": []}),  # a different playlist
    ],
)
def test_malformed_yt_dlp_output_is_a_tool_failure(stdout: str) -> None:
    with pytest.raises(ToolFailureError):
        list_json(stdout)


@given(st.lists(st.one_of(st.none(), st.integers(), st.text(max_size=15), st.dictionaries(
    st.sampled_from(["id", "title", "duration"]), st.one_of(st.none(), st.integers(), st.text(max_size=15)),
)), max_size=30), st.integers(1, 40))
def test_whatever_the_listing_holds_the_result_is_bounded_unique_and_valid(
    items: list[Any], limit: int
) -> None:
    from common.youtube_refs import is_video_id

    result = list_json(json.dumps(listing_doc(items)), limit=limit)
    ids = [e.video_id for e in result.entries]
    assert len(ids) <= limit
    assert len(ids) == len(set(ids))
    assert all(is_video_id(i) for i in ids)


# --- failures --------------------------------------------------------------------------


def _recorded(needle: str) -> list[str]:
    import tomllib

    cases = tomllib.loads(CASES.read_text())["case"]
    return [c["stderr"] for c in cases if needle in c["stderr"]]


def test_a_missing_playlist_is_permanently_removed() -> None:
    [stderr] = _recorded("PL0123456789ABCDEF: YouTube said: The playlist does not exist")
    with pytest.raises(PermanentSourceError) as excinfo:
        failing(stderr).list_playlist("PL0123456789ABCDEF", limit=5)
    assert excinfo.value.reason == UnavailableReason.REMOVED


def test_a_private_playlist_reads_like_a_missing_one() -> None:
    [stderr] = _recorded("PLtPgu7CB4gbY9oDN3drwC3cMbJggS7dKl")
    with pytest.raises(PermanentSourceError) as excinfo:
        failing(stderr).list_playlist("PLtPgu7CB4gbY9oDN3drwC3cMbJggS7dKl", limit=5)
    assert excinfo.value.reason == UnavailableReason.REMOVED


def test_a_shape_valid_id_that_youtube_rejects_is_a_tool_failure() -> None:
    [stderr] = _recorded("HTTP Error 400: Bad Request")
    with pytest.raises(ToolFailureError):
        failing(stderr).list_playlist("PL" + "a" * 32, limit=5)


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube:tab] HTTP Error 429: Too Many Requests\n",
        "ERROR: [youtube:tab] Sign in to confirm you’re not a bot. Use --cookies\n",
    ],
)
def test_rate_limiting_is_rate_limited(stderr: str) -> None:
    with pytest.raises(RateLimitedError):
        failing(stderr).list_playlist(PLAYLIST, limit=5)


def test_a_timeout_is_a_transient_network_error() -> None:
    catalog = YouTubeCatalog(runner=FakeRunner(subprocess.TimeoutExpired(["yt-dlp"], 90)))
    with pytest.raises(TransientNetworkError):
        catalog.list_playlist(PLAYLIST, limit=5)


def test_a_network_failure_is_a_transient_network_error() -> None:
    stderr = "ERROR: [youtube:tab] Unable to download API page: <urlopen error [Errno -3] Temporary failure in name resolution>\n"
    with pytest.raises(TransientNetworkError):
        failing(stderr).list_playlist(PLAYLIST, limit=5)


def test_unexpected_stderr_is_a_tool_failure() -> None:
    with pytest.raises(ToolFailureError):
        failing("Traceback (most recent call last):\nKeyError: 'x'\n").list_playlist(PLAYLIST, limit=5)
