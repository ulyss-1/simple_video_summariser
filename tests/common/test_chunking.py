"""Transcript chunker (#21, architecture.md §6/§7.3)."""

import math
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.chunking import chunk_segments, chunk_strategy
from common.models import Chunk, Segment


def seg(start: float, end: float, text: str | None = None) -> Segment:
    return Segment(start=start, end=end, text=text if text is not None else f"t{start}")


def chunk(
    segments: Sequence[Segment], chunk_sec: int = 900, overlap_sec: int = 60
) -> list[Chunk]:
    return chunk_segments(segments, chunk_sec=chunk_sec, overlap_sec=overlap_sec)


def texts(c: Chunk) -> list[str]:
    return c.text.split(" ")


# --- chunk_strategy -----------------------------------------------------


def test_strategy_label_with_overlap() -> None:
    assert chunk_strategy(900, 60) == "time:900:60"


def test_strategy_label_without_overlap() -> None:
    assert chunk_strategy(900, 0) == "time:900:0"


# --- validation ---------------------------------------------------------

BAD_PARAMS = [
    (0, 0),
    (-5, 0),
    (900, -1),
    (900, 900),
    (900, 901),
    (True, 0),
    (900, False),
    (900.0, 60),
    (900, 60.0),
    ("900", 60),
    (None, 0),
]


@pytest.mark.parametrize(("chunk_sec", "overlap_sec"), BAD_PARAMS)
def test_strategy_rejects_bad_parameters(
    chunk_sec: object, overlap_sec: object
) -> None:
    with pytest.raises(ValueError):
        chunk_strategy(chunk_sec, overlap_sec)  # type: ignore[arg-type]


@pytest.mark.parametrize(("chunk_sec", "overlap_sec"), BAD_PARAMS)
def test_chunk_segments_rejects_bad_parameters(
    chunk_sec: object, overlap_sec: object
) -> None:
    with pytest.raises(ValueError):
        chunk_segments([seg(0, 1)], chunk_sec=chunk_sec, overlap_sec=overlap_sec)  # type: ignore[arg-type]


def test_parameters_are_keyword_only_without_defaults() -> None:
    with pytest.raises(TypeError):
        chunk_segments([seg(0, 1)], 900, 60)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        chunk_segments([seg(0, 1)])  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (-1.0, 5.0),
        (0.0, -1.0),
        (math.nan, 5.0),
        (0.0, math.nan),
        (0.0, math.inf),
        (math.inf, math.inf),
        (-math.inf, 1.0),
        (10.0, 5.0),
    ],
)
def test_bad_segment_raises_naming_its_index(start: float, end: float) -> None:
    segments = [
        seg(0, 1, "ok"),
        seg(2, 3, "ok2"),
        Segment(start=start, end=end, text="bad"),
    ]
    with pytest.raises(ValueError, match=r"\b2\b"):
        chunk(segments)


def test_bad_segment_index_refers_to_the_callers_order_not_sorted_order() -> None:
    segments = [seg(50, 60, "a"), seg(math.nan, 1, "bad"), seg(0, 1, "b")]
    with pytest.raises(ValueError, match=r"\b1\b"):
        chunk(segments)


@pytest.mark.parametrize("start", [1e20, 1e300, 1.7976931348623157e308, 2.0**53 * 2])
@pytest.mark.parametrize("chunk_sec", [1, 900, 3600])
def test_start_beyond_the_exact_bound_raises_naming_its_index(
    start: float, chunk_sec: int
) -> None:
    segments = [seg(0, 1, "ok"), Segment(start=start, end=start, text="far")]
    with pytest.raises(ValueError, match=r"\b1\b"):
        chunk_segments(segments, chunk_sec=chunk_sec, overlap_sec=0)


def test_start_at_the_exact_bound_is_chunked() -> None:
    far = 2.0**53
    chunks = chunk([Segment(start=far, end=far, text="far")], 900, 0)
    assert [c.text for c in chunks] == ["far"]


