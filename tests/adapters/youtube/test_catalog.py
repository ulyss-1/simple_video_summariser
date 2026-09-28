"""YouTube channel catalog adapter (issue #27).

Fixtures under fixtures/catalog/ are trimmed yt-dlp ``--flat-playlist
--dump-single-json`` output (README.md there says which are real recordings and
which are hand-made). yt-dlp itself never runs: a ``FakeRunner`` stands in.
"""

import ast
import json
import logging
import pathlib
import subprocess
import sys
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import adapters.youtube.catalog
import common.models
from adapters.youtube.catalog import (
    DEFAULT_TIMEOUT_SEC,
    MAX_CATALOG_LIMIT,
    YouTubeCatalog,
)
from common.errors import (
    PermanentSourceError,
    RateLimitedError,
    ToolFailureError,
    TransientNetworkError,
    UnavailableReason,
)
from common.models import CatalogEntry, CatalogSource, ChannelCatalog
from tests.adapters.youtube.fakes import FakeRunner, completed

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "catalog"
CHANNEL = "UCXUPKJO5MZQN11PqgIvyuvQ"
PLAYLIST = "UUXUPKJO5MZQN11PqgIvyuvQ"
URL = f"https://www.youtube.com/playlist?list={PLAYLIST}"
YTDLP = [sys.executable, "-m", "yt_dlp"]


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 characters


def listing(entries: list[Any], **top: Any) -> dict[str, Any]:
    return {"id": PLAYLIST, "playlist_count": len(entries), "entries": entries} | top


def entry(n: int, **extra: Any) -> dict[str, Any]:
    return {"id": vid(n), "title": f"title {n}", "duration": 60 + n} | extra


def list_json(
    info: str | dict[str, Any], *, limit: int = 50, channel: str = CHANNEL
) -> ChannelCatalog:
    stdout = info if isinstance(info, str) else json.dumps(info)
    return YouTubeCatalog(runner=FakeRunner(completed(stdout=stdout))).list_uploads(
        channel, limit=limit
    )


def failing(stderr: str) -> YouTubeCatalog:
    return YouTubeCatalog(runner=FakeRunner(completed(stderr=stderr, returncode=1)))


# --- port and types ----------------------------------------------------------


def test_youtube_catalog_is_a_catalog_source() -> None:
    source: CatalogSource = YouTubeCatalog(runner=FakeRunner())
    assert callable(source.list_uploads)


def test_catalog_types_are_immutable() -> None:
    catalog = list_json(fixture_text("small_channel_end5"))
    with pytest.raises(AttributeError):
        catalog.total_count = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        catalog.entries[0].title = "x"  # type: ignore[misc]


def test_default_timeout_is_300_seconds() -> None:
    assert DEFAULT_TIMEOUT_SEC == 300
    assert MAX_CATALOG_LIMIT == 5000


def test_the_constructor_timeout_reaches_the_runner() -> None:
    runner = FakeRunner(completed(stdout=json.dumps(listing([]))))
    YouTubeCatalog(timeout=42.5, runner=runner).list_uploads(CHANNEL, limit=5)
    assert [t for _, t in runner.calls] == [42.5]


def test_the_default_timeout_reaches_the_runner() -> None:
    runner = FakeRunner(completed(stdout=json.dumps(listing([]))))
    YouTubeCatalog(runner=runner).list_uploads(CHANNEL, limit=5)
    assert [t for _, t in runner.calls] == [DEFAULT_TIMEOUT_SEC]


def test_a_timeout_is_a_transient_network_error() -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1))
    with pytest.raises(TransientNetworkError):
        YouTubeCatalog(timeout=1, runner=runner).list_uploads(CHANNEL, limit=5)


# --- input validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "channel_id",
    [
        CHANNEL + "x",  # 25 characters
        CHANNEL[:-1],  # 23 characters
        PLAYLIST,  # the uploads playlist ID
        "@NASA",
        f"https://www.youtube.com/channel/{CHANNEL}",
        f"https://www.youtube.com/playlist?list={PLAYLIST}",
        "UCXUPKJO5MZQN11PqgIv yuv"[:24],  # space
        "UCXUPKJO5MZQN11PqgIvyu;",  # ;
        CHANNEL[:-1] + "\n",  # 24 characters ending in a newline
        CHANNEL + "\n",
        "",
        None,
        123,
        b"UCXUPKJO5MZQN11PqgIvyuvQ",
    ],
)
def test_a_malformed_channel_id_is_rejected_before_any_process_starts(
    channel_id: Any,
) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        YouTubeCatalog(runner=runner).list_uploads(channel_id, limit=5)
    assert runner.calls == []


