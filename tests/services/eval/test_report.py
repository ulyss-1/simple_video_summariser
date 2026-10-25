"""The eval report renderer (issue #62): pure functions, no database."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

import pytest

from common.models import Analysis, AnalysisRun, Claim, Quote, Topic
from services.eval.report import (
    RunLabel,
    VideoEntry,
    group_windows,
    md_text,
    render_report,
    timestamp_link,
    window_label,
)

VID = "dQw4w9WgXcQ"
OTHER = "-wNyEUrxzFU"
LABEL_A = RunLabel("qwen3.5:4b", "v1")
LABEL_B = RunLabel("claude-haiku-4-5", "v2")


def claim(text: str, start: float | None, speaker: str = "unknown", conf: str | None = None) -> Claim:
    return Claim(text=text, speaker=speaker, start_sec=start, confidence=conf)


def run(
    label: RunLabel,
    *,
    claims: tuple[Claim, ...] = (),
    quotes: tuple[Quote, ...] = (),
    topics: tuple[Topic, ...] = (),
    roster: object = None,
    source: str = "whisper",
    strategy: str = "900s/60s",
    transcript_id: int = 1,
    tldr: str = "summary",
    tokens: tuple[int, int] = (100, 50),
    duration_ms: int | None = 1500,
    cost: float | None = None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    calls: int | None = None,
    created: datetime | None = datetime(2026, 10, 1, 12, 30, 5, tzinfo=UTC),
) -> AnalysisRun:
    return AnalysisRun(
        Analysis(
            video_id=VID,
            transcript_id=transcript_id,
            chunk_strategy=strategy,
            model=label.model,
            prompt_version=label.prompt_version,
            tldr=tldr,
            speaker_roster=roster,  # type: ignore[arg-type]
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_usd=cost,
            duration_ms=duration_ms,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            calls=calls,
            topics=topics,
            claims=claims,
            quotes=quotes,
            created_at=created,
        ),
        source,
    )


# ------------------------------------------------------------------- windows


def test_both_sides_empty_gives_no_windows() -> None:
    assert group_windows([], []) == ()


def test_window_boundaries() -> None:
    items = [claim("a", 0), claim("b", 299.999), claim("c", 300), claim("d", 599.999)]
    windows = group_windows(items, [])
    assert [(w.index, [c.text for c in w.a]) for w in windows] == [(0, ["a", "b"]), (1, ["c", "d"])]
    assert window_label(0) == "0:00-5:00"
    assert window_label(1) == "5:00-10:00"
    assert window_label(12) == "1:00:00-1:05:00"


def test_a_window_with_only_one_side_still_appears() -> None:
    windows = group_windows([claim("a", 10)], [claim("b", 700)])
    assert [(w.index, len(w.a), len(w.b)) for w in windows] == [(0, 1, 0), (2, 0, 1)]


def test_missing_and_invalid_timestamps_go_in_a_final_no_timestamp_group() -> None:
    bad = [claim("n", None), claim("neg", -1), claim("nan", math.nan), claim("inf", math.inf)]
    windows = group_windows([claim("ok", 5), *bad], [])
    assert [w.index for w in windows] == [0, None]
    assert [c.text for c in windows[-1].a] == ["n", "neg", "nan", "inf"]


def test_only_no_timestamp_items() -> None:
    windows = group_windows([claim("n", None)], [claim("m", None)])
    assert [(w.index, len(w.a), len(w.b)) for w in windows] == [(None, 1, 1)]


def test_a_three_hour_run_has_36_windows() -> None:
    items = [claim(f"c{i}", i * 300.0) for i in range(36)]
    assert len(group_windows(items, [])) == 36


# ---------------------------------------------------------------------- links


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (0, f"https://www.youtube.com/watch?v={VID}&t=0s"),
        (75.9, f"https://www.youtube.com/watch?v={VID}&t=75s"),
        (3600.0, f"https://www.youtube.com/watch?v={VID}&t=3600s"),
        (None, None),
        (-0.5, None),
        (math.nan, None),
        (math.inf, None),
        (-math.inf, None),
    ],
)
def test_timestamp_link(start: float | None, expected: str | None) -> None:
    assert timestamp_link(VID, start) == expected


def test_no_link_for_an_unvalidated_video_id() -> None:
    assert timestamp_link("not an id", 5) is None
    assert timestamp_link(f"{VID}&x=1", 5) is None


# ------------------------------------------------------------------- escaping

HOSTILE = "<script>alert(1)</script> | [x](javascript:alert(1)) **bold**"


def test_md_text_neutralises_html_markdown_and_links() -> None:
    out = md_text(HOSTILE)
    assert "<" not in out and ">" not in out
    assert "|" not in out.replace("\\|", "")
    assert "](" not in out
    assert "**" not in out.replace("\\*", "")
    assert "&lt;script&gt;" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\nb\r\nc\td", "a b c d"),
        ("\x1b[31mred\x00\x07", "red"),
        ("# heading", "\\# heading"),
        ("a_b*c`d\\e", "a\\_b\\*c\\`d\\\\e"),
        ("a & b", "a &amp; b"),
        ("see https://evil.example/x", "see https\\:\\/\\/evil.example/x"),
        ("", ""),
    ],
)
def test_md_text_cases(text: str, expected: str) -> None:
    assert md_text(text) == expected


# ------------------------------------------------------------------ rendering


def entry(a: AnalysisRun | None, b: AnalysisRun | None, vid: str = VID, title: str | None = "Talk") -> VideoEntry:
    return VideoEntry(vid, title, a, b)


def test_rendering_is_deterministic_and_carries_no_score() -> None:
    a = run(LABEL_A, claims=(claim("one", 10, "Ann", "high"), claim("two", None)))
    b = run(LABEL_B, claims=(claim("three", 12, "Ann"),))
    first = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    assert first == render_report(LABEL_A, LABEL_B, [entry(a, b)])
    lowered = first.lower()
    assert "winner" not in lowered and "score" not in lowered and "rank" not in lowered
    assert "2026-10-01 12:30:05" in first


def test_header_summary_table_and_totals() -> None:
    a = run(
        LABEL_A,
        claims=(claim("one", 1), claim("two", 2, "Ann")),
        quotes=(Quote(text="q", start_sec=3),),
        tokens=(10, 5),
        duration_ms=100,
    )
    b = run(LABEL_B, claims=(claim("x", 1),), tokens=(7, 3), duration_ms=250)
    out = render_report(LABEL_A, LABEL_B, [entry(a, b), entry(None, None, OTHER, None)])
    assert "qwen3.5:4b" in out and "claude-haiku-4-5" in out
    assert "Missing A: 1" in out and "Missing B: 1" in out
    rows = [line for line in out.splitlines() if line.startswith("|")]
    video_row = next(r for r in rows if "Talk" in r)
    cells = [c.strip() for c in video_row.strip("|").split("|")]
    # title, claims A/B, quotes A/B, unknown A/B, tokens A/B, duration A/B
    assert cells[1:] == ["2", "1", "1", "0", "1/2", "1/1", "15", "10", "100 ms", "250 ms"]
    missing_row = next(r for r in rows if OTHER in r)
    assert {c.strip() for c in missing_row.strip("|").split("|")[1:]} == {"-"}
    totals = next(r for r in rows if r.lower().startswith("| **total"))
    assert "15" in totals and "10" in totals


def test_video_not_in_database_and_video_with_neither_run_get_one_line() -> None:
    out = render_report(
        LABEL_A, LABEL_B, [entry(None, None, OTHER, None), entry(None, None, VID, "Has video")]
    )
    assert re.search(rf"{re.escape(OTHER)}.*not in the database", out)
    assert re.search(r"Has video.*no analysis for either run", out)


def test_a_video_with_one_run_shows_it_and_names_the_missing_one() -> None:
    a = run(LABEL_A, claims=(claim("only a", 5),), tldr="alpha tldr")
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert "alpha tldr" in out and "only a" in out
    assert "No analysis for claude-haiku-4-5 / v2" in out
    assert "Missing A: 0" in out and "Missing B: 1" in out


def test_notices_when_transcript_or_chunk_strategy_differ() -> None:
    a = run(LABEL_A, transcript_id=1, strategy="900s/60s")
    b = run(LABEL_B, transcript_id=2, strategy="600s/30s")
    out = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    assert "different transcripts" in out and "different chunk strategies" in out
    same = render_report(LABEL_A, LABEL_B, [entry(a, run(LABEL_B, transcript_id=1))])
    assert "different transcripts" not in same and "different chunk strategies" not in same


def test_run_details_cost_and_confidence_breakdown() -> None:
    a = run(
        LABEL_A,
        cost=None,
        claims=(
            claim("a", 1, "unknown", "high"),
            claim("b", 2, "Ann", "medium"),
            claim("c", 3, "Ann", "low"),
            claim("d", 4, "Ann", None),
        ),
    )
    b = run(LABEL_B, cost=0.0123)
    out = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    assert detail_row(out, "Cost (USD)") == ["not recorded", "0.0123"]
    assert "1 / 1 / 1 / 1" in out  # high / medium / low / not given


DETAIL_LABELS = [
    "Transcript source",
    "Chunk strategy",
    "Created (UTC)",
    "Tokens (in / out)",
    "Cache read tokens",
    "Cache write tokens",
    "Model calls",
    "Duration (ms)",
    "Cost (USD)",
    "Topics / claims / quotes",
    "Unattributed claims",
    "Claim confidence (high / medium / low / not given)",
    "Speakers coerced to unknown",
]


def detail_row(out: str, label: str) -> list[str]:
    """The A and B cells of the details-table row with this label."""
    rows = [ln for ln in out.splitlines() if ln.startswith(f"| {label} |")]
    assert len(rows) == 1, rows
    return [c.strip() for c in rows[0].strip().strip("|").split("|")[1:]]


def test_cache_and_call_rows_show_values_against_not_recorded() -> None:
    a = run(LABEL_A, cache_read=7, cache_write=11, calls=3)
    b = run(LABEL_B)
    out = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    assert "| Cache read tokens | 7 | not recorded |" in out
    assert detail_row(out, "Cache write tokens") == ["11", "not recorded"]
    assert detail_row(out, "Model calls") == ["3", "not recorded"]


def test_cache_and_call_rows_show_a_known_zero_as_zero() -> None:
    a = run(LABEL_A, cache_read=0, cache_write=0, calls=0)
    b = run(LABEL_B, cache_read=0, cache_write=0, calls=0)
    out = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    for label in ("Cache read tokens", "Cache write tokens", "Model calls"):
        assert detail_row(out, label) == ["0", "0"]


def test_cache_and_call_fields_are_independent() -> None:
    a = run(LABEL_A, cache_read=5, cache_write=None, calls=2)
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert detail_row(out, "Cache read tokens")[0] == "5"
    assert detail_row(out, "Cache write tokens")[0] == "not recorded"
    assert detail_row(out, "Model calls")[0] == "2"


def test_cache_and_call_numbers_have_no_separator() -> None:
    a = run(LABEL_A, cache_read=1000, cache_write=1234567, calls=12)
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert detail_row(out, "Cache read tokens")[0] == "1000"
    assert detail_row(out, "Cache write tokens")[0] == "1234567"


def test_missing_run_shows_a_dash_in_every_details_row() -> None:
    a = run(LABEL_A, cache_read=7, cache_write=11, calls=3)
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert len(DETAIL_LABELS) == 13
    assert [detail_row(out, label)[1] for label in DETAIL_LABELS] == ["-"] * 13


def test_details_table_row_order() -> None:
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A), run(LABEL_B))])
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("| Transcript source |"))
    labels = [ln.split("|")[1].strip() for ln in lines[start : start + len(DETAIL_LABELS)]]
    assert labels == DETAIL_LABELS


def test_claims_are_grouped_in_windows_with_links_speaker_and_confidence() -> None:
    a = run(LABEL_A, claims=(claim("early", 10, "Ann", "high"), claim("anon", 20)))
    b = run(LABEL_B, claims=(claim("late", 700, "Bob", "low"),))
    out = render_report(LABEL_A, LABEL_B, [entry(a, b)])
    assert "0:00-5:00" in out and "10:00-15:00" in out
    assert "5:00-10:00" not in out
    assert f"[0:10](https://www.youtube.com/watch?v={VID}&t=10s)" in out
    assert "Unattributed" in out and "Ann" in out and "Bob" in out
    assert "no claims in this window" in out


def test_items_without_a_timestamp_get_no_link() -> None:
    a = run(LABEL_A, claims=(claim("floating", None),))
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert "No timestamp" in out
    assert "t=" not in out


def test_quotes_are_grouped_the_same_way() -> None:
    a = run(LABEL_A, quotes=(Quote(text="said it", speaker="Ann", start_sec=301),))
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert "5:00-10:00" in out and "said it" in out


@pytest.mark.parametrize(
    "roster",
    [
        None,
        "text",
        [],
        42,
        {},
        {"speakers": None},
        {"speakers": []},
        {"speakers": "x"},
        {"speakers": [1, None, {"role": "host"}, {"name": 3}]},
    ],
)
def test_untrusted_rosters_never_raise_and_show_no_roster(roster: object) -> None:
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, roster=roster), None)])
    assert "No roster" in out


def test_a_roster_with_valid_and_invalid_entries_keeps_the_valid_ones() -> None:
    roster = {"speakers": [{"name": "Ann", "role": "host"}, 7, {"name": "Bob"}]}
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, roster=roster), None)])
    assert "Ann" in out and "host" in out and "Bob" in out


def test_hostile_text_renders_as_literal_text_everywhere() -> None:
    roster = {"speakers": [{"name": "A|B\nC", "role": "<b>host</b>"}]}
    a = run(
        LABEL_A,
        claims=(claim(HOSTILE, 5, "A|B\nC"),),
        quotes=(Quote(text=HOSTILE, speaker=HOSTILE, start_sec=5),),
        topics=(Topic(seq=0, title=HOSTILE, summary=HOSTILE, start_sec=5),),
        roster=roster,
        tldr=HOSTILE,
    )
    evil = RunLabel(HOSTILE, "v|1")
    out = render_report(evil, LABEL_B, [entry(a, None, title=HOSTILE)])
    assert "<script>" not in out and "<b>" not in out
    assert "](javascript:" not in out
    assert not re.search(r"\]\(\s*javascript", out)
    # Only the link we build ourselves may use "](" .
    for match in re.finditer(r"\]\(([^)]*)\)", out):
        assert match.group(1).startswith("https://www.youtube.com/watch?v=")
    assert "\x00" not in out


def test_table_rows_keep_their_column_count_with_hostile_cells() -> None:
    a = run(LABEL_A, roster={"speakers": [{"name": "x|y", "role": "r|s"}]})
    out = render_report(LABEL_A, LABEL_B, [entry(a, a, title="t|i\ntle")])
    for block in re.split(r"\n\n", out):
        rows = [line for line in block.splitlines() if line.startswith("|")]
        if len(rows) > 1:
            counts = {len(re.split(r"(?<!\\)\|", row)) for row in rows}
            assert len(counts) == 1, block


def test_a_huge_timestamp_does_not_break_rendering() -> None:
    a = run(LABEL_A, claims=(claim("far", 1e15),))
    assert "far" in render_report(LABEL_A, LABEL_B, [entry(a, None)])


def test_unknown_speakers_are_shown_as_unattributed_in_item_lines() -> None:
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, claims=(claim("x", 5),)), None)])
    assert "· Unattributed ·" in out
    assert "· unknown ·" not in out


def test_only_exact_lowercase_unknown_is_relabelled_unattributed() -> None:
    # Exact match, not case-insensitive and not stripped: "Unknown", "UNKNOWN"
    # and " unknown" are ordinary speaker names and are left as-is; only the
    # exact lowercase "unknown" is relabelled and counted. This must match
    # services/api/render.py (_UNKNOWN) and web/src/components/ClaimItem.tsx so
    # the three implementations cannot drift. #195.
    claims = (
        claim("a", 10, "unknown"),
        claim("b", 20, "Unknown"),
        claim("c", 30, "UNKNOWN"),
        claim("d", 40, " unknown"),
    )
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, claims=claims), None)])

    # Item-line relabel (one exact match); a case-insensitive comparison would
    # relabel three and strip() two.
    assert out.count("· Unattributed ·") == 1
    # Unknown-count cell: 1 of 4 claims. "3/4" or "2/4" would mean the count
    # used a loosened comparison.
    assert "| 1/4 |" in out


def test_window_headings_in_the_rendered_report_are_in_ascending_order() -> None:
    # Reversing the window order (in group_windows or in the renderer) leaves
    # every heading present but out of order, so a presence-only check cannot
    # see it. Pin the order explicitly. #195.
    a = run(LABEL_A, claims=(claim("early", 10), claim("mid", 400), claim("late", 700)))
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])

    positions = [out.index(f"#### {window_label(k)}") for k in (0, 1, 2)]
    assert positions == sorted(positions)


def test_a_zero_cost_is_shown_not_called_unrecorded() -> None:
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, cost=0.0), None)])
    assert "0.0000" in out


def test_a_missing_run_gets_no_lines_in_the_windows() -> None:
    a = run(LABEL_A, claims=(claim("x", 5),), quotes=(Quote(text="q", start_sec=5),))
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert not [line for line in out.splitlines() if line.startswith("- **B** ")]


def test_created_at_is_shown_in_utc_whatever_the_stored_zone() -> None:
    from datetime import timedelta, timezone

    created = datetime(2026, 10, 1, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    out = render_report(LABEL_A, LABEL_B, [entry(run(LABEL_A, created=created), None)])
    assert "2026-10-01 12:30:05" in out


# ----------------------------------------------------------- usage totals (#277)

USAGE_LABELS = ["Total cache read tokens", "Total cache write tokens", "Total model calls"]


def usage_block(out: str) -> list[str]:
    """The lines of the table under ``### Usage totals``."""
    lines = out.splitlines()
    assert lines.count("### Usage totals") == 1
    start = lines.index("### Usage totals")
    assert lines[start + 1] == ""
    block: list[str] = []
    for ln in lines[start + 2 :]:
        if not ln.startswith("|"):
            break
        block.append(ln)
    return block


