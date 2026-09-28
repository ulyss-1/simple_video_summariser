"""YouTube identifier checks shared by the adapters (trust boundary).

IDs reach URLs and yt-dlp arguments, so they are validated here before any
request or process starts. Used by the RSS feed adapter (#26) and the channel
catalog adapter (#27).
"""

from __future__ import annotations

import re

# ``UC`` plus 22 characters, 24 in all. ``fullmatch`` never accepts a trailing
# newline, unlike ``$``.
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


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
