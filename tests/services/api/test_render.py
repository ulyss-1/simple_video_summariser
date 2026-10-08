"""The pure analysis renderer (issue #45). No FastAPI, no database, no clock."""

from __future__ import annotations

import ast
import html
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from common.models import Analysis, Claim, Quote, Topic, VideoMeta
from services.api import render
from services.api.render import render_analysis_html

VID = "dQw4w9WgXcQ"
CH = "UC" + "a" * 22
WATCH = f"https://www.youtube.com/watch?v={VID}"
CREATED = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)

MARKERS = (
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "</style><b>x",
    "&amp;",
    "' onmouseover='x",
)


def make_video(**overrides: object) -> VideoMeta:
    base = VideoMeta(
        video_id=VID,
        channel_id=CH,
        title="A title",
        description="not rendered",
        duration_sec=125,
        published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        language=None,
        live_status=None,
        manual_subtitle_langs=(),
        auto_caption_langs=(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_analysis(**overrides: object) -> Analysis:
    base = Analysis(
        video_id=VID,
        transcript_id=1,
        chunk_strategy="time:900:60",
        model="model-x",
        prompt_version="v7",
        tldr="line one\nline two",
        speaker_roster={"speakers": [{"name": "Ann", "role": "host"}]},
        input_tokens=987654,
        output_tokens=123456,
        cost_usd=4.5678,
        duration_ms=31337,
        topics=(Topic(seq=0, title="Intro", summary="About it", start_sec=5.0),),
        claims=(Claim(text="Sky is blue", speaker="Ann", start_sec=61.5, confidence="high"),),
        quotes=(Quote(text="Hello there", speaker="Ann", start_sec=3.0),),
        id=1,
        created_at=CREATED,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def page(
    video: VideoMeta | None = None,
    channel_title: str | None = "A channel",
    analysis: Analysis | None = None,
) -> str:
    return render_analysis_html(
        video or make_video(), channel_title, analysis or make_analysis()
    )


class Tree(HTMLParser):
    """Records tags and attributes, and checks every element is closed in order."""

    VOID = frozenset({"meta", "br", "hr", "img", "link", "input"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with stack {self.stack}")
        else:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def parse(doc: str) -> Tree:
    tree = Tree()
    tree.feed(doc)
    tree.close()
    return tree


def visible(doc: str) -> str:
    return "".join(parse(doc).text)


# --- purity and packaging ---------------------------------------------------------


def test_render_imports_only_stdlib_and_common() -> None:
    source = Path(render.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    forbidden = ("fastapi", "starlette", "psycopg", "common.db", "adapters", "services")
    for name in imported:
        assert not name.startswith(forbidden), name
        assert name.split(".")[0] in {"common", "__future__"} or name.split(".")[0] in (
            "html", "importlib", "math", "string", "datetime", "typing", "collections", "re",
        ), name


def test_template_is_packaged_in_package_data() -> None:
    text = (Path(render.__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'"services\.api"\s*=\s*\[[^\]]*templates/', text)


def test_renders_the_same_whatever_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = page()
    monkeypatch.chdir(tmp_path)
    assert os.getcwd() == str(tmp_path)
    assert page() == expected


def test_output_is_byte_identical_across_calls() -> None:
    assert page().encode() == page().encode()


# --- document skeleton ------------------------------------------------------------


def test_skeleton_title_and_single_h1() -> None:
    doc = page()
    tree = parse(doc)
    assert doc.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in doc
    assert any(t == "meta" and a.get("name") == "viewport" for t, a in tree.tags)
    assert "<title>A title</title>" in doc
    assert [t for t, _ in tree.tags].count("h1") == 1
    assert "<h1>A title</h1>" in doc
    assert tree.errors == [] and tree.stack == []


@pytest.mark.parametrize("title", ["", None])
def test_empty_title_falls_back_to_the_video_id(title: str | None) -> None:
    doc = page(make_video(title=title))
    assert f"<title>{VID}</title>" in doc
    assert f"<h1>{VID}</h1>" in doc


# --- header -----------------------------------------------------------------------


def test_header_shows_channel_date_duration_and_watch_link() -> None:
    doc = page()
    text = visible(doc)
    assert "A channel" in text
    assert "2026-01-02" in text
    assert "2:05" in text
    assert f'<a href="{WATCH}" rel="noopener noreferrer">' in doc


def test_header_falls_back_to_channel_id_and_omits_empty_parts() -> None:
    text = visible(page(channel_title=None))
    assert CH in text
    text = visible(page(make_video(channel_id=""), channel_title=None))
    assert CH not in text and "A channel" not in text
    text = visible(page(channel_title=""))
    assert CH in text


def test_header_omits_null_date_and_duration() -> None:
    doc = page(make_video(published_at=None, duration_sec=None))
    assert "2026-01-02" not in doc
    assert 'class="duration"' not in doc and 'class="published"' not in doc


def test_published_date_is_in_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    video = make_video(published_at=datetime(2026, 1, 2, 0, 30, tzinfo=plus_two))
    assert "2026-01-01" in visible(page(video))


# --- timestamps -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00"),
        (59.9, "0:59"),
        (60, "1:00"),
        (3599, "59:59"),
        (3599.99, "59:59"),
        (3600, "1:00:00"),
        (36000, "10:00:00"),
    ],
)
def test_timestamp_formatter_boundaries(seconds: float, expected: str) -> None:
    assert render.format_timestamp(seconds) == expected


@pytest.mark.parametrize(
    ("start", "text", "t"),
    [(0, "0:00", 0), (59.9, "0:59", 59), (3600, "1:00:00", 3600), (36000, "10:00:00", 36000)],
)
def test_start_sec_becomes_a_timestamp_link(start: float, text: str, t: int) -> None:
    analysis = make_analysis(topics=(), quotes=(), claims=(Claim(text="c", start_sec=start),))
    doc = page(analysis=analysis)
    assert f'<a href="{WATCH}&amp;t={t}s" rel="noopener noreferrer">{text}</a>' in doc


@pytest.mark.parametrize("start", [None, -1, -0.5, float("nan"), float("inf")])
def test_null_or_negative_start_sec_has_no_link_and_no_time(start: float | None) -> None:
    analysis = make_analysis(topics=(), quotes=(), claims=(Claim(text="c", start_sec=start),))
    doc = page(analysis=analysis)
    assert "&amp;t=" not in doc
    assert "-0:" not in doc and "-1:" not in doc


# --- sections ---------------------------------------------------------------------


def test_tldr_comes_first_after_the_header_and_keeps_line_breaks() -> None:
    doc = page()
    assert doc.index("line one") < doc.index("Intro") < doc.index("Sky is blue")
    assert doc.index("A channel") < doc.index("line one")
    assert "line one\nline two" in doc
    assert "pre-line" in doc


def test_speakers_show_name_and_role() -> None:
    text = visible(page())
    assert "Ann" in text and "host" in text


@pytest.mark.parametrize(
    "roster",
    [
        None,
        [],
        "x",
        5,
        {},
        {"speakers": None},
        {"speakers": "Ann"},
        {"speakers": {}},
        {"speakers": []},
        {"speakers": ["Ann", 5, None, [], {}, {"name": ""}, {"name": 5}, {"name": None}]},
        {"speakers": [{"role": "host"}]},
    ],
)
def test_malformed_roster_leaves_the_speakers_section_out(roster: object) -> None:
    doc = page(analysis=make_analysis(speaker_roster=roster))
    assert "Speakers" not in visible(doc)


def test_malformed_roster_entries_are_skipped_but_good_ones_stay() -> None:
    roster = {"speakers": ["x", 5, {"name": 7}, {"name": "Bob", "role": 3}, {"name": "Cy"}]}
    text = visible(page(analysis=make_analysis(speaker_roster=roster)))
    assert "Speakers" in text and "Bob" in text and "Cy" in text


def test_topics_claims_quotes_are_shown_in_given_order() -> None:
    analysis = make_analysis(
        topics=(Topic(seq=0, title="T0"), Topic(seq=1, title="T1", summary="S1", start_sec=9)),
        claims=(Claim(text="C-b", start_sec=50), Claim(text="C-a", start_sec=10)),
        quotes=(Quote(text="Q-b"), Quote(text="Q-a")),
    )
    doc = page(analysis=analysis)
    assert doc.index("T0") < doc.index("T1") < doc.index("S1")
    assert doc.index("C-b") < doc.index("C-a")
    assert doc.index("Q-b") < doc.index("Q-a")


def test_claim_shows_text_speaker_confidence_and_time() -> None:
    text = visible(page())
    for part in ("Sky is blue", "Ann", "high", "1:01"):
        assert part in text


def test_unknown_speaker_is_labelled_unattributed_for_claims_and_quotes() -> None:
    analysis = make_analysis(
        claims=(Claim(text="c", speaker="unknown"),),
        quotes=(Quote(text="q", speaker="unknown"),),
    )
    text = visible(page(analysis=analysis))
    assert text.count("Unattributed") == 2
    assert "unknown" not in text


def test_null_confidence_shows_no_label_and_other_values_show_as_stored() -> None:
    none_doc = page(analysis=make_analysis(claims=(Claim(text="c", confidence=None),)))
    assert 'class="confidence"' not in none_doc
    odd = page(analysis=make_analysis(claims=(Claim(text="c", confidence="very <high>"),)))
    assert "very &lt;high&gt;" in odd


def test_empty_sections_show_a_fixed_line() -> None:
    text = visible(page(analysis=make_analysis(topics=(), claims=(), quotes=())))
    for line in ("No topics extracted.", "No claims extracted.", "No quotes extracted."):
        assert line in text
    for heading in ("Topics", "Claims", "Quotes"):
        assert heading in text


def test_footer_shows_model_prompt_version_and_created_at_only() -> None:
    doc = page()
    text = visible(doc)
    assert "model-x" in text and "v7" in text
    assert "2026-03-04T05:06:07+00:00" in text
    for secret in ("987654", "123456", "4.5678", "31337", "not rendered"):
        assert secret not in doc


def test_footer_created_at_is_converted_to_utc() -> None:
    created = datetime(2026, 3, 4, 7, 6, 7, tzinfo=timezone(timedelta(hours=2)))
    text = visible(page(analysis=make_analysis(created_at=created)))
    assert "2026-03-04T05:06:07+00:00" in text


# --- escaping ---------------------------------------------------------------------


def hostile() -> tuple[VideoMeta, str, Analysis]:
    def m(i: int) -> str:
        return MARKERS[i % len(MARKERS)]

    analysis = make_analysis(
        tldr="".join(MARKERS),
        speaker_roster={"speakers": [{"name": x, "role": x} for x in MARKERS]},
        topics=tuple(Topic(seq=i, title=m(i), summary=m(i + 1), start_sec=1) for i in range(5)),
        claims=tuple(
            Claim(text=m(i), speaker=m(i + 1), confidence=m(i + 2), start_sec=2) for i in range(5)
        ),
        quotes=tuple(Quote(text=m(i), speaker=m(i + 1), start_sec=3) for i in range(5)),
        model="".join(MARKERS),
        prompt_version="".join(MARKERS),
    )
    return make_video(title="".join(MARKERS)), "".join(MARKERS), analysis


def test_hostile_text_never_becomes_markup() -> None:
    video, channel, analysis = hostile()
    doc = render_analysis_html(video, channel, analysis)
    tree = parse(doc)

    assert "<script" not in doc and "<img" not in doc
    assert doc.count("</style>") == 1
    assert "<b>" not in doc
    names = [t for t, _ in tree.tags]
    assert "script" not in names and "img" not in names and "b" not in names
    for _, attrs in tree.tags:
        assert not {"onerror", "onmouseover", "src"} & set(attrs)
    assert tree.errors == [] and tree.stack == []
    for marker in MARKERS:
        assert html.escape(marker, quote=True) in doc


def test_ampersand_is_escaped_once() -> None:
    doc = page(analysis=make_analysis(tldr="&amp;"))
    assert "&amp;amp;" in doc
    assert "&amp;amp;amp;" not in doc


@pytest.mark.parametrize("marker", MARKERS)
def test_every_untrusted_field_is_escaped_including_in_the_title(marker: str) -> None:
    escaped = html.escape(marker, quote=True)
    analysis = make_analysis(
        tldr=marker,
        model=marker,
        prompt_version=marker,
        speaker_roster={"speakers": [{"name": marker, "role": marker}]},
        topics=(Topic(seq=0, title=marker, summary=marker),),
        claims=(Claim(text=marker, speaker=marker, confidence=marker),),
        quotes=(Quote(text=marker, speaker=marker),),
    )
    doc = page(make_video(title=marker), marker, analysis)
    assert f"<title>{escaped}</title>" in doc
    assert f"<h1>{escaped}</h1>" in doc
    # title, h1, channel, tldr, model, version, role, name, topic x2, claim x3, quote x2
    assert doc.count(escaped) >= 14
    stripped = doc.replace(escaped, "")
    assert marker not in stripped or marker == "&amp;"


def test_non_ascii_text_is_kept_as_utf8() -> None:
    text = "Café 日本語 😀 שלום"
    video = make_video(title=text)
    analysis = make_analysis(tldr=text, claims=(Claim(text=text, speaker=text),))
    doc = render_analysis_html(video, text, analysis)
    assert doc.count(text) >= 4
    assert text.encode("utf-8") in doc.encode("utf-8")
    assert "&#" not in doc


def test_template_dollar_signs_in_text_are_not_interpolated() -> None:
    doc = page(analysis=make_analysis(tldr="$title ${channel} $$"))
    assert "$title ${channel} $$" in doc


def test_a_hostile_video_id_never_reaches_a_url() -> None:
    doc = page(make_video(video_id='x"><script>'))
    assert "<script" not in doc
    assert "youtube.com" not in doc


# --- self-contained ---------------------------------------------------------------


def test_page_is_self_contained_and_well_formed() -> None:
    doc = page()
    tree = parse(doc)
    names = [t for t, _ in tree.tags]
    for forbidden in ("script", "iframe", "img", "object", "embed", "base", "form", "video"):
        assert forbidden not in names
    assert names.count("style") == 1
    assert not any(t == "link" for t in names)
    assert "@import" not in doc and "url(" not in doc and "@font-face" not in doc
    assert tree.errors == [] and tree.stack == []
    hrefs = [a["href"] for t, a in tree.tags if t == "a"]
    assert hrefs
    for href in hrefs:
        assert href is not None and re.fullmatch(
            r"https://www\.youtube\.com/watch\?v=dQw4w9WgXcQ(&t=\d+s)?", href
        )
    for t, a in tree.tags:
        if t == "a":
            assert a.get("rel") == "noopener noreferrer"
