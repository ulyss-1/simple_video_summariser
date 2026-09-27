"""Tests for the subtitle download adapter (issue #18).

No test here starts yt-dlp: ``WritingFakeRunner`` stands in for the process,
writing a fixture VTT into the ``--paths`` directory it was given (or raising,
for the error/timeout paths) instead of actually downloading anything.
"""

import ast
import pathlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

import adapters.youtube.subtitles
from adapters.youtube.subtitles import (
    SubtitleAvailability,
    YouTubeSubtitles,
    speaker_source,
)
from common.errors import PermanentSourceError, ToolFailureError, TransientNetworkError
from common.models import Segment, VideoMeta
from tests.adapters.youtube.fakes import completed

FIXTURES = Path(__file__).parents[2] / "fixtures" / "vtt"
MANUAL_FIXTURE = FIXTURES / "youtube_manual_iG9CE55wbtY.en.vtt"
AUTO_FIXTURE = FIXTURES / "youtube_auto_zjkBMFhNj_g.en.vtt"
HEADER_ONLY_FIXTURE = FIXTURES / "header_only.vtt"

YTDLP = [sys.executable, "-m", "yt_dlp"]


def make_meta(
    *,
    language: str | None = "en",
    manual_subtitle_langs: tuple[str, ...] = (),
    auto_caption_langs: tuple[str, ...] = (),
) -> VideoMeta:
    return VideoMeta(
        video_id="jNQXAC9IVRw",
        channel_id="UC4a-Gbdw7vOaccHmFo40b9g",
        title="Me at the zoo",
        description="",
        duration_sec=19,
        published_at=None,
        language=language,
        live_status="not_live",
        manual_subtitle_langs=manual_subtitle_langs,
        auto_caption_langs=auto_caption_langs,
    )


class WritingFakeRunner:
    """Records each argv; writes a file into the ``--paths`` dir, or raises."""

    def __init__(
        self,
        *,
        filename: str | None = None,
        content: str = "",
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.filename = filename
        self.content = content
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], float]] = []
        self.tmpdir_used: Path | None = None

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        tmpdir = Path(argv[list(argv).index("--paths") + 1])
        self.tmpdir_used = tmpdir
        assert tmpdir.is_dir()  # the adapter must create it before running yt-dlp
        if self.raises is not None:
            raise self.raises
        if self.filename is not None:
            (tmpdir / self.filename).write_text(self.content, encoding="utf-8")
        return completed(stdout="", stderr=self.stderr, returncode=self.returncode)


# --- available(): a pure function of VideoMeta, no network -----------------


def test_available_makes_no_process_call() -> None:
    runner = WritingFakeRunner()
    meta = make_meta(manual_subtitle_langs=("en",), auto_caption_langs=("en",))

    YouTubeSubtitles(runner=runner).available(meta)

    assert runner.calls == []


def test_no_english_tracks_returns_none_and_none_not_an_error() -> None:
    meta = make_meta(manual_subtitle_langs=("fr", "de"), auto_caption_langs=("fr",))
    result = YouTubeSubtitles().available(meta)
    assert result == SubtitleAvailability(manual=None, auto=None)


@pytest.mark.parametrize(
    ("langs", "expected"),
    [
        (("en",), "en"),
        (("en", "en-US"), "en"),
        (("en-US",), "en-US"),
        (("en-US", "en-GB"), "en-US"),
        (("en-GB",), "en-GB"),
        (("en-GB", "en-CA"), "en-GB"),
        (("en-CA",), "en-CA"),
        (("en-CA", "en-AU"), "en-AU"),  # any other en-*: deterministic, not first-seen
        (("fr", "en-NZ", "de"), "en-NZ"),
        ((), None),
    ],
)
def test_manual_language_preference_order(
    langs: tuple[str, ...], expected: str | None
) -> None:
    meta = make_meta(manual_subtitle_langs=langs)
    assert YouTubeSubtitles().available(meta).manual == expected


def test_auto_language_preference_order_matches_manual() -> None:
    meta = make_meta(language="en", auto_caption_langs=("en-CA", "en-US"))
    assert YouTubeSubtitles().available(meta).auto == "en-US"


def test_a_translated_en_auto_track_on_a_non_english_video_is_reported_as_none() -> None:
    meta = make_meta(language="uk", auto_caption_langs=("uk-orig", "en"))
    result = YouTubeSubtitles().available(meta)
    assert result.auto is None


def test_manual_subtitles_are_unaffected_by_non_english_language() -> None:
    # Manual subtitles are never a translation product the way auto captions
    # can be; only the auto side is gated on meta.language.
    meta = make_meta(language="uk", manual_subtitle_langs=("en",))
    assert YouTubeSubtitles().available(meta).manual == "en"


