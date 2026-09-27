"""Tests for the WebVTT parser (issue #17)."""

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapters.youtube.vtt import VttParseError, parse_vtt
from common.models import Segment

FIXTURES = Path(__file__).parents[2] / "fixtures" / "vtt"
MANUAL = "youtube_manual_iG9CE55wbtY.en.vtt"
AUTO = "youtube_auto_zjkBMFhNj_g.en.vtt"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def vtt(*blocks: str) -> str:
    """A WebVTT file from a header and blank-line-separated blocks."""
    return "\n\n".join(("WEBVTT", *blocks)) + "\n"


def texts(segments: list[Segment]) -> list[str]:
    return [s.text for s in segments]


# --- Structure --------------------------------------------------------------


def test_parses_standard_file_into_segments_with_millisecond_times() -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:04.250\nHello there.",
        "01:02:03.456 --> 01:02:05.001\nGeneral Kenobi.",
    )

    assert parse_vtt(source) == [
        Segment(start=1.0, end=4.25, text="Hello there.", speaker=None),
        Segment(start=3723.456, end=3725.001, text="General Kenobi.", speaker=None),
    ]


def test_parses_real_manual_subtitles() -> None:
    segments = parse_vtt(read_fixture(MANUAL))

    assert segments[0] == Segment(27.103, 29.678, "Good morning. How are you?")
    assert segments[1] == Segment(29.702, 31.105, "(Audience) Good.")
    assert (
        Segment(
            43.096,
            46.663,
            "There have been three themes running through the conference,",
        )
        in segments
    )
    assert segments[-1] == Segment(
        118.611,
        122.018,
        "because it's one of those things that goes deep with people, am I right?",
    )
    assert len(segments) == 36
    for segment in segments:
        assert "\n" not in segment.text
        assert "<" not in segment.text
        assert segment.text == segment.text.strip()


@pytest.mark.parametrize(
    "header",
    [
        "WEBVTT",
        "\ufeffWEBVTT",
        "WEBVTT - Some title",
        "WEBVTT\tsomething",
        "WEBVTT\nKind: captions\nLanguage: en",
        "\n  \nWEBVTT",
        "\ufeff \nWEBVTT",
    ],
)
def test_accepts_header_variants(header: str) -> None:
    source = header + "\n\n00:00:01.000 --> 00:00:02.000\nHi.\n"

    assert parse_vtt(source) == [Segment(1.0, 2.0, "Hi.")]


def test_cue_directly_after_header_without_blank_line_is_parsed() -> None:
    source = "WEBVTT\nKind: captions\n00:00:01.000 --> 00:00:02.000\nHi.\n"

    assert parse_vtt(source) == [Segment(1.0, 2.0, "Hi.")]


def test_note_style_and_region_blocks_are_skipped() -> None:
    source = vtt(
        "STYLE\n::cue {\n  color: yellow;\n}",
        "REGION\nid:fred\nwidth:40%",
        "NOTE This is a comment\nspanning two lines",
        "00:00:01.000 --> 00:00:02.000\nOne.",
        "NOTE\nanother comment",
        "00:00:03.000 --> 00:00:04.000\nTwo.",
    )

    assert parse_vtt(source) == [Segment(1.0, 2.0, "One."), Segment(3.0, 4.0, "Two.")]


def test_cue_identifiers_are_optional_and_ignored() -> None:
    source = vtt(
        "intro\n00:00:01.000 --> 00:00:02.000\nOne.",
        "00:00:03.000 --> 00:00:04.000\nTwo.",
        "42\n00:00:05.000 --> 00:00:06.000\nThree.",
    )

    assert texts(parse_vtt(source)) == ["One.", "Two.", "Three."]


def test_timestamps_without_hours_are_parsed() -> None:
    source = vtt("01:02.500 --> 59:59.999\nShort form.")

    assert parse_vtt(source) == [Segment(62.5, 3599.999, "Short form.")]


def test_cue_settings_after_timestamp_are_ignored() -> None:
    source = vtt("00:00:01.000 --> 00:00:02.000 align:start position:0% line:90%\nHi.")

    assert parse_vtt(source) == [Segment(1.0, 2.0, "Hi.")]


def test_crlf_line_endings_are_handled() -> None:
    source = (
        "WEBVTT\r\n\r\n"
        "1\r\n00:00:01.000 --> 00:00:02.000\r\nFirst\r\nline.\r\n\r\n"
        "00:00:03.000 --> 00:00:04.000\r\nSecond.\r\n"
    )

    assert parse_vtt(source) == [
        Segment(1.0, 2.0, "First line."),
        Segment(3.0, 4.0, "Second."),
    ]


def test_cue_lines_are_joined_with_single_space_and_trimmed() -> None:
    source = vtt("00:00:01.000 --> 00:00:02.000\n  First line  \n\tsecond line\t")

    assert texts(parse_vtt(source)) == ["First line second line"]


