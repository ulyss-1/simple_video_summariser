"""``parse_playlist_ref``: the trust boundary for a pasted playlist (#342)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.youtube_refs import (
    MAX_REF_CHARS,
    PLAYLIST_ID_PATTERN,
    is_playlist_id,
    parse_playlist_ref,
    playlist_url,
)

PL = "PLAqhIrjkxbuWI23v9cThsA9GvCAUhRvKZ"  # real, from the recorded fixture
DASH_PL = "PL-_" + "a" * 10
VIDEO = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "text",
    [
        PL,
        f"  {PL}\n",
        f"https://www.youtube.com/playlist?list={PL}",
        f"http://www.youtube.com/playlist?list={PL}",
        f"https://youtube.com/playlist?list={PL}",
        f"https://m.youtube.com/playlist?list={PL}",
        f"www.youtube.com/playlist?list={PL}",
        f"youtube.com/playlist?list={PL}",
        f"HTTPS://WWW.YOUTUBE.COM/playlist?list={PL}",
        f"https://www.youtube.com/playlist/?list={PL}",
        f"https://www.youtube.com/playlist?list={PL}&si=abc",
        f"https://www.youtube.com/playlist?si=abc&list={PL}#frag",
        f"https://www.youtube.com/watch?v={VIDEO}&list={PL}",
        f"https://www.youtube.com/watch?list={PL}&v={VIDEO}&index=3",
    ],
)
def test_each_accepted_form_returns_the_playlist_id(text: str) -> None:
    assert parse_playlist_ref(text) == PL


@pytest.mark.parametrize(
    "playlist_id",
    [
        "PL" + "a" * 10,  # shortest: 12 characters
        "PL" + "a" * 62,  # longest: 64 characters
        "OL" + "b" * 20,
        "UU" + "c" * 22,
        "FL" + "d" * 22,
        DASH_PL,
        "PL0123456789ABCDEF",  # the older 18-character form
    ],
)
def test_every_accepted_prefix_and_the_length_bounds(playlist_id: str) -> None:
    assert parse_playlist_ref(playlist_id) == playlist_id
    assert is_playlist_id(playlist_id)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "RD" + "a" * 20,  # a mix: personalised and endless
        "RDMM",
        "RDCLAK5uy_" + "a" * 20,
        "LL",
        "WL",
        "LL" + "a" * 20,
        "WL" + "a" * 20,
        "XX" + "a" * 20,  # unknown prefix
        "pl" + "a" * 20,  # prefixes are upper case
        "PL" + "a" * 9,  # one short of 12
        "PL" + "a" * 63,  # one past 64
        "PL" + "a" * 10 + "!",
        "PL" + "a" * 10 + " x",
        VIDEO,
        "https://www.youtube.com/playlist",
        "https://www.youtube.com/playlist?list=",
        f"https://www.youtube.com/playlist?list={PL}&list={PL}",
        f"https://www.youtube.com/playlist?list={PL}&list=",
        f"https://www.youtube.com/playlist?list=RD{'a' * 20}",
        f"https://www.youtube.com/watch?v={VIDEO}&list=RD{'a' * 20}",
        f"https://www.youtube.com/watch?v={VIDEO}&list=LL",
        f"https://www.youtube.com/watch?v={VIDEO}&list=WL",
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/playlist?list=%50{PL[1:]}",  # an encoded P
        f"https://www.youtube.com/playlist?List={PL}",
        f"https://www.youtube.com/channel/UCXUPKJO5MZQN11PqgIvyuvQ?list={PL}",
        f"https://www.youtube.com/embed/videoseries?list={PL}",
        f"https://music.youtube.com/playlist?list={PL}",
        f"https://example.com/playlist?list={PL}",
        f"https://www.youtube.com.evil.com/playlist?list={PL}",
        f"https://www.youtube.com:8080/playlist?list={PL}",
        f"https://user@www.youtube.com/playlist?list={PL}",
        f"https://user:pw@www.youtube.com/playlist?list={PL}",
        f"ftp://www.youtube.com/playlist?list={PL}",
        f"javascript:alert(1)//{PL}",
        f"https://www.youtube.com/playlist?list={PL}\x00",
        f"https://www.youtube.com/playlist?list={PL}\\x",
        "https://",
    ],
)
def test_refused_references_raise_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_playlist_ref(text)


def test_input_over_the_limit_is_refused_before_parsing() -> None:
    parse_playlist_ref("PL" + "a" * 62)  # sanity: well under the limit
    with pytest.raises(ValueError, match=str(MAX_REF_CHARS)):
        parse_playlist_ref(f"https://www.youtube.com/playlist?list={PL}&x=" + "a" * MAX_REF_CHARS)


def test_non_string_input_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_playlist_ref(None)  # type: ignore[arg-type]


def test_the_message_echoes_at_most_a_short_escaped_prefix() -> None:
    secret = "SECRET\x1b[31m" + "z" * 500
    with pytest.raises(ValueError) as caught:
        parse_playlist_ref(secret)
    message = str(caught.value)
    assert "\x1b" not in message
    assert "z" * 100 not in message
    assert len(message) < 200


def test_a_refused_host_is_not_echoed_in_full() -> None:
    with pytest.raises(ValueError) as caught:
        parse_playlist_ref("https://" + "h" * 300 + ".com/playlist?list=" + PL)
    assert "h" * 100 not in str(caught.value)


def test_playlist_url_is_the_canonical_form_and_validates() -> None:
    assert playlist_url(PL) == f"https://www.youtube.com/playlist?list={PL}"
    with pytest.raises(ValueError):
        playlist_url("RD" + "a" * 20)
    with pytest.raises(ValueError):
        playlist_url("--help")


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_FORMS = [
    "{}",
    "https://www.youtube.com/playlist?list={}",
    "m.youtube.com/playlist?list={}",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={}",
]


@given(st.text(max_size=120), st.sampled_from(_FORMS))
def test_anything_accepted_fullmatches_the_id_pattern(text: str, form: str) -> None:
    for candidate in (text, form.format(text)):
        try:
            result = parse_playlist_ref(candidate)
        except ValueError:
            continue
        assert PLAYLIST_ID_PATTERN.fullmatch(result)
        assert result[:2] in ("PL", "OL", "UU", "FL")
        assert 12 <= len(result) <= 64


@given(
    st.sampled_from(["PL", "OL", "UU", "FL"]),
    st.text(alphabet=_ALPHABET, min_size=10, max_size=62),
    st.sampled_from(_FORMS),
)
def test_a_valid_id_round_trips_through_every_form(prefix: str, tail: str, form: str) -> None:
    playlist_id = prefix + tail
    assert parse_playlist_ref(form.format(playlist_id)) == playlist_id


@given(st.text(min_size=1, max_size=40).filter(lambda s: s[:2] not in ("PL", "OL", "UU", "FL")))
def test_an_id_with_another_prefix_is_never_accepted(tail: str) -> None:
    with pytest.raises(ValueError):
        parse_playlist_ref(f"https://www.youtube.com/playlist?list={tail}")
