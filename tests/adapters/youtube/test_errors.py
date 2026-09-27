import ast
import json
import pathlib
import tomllib
from typing import Any

import pytest

import adapters.youtube.errors
from adapters.youtube.errors import UpcomingVideoError, decode_ytdlp_json, from_ytdlp
from common.errors import (
    ErrorClass,
    JobError,
    PermanentSourceError,
    ToolFailureError,
    classify,
)

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "ytdlp_errors"
CASES: list[dict[str, Any]] = tomllib.loads((FIXTURES / "cases.toml").read_text())[
    "case"
]


def _case_id(case: dict[str, Any]) -> str:
    last = case["stderr"].strip().splitlines()[-1:] or ["<empty>"]
    return f"{case['error_class']}:{last[0][:60]}"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_real_ytdlp_stderr_maps_to_the_expected_class_and_reason(
    case: dict[str, Any],
) -> None:
    exc = from_ytdlp(case["stderr"], case["returncode"])

    assert isinstance(exc, JobError)
    assert classify(exc) == case["error_class"]
    if "reason" in case:
        assert isinstance(exc, PermanentSourceError)
        assert exc.reason == case["reason"]
    assert isinstance(exc, UpcomingVideoError) == case.get("upcoming", False)


def test_fixture_table_covers_every_class_yt_dlp_can_produce() -> None:
    produced = {c["error_class"] for c in CASES}
    assert produced == {
        "PERMANENT_SOURCE",
        "TRANSIENT_NETWORK",
        "RATE_LIMITED",
        "TOOL_FAILURE",
        "RESOURCE",
    }
    reasons = {c["reason"] for c in CASES if "reason" in c}
    assert reasons == {"removed", "private", "geoblocked", "agegated"}


def test_an_upcoming_video_retries_as_transient_network_until_65() -> None:
    exc = from_ytdlp(
        "ERROR: [youtube] j9epFget1W8: This live event will begin in 22 hours.\n", 1
    )
    assert isinstance(exc, UpcomingVideoError)
    assert classify(exc) is ErrorClass.TRANSIENT_NETWORK


def test_only_the_error_part_of_stderr_decides_the_class() -> None:
    # A retried timeout warning before a hard failure must not make it transient.
    stderr = (
        "WARNING: [youtube] timed out. Retrying (1/3)...\n"
        "ERROR: [youtube] yZIXLfi8CZQ: Private video\n"
    )
    exc = from_ytdlp(stderr, 1)
    assert isinstance(exc, PermanentSourceError)
    assert exc.reason == "private"


def test_warnings_alone_are_still_classified() -> None:
    exc = from_ytdlp("WARNING: [youtube] timed out. Retrying (1/3)...\n", 1)
    assert classify(exc) is ErrorClass.TRANSIENT_NETWORK


@pytest.mark.parametrize("stderr", ["", "\n", "   \n\t"])
def test_empty_stderr_is_tool_failure_never_bug(stderr: str) -> None:
    exc = from_ytdlp(stderr, 1)
    assert classify(exc) is ErrorClass.TOOL_FAILURE


def test_the_job_error_message_keeps_the_error_text_and_exit_code() -> None:
    exc = from_ytdlp("ERROR: [youtube] yZIXLfi8CZQ: Private video\n", 1)
    assert "Private video" in str(exc)
    assert "1" in str(exc)


def test_a_huge_stderr_does_not_become_a_huge_message() -> None:
    stderr = (
        "[download]  1.0% of 10MiB\n" * 100_000 + "ERROR: [youtube] x: Private video\n"
    )
    exc = from_ytdlp(stderr, 1)
    assert len(str(exc)) < 2_000
    assert "Private video" in str(exc)


# --- JSON output ----------------------------------------------------------

# The first bytes of real `yt-dlp --dump-json` output (yt-dlp 2026.08.19, live run
# on 2026-09-27, https://www.youtube.com/watch?v=jNQXAC9IVRw), cut off mid-stream.
# Cut before the first format URL, which embeds the caller's IP address.
TRUNCATED_DUMP_JSON = (
    '{"id": "jNQXAC9IVRw", "title": "Me at the zoo", "formats": [{"format_id": "233", '
)


@pytest.mark.parametrize(
    "stdout",
    [TRUNCATED_DUMP_JSON, "", "not json"],
    ids=["truncated", "empty", "garbage"],
)
def test_undecodable_ytdlp_output_is_tool_failure(stdout: str) -> None:
    with pytest.raises(ToolFailureError) as info:
        decode_ytdlp_json(stdout)
    assert classify(info.value) is ErrorClass.TOOL_FAILURE
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_decodable_ytdlp_output_is_returned() -> None:
    assert decode_ytdlp_json('{"id": "jNQXAC9IVRw", "title": "Me at the zoo"}') == {
        "id": "jNQXAC9IVRw",
        "title": "Me at the zoo",
    }


# --- import direction -----------------------------------------------------


def test_youtube_errors_imports_no_service() -> None:
    tree = ast.parse(pathlib.Path(adapters.youtube.errors.__file__).read_text())
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module
            else []
        )
    }
    assert "services" not in imported
