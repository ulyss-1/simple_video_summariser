"""WebVTT parsing for YouTube subtitles: manual tracks and rolling auto captions.

`parse_vtt` is a pure function. It is deliberately lenient: a cue it cannot
read is skipped and the rest of the file still parses. The only input it
rejects outright is text that is not WebVTT at all.

YouTube auto captions are "rolling": every cue repeats the previous line
above the new one, and a 10 ms "echo" cue repeats the line just finished.
Both are removed generically. The leading lines of a cue that repeat the
trailing lines of the cue before it are dropped, and a cue left with nothing
new extends the previous segment instead of starting one.
"""

import html
import re
from dataclasses import replace

from common.models import Segment


class VttParseError(ValueError):
    """The input is not a WebVTT file."""


_LEADING_JUNK = re.compile(r"^[\s\ufeff]+")
_SIGNATURE = re.compile(r"WEBVTT(?:[ \t\n]|$)")
_TIMESTAMP = r"(?:([0-9]{1,10}):)?([0-9]{2}):([0-9]{2})\.([0-9]{3})"
_TIMING = re.compile(rf"[ \t]*{_TIMESTAMP}[ \t]*-->[ \t]*{_TIMESTAMP}(?:[ \t].*)?")
_TAG = re.compile(r"<([^>\n]*)>")
_VOICE = re.compile(r"v(?:\.[^ \t]*)?(?:[ \t]+(.*))?")
_SPACES = re.compile(r"[ \t\f\v]+")
# int()'s 4300-digit conversion limit counts leading zeros, so a decimal
# reference can trip it while representing a tiny (or zero) value. Strip
# leading zeros before decoding so int() only ever sees significant digits.
_DECIMAL_REF = re.compile(r"&#([0-9]+)(;?)")
# The highest codepoint (U+10FFFF) is 7 decimal digits, so 8+ significant
# digits are always out of range; skip int() for those and go straight to
# the replacement character html.unescape would produce anyway.
_MAX_CODEPOINT_DIGITS = 7

# A run of text and who says it; a cue line is a tuple of runs.
type _Run = tuple[str | None, str]
type _Line = tuple[_Run, ...]


def parse_vtt(text: str) -> list[Segment]:
    """Parse WebVTT into time-ordered segments with each spoken line once.

    Raises `VttParseError` if `text` is non-empty and, after any BOM and
    whitespace, does not start with the `WEBVTT` signature. Never raises
    anything else.
    """
    if text == "":
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _LEADING_JUNK.sub("", text, count=1)
    if not _SIGNATURE.match(text):
        raise VttParseError("input does not start with a WEBVTT header")

    cues = []
    for block in _blocks(text.split("\n")[1:]):
        cue = _parse_cue(block)
        if cue is not None:
            cues.append(cue)
    cues.sort(key=lambda cue: cue[0])
    return _collapse(cues)


def _blocks(lines: list[str]) -> list[list[str]]:
    """Split lines into blank-line separated blocks, dropping the header.

    A line containing `-->` also starts a new block when the current block
    already has a timing line, so cues missing their blank-line separator
    are still found.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    has_timing = False
    in_header = True
    for line in lines:
        if in_header:
            if line == "":
                in_header = False
                continue
            if "-->" not in line:
                continue
            in_header = False
        if line == "":
            if current:
                blocks.append(current)
            current, has_timing = [], False
            continue
        if "-->" in line:
            if has_timing:
                blocks.append(current)
                current = []
            has_timing = True
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _parse_cue(block: list[str]) -> tuple[float, float, list[_Line]] | None:
    """Return (start, end, lines) for a cue block; None for anything else.

    The timing line is the first line, or the second after an identifier.
    NOTE, STYLE and REGION blocks have neither, so they are skipped here.
    """
    if "-->" in block[0]:
        timing_index = 0
    elif len(block) > 1 and "-->" in block[1]:
        timing_index = 1
    else:
        return None
    match = _TIMING.fullmatch(block[timing_index])
    if match is None:
        return None
    start = _seconds(match.groups()[:4])
    end = _seconds(match.groups()[4:])
    if start is None or end is None or end < start:
        return None
    return start, end, _clean_lines(block[timing_index + 1 :])


def _seconds(parts: tuple[str | None, ...]) -> float | None:
    hours, minutes, seconds, millis = (int(part or 0) for part in parts)
    if minutes > 59 or seconds > 59:
        return None
    return (((hours * 60 + minutes) * 60 + seconds) * 1000 + millis) / 1000


def _clean_lines(payload: list[str]) -> list[_Line]:
    """Strip markup from cue text, keeping who says what; drop empty lines."""
    voice: str | None = None
    lines: list[_Line] = []
    for raw in payload:
        runs: list[_Run] = []
        position = 0
        for match in _TAG.finditer(raw):
            _append(runs, voice, _decode(raw[position : match.start()]))
            voice = _next_voice(match.group(1), voice)
            position = match.end()
        _append(runs, voice, _decode(raw[position:]))
        line = tuple(
            (speaker, cleaned)
            for speaker, chunk in runs
            if (cleaned := _SPACES.sub(" ", chunk).strip())
        )
        if line:
            lines.append(line)
    return lines


def _decode(chunk: str) -> str:
    return html.unescape(_DECIMAL_REF.sub(_shrink_decimal_ref, chunk))


def _shrink_decimal_ref(match: re.Match[str]) -> str:
    """Drop a decimal reference's leading zeros before html.unescape sees it.

    A reference with 8+ significant digits is always past U+10FFFF, so it is
    replaced directly instead of calling int() on a possibly huge string.
    """
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > _MAX_CODEPOINT_DIGITS:
        return "\ufffd"
    return f"&#{digits}{match.group(2)}"


def _append(runs: list[_Run], voice: str | None, chunk: str) -> None:
    """Add text to the current run; only non-blank text can change speaker."""
    if not chunk:
        return
    if runs and (runs[-1][0] == voice or not chunk.strip()):
        runs[-1] = (runs[-1][0], runs[-1][1] + chunk)
    else:
        runs.append((voice, chunk))


def _next_voice(tag: str, voice: str | None) -> str | None:
    if tag.startswith("/v"):
        return None
    match = _VOICE.fullmatch(tag)
    if match is None:
        return voice
    name = _decode(match.group(1) or "").strip()
    return name or None


def _collapse(cues: list[tuple[float, float, list[_Line]]]) -> list[Segment]:
    """Remove rolling-caption repeats and merge consecutive identical text."""
    segments: list[Segment] = []
    previous: list[_Line] = []
    for start, end, lines in cues:
        new = lines[_overlap(previous, lines) :]
        previous = lines
        pieces = _pieces(new)
        if not pieces:
            if lines and segments:  # nothing new: the cue repeats the last one
                segments[-1] = replace(segments[-1], end=max(segments[-1].end, end))
            continue
        for speaker, text in pieces:
            last = segments[-1] if segments else None
            if last is not None and (last.speaker, last.text) == (speaker, text):
                segments[-1] = replace(last, end=max(last.end, end))
            else:
                segments.append(Segment(start, end, text, speaker))
    return segments


def _overlap(previous: list[_Line], current: list[_Line]) -> int:
    """Length of the longest tail of `previous` that heads `current`."""
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _pieces(lines: list[_Line]) -> list[_Run]:
    """Join lines with single spaces, splitting where the speaker changes."""
    pieces: list[_Run] = []
    for line in lines:
        for speaker, text in line:
            if pieces and pieces[-1][0] == speaker:
                pieces[-1] = (speaker, f"{pieces[-1][1]} {text}")
            else:
                pieces.append((speaker, text))
    return pieces
