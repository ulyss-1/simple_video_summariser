"""Server-side analysis page (issue #45; architecture.md 2, 8.3; D12c).

``render_analysis_html`` turns one video's latest analysis into a complete,
self-contained HTML page: one inline ``<style>``, no script, no external
resource. It is pure: it takes ``common.models`` values, does no I/O except
loading its template through ``importlib.resources``, and its output depends
only on its arguments. The v2 PDF/email export reuses it.

Every text field comes from YouTube or an LLM. Each value that is interpolated
goes through ``_e``, the one escaping helper. The only URLs on the page are
``https://www.youtube.com/watch?...`` built from a validated ``video_id``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from html import escape
from importlib.resources import files
from string import Template

from common.models import Analysis, Claim, Quote, Topic, VideoMeta
from common.youtube_refs import is_video_id

_WATCH = "https://www.youtube.com/watch?v="
_REL = 'rel="noopener noreferrer"'
UNATTRIBUTED = "Unattributed"
_UNKNOWN = "unknown"


def _e(value: object) -> str:
    """The single escaping point for untrusted text."""
    return escape(str(value), quote=True)


def format_timestamp(seconds: float) -> str:
    """``M:SS`` under an hour, ``H:MM:SS`` from an hour up; fractions are floored."""
    total = max(0, math.floor(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _link(video_id: str, text: str, suffix: str = "") -> str:
    if not is_video_id(video_id):
        return _e(text)
    return f'<a href="{_WATCH}{video_id}{suffix}" {_REL}>{_e(text)}</a>'


def _time(video_id: str, start_sec: float | None) -> str:
    """A timestamp link, or ``""`` for a NULL, negative or non-finite value."""
    if start_sec is None or not math.isfinite(start_sec) or start_sec < 0:
        return ""
    return _link(video_id, format_timestamp(start_sec), f"&amp;t={math.floor(start_sec)}s")


def _span(css_class: str, inner: str) -> str:
    return f'<span class="{css_class}">{inner}</span>' if inner else ""


def _join(parts: list[str]) -> str:
    return " ".join(part for part in parts if part)


def _speaker(name: str) -> str:
    return UNATTRIBUTED if name == _UNKNOWN else name


def _items(items: list[str], empty: str) -> str:
    if not items:
        return f'<p class="empty">{empty}</p>'
    return "<ul>\n" + "\n".join(f"<li>{item}</li>" for item in items) + "\n</ul>"


def _topic(video_id: str, topic: Topic) -> str:
    head = _join([_span("time", _time(video_id, topic.start_sec)), f"<strong>{_e(topic.title)}</strong>"])
    summary = f'<p class="sub">{_e(topic.summary)}</p>' if topic.summary else ""
    return head + summary


def _claim(video_id: str, claim: Claim) -> str:
    confidence = _span("confidence", _e(claim.confidence)) if claim.confidence else ""
    who = _span("who", _e(_speaker(claim.speaker)))
    return _join([_span("time", _time(video_id, claim.start_sec)), _e(claim.text), who, confidence])


def _quote(video_id: str, quote: Quote) -> str:
    who = _span("who", _e(_speaker(quote.speaker)))
    return _join(
        [_span("time", _time(video_id, quote.start_sec)), f"<q>{_e(quote.text)}</q>", who]
    )


def _roster(roster: object) -> list[str]:
    """Speaker lines from the untrusted ``speaker_roster`` JSON; never raises."""
    if not isinstance(roster, dict):
        return []
    speakers = roster.get("speakers")
    if not isinstance(speakers, list):
        return []
    lines: list[str] = []
    for entry in speakers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        role = entry.get("role")
        role_html = _span("who", _e(role)) if isinstance(role, str) and role else ""
        lines.append(_join([f"<strong>{_e(name)}</strong>", role_html]))
    return lines


def _meta(video: VideoMeta, channel_title: str | None) -> str:
    parts = [
        _e(channel_title or video.channel_id or ""),
        _e(_utc(video.published_at).date().isoformat()) if video.published_at else "",
        _e(format_timestamp(video.duration_sec))
        if video.duration_sec is not None and video.duration_sec >= 0
        else "",
        _link(video.video_id, "Watch on YouTube") if is_video_id(video.video_id) else "",
    ]
    return " &middot; ".join(part for part in parts if part)


def _footer(analysis: Analysis) -> str:
    parts = [f"Model: {_e(analysis.model)}", f"Prompt version: {_e(analysis.prompt_version)}"]
    if analysis.created_at is not None:
        parts.append(f"Analysed: {_e(_utc(analysis.created_at).isoformat())}")
    return " &middot; ".join(parts)


def _template() -> Template:
    text = (files("services.api") / "templates" / "analysis.html").read_text(encoding="utf-8")
    return Template(text)


def render_analysis_html(video: VideoMeta, channel_title: str | None, analysis: Analysis) -> str:
    """The whole page for ``analysis`` of ``video``."""
    vid = video.video_id
    title = _e(video.title or vid)
    speakers = _roster(analysis.speaker_roster)
    speakers_section = (
        f"<section>\n<h2>Speakers</h2>\n{_items(speakers, '')}\n</section>" if speakers else ""
    )
    tldr = (
        f'<p class="tldr">{_e(analysis.tldr)}</p>'
        if analysis.tldr
        else '<p class="empty">No summary.</p>'
    )
    return _template().substitute(
        title=title,
        meta=_meta(video, channel_title),
        tldr_block=tldr,
        speakers_section=speakers_section,
        topics_block=_items([_topic(vid, t) for t in analysis.topics], "No topics extracted."),
        claims_block=_items([_claim(vid, c) for c in analysis.claims], "No claims extracted."),
        quotes_block=_items([_quote(vid, q) for q in analysis.quotes], "No quotes extracted."),
        footer=_footer(analysis),
    )
