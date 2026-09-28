"""Result formatting and sanitizing of untrusted text for the terminal (issue #31)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.models import Analysis, Claim, Quote, Topic
from services.cli.output import format_result, format_timestamp, sanitize_text


def analysis(**overrides: object) -> Analysis:
    fields: dict[str, object] = {
        "video_id": "iG9CE55wbtY",
        "transcript_id": 1,
        "chunk_strategy": "time:600:60",
        "model": "qwen-test",
        "prompt_version": "v7",
        "tldr": "Schools should teach creativity.",
        "topics": (
            Topic(seq=0, title="Opening", start_sec=12.9),
            Topic(seq=1, title="Middle", start_sec=754.0),
            Topic(seq=2, title="Long tail", start_sec=3723.0),
            Topic(seq=3, title="Untimed"),
        ),
        "claims": (Claim(text="a"), Claim(text="b"), Claim(text="c")),
        "quotes": (Quote(text="q"), Quote(text="r")),
        "id": 5,
        "created_at": datetime(2026, 9, 28, tzinfo=UTC),
    }
    fields.update(overrides)
    return Analysis(**fields)  # type: ignore[arg-type]


def render(**overrides: object) -> str:
    return format_result(
        title="Do schools kill creativity?",
        video_id="iG9CE55wbtY",
        transcript_source="youtube_manual",
        analysis=analysis(**overrides),
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0:00"),
        (0.9, "0:00"),
        (5, "0:05"),
        (65, "1:05"),
        (599.99, "9:59"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3723, "1:02:03"),
        (36000, "10:00:00"),
    ],
)
def test_timestamps_are_m_ss_below_an_hour_and_h_mm_ss_from_an_hour(
    seconds: float, text: str
) -> None:
    assert format_timestamp(seconds) == text


def test_the_result_shows_title_id_source_model_prompt_version_and_tldr() -> None:
    text = render()
    assert "Do schools kill creativity?" in text
    assert "iG9CE55wbtY" in text
    assert "youtube_manual" in text
    assert "qwen-test" in text
    assert "v7" in text
    assert "Schools should teach creativity." in text


def test_topics_carry_a_timestamp_prefix_unless_start_sec_is_none() -> None:
    lines = render().splitlines()
    topic_lines = [line.strip() for line in lines if line.strip().endswith(
        ("Opening", "Middle", "Long tail", "Untimed")
    )]
    assert topic_lines == [
        "[0:12] Opening",
        "[12:34] Middle",
        "[1:02:03] Long tail",
        "Untimed",
    ]


def test_claim_and_quote_counts_are_shown() -> None:
    lines = render().splitlines()
    assert any("Claims" in line and "3" in line for line in lines)
    assert any("Quotes" in line and "2" in line for line in lines)


def test_an_analysis_without_topics_still_renders() -> None:
    text = render(topics=(), claims=(), quotes=())
    assert "Schools should teach creativity." in text


def test_untrusted_text_has_escape_sequences_and_control_bytes_removed() -> None:
    text = format_result(
        title="T\x1b[31mitle\x07\x00",
        video_id="iG9CE55wbtY",
        transcript_source="youtube_manual",
        analysis=analysis(
            tldr="clean\x1b[2J\x1b]0;pwned\x07 end",
            topics=(Topic(seq=0, title="Top\x85ic\x1b[1;31m", start_sec=1.0),),
        ),
    )
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "\x00" not in text
    assert "\x85" not in text
    assert "pwned" not in text
    assert "clean end" in text
    assert "Title" in text
    assert "[0:01] Topic" in text


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain", "plain"),
        ("keep\nnewline\tand tab", "keep\nnewline\tand tab"),
        ("a\x1b[2Jb", "ab"),
        ("a\x1b[1;38;5;196mb\x1b[0m", "ab"),
        ("a\x1b]0;title\x07b", "ab"),
        ("a\x1b]8;;http://evil\x1b\\link\x1b]8;;\x1b\\b", "alinkb"),
        ("a\x1bcb", "ab"),
        ("a\x1b", "a"),
        ("a\x9b31mb", "ab"),
        ("a\x9d0;x\x9cb", "ab"),
        ("a\rb\x00c\x08d\x7fe\x85f", "abcdef"),
        ("café 中文 \U0001f600", "café 中文 \U0001f600"),
    ],
)
def test_sanitize_text_cases(raw: str, clean: str) -> None:
    assert sanitize_text(raw) == clean


@given(st.text())
def test_sanitized_text_has_no_control_characters_except_newline_and_tab(raw: str) -> None:
    for char in sanitize_text(raw):
        assert char in "\n\t" or not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F)