def usage_row(out: str, label: str) -> list[str]:
    """The A and B cells of the Usage totals row with this label."""
    rows = [ln for ln in usage_block(out) if ln.startswith(f"| {label} |")]
    assert len(rows) == 1, rows
    return [c.strip() for c in rows[0].strip().strip("|").split("|")[1:]]


def usage_cells(out: str) -> dict[str, list[str]]:
    return {label: usage_row(out, label) for label in USAGE_LABELS}


def test_usage_totals_sum_each_side_and_show_not_recorded() -> None:
    a1 = run(LABEL_A, cache_read=7, cache_write=11, calls=3)
    a2 = run(LABEL_A, cache_read=4, cache_write=0, calls=2)
    out = render_report(LABEL_A, LABEL_B, [entry(a1, run(LABEL_B)), entry(a2, run(LABEL_B), OTHER)])
    assert usage_cells(out) == {
        "Total cache read tokens": ["11", "not recorded"],
        "Total cache write tokens": ["11", "not recorded"],
        "Total model calls": ["5", "not recorded"],
    }


def test_usage_totals_show_known_zeros_as_zero() -> None:
    def zero(label: RunLabel) -> AnalysisRun:
        return run(label, cache_read=0, cache_write=0, calls=0)

    entries = [entry(zero(LABEL_A), zero(LABEL_B)), entry(zero(LABEL_A), zero(LABEL_B), OTHER)]
    out = render_report(LABEL_A, LABEL_B, entries)
    assert all(cells == ["0", "0"] for cells in usage_cells(out).values())


