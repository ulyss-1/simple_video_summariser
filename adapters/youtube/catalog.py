"""YouTube implementation of the ``CatalogSource`` port (architecture.md C4, 8.3).

``list_uploads`` lists a channel's uploads, newest first, by running yt-dlp in
flat mode against the channel's uploads playlist (``UU`` plus the channel ID
without its ``UC``). It never fetches per-video metadata, and it lists at most
``MAX_CATALOG_LIMIT`` entries. The listing is untrusted input: bad entries are
dropped, not trusted.

A channel that has no uploads playlist gets no special treatment here: yt-dlp
reports "The playlist does not exist" for a channel ID that does not exist, for a
terminated channel, and for real channels with no uploads (checked live with
yt-dlp 2026.08.19 on the terminated channel UCx7T6qYK4VaP2-OhorrFS3Q and on
YouTube's own "Sports", "Music" and "Gaming" channels). All raise
``PermanentSourceError(REMOVED)``. No real channel was found whose uploads
playlist exists with zero entries; if yt-dlp returns one, the catalog has no
entries.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from adapters.youtube.errors import decode_ytdlp_json
from adapters.youtube.ytdlp import ProcessRunner, run_process, run_ytdlp
from common.errors import ToolFailureError
from common.models import CatalogEntry, ChannelCatalog, PlaylistListing
from common.youtube_refs import is_video_id, playlist_url, validate_channel_id

_logger = logging.getLogger(__name__)

# A flat listing of thousands of entries pages through many continuation requests.
DEFAULT_TIMEOUT_SEC = 300.0
MAX_CATALOG_LIMIT = 5000

_FLAGS = ("--flat-playlist", "--dump-single-json")


class YouTubeCatalog:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        runner: ProcessRunner = run_process,
        cookie_file: Path | None = None,
    ) -> None:
        self._timeout = timeout
        self._runner = runner
        self._cookie_file = cookie_file

    def list_uploads(self, channel_id: str, *, limit: int) -> ChannelCatalog:
        validate_channel_id(channel_id)
        _check_limit(limit)
        playlist_id = "UU" + channel_id[2:]
        # Always the full URL: a bare ID starting with "-" would read as an option.
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        stdout = run_ytdlp(
            [*_FLAGS, "--playlist-end", str(limit), "--no-warnings", url],
            timeout=self._timeout,
            runner=self._runner,
            cookie_file=self._cookie_file,
        )
        return _parse(decode_ytdlp_json(stdout), channel_id, playlist_id, limit)

    def list_playlist(self, playlist_id: str, *, limit: int) -> PlaylistListing:
        """List a public playlist's first ``limit`` entries in playlist order (#342).

        The same flat listing as ``list_uploads``, for any playlist ID that
        ``playlist_url`` accepts. Private and deleted entries come back as yt-dlp
        lists them (a title such as ``[Private video]``); a missing or private
        playlist is ``PermanentSourceError`` (YouTube does not tell them apart).
        """
        url = playlist_url(playlist_id)  # ValueError for anything but a safe ID
        _check_limit(limit)
        stdout = run_ytdlp(
            [*_FLAGS, "--playlist-end", str(limit), "--no-warnings", url],
            timeout=self._timeout,
            runner=self._runner,
            cookie_file=self._cookie_file,
        )
        info = decode_ytdlp_json(stdout)
        entries, skipped = _collect(_listing(info, playlist_id), playlist_id, limit)
        title = info.get("title")
        return PlaylistListing(
            playlist_id,
            title if isinstance(title, str) and title else None,
            entries,
            _count(info.get("playlist_count")),
            skipped,
        )


def _check_limit(limit: object) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_CATALOG_LIMIT
    ):
        raise ValueError(f"limit must be an int in 1..{MAX_CATALOG_LIMIT}: {limit!r}")


def _listing(info: Any, playlist_id: str) -> list[Any]:
    """The ``entries`` of ``info``, once it is known to describe ``playlist_id``."""
    if not isinstance(info, dict):
        raise ToolFailureError(
            f"yt-dlp output is a JSON {type(info).__name__}, not an object"
        )
    if info.get("id") != playlist_id:
        raise ToolFailureError(
            f"yt-dlp listed playlist {info.get('id')!r}, asked for {playlist_id!r}"
        )
    raw = info.get("entries")
    if not isinstance(raw, list):
        raise ToolFailureError("yt-dlp output has no 'entries' list")
    return raw


def _collect(
    raw: list[Any], owner: str, limit: int
) -> tuple[tuple[CatalogEntry, ...], int]:
    """The first ``limit`` valid, distinct entries of ``raw``, and how many were skipped."""
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    dropped = repeated = 0
    for item in raw:
        if len(entries) >= limit:
            break
        entry = _entry(item)
        if entry is None:
            dropped += 1
        elif entry.video_id in seen:
            repeated += 1
        else:
            seen.add(entry.video_id)
            entries.append(entry)
    if dropped:
        # Counts only: the raw values are untrusted.
        _logger.warning("dropped %d malformed catalog entries for %s", dropped, owner)
    return tuple(entries), dropped + repeated


def _parse(info: Any, channel_id: str, playlist_id: str, limit: int) -> ChannelCatalog:
    entries, _skipped = _collect(_listing(info, playlist_id), channel_id, limit)
    return ChannelCatalog(channel_id, entries, _count(info.get("playlist_count")))


def _entry(item: Any) -> CatalogEntry | None:
    if not isinstance(item, dict):
        return None
    video_id = item.get("id")
    if not isinstance(video_id, str) or not is_video_id(video_id):
        return None
    title = item.get("title")
    return CatalogEntry(
        video_id=video_id,
        title=title if isinstance(title, str) and title else None,
        duration_sec=_duration(item.get("duration")),
    )


def _duration(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value >= 0 else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