def test_blank_segment_with_huge_start_is_still_ignored() -> None:
    assert chunk([Segment(start=1e20, end=1e20, text=" ")], 900, 0) == []


# --- edge cases ---------------------------------------------------------


def test_empty_input_gives_no_chunks() -> None:
    assert chunk([]) == []


def test_only_blank_segments_give_no_chunks() -> None:
    assert chunk([seg(0, 1, ""), seg(1, 2, "   \n\t")]) == []


def test_chunk_fields_for_the_export_to_save_chunks() -> None:
    [c] = chunk([seg(1, 2, " hello "), seg(3, 4, "world")])
    assert c == Chunk(
        seq=0,
        start_sec=1,
        end_sec=4,
        text="hello world",
        chunk_strategy="time:900:60",
        transcript_id=None,
        id=None,
    )


def test_all_starts_before_first_boundary_give_one_chunk_covering_everything() -> None:
    [c] = chunk([seg(0, 10, "a"), seg(500, 510, "b"), seg(899.9, 905, "c")])
    assert (c.seq, c.start_sec, c.end_sec) == (0, 0, 905)
    assert texts(c) == ["a", "b", "c"]


def test_segment_starting_exactly_on_boundary_opens_second_chunk() -> None:
    first, second = chunk([seg(0, 10, "a"), seg(900.0, 910, "b")])
    assert texts(first) == ["a"]
    assert second.seq == 1
    assert "b" in texts(second)
    assert "a" not in texts(second)


def test_segment_at_chunk_minus_overlap_is_in_both_chunks() -> None:
    first, second = chunk([seg(0, 1, "a"), seg(840.0, 841, "edge"), seg(900, 901, "b")])
    assert texts(first) == ["a", "edge"]
    assert texts(second) == ["edge", "b"]


def test_segment_just_before_overlap_zone_is_only_in_first_chunk() -> None:
    first, second = chunk([seg(839.999, 841, "early"), seg(900, 901, "b")])
    assert texts(first) == ["early"]
    assert texts(second) == ["b"]


def test_zero_overlap_never_repeats_a_segment() -> None:
    chunks = chunk(
        [seg(0, 1, "a"), seg(899, 900, "b"), seg(900, 901, "c")], overlap_sec=0
    )
    assert [texts(c) for c in chunks] == [["a", "b"], ["c"]]


def test_single_long_segment_gives_one_chunk_extending_past_boundaries() -> None:
    [c] = chunk([seg(0, 2000, "long")])
    assert (c.seq, c.start_sec, c.end_sec) == (0, 0, 2000)


def test_crossing_segment_stays_whole_in_its_own_window() -> None:
    first, second = chunk([seg(700, 950, "cross"), seg(1000, 1010, "b")])
    assert texts(first) == ["cross"]
    assert first.end_sec == 950
    assert texts(second) == ["b"]


def test_empty_window_is_skipped_and_seq_stays_contiguous() -> None:
    first, second = chunk([seg(0, 10, "a"), seg(2000, 2010, "b")])
    assert (first.seq, second.seq) == (0, 1)
    assert second.start_sec == 2000
    assert texts(second) == ["b"]


def test_first_segment_after_first_window_starts_seq_at_zero() -> None:
    [c] = chunk([seg(1000, 1010, "a")])
    assert (c.seq, c.start_sec) == (0, 1000)


def test_chunk_of_only_overlap_segments_is_never_emitted() -> None:
    # "tail" starts in window 0's overlap zone for window 1, but window 1 has no core.
    chunks = chunk([seg(850, 860, "tail"), seg(2000, 2010, "far")])
    assert [texts(c) for c in chunks] == [["tail"], ["far"]]


def test_zero_length_segment_is_kept() -> None:
    [c] = chunk([seg(5, 5, "blip")])
    assert (c.start_sec, c.end_sec, c.text) == (5, 5, "blip")