def test_unknown_language_does_not_suppress_auto_captions() -> None:
    meta = make_meta(language=None, auto_caption_langs=("en",))
    assert YouTubeSubtitles().available(meta).auto == "en"


def test_english_language_variant_does_not_suppress_auto_captions() -> None:
    meta = make_meta(language="en-US", auto_caption_langs=("en",))
    assert YouTubeSubtitles().available(meta).auto == "en"


def test_subtitle_availability_is_immutable() -> None:
    availability = SubtitleAvailability(manual="en", auto=None)
    with pytest.raises(AttributeError):
        availability.manual = "en-US"  # type: ignore[misc]


# --- fetch(): argv, kind selection, temp directory --------------------------


def test_fetch_runs_yt_dlp_with_the_documented_flags_for_manual() -> None:
    runner = WritingFakeRunner(
        filename="video.en.vtt", content=MANUAL_FIXTURE.read_text(encoding="utf-8")
    )

    YouTubeSubtitles(runner=runner, timeout=42).fetch("jNQXAC9IVRw", "en", "manual")

    [(argv, timeout)] = runner.calls
    assert timeout == 42
    assert argv[: len(YTDLP)] == YTDLP
    rest = argv[len(YTDLP) :]
    assert "--skip-download" in rest
    assert rest[rest.index("--sub-format") + 1] == "vtt"
    assert rest[rest.index("--sub-langs") + 1] == "en"
    assert "--write-subs" in rest
    assert "--write-auto-subs" not in rest
    assert rest[-1] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_fetch_uses_write_auto_subs_for_kind_auto() -> None:
    runner = WritingFakeRunner(
        filename="video.en.vtt", content=AUTO_FIXTURE.read_text(encoding="utf-8")
    )

    YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "auto")

    [(argv, _)] = runner.calls
    assert "--write-auto-subs" in argv
    assert "--write-subs" not in argv


def test_fetch_passes_a_full_watch_url_never_a_bare_id() -> None:
    runner = WritingFakeRunner(filename="x.en.vtt", content="WEBVTT\n")
    YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")
    [(argv, _)] = runner.calls
    assert "jNQXAC9IVRw" not in argv[:-1]


@pytest.mark.parametrize("kind", ["", "manual-and-auto", "MANUAL", "both"])
def test_an_unrecognized_kind_is_rejected_before_any_process_starts(
    kind: str,
) -> None:
    runner = WritingFakeRunner()
    with pytest.raises(ValueError):
        YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", kind)
    assert runner.calls == []


def test_a_malformed_video_id_is_rejected_before_any_process_starts() -> None:
    runner = WritingFakeRunner()
    with pytest.raises(ValueError):
        YouTubeSubtitles(runner=runner).fetch("not-an-id", "en", "manual")
    assert runner.calls == []


def test_fetch_finds_the_file_by_glob_regardless_of_its_stem() -> None:
    # yt-dlp names files after the video title, not a fixed pattern; only the
    # `.<lang>.vtt` suffix is reliable.
    runner = WritingFakeRunner(
        filename="Do schools kill creativity - Sir Ken Robinson.en.vtt",
        content=MANUAL_FIXTURE.read_text(encoding="utf-8"),
    )
    segments = YouTubeSubtitles(runner=runner).fetch("iG9CE55wbtY", "en", "manual")
    assert segments[0] == Segment(27.103, 29.678, "Good morning. How are you?")


def test_fetch_parses_real_auto_caption_fixture() -> None:
    runner = WritingFakeRunner(
        filename="video.en.vtt", content=AUTO_FIXTURE.read_text(encoding="utf-8")
    )
    segments = YouTubeSubtitles(runner=runner).fetch("zjkBMFhNj_g", "en", "auto")
    assert segments[0].text.startswith("hi everyone")


# --- temp directory hygiene --------------------------------------------------


def test_the_temp_directory_is_removed_on_success() -> None:
    runner = WritingFakeRunner(filename="x.en.vtt", content="WEBVTT\n")
    YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")
    assert runner.tmpdir_used is not None
    assert not runner.tmpdir_used.exists()


def test_the_temp_directory_is_removed_when_yt_dlp_fails() -> None:
    runner = WritingFakeRunner(
        stderr="ERROR: [youtube] jNQXAC9IVRw: Private video\n", returncode=1
    )
    with pytest.raises(PermanentSourceError):
        YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")
    assert runner.tmpdir_used is not None
    assert not runner.tmpdir_used.exists()


def test_the_temp_directory_is_removed_on_timeout() -> None:
    runner = WritingFakeRunner(raises=subprocess.TimeoutExpired(["yt-dlp"], 5))
    with pytest.raises(TransientNetworkError):
        YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")
    assert runner.tmpdir_used is not None
    assert not runner.tmpdir_used.exists()