def test_a_channel_id_starting_its_suffix_with_a_dash_gets_a_full_url() -> None:
    channel = "UC-wNyEUrxzFU01234567890"
    assert len(channel) == 24
    playlist = "UU" + channel[2:]
    runner = FakeRunner(completed(stdout=json.dumps(listing([], id=playlist))))

    YouTubeCatalog(runner=runner).list_uploads(channel, limit=7)

    [(argv, _)] = runner.calls
    assert argv == [
        *YTDLP,
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        "7",
        "--no-warnings",
        f"https://www.youtube.com/playlist?list={playlist}",
    ]
    assert not any(a.startswith(("UC", "UU")) for a in argv)


@pytest.mark.parametrize("limit", [0, -1, 5001, True, False, 2.0, None, "5"])
def test_a_bad_limit_is_rejected_before_any_process_starts(limit: Any) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        YouTubeCatalog(runner=runner).list_uploads(CHANNEL, limit=limit)
    assert runner.calls == []


@pytest.mark.parametrize("limit", [1, 5000])
def test_the_limit_bounds_are_accepted(limit: int) -> None:
    runner = FakeRunner(completed(stdout=json.dumps(listing([]))))
    YouTubeCatalog(runner=runner).list_uploads(CHANNEL, limit=limit)
    [(argv, _)] = runner.calls
    assert argv[argv.index("--playlist-end") + 1] == str(limit)


# --- the yt-dlp call -------------------------------------------------------------


def test_one_flat_listing_run_per_call_with_no_per_video_extraction() -> None:
    runner = FakeRunner(completed(stdout=json.dumps(listing([entry(1)]))))

    YouTubeCatalog(runner=runner).list_uploads(CHANNEL, limit=20)

    [(argv, _)] = runner.calls
    assert argv == [
        *YTDLP,
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        "20",
        "--no-warnings",
        URL,
    ]


# --- output parsing --------------------------------------------------------------


def test_the_full_recording_is_listed_in_yt_dlps_order() -> None:
    catalog = list_json(fixture_text("small_channel_full"), limit=50)

    assert catalog.channel_id == CHANNEL
    assert catalog.total_count == 17
    assert len(catalog.entries) == 17
    assert catalog.entries[0] == CatalogEntry("EWvNQjAaOHw", "How I use LLMs", 7872)
    assert catalog.entries[-1].video_id == "Jv1ayv-04H4"
    assert [e.video_id for e in catalog.entries] == [
        e["id"] for e in json.loads(fixture_text("small_channel_full"))["entries"]
    ]


def test_a_playlist_end_run_reports_the_channels_full_count() -> None:
    catalog = list_json(fixture_text("small_channel_end5"), limit=5)

    assert len(catalog.entries) == 5
    assert catalog.total_count == 17
    full = list_json(fixture_text("small_channel_full"), limit=50)
    assert catalog.entries == full.entries[:5]


def test_an_empty_uploads_playlist_gives_no_entries() -> None:
    catalog = list_json(fixture_text("empty_channel"))
    assert catalog == ChannelCatalog(CHANNEL, (), 0)


def test_at_most_limit_entries_are_returned_even_if_yt_dlp_returns_more() -> None:
    info = listing([entry(n) for n in range(8)])

    catalog = list_json(info, limit=5)

    assert [e.video_id for e in catalog.entries] == [vid(n) for n in range(5)]
    assert catalog.total_count == 8


def test_dropped_entries_do_not_count_towards_the_limit() -> None:
    bad: list[Any] = [None, "x", 7, [], {"id": "short"}, {"title": "no id"}]
    info = listing([bad[0], entry(0), bad[1], bad[2], entry(1), bad[3], entry(2)])

    catalog = list_json(info, limit=2)

    assert [e.video_id for e in catalog.entries] == [vid(0), vid(1)]


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "short",
        "twelve_chars",
        "has space!!",
        "semi;colon;",
        "nl\n1234567",
        "é" * 11,
        5,
        None,
        True,
    ],
)
def test_an_entry_with_an_invalid_id_is_dropped(bad_id: Any) -> None:
    info = listing([{"id": bad_id, "title": "t"}, entry(1)])
    assert [e.video_id for e in list_json(info).entries] == [vid(1)]