def test_blank_segments_do_not_affect_span_or_windows() -> None:
    [c] = chunk([seg(0, 1, "  "), seg(10, 20, "a"), seg(5000, 6000, "")])
    assert (c.start_sec, c.end_sec, c.text) == (10, 20, "a")


def test_blank_segment_alone_in_a_window_does_not_create_a_chunk() -> None:
    chunks = chunk([seg(0, 1, "a"), seg(1000, 1001, " "), seg(1900, 1901, "b")])
    assert [c.seq for c in chunks] == [0, 1]
    assert texts(chunks[1]) == ["b"]


def test_overlap_segments_widen_the_span() -> None:
    _, second = chunk([seg(0, 1, "a"), seg(845, 870, "edge"), seg(900, 901, "b")])
    assert (second.start_sec, second.end_sec) == (845, 901)


def test_text_is_stripped_and_single_space_joined() -> None:
    [c] = chunk([seg(0, 1, "  a b\n"), seg(1, 2, "\tc  ")])
    assert c.text == "a b c"


def test_unsorted_input_gives_time_ordered_chunks_and_is_not_mutated() -> None:
    segments = [seg(950, 951, "c"), seg(0, 1, "a"), seg(10, 11, "b")]
    snapshot = list(segments)
    first, second = chunk(segments)
    assert texts(first) == ["a", "b"]
    assert texts(second) == ["c"]
    assert segments == snapshot


def test_equal_start_ties_are_ordered_by_end_then_input_order() -> None:
    [c] = chunk([seg(1, 5, "x"), seg(1, 2, "y"), seg(1, 5, "z")])
    assert texts(c) == ["y", "x", "z"]


def test_chunk_strategy_label_is_set_from_parameters() -> None:
    chunks = chunk([seg(0, 1), seg(100, 101)], chunk_sec=50, overlap_sec=5)
    assert {c.chunk_strategy for c in chunks} == {"time:50:5"}
    assert all(c.transcript_id is None and c.id is None for c in chunks)


