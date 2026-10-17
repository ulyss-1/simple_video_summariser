"""Log-safe rendering of URLs that may carry secrets (issues #84, #67).

Never scrub a URL out of free text with ``str.replace``: exception messages
quote *fragments* of a URL (``http.client.InvalidURL`` quotes the path or the
port), so a whole-string match misses them. Rebuild the safe value from parsed
components instead; whatever is not emitted cannot leak.
"""

from __future__ import annotations

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