def test_usage_totals_partial_count_ignores_missing_runs() -> None:
    third = "9bZkp7q19f0"
    out = render_report(
        LABEL_A,
        LABEL_B,
        [
            entry(run(LABEL_A, cache_read=5, cache_write=None, calls=2), None),
            entry(run(LABEL_A, cache_read=None, cache_write=None, calls=1), None, OTHER),
            entry(None, None, third),
        ],
    )
    assert usage_row(out, "Total cache read tokens")[0] == "5 (1 of 2 runs recorded)"
    assert usage_row(out, "Total cache write tokens")[0] == "not recorded"
    assert usage_row(out, "Total model calls")[0] == "3"


def test_usage_totals_known_zero_next_to_none_is_a_partial_zero() -> None:
    out = render_report(
        LABEL_A,
        LABEL_B,
        [entry(run(LABEL_A, cache_read=0), None), entry(run(LABEL_A, cache_read=None), None, OTHER)],
    )
    assert usage_row(out, "Total cache read tokens")[0] == "0 (1 of 2 runs recorded)"


def test_usage_totals_side_without_a_present_run_is_a_dash() -> None:
    a = run(LABEL_A, cache_read=2, cache_write=3, calls=4)
    out = render_report(LABEL_A, LABEL_B, [entry(a, None)])
    assert usage_cells(out) == {
        "Total cache read tokens": ["2", "-"],
        "Total cache write tokens": ["3", "-"],
        "Total model calls": ["4", "-"],
    }


