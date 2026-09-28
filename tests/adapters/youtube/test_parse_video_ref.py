"""``parse_video_ref``: the CLI's and the API's trust boundary for user-supplied videos (#31)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapters.youtube.ids import parse_video_ref
from adapters.youtube.metadata import watch_url

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
        f"https://music.youtube.com/watch?v={ID}",
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
