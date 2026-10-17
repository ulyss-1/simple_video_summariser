"""Log-safe rendering of URLs that may carry secrets (issues #84, #67).

Never scrub a URL out of free text with ``str.replace``: exception messages
quote *fragments* of a URL (``http.client.InvalidURL`` quotes the path or the
port), so a whole-string match misses them. Rebuild the safe value from parsed
components instead; whatever is not emitted cannot leak.

``redact`` (issue #196) is the one shared redactor for free text that is about
to be stored or logged (``jobs.last_error``, ``planner_task_runs.last_error``,
log lines). Everything it replaces becomes ``placeholder`` (default
``<redacted>``). Rules, applied in this order:

1. Caller-supplied literal secret values, matched by value, longest first.
2. ``Authorization:`` / ``Proxy-Authorization:`` header (case-insensitive,
   ``:`` or ``=``, optionally quoted): the scheme is kept, the credential
   dropped (``Authorization: Bearer <redacted>``).
3. ``Cookie:`` / ``Set-Cookie:``: the rest of the line is dropped.
4. URLs whose authority carries userinfo (``scheme://user:pw@host/...``) are
   rebuilt as ``scheme://host[:port]`` by parsing (path, query and fragment
   are dropped); an unparseable one is replaced whole. A URL without userinfo
   is left alone (``https://youtube.com/@handle`` survives).
5. API keys by prefix: ``sk-ant-`` or ``sk-`` (not preceded by a token
   character) followed by 16 or more of ``[A-Za-z0-9_-]``.
6. Bare high-entropy token, deliberately narrow: a maximal run of
   ``[A-Za-z0-9_-]`` that is at least ``BARE_TOKEN_MIN_CHARS`` (40) long,
   contains an uppercase letter, a lowercase letter and a digit, and has at
   least 16 distinct characters. UUIDs and SHA hashes (hex, one letter case),
   ``0x`` addresses, dotted module paths and file paths (``.`` and ``/``
   delimit tokens), ports and statuses (short, digits only) are never
   touched. Not covered: base64 with ``+/=`` and any secret shorter than 40
   characters that has no known prefix or header - pass those as literals.

Redaction is idempotent: the placeholder matches none of the rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit


def public_url(value: str, *, invalid: str = "<invalid URL>") -> str:
    """``scheme://hostname[:port]`` rebuilt from the parsed ``value``, safe to log.

    Only those three components are emitted; userinfo, path, params, query and
    fragment never are, wherever a secret was put. ``invalid`` is returned when
    the value does not parse to an http(s) URL with a host and a valid port.
    """
    try:
        parts = urlsplit(value)
        name = parts.hostname
        port = parts.port
    except ValueError:
        return invalid
    if parts.scheme not in ("http", "https") or not name:
        return invalid
    shown = f"[{name}]" if ":" in name else name
    return f"{parts.scheme}://{shown}" + (f":{port}" if port is not None else "")


DSN_PASSWORD_PLACEHOLDER = "***"
_DSN_SCHEMES = ("postgresql", "postgres")


def safe_dsn(value: str, *, invalid: str = "<invalid DSN>") -> str:
    """``postgresql://user:***@host:port/dbname`` rebuilt from the parsed ``value``.

    Like ``public_url``, the result is assembled from parsed components, never
    scrubbed out of the original text: the password is replaced by a fixed
    placeholder, and the query string (which may carry ``password=``) and
    fragment are dropped. ``invalid`` is returned for anything that does not
    parse as a ``postgresql://`` or ``postgres://`` URI - including a
    malformed one such as ``postgresql://u:pw@[bad/db``, whose password a
    replace-based scrubber could never find.
    """
    try:
        parts = urlsplit(value)
        name = parts.hostname
        port = parts.port
        user = parts.username
        has_password = parts.password is not None
    except ValueError:
        return invalid
    if parts.scheme not in _DSN_SCHEMES:
        return invalid
    userinfo = ""
    if user is not None or has_password:
        userinfo = user or ""
        if has_password:
            userinfo += f":{DSN_PASSWORD_PLACEHOLDER}"
        userinfo += "@"
    shown = f"[{name}]" if name and ":" in name else (name or "")
    return (
        f"{parts.scheme}://{userinfo}{shown}"
        + (f":{port}" if port is not None else "")
        + parts.path
    )


REDACTED = "<redacted>"
BARE_TOKEN_MIN_CHARS = 40
_BARE_TOKEN_MIN_DISTINCT = 16

_AUTH_HEADER = re.compile(
    r"""(?P<name>\b(?:Proxy-)?Authorization)(?P<sep>["']?[ \t]*[:=][ \t]*["']?)"""
    r"""(?:(?P<scheme>[A-Za-z][\w-]*)[ \t]+)?(?P<cred>[^\s"',;]+)""",
    re.IGNORECASE,
)
_COOKIE_HEADER = re.compile(
    r"""\b(?P<name>(?:Set-)?Cookie)\b["']?[ \t]*[:=][ \t]*["']?[^\r\n]*""", re.IGNORECASE
)
_URL = re.compile(r"""[A-Za-z][A-Za-z0-9+.\-]*://[^\s'"<>]+""")
_API_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-(?:ant-)?[A-Za-z0-9_-]{16,}")
_BARE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{{{BARE_TOKEN_MIN_CHARS},}}(?![A-Za-z0-9_-])"
)


def redact_literals(text: str, secrets: Iterable[str], placeholder: str = REDACTED) -> str:
    """``text`` with every non-empty literal in ``secrets`` replaced, longest first."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, placeholder)
    return text


def _redact_url(match: re.Match[str], placeholder: str) -> str:
    token = match.group(0)
    authority = re.split(r"[/?#]", token.split("://", 1)[1], maxsplit=1)[0]
    if "@" not in authority:
        return token
    try:
        parts = urlsplit(token)
        name = parts.hostname
        port = parts.port
    except ValueError:
        return placeholder
    if not name:
        return placeholder
    shown = f"[{name}]" if ":" in name else name
    return f"{parts.scheme}://{shown}" + (f":{port}" if port is not None else "")


def _redact_bare_token(match: re.Match[str], placeholder: str) -> str:
    token = match.group(0)
    if (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
        and len(set(token)) >= _BARE_TOKEN_MIN_DISTINCT
    ):
        return placeholder
    return token


def redact(text: str, *, secrets: Iterable[str] = (), placeholder: str = REDACTED) -> str:
    """``text`` with secrets replaced by ``placeholder``; see the module docstring."""
    text = redact_literals(text, secrets, placeholder)
    text = _AUTH_HEADER.sub(
        lambda m: m["name"] + m["sep"] + (f"{m['scheme']} " if m["scheme"] else "") + placeholder,
        text,
    )
    text = _COOKIE_HEADER.sub(lambda m: f"{m['name']}: {placeholder}", text)
    text = _URL.sub(lambda m: _redact_url(m, placeholder), text)
    text = _API_KEY.sub(placeholder, text)
    return _BARE_TOKEN.sub(lambda m: _redact_bare_token(m, placeholder), text)


def make_redactor(
    secrets: Iterable[str] = (), placeholder: str = REDACTED
) -> Callable[[str], str]:
    """``redact`` bound to fixed literal ``secrets`` and ``placeholder``."""
    literals = tuple(secrets)
    return lambda text: redact(text, secrets=literals, placeholder=placeholder)