def test_each_fetch_gets_a_fresh_temp_directory() -> None:
    runner = WritingFakeRunner(filename="x.en.vtt", content="WEBVTT\n")
    source = YouTubeSubtitles(runner=runner)
    source.fetch("jNQXAC9IVRw", "en", "manual")
    first = runner.tmpdir_used
    source.fetch("jNQXAC9IVRw", "en", "manual")
    second = runner.tmpdir_used
    assert first != second


# --- exit 0 but no file written --------------------------------------------


def test_exit_zero_with_no_vtt_file_written_raises_tool_failure() -> None:
    runner = WritingFakeRunner(filename=None)  # yt-dlp "succeeds" but writes nothing
    with pytest.raises(ToolFailureError):
        YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")


def test_exit_zero_with_a_file_for_a_different_language_raises_tool_failure() -> None:
    runner = WritingFakeRunner(filename="video.fr.vtt", content="WEBVTT\n")
    with pytest.raises(ToolFailureError):
        YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")


# --- zero segments means "no subtitles", not an error -----------------------


def test_a_file_that_parses_to_zero_segments_returns_empty_list() -> None:
    runner = WritingFakeRunner(
        filename="video.en.vtt", content=HEADER_ONLY_FIXTURE.read_text(encoding="utf-8")
    )
    assert YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual") == []


# --- speaker labels -----------------------------------------------------


def _fetch_from_cue(cue_text: str) -> list[Segment]:
    vtt = f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{cue_text}\n"
    runner = WritingFakeRunner(filename="video.en.vtt", content=vtt)
    return YouTubeSubtitles(runner=runner).fetch("jNQXAC9IVRw", "en", "manual")


def test_v_tag_speakers_are_kept_as_is() -> None:
    [segment] = _fetch_from_cue("<v Jane Doe>Hello there.")
    assert segment.speaker == "Jane Doe"
    assert segment.text == "Hello there."


def test_an_uppercase_label_becomes_the_speaker_in_title_case() -> None:
    [segment] = _fetch_from_cue("JANE DOE: Hello there.")
    assert segment.speaker == "Jane Doe"
    assert segment.text == "Hello there."


def test_an_arrow_prefixed_uppercase_label_becomes_the_speaker() -> None:
    [segment] = _fetch_from_cue(">> HOST: Welcome back.")
    assert segment.speaker == "Host"
    assert segment.text == "Welcome back."


def test_a_single_word_uppercase_label_is_recognized() -> None:
    [segment] = _fetch_from_cue("NARRATOR: The story begins.")
    assert segment.speaker == "Narrator"


def test_a_bare_arrow_is_removed_and_leaves_speaker_none() -> None:
    [segment] = _fetch_from_cue(">> Let's keep going.")
    assert segment.speaker is None
    assert segment.text == "Let's keep going."


@pytest.mark.parametrize(
    "cue_text",
    [
        "Note: this matters",
        "Bob Smith: hi there",  # mixed case
        "ONE TWO THREE FOUR: hi",  # more than 3 words before the colon
    ],
    ids=["lowercase-tail", "mixed-case", "too-many-words"],
)
def test_ordinary_sentences_with_a_colon_are_left_alone(cue_text: str) -> None:
    [segment] = _fetch_from_cue(cue_text)
    assert segment.speaker is None
    assert segment.text == cue_text


def test_a_three_word_uppercase_label_is_the_boundary_that_still_matches() -> None:
    [segment] = _fetch_from_cue("JANE VAN DOE: hi")
    assert segment.speaker == "Jane Van Doe"


# --- speaker_source -----------------------------------------------------


def test_speaker_source_is_subtitle_labels_when_any_segment_has_a_speaker() -> None:
    segments = [
        Segment(0, 1, "hi", speaker=None),
        Segment(1, 2, "there", speaker="Jane"),
    ]
    assert speaker_source(segments) == "subtitle_labels"


def test_speaker_source_is_none_when_no_segment_has_a_speaker() -> None:
    segments = [Segment(0, 1, "hi", speaker=None), Segment(1, 2, "there", speaker=None)]
    assert speaker_source(segments) == "none"


def test_speaker_source_is_none_for_an_empty_transcript() -> None:
    assert speaker_source([]) == "none"


# --- import direction --------------------------------------------------


def _top_level_imports(module: object) -> set[str]:
    tree = ast.parse(pathlib.Path(module.__file__).read_text())  # type: ignore[attr-defined]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_subtitles_adapter_imports_no_service() -> None:
    assert "services" not in _top_level_imports(adapters.youtube.subtitles)
