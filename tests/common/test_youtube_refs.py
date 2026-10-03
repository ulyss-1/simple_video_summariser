"""``parse_video_ref``: the CLI's and the API's trust boundary for user-supplied videos (#31, #40)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.youtube_refs import parse_channel_ref, parse_video_ref


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


ID = "dQw4w9WgXcQ"
DASH_ID = "-wNyEUrxzFU"


@pytest.mark.parametrize(
    "text",
    [
        ID,
        DASH_ID,
        f"  {ID}\n",
        f"https://www.youtube.com/watch?v={ID}",
        f"http://www.youtube.com/watch?v={ID}",
        f"https://youtube.com/watch?v={ID}",
        f"https://m.youtube.com/watch?v={ID}",
        f"www.youtube.com/watch?v={ID}",
        f"youtube.com/watch?v={ID}",
        f"https://www.youtube.com/watch?v={ID}&t=42s&list=PLabc123",
        f"https://www.youtube.com/watch?list=PLabc123&v={ID}",
        f"https://www.youtube.com/watch?v={ID}#t=10",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={ID}",
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?t=42",
        f"https://youtu.be/{ID}?si=abcDEF123",
        f"youtu.be/{ID}",
        f"youtube.com/shorts/{ID}",
        f"https://www.youtube.com/shorts/{ID}?feature=share",
        f"youtube.com/live/{ID}",
        f"https://www.youtube.com/live/{ID}?si=x",
        f"youtube.com/embed/{ID}",
        f"https://www.youtube.com/embed/{ID}",
        f"https://youtu.be/{DASH_ID}",
        f"https://www.youtube.com/watch?v={DASH_ID}",
    ],
)
def test_every_accepted_form_yields_the_bare_id(text: str) -> None:
    expected = DASH_ID if DASH_ID in text else ID
    assert parse_video_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\t",
        ID[:10],
        ID + "x",
        "dQw4w9WgXc!",
        "dQw4w9WgXc ",  # 10 chars once stripped
        "dQw4w9 gXcQ",
        "dQw4w9WgXcé",
        f"{ID}\n{ID}",
        f"https://youtube.com.evil.com/watch?v={ID}",
        f"https://evil.com/watch?v={ID}",
        f"evil.com/watch?v={ID}",
        f"https://notyoutube.com/watch?v={ID}",
        f"https://www.youtube.com.evil.com/watch?v={ID}",
        f"https://evil.com/https://www.youtube.com/watch?v={ID}",
        f"https://user@www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com:8080/watch?v={ID}",
        f"https://www.youtube.com@evil.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",  # video only (owner, 2026-10-03)
        f"https://youtu.be.evil.com/{ID}",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?list=PLabc123",
        f"https://www.youtube.com/watch?v={ID}&v={ID}",
        f"https://www.youtube.com/watch?v={ID}&v=",
        "https://www.youtube.com/watch?v=",
        f"https://www.youtube.com/watch?v={ID[:10]}",
        f"https://www.youtube.com/watch?v={ID}x",
        f"https://www.youtube.com/watch?w={ID}",
        "https://www.youtube.com/playlist?list=PLabc123",
        "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        "https://www.youtube.com/@handle",
        "https://www.youtube.com/c/somename",
        "https://www.youtube.com/user/somename",
        "https://www.youtube.com/",
        "https://youtu.be/",
        f"https://youtu.be/{ID}/extra",
        f"https://www.youtube.com/shorts/{ID}/extra",
        f"https://www.youtube.com/shorts/{ID[:10]}",
        f"https://www.youtube.com/v/{ID}",
        f"file:///etc/passwd?v={ID}",
        f"file://www.youtube.com/watch?v={ID}",
        "javascript:alert(1)",
        f"javascript://www.youtube.com/watch?v={ID}%0Aalert(1)",
        f"ftp://www.youtube.com/watch?v={ID}",
        f"data:text/html,{ID}",
        f"https://www.youtube.com/watch?v={ID}\nhttps://evil.com",
        f"https://www.youtube.com\\@evil.com/watch?v={ID}",
        "-" + ID,
    ],
)
def test_every_rejected_form_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_video_ref(text)


def test_input_of_exactly_2048_characters_is_still_parsed() -> None:
    padding = "&x=" + "a" * (2048 - len(f"https://www.youtube.com/watch?v={ID}") - 3)
    text = f"https://www.youtube.com/watch?v={ID}{padding}"
    assert len(text) == 2048
    assert parse_video_ref(text) == ID


def test_input_of_2049_characters_is_rejected() -> None:
    padding = "&x=" + "a" * (2049 - len(f"https://www.youtube.com/watch?v={ID}") - 3)
    text = f"https://www.youtube.com/watch?v={ID}{padding}"
    assert len(text) == 2049
    with pytest.raises(ValueError):
        parse_video_ref(text)


def test_the_error_message_is_one_short_line_even_for_hostile_input() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_video_ref("x" * 5000 + "\x1b[2J\nsecond line")
    message = str(excinfo.value)
    assert "\n" not in message
    assert "\x1b" not in message
    assert len(message) < 300


_ID_CHARS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=11,
    max_size=11,
)


@given(_ID_CHARS)
def test_any_valid_id_round_trips_through_the_bare_and_the_watch_url_forms(video_id: str) -> None:
    assert parse_video_ref(video_id) == video_id
    assert parse_video_ref(watch_url(video_id)) == video_id


_BAD_ID_CHARS = st.sampled_from([";", " ", "%", ".", "é", "/", "?", "#", "&", "=", "+", "\n"])


@st.composite
def _ids_with_a_bad_char(draw: st.DrawFn) -> str:
    base = list(draw(_ID_CHARS))
    base[draw(st.integers(0, 10))] = draw(_BAD_ID_CHARS)
    return "".join(base)


_VIDEO_FORMS = (
    "{}",
    "https://www.youtube.com/watch?v={}",
    "http://youtube.com/watch?t=5&v={}&si=x",
    "m.youtube.com/watch?v={}&list=PL1&feature=share",
    "https://youtu.be/{}",
    "youtu.be/{}?t=30",
    "https://www.youtube.com/shorts/{}",
    "https://youtube.com/live/{}",
    "https://www.youtube.com/embed/{}",
)


@given(_ID_CHARS, st.sampled_from(_VIDEO_FORMS))
def test_any_valid_id_round_trips_through_every_accepted_url_form(
    video_id: str, form: str
) -> None:
    assert parse_video_ref(form.format(video_id)) == video_id


@given(_ids_with_a_bad_char(), st.sampled_from(_VIDEO_FORMS))
def test_an_id_with_a_character_outside_the_set_is_rejected_in_every_form(
    video_id: str, form: str
) -> None:
    with pytest.raises(ValueError):
        parse_video_ref(form.format(video_id))


# --- parse_channel_ref (#40) -------------------------------------------------

CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"


@pytest.mark.parametrize(
    "text",
    [
        CHANNEL,
        f"  {CHANNEL}\n",
        f"https://www.youtube.com/channel/{CHANNEL}",
        f"http://www.youtube.com/channel/{CHANNEL}",
        f"https://youtube.com/channel/{CHANNEL}",
        f"https://m.youtube.com/channel/{CHANNEL}",
        f"www.youtube.com/channel/{CHANNEL}",
        f"youtube.com/channel/{CHANNEL}",
        f"HTTPS://WWW.YOUTUBE.COM/channel/{CHANNEL}",
        f"https://www.youtube.com/channel/{CHANNEL}/",
        f"https://www.youtube.com/channel/{CHANNEL}/videos",
        f"https://www.youtube.com/channel/{CHANNEL}?view_as=subscriber",
        f"https://www.youtube.com/channel/{CHANNEL}/videos?sort=dd#x",
    ],
)
def test_every_accepted_channel_form_yields_the_bare_channel_id(text: str) -> None:
    assert parse_channel_ref(text) == CHANNEL


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        CHANNEL[:-1],
        CHANNEL + "x",
        "UX" + CHANNEL[2:],
        "uc" + CHANNEL[2:],
        CHANNEL[:-1] + ";",
        CHANNEL[:-1] + "%",
        CHANNEL[:-1] + ".",
        CHANNEL[:-1] + "é",
        CHANNEL[:10] + " " + CHANNEL[11:],
        f"https://youtube.com.evil.com/channel/{CHANNEL}",
        f"https://evilyoutube.com/channel/{CHANNEL}",
        f"https://youtube.com@evil.com/channel/{CHANNEL}",
        f"https://user@www.youtube.com/channel/{CHANNEL}",
        f"https://www.youtube.com:8080/channel/{CHANNEL}",
        f"https://music.youtube.com/channel/{CHANNEL}",
        f"https://youtu.be/channel/{CHANNEL}",
        f"javascript:alert('{CHANNEL}')",
        f"ftp://www.youtube.com/channel/{CHANNEL}",
        f"https://www.youtube.com/channel/{CHANNEL}/videos/extra",
        f"https://www.youtube.com/channel/{CHANNEL}/playlists",
        f"https://www.youtube.com/channel/{CHANNEL[:-1]}",
        "https://www.youtube.com/channel/",
        f"https://www.youtube.com/watch?v={ID}",
        ID,
        "https://www.youtube.com/playlist?list=PLabc123",
        "x" * 2049,
    ],
)
def test_every_rejected_channel_form_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_channel_ref(text)


@pytest.mark.parametrize(
    "text",
    [
        "@somehandle",
        "https://www.youtube.com/@somehandle",
        "youtube.com/@somehandle/videos",
        "https://www.youtube.com/c/somename",
        "https://www.youtube.com/user/somename",
    ],
)
def test_handle_and_legacy_channel_urls_are_rejected_with_a_channel_id_hint(text: str) -> None:
    with pytest.raises(ValueError, match="only channel-ID URLs"):
        parse_channel_ref(text)


def test_a_channel_ref_of_exactly_2048_characters_is_still_parsed() -> None:
    base = f"https://www.youtube.com/channel/{CHANNEL}?x="
    text = base + "a" * (2048 - len(base))
    assert len(text) == 2048
    assert parse_channel_ref(text) == CHANNEL


_CHANNEL_SUFFIX = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=22,
    max_size=22,
)


@given(_CHANNEL_SUFFIX)
def test_any_valid_channel_id_round_trips_through_the_bare_and_url_forms(suffix: str) -> None:
    channel_id = "UC" + suffix
    assert parse_channel_ref(channel_id) == channel_id
    assert parse_channel_ref(f"https://www.youtube.com/channel/{channel_id}/videos") == channel_id


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com/watch?v={ID[:10]}%51",
        f"https://www.youtube.com/watch?v={ID[:10]}+",
        f"https://www.youtube.com/watch?%76={ID}",
    ],
)
def test_percent_encoded_or_plus_ids_in_the_query_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_video_ref(text)


def test_whitespace_padding_counts_toward_the_length_limit() -> None:
    with pytest.raises(ValueError, match="longer than 2048"):
        parse_video_ref(ID + " " * 2048)
    with pytest.raises(ValueError, match="longer than 2048"):
        parse_channel_ref(CHANNEL + " " * 2048)


_NOT_ID_CHARS = st.characters(codec="utf-8").filter(
    lambda c: c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


@given(_ID_CHARS, st.integers(0, 10), _NOT_ID_CHARS, st.sampled_from(_VIDEO_FORMS))
def test_any_character_outside_the_id_set_is_rejected_in_every_form(
    valid: str, at: int, bad: str, form: str
) -> None:
    video_id = valid[:at] + bad + valid[at + 1 :]
    with pytest.raises(ValueError):
        parse_video_ref(form.format(video_id))