# --- Markup -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("<c>plain class</c>", "plain class"),
        ("<c.colorE5E5E5>coloured</c>", "coloured"),
        ("<i>italic</i> and <b>bold</b> and <u>under</u>", "italic and bold and under"),
        ("<ruby>漢<rt>kan</rt></ruby>字", "漢kan字"),
        ("hi<00:00:01.234><c> there</c><00:00:01.500><c> you</c>", "hi there you"),
        ("<c.yellow.bg_blue>nested <i>tags</i></c>", "nested tags"),
        ("<lang en>tagged language</lang>", "tagged language"),
    ],
)
def test_markup_is_removed_and_inner_text_kept(payload: str, expected: str) -> None:
    source = vtt(f"00:00:01.000 --> 00:00:02.000\n{payload}")

    assert texts(parse_vtt(source)) == [expected]


@pytest.mark.parametrize(
    "payload",
    [
        "<v Jane Doe>text</v>",
        "<v Jane Doe>text",
        "<v.loud Jane Doe>text</v>",
        "<v  Jane Doe >text</v>",
    ],
)
def test_voice_tag_becomes_speaker(payload: str) -> None:
    source = vtt(f"00:00:01.000 --> 00:00:02.000\n{payload}")

    assert parse_vtt(source) == [Segment(1.0, 2.0, "text", speaker="Jane Doe")]


def test_voice_spans_multiple_lines_of_a_cue() -> None:
    source = vtt("00:00:01.000 --> 00:00:02.000\n<v Jane>first line\nsecond line</v>")

    assert parse_vtt(source) == [Segment(1.0, 2.0, "first line second line", "Jane")]


def test_cue_with_two_voices_yields_one_segment_per_speaker() -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane>Hi, Bob.</v>\n<v Bob>Hi, Jane.</v>"
    )

    assert parse_vtt(source) == [
        Segment(1.0, 2.0, "Hi, Bob.", "Jane"),
        Segment(1.0, 2.0, "Hi, Jane.", "Bob"),
    ]


def test_character_references_are_decoded() -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\nTom &amp; Jerry &lt;3 &gt;&gt; a&nbsp;b"
    )

    assert texts(parse_vtt(source)) == ["Tom & Jerry <3 >> a\u00a0b"]


def test_out_of_range_numeric_references_decode_to_replacement_character() -> None:
    # html.unescape alone raises ValueError on a 4300+ digit decimal reference.
    huge = "&#" + "9" * 5000 + ";"
    source = vtt(
        f"00:00:01.000 --> 00:00:02.000\n<v {huge}>a{huge}b &#x110000; &#0065;"
    )

    assert parse_vtt(source) == [Segment(1.0, 2.0, "a\ufffdb \ufffd A", "\ufffd")]


def test_escaped_tag_text_is_kept_as_text() -> None:
    source = vtt("00:00:01.000 --> 00:00:02.000\n&lt;c&gt;not a tag&lt;/c&gt;")

    assert texts(parse_vtt(source)) == ["<c>not a tag</c>"]


@pytest.mark.parametrize(
    "payload", ["<c> </c>", " ", "&nbsp;", "<i></i>\n<b> </b>", ""]
)
def test_cue_empty_after_stripping_is_dropped(payload: str) -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\nBefore.",
        f"00:00:03.000 --> 00:00:04.000\n{payload}".rstrip("\n"),
        "00:00:05.000 --> 00:00:06.000\nAfter.",
    )

    assert parse_vtt(source) == [
        Segment(1.0, 2.0, "Before."),
        Segment(5.0, 6.0, "After."),
    ]


# --- Rolling auto captions --------------------------------------------------

INLINE_TIMESTAMP = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")
ANY_TAG = re.compile(r"<[^>]*>")
TIMING = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def _ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def test_auto_captions_contain_each_word_timed_line_once() -> None:
    raw = read_fixture(AUTO)
    expected_words = [
        word
        for line in raw.split("\n")
        if INLINE_TIMESTAMP.search(line)
        for word in ANY_TAG.sub("", line).split()
    ]
    assert len(expected_words) > 200  # the oracle really read the fixture

    segments = parse_vtt(raw)

    assert " ".join(texts(segments)).split() == expected_words


def test_auto_captions_have_no_echo_cues() -> None:
    raw = read_fixture(AUTO)
    echoes = set()
    for line in raw.split("\n"):
        if m := TIMING.match(line):
            start, end = _ms(*m.groups()[:4]), _ms(*m.groups()[4:])
            if end - start == 10:
                echoes.add(start / 1000)
    assert len(echoes) > 20  # the fixture really has rolling echo cues

    segments = parse_vtt(raw)

    assert not {s.start for s in segments} & echoes
    assert all(s.end - s.start > 0.0105 for s in segments)