def test_usage_totals_block_is_present_when_every_run_is_missing() -> None:
    out = render_report(LABEL_A, LABEL_B, [entry(None, None, OTHER, None)])
    assert all(cells == ["-", "-"] for cells in usage_cells(out).values())


def test_usage_totals_sit_between_the_summary_table_and_the_first_video() -> None:
    a = run(LABEL_A, cache_read=1, cache_write=2, calls=3)
    out = render_report(LABEL_A, LABEL_B, [entry(a, None), entry(a, None, OTHER)])
    lines = out.splitlines()
    total = next(i for i, ln in enumerate(lines) if ln.startswith("| **Total**"))
    heading = lines.index("### Usage totals")
    first_video = next(i for i, ln in enumerate(lines) if ln.startswith("## ") and ln != "## Summary")
    assert lines[total + 1] == "" and heading == total + 2
    assert lines[heading + 1] == ""
    block = usage_block(out)
    assert block[0] == "|  | A | B |"
    assert [ln.split("|")[1].strip() for ln in block[2:]] == USAGE_LABELS
    assert lines[heading + 2 + len(block)] == "" and first_video == heading + 3 + len(block)
    assert "score" not in "\n".join(block).lower()


def test_usage_totals_add_large_numbers_without_a_separator() -> None:
    out = render_report(
        LABEL_A,
        LABEL_B,
        [entry(run(LABEL_A, cache_read=1000), None), entry(run(LABEL_A, cache_read=1234567), None, OTHER)],
    )
    assert usage_row(out, "Total cache read tokens")[0] == "1235567"