def test_many_segments_are_chunked_without_quadratic_blowup() -> None:
    segments = [seg(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(80_000)]  # ~11 h
    chunks = chunk(segments)
    assert len(chunks) == math.ceil(80_000 * 0.5 / 900)
    assert [c.seq for c in chunks] == list(range(len(chunks)))


# --- properties ---------------------------------------------------------

_time = st.floats(min_value=0, max_value=40_000, allow_nan=False, allow_infinity=False)


@st.composite
def _segments(draw: st.DrawFn, *, min_size: int = 0) -> list[Segment]:
    spans = draw(
        st.lists(
            st.tuples(_time, st.floats(min_value=0, max_value=3000, allow_nan=False)),
            min_size=min_size,
            max_size=40,
        )
    )
    blanks = draw(st.lists(st.booleans(), min_size=len(spans), max_size=len(spans)))
    return [
        Segment(start=s, end=s + d, text="  " if blank else f"w{i}")
        for i, ((s, d), blank) in enumerate(zip(spans, blanks, strict=True))
    ]


@st.composite
def _params(draw: st.DrawFn) -> tuple[int, int]:
    chunk_sec = draw(st.integers(1, 3600))
    return chunk_sec, draw(st.integers(0, chunk_sec - 1))


def _content(segments: Sequence[Segment]) -> list[Segment]:
    return [s for s in segments if s.text.strip()]


def _windows(segments: Sequence[Segment], chunk_sec: int) -> list[int]:
    """Reference model: sorted distinct core windows of the non-blank segments."""
    return sorted({int(s.start // chunk_sec) for s in _content(segments)})


@given(_segments(), _params())
def test_property_coverage(segments: list[Segment], params: tuple[int, int]) -> None:
    chunk_sec, overlap_sec = params
    chunks = chunk(segments, chunk_sec, overlap_sec)
    windows = _windows(segments, chunk_sec)
    assert len(chunks) == len(windows)
    for s in _content(segments):
        index = windows.index(int(s.start // chunk_sec))
        assert s.text in texts(chunks[index])


@given(_segments(), _params())
def test_property_bounded_overlap(
    segments: list[Segment], params: tuple[int, int]
) -> None:
    chunk_sec, overlap_sec = params
    chunks = chunk(segments, chunk_sec, overlap_sec)
    windows = _windows(segments, chunk_sec)
    by_text = {s.text: s for s in _content(segments)}
    membership: dict[str, list[int]] = {}
    for c in chunks:
        for t in texts(c):
            membership.setdefault(t, []).append(c.seq)
    for t, seqs in membership.items():
        assert len(seqs) <= 2
        if len(seqs) == 2:
            assert seqs[1] == seqs[0] + 1
            w = windows[seqs[1]] * chunk_sec
            assert w - overlap_sec <= by_text[t].start < w


@given(_segments(), _params())
def test_property_order(segments: list[Segment], params: tuple[int, int]) -> None:
    chunk_sec, overlap_sec = params
    chunks = chunk(segments, chunk_sec, overlap_sec)
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    starts = [c.start_sec for c in chunks]
    assert starts == sorted(starts)
    assert all(c.start_sec <= c.end_sec for c in chunks)


@given(_segments(), _params())
def test_property_single_chunk_iff_all_segments_share_one_core_window(
    segments: list[Segment], params: tuple[int, int]
) -> None:
    chunk_sec, overlap_sec = params
    chunks = chunk(segments, chunk_sec, overlap_sec)
    assert (len(chunks) == 1) == (len(_windows(segments, chunk_sec)) == 1)
    content = _content(segments)
    if content and all(s.start < chunk_sec for s in content):
        assert len(chunks) == 1
        assert chunks[0].seq == 0


@given(_segments(), _segments(), _params(), _time)
def test_property_prefix_stability(
    base: list[Segment], extra: list[Segment], params: tuple[int, int], gap: float
) -> None:
    chunk_sec, overlap_sec = params
    boundary = gap
    late = [
        Segment(start=boundary + s.start, end=boundary + s.end, text=f"late{i}")
        for i, s in enumerate(extra)
    ]
    before = chunk(base, chunk_sec, overlap_sec)
    after = chunk([*base, *late], chunk_sec, overlap_sec)
    windows = _windows(base, chunk_sec)
    for index, k in enumerate(windows):
        if (k + 1) * chunk_sec <= boundary:
            assert after[index] == before[index]


@given(_segments(), _params())
def test_property_determinism(segments: list[Segment], params: tuple[int, int]) -> None:
    chunk_sec, overlap_sec = params
    assert chunk(segments, chunk_sec, overlap_sec) == chunk(
        list(segments), chunk_sec, overlap_sec
    )


@given(
    st.lists(
        st.floats(min_value=0, max_value=2.0**53, allow_nan=False),
        min_size=1,
        max_size=10,
    ),
    _params(),
)
def test_property_coverage_for_large_finite_starts(
    starts: list[float], params: tuple[int, int]
) -> None:
    chunk_sec, overlap_sec = params
    segments = [Segment(start=s, end=s, text=f"w{i}") for i, s in enumerate(starts)]
    chunks = chunk(segments, chunk_sec, overlap_sec)
    windows = _windows(segments, chunk_sec)
    assert len(chunks) == len(windows)
    for s in segments:
        assert s.text in texts(chunks[windows.index(int(s.start // chunk_sec))])


@given(
    st.floats(
        min_value=2.0**53, allow_nan=False, allow_infinity=False, exclude_min=True
    ),
    _params(),
)
def test_property_start_beyond_bound_raises_value_error(
    start: float, params: tuple[int, int]
) -> None:
    chunk_sec, overlap_sec = params
    with pytest.raises(ValueError, match="start"):
        chunk([Segment(start=start, end=start, text="x")], chunk_sec, overlap_sec)