def test_rolling_line_without_word_timing_is_kept_once() -> None:
    # YouTube emits a lone word with no inline timestamp tags.
    source = vtt(
        "00:00:01.000 --> 00:00:02.990 align:start position:0%\n \n"
        "so<00:00:01.500><c> the</c>",
        "00:00:02.990 --> 00:00:03.000 align:start position:0%\nso the\n ",
        "00:00:03.000 --> 00:00:03.990 align:start position:0%\nso the\nparameters",
        "00:00:03.990 --> 00:00:04.000 align:start position:0%\nparameters\n ",
        "00:00:04.000 --> 00:00:05.990 align:start position:0%\nparameters\n"
        "are<00:00:04.500><c> stored</c>",
    )

    assert parse_vtt(source) == [
        Segment(1.0, 3.0, "so the"),
        Segment(3.0, 4.0, "parameters"),
        Segment(4.0, 5.99, "are stored"),
    ]


def test_consecutive_identical_cues_merge_into_one_segment() -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\nWell.",
        "00:00:02.500 --> 00:00:03.000\nYeah.",
        "00:00:03.000 --> 00:00:04.000\nYeah.",
        "00:00:04.200 --> 00:00:05.000\nYeah.",
        "00:00:06.000 --> 00:00:07.000\nRight.",
    )

    assert parse_vtt(source) == [
        Segment(1.0, 2.0, "Well."),
        Segment(2.5, 5.0, "Yeah."),
        Segment(6.0, 7.0, "Right."),
    ]


def test_non_consecutive_repeats_are_not_merged() -> None:
    source = vtt(
        "00:00:10.000 --> 00:00:12.000\nThank you.",
        "00:01:00.000 --> 00:01:02.000\nSomething else.",
        "00:05:00.000 --> 00:05:02.000\nThank you.",
    )

    assert parse_vtt(source) == [
        Segment(10.0, 12.0, "Thank you."),
        Segment(60.0, 62.0, "Something else."),
        Segment(300.0, 302.0, "Thank you."),
    ]


def test_identical_text_from_different_speakers_is_not_merged() -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane>Yes.</v>",
        "00:00:02.000 --> 00:00:03.000\n<v Bob>Yes.</v>",
    )

    assert parse_vtt(source) == [
        Segment(1.0, 2.0, "Yes.", "Jane"),
        Segment(2.0, 3.0, "Yes.", "Bob"),
    ]


# --- Malformed input --------------------------------------------------------


def test_cues_with_bad_timestamps_or_end_before_start_are_skipped() -> None:
    assert parse_vtt(read_fixture("malformed_timestamps.vtt")) == [
        Segment(1.0, 2.0, "First cue."),
        Segment(8.0, 9.0, "Last cue."),
    ]


@pytest.mark.parametrize(
    "timing",
    [
        "00:00:03.00 --> 00:00:04.000",  # two-digit milliseconds
        "0:03.000 --> 0:04.000",  # one-digit minutes without hours
        "00:03:000 --> 00:04:000",  # colon instead of dot
        "00:60.000 --> 00:61.000",  # seconds out of range
        "00:00:03.000 --> ",  # no end time
        "\u0660\u0660:\u0660\u0663.\u0660\u0660\u0660 --> 00:04.000",  # non-ASCII digits
    ],
)
def test_unparseable_timing_line_skips_only_that_cue(timing: str) -> None:
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\nBefore.",
        f"{timing}\nBroken.",
        "00:00:05.000 --> 00:00:06.000\nAfter.",
    )

    assert texts(parse_vtt(source)) == ["Before.", "After."]


def test_file_cut_inside_timing_line_returns_complete_cues() -> None:
    assert parse_vtt(read_fixture("truncated_in_timing.vtt")) == [
        Segment(1.0, 2.0, "First cue."),
        Segment(3.0, 4.0, "Second cue."),
    ]


def test_file_cut_inside_cue_text_returns_complete_cues_first() -> None:
    segments = parse_vtt(read_fixture("truncated_in_text.vtt"))

    assert segments[:2] == [
        Segment(1.0, 2.0, "First cue."),
        Segment(3.0, 4.0, "Second cue."),
    ]
    # A cut inside the text is indistinguishable from a last cue with no
    # trailing newline, so the partial text is kept rather than lost.
    assert segments[2:] == [Segment(5.0, 6.0, "Third cue was cut in the mid")]


@pytest.mark.parametrize(
    "source",
    ["", "WEBVTT", "WEBVTT\n", "\ufeffWEBVTT\r\n\r\n", "   \n", "\ufeff"],
)
def test_empty_or_header_only_input_returns_empty_list(source: str) -> None:
    assert parse_vtt(source) == []


def test_header_only_fixture_returns_empty_list() -> None:
    assert parse_vtt(read_fixture("header_only.vtt")) == []