def test_one_warning_per_call_counts_the_dropped_entries_without_raw_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SECRET;rm -rf"
    info = listing([None, {"id": secret}, entry(1), {"id": "bad"}, "x", entry(2)])

    with caplog.at_level(logging.DEBUG, logger="adapters.youtube.catalog"):
        catalog = list_json(info)

    assert len(catalog.entries) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "4" in message
    assert secret not in message
    assert "SECRET" not in message


def test_no_warning_when_nothing_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="adapters.youtube.catalog"):
        list_json(listing([entry(1), entry(2)]))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_duplicated_id_is_kept_only_at_its_first_position() -> None:
    info = listing(
        [
            entry(1),
            entry(2, title="first"),
            entry(1),
            entry(2, title="second"),
            entry(3),
        ]
    )

    catalog = list_json(info)

    assert [e.video_id for e in catalog.entries] == [vid(1), vid(2), vid(3)]
    assert catalog.entries[1].title == "first"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("A title", "A title"),
        ("", None),
        (None, None),
        (5, None),
        (["x"], None),
        (True, None),
    ],
)
def test_title_is_a_non_empty_string_or_none(title: Any, expected: str | None) -> None:
    catalog = list_json(listing([{"id": vid(1), "title": title}]))
    assert catalog.entries[0].title == expected


def test_a_missing_title_is_none() -> None:
    assert list_json(listing([{"id": vid(1)}])).entries[0].title is None


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0, 0),
        (61, 61),
        (61.9, 61),
        (-1, None),
        (-0.5, None),
        (True, None),
        (False, None),
        ("61", None),
        (None, None),
        ([61], None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_duration_is_an_int_or_none(duration: Any, expected: int | None) -> None:
    # json.dumps writes NaN/Infinity, which json.loads reads back.
    catalog = list_json(listing([{"id": vid(1), "duration": duration}]))
    assert catalog.entries[0].duration_sec == expected
    assert type(catalog.entries[0].duration_sec) in (int, type(None))


def test_a_missing_duration_is_none() -> None:
    assert list_json(listing([{"id": vid(1)}])).entries[0].duration_sec is None


def test_placeholder_entries_are_kept_for_ingest_to_classify() -> None:
    info = listing(
        [
            {"id": vid(1), "title": "[Private video]", "duration": None},
            {"id": vid(2), "title": "[Deleted video]"},
        ]
    )

    catalog = list_json(info)

    assert [e.title for e in catalog.entries] == ["[Private video]", "[Deleted video]"]
    assert [e.duration_sec for e in catalog.entries] == [None, None]


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (17, 17),
        (0, 0),
        (-1, None),
        (True, None),
        (3.0, None),
        ("17", None),
        (None, None),
    ],
)
def test_total_count_is_a_non_negative_int_or_none(
    count: Any, expected: int | None
) -> None:
    info = listing([entry(1)], playlist_count=count)
    assert list_json(info).total_count == expected


def test_a_missing_playlist_count_is_none() -> None:
    info = listing([entry(1)])
    del info["playlist_count"]
    assert list_json(info).total_count is None


def test_a_count_smaller_than_the_entries_is_reported_as_is() -> None:
    info = listing([entry(1), entry(2), entry(3)], playlist_count=1)
    catalog = list_json(info)
    assert catalog.total_count == 1
    assert len(catalog.entries) == 3


# --- property --------------------------------------------------------------------

_valid_entry = st.integers(0, 9).map(entry)
_invalid_entry = st.one_of(
    st.none(),
    st.text(max_size=5),
    st.integers(),
    st.just({"id": "short"}),
    st.just({"title": "no id"}),
    st.just({"id": "bad id;bad!"}),
)


@given(
    entries=st.lists(st.one_of(_valid_entry, _invalid_entry), max_size=30),
    limit=st.integers(1, 40),
)
def test_result_is_bounded_valid_unique_and_a_subsequence_of_the_input(
    entries: list[Any], limit: int
) -> None:
    catalog = list_json(listing(entries), limit=limit)

    ids = [e.video_id for e in catalog.entries]
    assert len(ids) <= limit
    assert all(len(i) == 11 and i.startswith("vid") for i in ids)
    assert len(set(ids)) == len(ids)
    source = iter(e["id"] for e in entries if isinstance(e, dict) and "id" in e)
    assert all(i in source for i in ids)  # consumes the iterator: order-preserving


