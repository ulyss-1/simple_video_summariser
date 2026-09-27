"""Phase 0: measure English subtitle coverage for a list of YouTube channels.

Throwaway measurement script (issue #2), not the production path. It imports
nothing from common/, adapters/ or services/, and uses only the stdlib plus
yt-dlp (called as ``sys.executable -m yt_dlp`` so the venv's pinned copy runs).

Usage:
    python scripts/phase0/subtitle_coverage.py channels.txt [--out results.csv]
    python scripts/phase0/subtitle_coverage.py --from-csv results.csv

channels.txt holds one channel ID (UC...) per line; blank lines and lines
starting with ``#`` are ignored.

Each video of a channel's RSS feed lands in exactly one bucket:
    manual    ``subtitles`` has an English track (en or en-*)
    auto      only ``automatic_captions`` has English, and the video's
              original language is English
    none      no usable English track (including a machine-translated
              ``en`` auto track on a non-English video)
    excluded  Shorts, upcoming premieres, live streams - not in percentages
    error     yt-dlp failed; the reason is the first line of its stderr
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
WATCH_URL = "https://www.youtube.com/watch?v={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
EXCLUDED_LIVE_STATUSES = ("is_upcoming", "is_live")

BUCKETS = ("manual", "auto", "none", "error", "excluded")
COUNTED_BUCKETS = ("manual", "auto", "none", "error")
CSV_FIELDS = ("channel_id", "video_id", "title", "duration_sec", "bucket", "reason")

HTTP_TIMEOUT_SEC = 30
YTDLP_TIMEOUT_SEC = 300


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    link: str


@dataclass(frozen=True)
class VideoResult:
    channel_id: str
    video_id: str
    title: str
    duration_sec: int | None
    bucket: str
    reason: str


class ChannelError(Exception):
    """The channel's feed could not be read; the run continues."""


# --- input -----------------------------------------------------------------


def read_channel_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


# --- RSS feed ----------------------------------------------------------------


