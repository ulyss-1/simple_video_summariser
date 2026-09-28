"""YouTube identifier checks shared by the adapters (trust boundary).

IDs reach URLs and yt-dlp arguments, so they are validated here before any
request or process starts. Used by the RSS feed adapter (#26) and the channel
catalog adapter (#27).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

# ``UC`` plus 22 characters, 24 in all. ``fullmatch`` never accepts a trailing
# newline, unlike ``$``.
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

MAX_REF_CHARS = 2048
_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_SHORT_HOST = "youtu.be"
_ID_PATH_PREFIXES = ("shorts", "live", "embed")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")
_BAD_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\]")


def is_channel_id(value: object) -> bool:
    return isinstance(value, str) and _CHANNEL_ID.fullmatch(value) is not None


def is_video_id(value: object) -> bool:
    """A video ID may start with ``-``; never pass a bare one to a process."""
    return isinstance(value, str) and _VIDEO_ID.fullmatch(value) is not None


def validate_channel_id(value: object) -> str:
    """Return ``value`` if it is a canonical ``UC...`` channel ID, else ``ValueError``."""
    if not isinstance(value, str) or _CHANNEL_ID.fullmatch(value) is None:
        raise ValueError(f"not a YouTube channel ID: {value!r}")
    return value


def parse_video_ref(text: str) -> str:
    """The 11-character video ID in ``text``, a bare ID or a YouTube video URL.

    Accepts a bare ID, ``watch?v=``, ``youtu.be/`` and ``/shorts/``, ``/live/``,
    ``/embed/`` URLs on ``youtube.com``, ``www.youtube.com``, ``m.youtube.com``
    and ``youtu.be`` only (http, https or no scheme; host case-insensitive).
    Anything else raises ``ValueError`` with a one-line message that never echoes
    more than a short, escaped prefix of the input: channels, playlists,
    handles, other hosts (also ones with a port or user info), other schemes,
    a missing, repeated or empty ``v``, and input over ``MAX_REF_CHARS``.

    Only the returned ID may be passed on. It can start with ``-``, so a
    process must get ``watch_url(id)``, never the bare ID.
    """
    ref = text.strip() if isinstance(text, str) else ""
    if not ref:
        raise ValueError("a video ID or URL is required")
    shown = repr(ref[:60]) + ("..." if len(ref) > 60 else "")
    if len(ref) > MAX_REF_CHARS:
        raise ValueError(f"input is longer than {MAX_REF_CHARS} characters: {shown}")
    if is_video_id(ref):
        return ref
    if _BAD_CHARS.search(ref):
        raise ValueError(f"not a YouTube video ID or URL: {shown}")

    scheme = _SCHEME.match(ref)
    if scheme is not None:
        if scheme.group(1).lower() not in ("http", "https"):
            raise ValueError(f"only http(s) YouTube URLs are accepted: {shown}")
        url = ref
    elif re.match(r"[^/?#]*:", ref):
        # "javascript:...", "file:x", "youtube.com:443/..." - a scheme or a port.
        raise ValueError(f"not a YouTube video ID or URL: {shown}")
    else:
        url = "https://" + ref
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError(f"not a YouTube video ID or URL: {shown}") from None

    # ``netloc`` keeps user info and a port, so those never match a host.
    host = parts.netloc.lower()
    segments = parts.path.split("/")[1:]
    if segments and segments[-1] == "":
        segments.pop()
    candidate: str | None = None
    if host == _SHORT_HOST:
        if len(segments) == 1:
            candidate = segments[0]
    elif host in _HOSTS:
        if segments == ["watch"]:
            values = [v for k, v in parse_qsl(parts.query, keep_blank_values=True) if k == "v"]
            if len(values) == 1:
                candidate = values[0]
        elif len(segments) == 2 and segments[0] in _ID_PATH_PREFIXES:
            candidate = segments[1]
    else:
        raise ValueError(f"not a YouTube video URL (host {host[:60]!r}): {shown}")
    if candidate is None or not is_video_id(candidate):
        raise ValueError(f"no single valid video ID in the input: {shown}")
    return candidate