@pytest.mark.parametrize(
    "source",
    [
        "hello",
        "webvtt\n",
        "WEBVTTX\n",
        "X WEBVTT\n",
        "00:00:01.000 --> 00:00:02.000\nNo header.\n",
    ],
)
def test_input_without_webvtt_header_raises(source: str) -> None:
    with pytest.raises(VttParseError):
        parse_vtt(source)


def test_srt_file_raises() -> None:
    with pytest.raises(VttParseError):
        parse_vtt(read_fixture("not_webvtt.srt"))


# --- Properties -------------------------------------------------------------

WORD = st.text(
    alphabet=st.characters(
        exclude_categories=("Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp", "Zs")
    ),
    min_size=1,
    max_size=6,
)
PHRASE = st.lists(WORD, min_size=1, max_size=4).map(" ".join)


MILLIS = st.integers(min_value=0, max_value=5_000_000)


def _to_cues(raw: list[tuple[int, int, str, str | None]]) -> list[Segment]:
    """Well-formed cues in start order, no two neighbours with equal text+speaker."""
    start_ms = 0
    cues: list[Segment] = []
    for gap, duration, text, speaker in raw:
        start_ms += gap
        if cues and (cues[-1].text, cues[-1].speaker) == (text, speaker):
            continue
        cues.append(
            Segment(start_ms / 1000, (start_ms + duration) / 1000, text, speaker)
        )
    return cues


def cue_lists() -> st.SearchStrategy[list[Segment]]:
    item = st.tuples(MILLIS, MILLIS, PHRASE, st.none() | PHRASE)
    return st.lists(item, max_size=8).map(_to_cues)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _timestamp(seconds: float, short: bool) -> str:
    total_ms = round(seconds * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    if short and hours == 0:
        return f"{minutes:02}:{secs:02}.{ms:03}"
    return f"{hours:02}:{minutes:02}:{secs:02}.{ms:03}"


@st.composite
def rendered(draw: st.DrawFn, cues: list[Segment]) -> str:
    """Render cues as WebVTT, randomising the syntax the parser must accept."""
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    header = draw(st.sampled_from(["WEBVTT", "\ufeffWEBVTT", "WEBVTT - title"]))
    blocks = [header + draw(st.sampled_from(["", "\nKind: captions\nLanguage: en"]))]
    for index, cue in enumerate(cues):
        lines = []
        if draw(st.booleans()):
            lines.append(f"cue-{index}")
        short = draw(st.booleans())
        timing = f"{_timestamp(cue.start, short)} --> {_timestamp(cue.end, short)}"
        if draw(st.booleans()):
            timing += " align:start position:0%"
        lines.append(timing)
        body = _escape(cue.text)
        if cue.speaker is not None:
            body = f"<v {_escape(cue.speaker)}>{body}</v>"
        lines.append(body)
        blocks.append("\n".join(lines))
        if draw(st.booleans()):
            blocks.append("NOTE a comment")
    return ("\n\n".join(blocks) + "\n").replace("\n", newline)


@given(st.data())
def test_property_render_then_parse_round_trips(data: st.DataObject) -> None:
    cues = data.draw(cue_lists())
    source = data.draw(rendered(cues))

    assert parse_vtt(source) == cues


VTT_FRAGMENTS = st.sampled_from(
    [
        "WEBVTT",
        "",
        "\r",
        " ",
        "-->",
        " --> ",
        "00:00:01.000",
        "01:02.345",
        "99:59:59.999",
        "00:00:0",
        "00:61:00.000",
        "NOTE",
        "STYLE",
        "REGION",
        "<v Jane>",
        "</v>",
        "<c.color>",
        "<00:00:01.000>",
        "<",
        ">",
        "&amp;",
        "&",
        "&#0;",
        "&#x110000;",
        "\ufeff",
        "text",
        "align:start",
    ]
)
VTT_LIKE = st.lists(
    st.lists(VTT_FRAGMENTS | st.text(max_size=5), max_size=6).map("".join), max_size=30
).map(lambda lines: "WEBVTT\n" + "\n".join(lines))


@given(st.text() | VTT_LIKE)
def test_property_any_input_returns_sorted_segments_or_raises_vtt_parse_error(
    source: str,
) -> None:
    try:
        segments = parse_vtt(source)
    except VttParseError:
        return

    assert all(isinstance(s, Segment) for s in segments)
    starts = [s.start for s in segments]
    assert starts == sorted(starts)


@given(st.data())
def test_property_output_is_sorted_by_start_for_out_of_order_cues(
    data: st.DataObject,
) -> None:
    cues = data.draw(cue_lists())
    shuffled = data.draw(st.permutations(cues))

    segments = parse_vtt(data.draw(rendered(shuffled)))

    starts = [s.start for s in segments]
    assert starts == sorted(starts)
