"""YouTube identifier and reference parsing (trust boundary; #31, #40).

Pure functions with no I/O. IDs reach URLs, yt-dlp arguments and the
database, so they are validated here before any request, process or write.
Used by the RSS feed adapter (#26), the channel catalog adapter (#27), the
video metadata adapter, the CLI (#31) and the HTTP API (#40). It lives in
``common/`` so the API can use it without importing an adapter.

Only video hosts are accepted; ``music.youtube.com`` is rejected on purpose
(owner decision, 2026-10-03: the product is about video).
"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit

# ``UC`` plus 22 characters, 24 in all. Always use ``fullmatch``: unlike
# ``$``, it never accepts a trailing newline.
CHANNEL_ID_PATTERN = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
_CHANNEL_ID = CHANNEL_ID_PATTERN
_VIDEO_ID = VIDEO_ID_PATTERN

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
    raw = text if isinstance(text, str) else ""
    if len(raw) > MAX_REF_CHARS:
        raise ValueError(f"input is longer than {MAX_REF_CHARS} characters")
    ref = raw.strip()
    if not ref:
        raise ValueError("a video ID or URL is required")
    shown = repr(ref[:60]) + ("..." if len(ref) > 60 else "")
    if is_video_id(ref):
        return ref
    if _BAD_CHARS.search(ref):
        raise ValueError(f"not a YouTube video ID or URL: {shown}")

    parts = _split_url(ref, shown, what="video ID or URL")

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
            # Raw, not ``parse_qsl``: that would decode ``%51`` into ``Q``.
            values = [p[2:] for p in parts.query.split("&") if p.startswith("v=") or p == "v"]
            if len(values) == 1:
                candidate = values[0]
        elif len(segments) == 2 and segments[0] in _ID_PATH_PREFIXES:
            candidate = segments[1]
    else:
        raise ValueError(f"not a YouTube video URL (host {host[:60]!r}): {shown}")
    if candidate is None or not is_video_id(candidate):
        raise ValueError(f"no single valid video ID in the input: {shown}")
    return candidate


def parse_channel_ref(text: str) -> str:
    """The ``UC...`` channel ID in ``text``, a bare ID or a ``/channel/<id>`` URL.

    Accepts a bare channel ID or ``/channel/<id>`` on ``youtube.com``,
    ``www.youtube.com`` and ``m.youtube.com`` (http, https or no scheme; host
    case-insensitive), optionally followed by ``/``, ``/videos``, a query or a
    fragment. ``@handle``, ``/c/`` and ``/user/`` URLs are rejected: resolving
    them needs a network call (#81). Everything else is rejected as in
    ``parse_video_ref``, with the same short, escaped message.
    """
    raw = text if isinstance(text, str) else ""
    if len(raw) > MAX_REF_CHARS:
        raise ValueError(f"input is longer than {MAX_REF_CHARS} characters")
    ref = raw.strip()
    if not ref:
        raise ValueError("a channel ID or URL is required")
    shown = repr(ref[:60]) + ("..." if len(ref) > 60 else "")
    if is_channel_id(ref):
        return ref
    if ref.startswith("@"):
        raise UnsupportedChannelRef(_CHANNEL_ID_ONLY)
    if _BAD_CHARS.search(ref):
        raise ValueError(f"not a YouTube channel ID or URL: {shown}")

    parts = _split_url(ref, shown, what="channel ID or URL")
    host = parts.netloc.lower()
    if host not in _HOSTS:
        raise ValueError(f"not a YouTube channel URL (host {host[:60]!r}): {shown}")
    segments = parts.path.split("/")[1:]
    if segments and segments[-1] == "":
        segments.pop()
    if segments and (segments[0].startswith("@") or segments[0] in ("c", "user")):
        raise UnsupportedChannelRef(_CHANNEL_ID_ONLY)
    if (
        len(segments) in (2, 3)
        and segments[0] == "channel"
        and (len(segments) == 2 or segments[2] == "videos")
        and is_channel_id(segments[1])
    ):
        return segments[1]
    raise ValueError(f"no valid channel ID in the input: {shown}")


class UnsupportedChannelRef(ValueError):
    """An ``@handle``, ``/c/`` or ``/user/`` reference: valid, but needs a lookup (#81)."""


_CHANNEL_ID_ONLY = (
    "only channel-ID URLs (youtube.com/channel/UC...) are supported, "
    "not @handle, /c/ or /user/ URLs"
)


def _split_url(ref: str, shown: str, *, what: str) -> SplitResult:
    """``ref`` as URL parts; ``https://`` is assumed when there is no scheme.

    Only ``http`` and ``https`` are accepted. A bare ``name:`` prefix
    (``javascript:...``, ``file:x``, ``youtube.com:443/...``) is a scheme or a
    port and is rejected.
    """
    scheme = _SCHEME.match(ref)
    if scheme is not None:
        if scheme.group(1).lower() not in ("http", "https"):
            raise ValueError(f"only http(s) YouTube URLs are accepted: {shown}")
        url = ref
    elif re.match(r"[^/?#]*:", ref):
        raise ValueError(f"not a YouTube {what}: {shown}")
    else:
        url = "https://" + ref
    try:
        return urlsplit(url)
    except ValueError:
        raise ValueError(f"not a YouTube {what}: {shown}") from None