# --- malformed output ------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        '"UU"',
        json.dumps({"id": PLAYLIST}),  # no entries
        json.dumps({"id": PLAYLIST, "entries": None}),
        json.dumps({"id": PLAYLIST, "entries": {"a": 1}}),
        json.dumps({"id": PLAYLIST, "entries": "abc"}),
        json.dumps({"entries": []}),  # no id
        json.dumps({"id": None, "entries": []}),
        json.dumps({"id": CHANNEL, "entries": []}),  # a UC, not a UU, ID
        json.dumps({"id": "UUaaaaaaaaaaaaaaaaaaaaaa", "entries": []}),  # other channel
    ],
)
def test_malformed_yt_dlp_output_is_a_tool_failure(stdout: str) -> None:
    with pytest.raises(ToolFailureError):
        list_json(stdout)


# --- failures --------------------------------------------------------------------


def _cases() -> list[str]:
    import tomllib

    path = (
        pathlib.Path(__file__).parents[2] / "fixtures" / "ytdlp_errors" / "cases.toml"
    )
    cases = tomllib.loads(path.read_text())["case"]
    return [
        c["stderr"]
        for c in cases
        if "youtube:tab" in c["stderr"] and "playlist does not exist" in c["stderr"]
    ]


def test_the_recorded_missing_playlist_cases_exist() -> None:
    assert len(_cases()) >= 3


@pytest.mark.parametrize("stderr", _cases())
def test_a_missing_or_upload_less_channel_is_permanently_removed(stderr: str) -> None:
    with pytest.raises(PermanentSourceError) as excinfo:
        failing(stderr).list_uploads(CHANNEL, limit=5)
    assert excinfo.value.reason == UnavailableReason.REMOVED


def test_a_terminated_channel_is_permanently_removed() -> None:
    # Recorded live (cases.toml): the uploads playlist of the terminated channel
    # UCx7T6qYK4VaP2-OhorrFS3Q says "The playlist does not exist", exactly like a
    # missing channel.
    recorded = [c for c in _cases() if "UUx7T6qYK4VaP2-OhorrFS3Q" in c]
    assert len(recorded) == 1
    with pytest.raises(PermanentSourceError) as excinfo:
        failing(recorded[0]).list_uploads("UCx7T6qYK4VaP2-OhorrFS3Q", limit=5)
    assert excinfo.value.reason == UnavailableReason.REMOVED


def test_a_real_channel_without_uploads_looks_like_a_missing_channel() -> None:
    # yt-dlp 2026.08.19 says "The playlist does not exist" for YouTube's own
    # "Sports" channel (UCEgdi0XIXXZ-qJOFPf4JSKw), which has no uploads. Pinned
    # here and in the module docstring: it is reported as REMOVED, not as an
    # empty catalog.
    stderr = (
        "ERROR: [youtube:tab] UUEgdi0XIXXZ-qJOFPf4JSKw: "
        "YouTube said: The playlist does not exist.\n"
    )
    with pytest.raises(PermanentSourceError) as excinfo:
        failing(stderr).list_uploads("UCEgdi0XIXXZ-qJOFPf4JSKw", limit=5)
    assert excinfo.value.reason == UnavailableReason.REMOVED
    assert "no uploads" in (adapters.youtube.catalog.__doc__ or "")


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube:tab] HTTP Error 429: Too Many Requests\n",
        "ERROR: [youtube:tab] Sign in to confirm you’re not a bot. Use --cookies\n",
    ],
)
def test_rate_limiting_and_the_bot_check_are_rate_limited(stderr: str) -> None:
    with pytest.raises(RateLimitedError):
        failing(stderr).list_uploads(CHANNEL, limit=5)


# --- layering --------------------------------------------------------------------


def test_common_does_not_import_adapters_and_catalog_does_not_import_services() -> None:
    def imports(module: Any) -> set[str]:
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    assert not any(n.split(".")[0] == "adapters" for n in imports(common.models))
    assert not any(
        n.split(".")[0] == "services" for n in imports(adapters.youtube.catalog)
    )