def fetch_feed(channel_id: str) -> list[FeedEntry]:
    if not CHANNEL_ID_RE.fullmatch(channel_id):
        raise ChannelError(f"invalid channel ID {channel_id!r}")
    request = urllib.request.Request(
        FEED_URL.format(channel_id), headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:
        raise ChannelError(f"feed returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChannelError(f"feed request failed: {exc}") from exc
    try:
        return parse_feed(body)
    except ET.ParseError as exc:
        raise ChannelError(f"feed is not valid XML: {exc}") from exc


def parse_feed(body: bytes) -> list[FeedEntry]:
    root = ET.fromstring(body)
    entries: list[FeedEntry] = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS).strip()
        title = entry.findtext("atom:title", default="", namespaces=NS).strip()
        link_el = entry.find("atom:link[@rel='alternate']", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        entries.append(FeedEntry(video_id=video_id, title=title, link=link))
    return entries


# --- classification ------------------------------------------------------------


def is_english(code: str) -> bool:
    return code == "en" or code.startswith("en-")


def original_language(info: dict[str, Any]) -> str | None:
    """The video's spoken language: yt-dlp's ``language``, else the ``-orig`` track."""
    language = info.get("language")
    if isinstance(language, str) and language:
        return language
    auto = info.get("automatic_captions") or {}
    for key in auto:
        if isinstance(key, str) and key.endswith("-orig"):
            return key.removesuffix("-orig")
    return None


def classify(info: dict[str, Any]) -> tuple[str, str]:
    """Bucket and reason for one video's ``yt-dlp --dump-json`` output."""
    live_status = info.get("live_status")
    if live_status in EXCLUDED_LIVE_STATUSES:
        return "excluded", f"live_status={live_status}"

    subtitles = info.get("subtitles") or {}
    manual_en = sorted(
        k
        for k in subtitles
        if isinstance(k, str) and k != "live_chat" and is_english(k)
    )
    if manual_en:
        return "manual", "subtitles: " + ",".join(manual_en)

    auto = info.get("automatic_captions") or {}
    auto_en = sorted(k for k in auto if isinstance(k, str) and is_english(k))
    language = original_language(info)
    if not auto_en:
        return "none", f"no English track; language={language}"
    if language is None:
        return "none", "English auto captions but original language unknown"
    if not is_english(language):
        return "none", f"machine-translated en only; language={language}"
    return "auto", f"automatic_captions: en; language={language}"


# Printed on every call when no JS runtime is installed; it is environment
# noise, never the reason a video failed, so it is skipped as a "first line".
NOISE_MARKERS = ("No supported JavaScript runtime",)


def first_line(stderr: str) -> str:
    """First meaningful line of yt-dlp's stderr."""
    for line in stderr.splitlines():
        line = line.strip()
        if line and not any(marker in line for marker in NOISE_MARKERS):
            return line
    return ""


def check_video(channel_id: str, entry: FeedEntry) -> VideoResult:
    def result(bucket: str, reason: str, duration: int | None = None) -> VideoResult:
        return VideoResult(
            channel_id, entry.video_id, entry.title, duration, bucket, reason
        )

    if not VIDEO_ID_RE.fullmatch(entry.video_id):
        return result("error", f"invalid video ID in feed: {entry.video_id!r}")

    # Full watch URL, never a bare ID: an ID may start with "-".
    # --ignore-no-formats-error lets upcoming premieres still dump metadata
    # (live_status=is_upcoming). It also turns "unavailable", "private" and
    # bot-check failures into a warning plus a stub with no formats, which is
    # caught below and recorded as an error.
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-json",
        "--skip-download",
        "--ignore-no-formats-error",
        WATCH_URL.format(entry.video_id),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_SEC, check=False
        )
    except subprocess.TimeoutExpired:
        return result("error", f"yt-dlp timed out after {YTDLP_TIMEOUT_SEC}s")
    if proc.returncode != 0:
        reason = first_line(proc.stderr) or f"yt-dlp exited with {proc.returncode}"
        return result("error", reason)
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return result("error", f"yt-dlp output is not JSON: {exc}")
    if not isinstance(info, dict):
        return result("error", "yt-dlp output is not a JSON object")

    live_status = info.get("live_status")
    if not info.get("formats") and live_status not in EXCLUDED_LIVE_STATUSES:
        return result("error", first_line(proc.stderr) or "yt-dlp found no formats")

    raw_duration = info.get("duration")
    duration = int(raw_duration) if isinstance(raw_duration, (int, float)) else None
    bucket, reason = classify(info)
    return result(bucket, reason, duration)


# --- run -------------------------------------------------------------------------


def scan(
    channel_ids: list[str], delay: float
) -> tuple[list[VideoResult], dict[str, str]]:
    results: list[VideoResult] = []
    channel_errors: dict[str, str] = {}
    calls = 0
    for channel_id in channel_ids:
        log(f"channel {channel_id}: fetching feed")
        try:
            entries = fetch_feed(channel_id)
        except ChannelError as exc:
            channel_errors[channel_id] = str(exc)
            log(f"channel {channel_id}: ERROR {exc}")
            continue
        log(f"channel {channel_id}: {len(entries)} videos")
        for entry in entries:
            if "/shorts/" in entry.link:
                results.append(
                    VideoResult(
                        channel_id,
                        entry.video_id,
                        entry.title,
                        None,
                        "excluded",
                        "short",
                    )
                )
                continue
            # One yt-dlp call at a time, spaced out to avoid rate limiting.
            if calls and delay > 0:
                time.sleep(delay)
            calls += 1
            video = check_video(channel_id, entry)
            log(f"  {entry.video_id} {video.bucket}: {video.reason}")
            results.append(video)
    return results, channel_errors


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- CSV ---------------------------------------------------------------------------


def write_csv(path: Path, results: list[VideoResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for r in results:
            duration = "" if r.duration_sec is None else str(r.duration_sec)
            writer.writerow(
                [r.channel_id, r.video_id, r.title, duration, r.bucket, r.reason]
            )


def read_csv(path: Path) -> list[VideoResult]:
    results: list[VideoResult] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            duration = row["duration_sec"]
            results.append(
                VideoResult(
                    channel_id=row["channel_id"],
                    video_id=row["video_id"],
                    title=row["title"],
                    duration_sec=int(duration) if duration else None,
                    bucket=row["bucket"],
                    reason=row["reason"],
                )
            )
    return results


# --- summary -----------------------------------------------------------------------


def format_table(label: str, results: list[VideoResult]) -> list[str]:
    counts = Counter(r.bucket for r in results)
    seconds: Counter[str] = Counter()
    for r in results:
        seconds[r.bucket] += r.duration_sec or 0
    counted = sum(counts[b] for b in COUNTED_BUCKETS)

    lines = [
        (
            f"{label}: {len(results)} videos, {counted} counted, "
            f"{counts['excluded']} excluded"
        ),
        f"  {'bucket':<9} {'count':>5} {'%':>7} {'hours':>8}",
    ]
    for bucket in BUCKETS:
        if bucket == "excluded":
            pct = "-"
        elif counted:
            pct = f"{100 * counts[bucket] / counted:.1f}%"
        else:
            pct = "n/a"
        hours = seconds[bucket] / 3600
        lines.append(f"  {bucket:<9} {counts[bucket]:>5} {pct:>7} {hours:>8.2f}")

    excluded_reasons = Counter(r.reason for r in results if r.bucket == "excluded")
    if excluded_reasons:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(excluded_reasons.items()))
        lines.append(f"  excluded by reason: {detail}")
    errors = Counter(r.reason for r in results if r.bucket == "error")
    for reason, n in errors.most_common():
        lines.append(f"  error x{n}: {reason}")
    return lines


def format_summary(results: list[VideoResult], channel_errors: dict[str, str]) -> str:
    channel_order: list[str] = []
    for r in results:
        if r.channel_id not in channel_order:
            channel_order.append(r.channel_id)

    lines: list[str] = []
    for channel_id in channel_order:
        rows = [r for r in results if r.channel_id == channel_id]
        lines.extend(format_table(f"Channel {channel_id}", rows))
        lines.append("")
    for channel_id, error in channel_errors.items():
        lines.append(f"Channel {channel_id}: ERROR {error}")
        lines.append("")
    lines.extend(format_table("TOTAL", results))
    lines.append(
        "Hours are yt-dlp durations; Shorts and errored videos have no duration."
    )
    if channel_errors:
        lines.append(f"{len(channel_errors)} channel(s) failed.")
    return "\n".join(lines)


# --- entry point -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure English subtitle coverage of YouTube channels."
    )
    parser.add_argument("channels", nargs="?", type=Path, help="file of channel IDs")
    parser.add_argument("--out", type=Path, help="write one CSV row per video")
    parser.add_argument(
        "--from-csv",
        type=Path,
        help="reprint the summary from a previous --out file, without network calls",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="seconds between yt-dlp calls (default: 2)",
    )
    args = parser.parse_args(argv)

    if args.from_csv is not None:
        if args.channels is not None:
            parser.error("pass either a channels file or --from-csv, not both")
        print(format_summary(read_csv(args.from_csv), {}))
        return 0
    if args.channels is None:
        parser.error("a channels file is required unless --from-csv is given")
    if args.delay < 0:
        parser.error("--delay must be >= 0")

    results, channel_errors = scan(read_channel_ids(args.channels), args.delay)
    if args.out is not None:
        write_csv(args.out, results)
    print(format_summary(results, channel_errors))
    return 1 if channel_errors else 0


if __name__ == "__main__":
    sys.exit(main())
