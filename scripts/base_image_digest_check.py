"""Compare digest-pinned base images with what Docker Hub serves today (#241, #249, #250).

Run by the scheduled workflow ``.github/workflows/base-image-digest-check.yml``.
Usage: ``base_image_digest_check.py <file> [<file> ...] [--stable-line <repository>]...
[--end-of-life <repository>:<line>=<date>]...``. A path ending in
``.yml`` or ``.yaml`` is read as a Compose file (line-based, no YAML parser: the
``!reset`` / ``!override`` tags of the overlay defeat a plain loader), any other
path as a Dockerfile. Every ``FROM`` or ``image:`` that carries both a tag and
an ``@sha256:`` digest is checked; one without a digest is skipped. Every file
is parsed before the registry is contacted, and each distinct (repository, tag)
is looked up once. Nothing about the images is hard-coded here.

``--stable-line <repository>`` (the name exactly as written in the ``FROM``;
repeatable) opts that repository in to a second, independent check: is the
pinned release line still the newest stable one, and does upstream still
publish it? Once a line reaches end of life its tag stops moving (so the digest
check goes quiet) and is eventually deleted, which a digest lookup treats as an
outage. The stable-line rule: a pinned tag of the form
``<major>.<minor>-<variant>`` is a stable line when ``<minor>`` is even.
Another tag is a newer stable line when it has the same ``<variant>``, an even
minor, and a larger ``(major, minor)`` compared numerically. So ``1.32`` and
``2.0`` are newer than ``1.30``, ``1.4`` is not, and ``1.31`` and ``1.33`` are
never counted. The tag list is read from ``/v2/<repository>/tags/list``,
following ``Link: rel="next"`` pages up to a bound. Without the flag nothing
below changes.

``--end-of-life <repository>:<line>=<date>`` (repeatable; #265, for example
``node:24=2028-04-30``) opts a repository in to a third check that makes no
network call: is the pinned release line within a warning window of its end of
life, or past it? The repository is written as in the ``FROM``; ``<date>`` is
``YYYY-MM-DD`` or ``YYYY-MM`` (the first day of that month, the earliest it can
be). The line of a pinned reference is its tag up to the first ``-``, and must be
digits, optionally followed by a dot and digits, and equal the option's line, so moving a tag without
its date is a fault. From ``EOL - window`` days (default 180, injectable) the
check gives ``::error title=Base image line nears end of life``; from the end of
life date on, ``::error title=Base image line is past end of life``. One
annotation per repository and line. The clock is the injectable ``today``.

It only reports; it never edits a file (a human bumps the pin, as with
yt-dlp, #68). Standard library only.

Exit codes:

- ``0`` and no stdout: every pin equals the registry's digest for its tag.
- ``1`` and one GitHub Actions ``::error`` line per stale reference, naming
  the file, the line, the pinned and upstream digest and the replacement
  reference (a ``FROM`` reference, or the full Compose ``image:`` value).
  An opted-in repository whose line is superseded gives exit ``1`` and one
  ``::error title=Base image stable line superseded`` line; one whose pinned
  tag upstream no longer lists gives ``::error title=Base image line withdrawn
  upstream``. An end-of-life annotation (above) also gives exit ``1``. The
  checks are independent of each other.
- ``0`` and one stderr line per reference whose lookup failed: the registry
  could not be reached or answered with something unusable (the tag list
  included); the check runs again next time. A real staleness elsewhere still gives exit ``1``.
- ``2``: a repository fault (no file given, file unreadable, no pin,
  malformed pin, an ``ARG`` or ``${}`` in the reference, a registry other than
  Docker Hub; for ``--stable-line``: a repository without a pin, a pinned tag
  that is not ``<major>.<minor>-<variant>``, an odd minor, a digit in the
  variant; for ``--end-of-life``: a repository without a pin, a line that differs
  from the pinned tag's or a tag without a numeric line, a malformed or
  impossible date, a repository given twice with different dates or named by
  ``--stable-line`` too). The registry is never contacted.

The network is one injectable seam (``Fetch``), so tests make no network call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NoReturn

TOKEN_URL = "https://auth.docker.io/token"
TOKEN_SERVICE = "registry.docker.io"
REGISTRY_URL = "https://registry-1.docker.io"
TIMEOUT_SEC = 20.0
MAX_BODY_BYTES = 1024 * 1024
MAX_TAG_PAGES = 10
EOL_WINDOW_DAYS = 180
#: The tag is a multi-platform index; without these Docker Hub may answer with
#: a single-platform manifest whose digest can never equal the pin.
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)

#: ``fetch(method, url, headers, timeout_sec)`` returns ``(status, headers,
#: body)``. HTTP error statuses are returned, not raised; any other failure
#: raises.
type Fetch = Callable[[str, str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


class RepositoryFault(Exception):
    pass


@dataclass(frozen=True)
class Reference:
    name: str  # repository path on Docker Hub, e.g. ``library/node``
    tag: str
    digest: str
    written: str = ""  # the name as written in the file, e.g. ``node``
    line: int = 0  # 1-based line in the file
    compose: bool = False

    def pinned(self, digest: str | None = None) -> str:
        return f"{self.written or self.name}:{self.tag}@{digest or self.digest}"


def urllib_fetch(
    method: str, url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), dict(response.headers.items()), response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as err:
        with err:
            return int(err.code), dict(err.headers.items()), b""


def _from_image(line: str) -> str | None:
    """The image token of a ``FROM`` line, flags and ``AS <name>`` dropped."""
    tokens = line.split("#", 1)[0].split()
    if not tokens or tokens[0].upper() != "FROM":
        return None
    rest = [t for t in tokens[1:] if not t.startswith("--")]
    return rest[0] if rest else None


def _parse_reference(image: str) -> Reference:
    if "$" in image:
        raise RepositoryFault(f"{image!r} is built from an ARG; pin a literal reference")
    base, _, digest = image.partition("@")
    if not _DIGEST.fullmatch(digest):
        raise RepositoryFault(f"{image!r} has a malformed digest (want sha256:<64 lowercase hex>)")
    name, sep, tag = base.rpartition(":")
    if not sep or "/" in tag:
        name, tag = base, ""
    if not tag:
        raise RepositoryFault(f"{image!r} has a digest but no tag")
    if not _TAG.fullmatch(tag):
        raise RepositoryFault(f"{image!r} has an unusable tag")
    written = name
    head, slash, tail = name.partition("/")
    if slash and (head == "docker.io" or "." in head or ":" in head or head == "localhost"):
        if head != "docker.io":
            raise RepositoryFault(f"{image!r} names registry {head}; only Docker Hub is supported")
        name = tail
    if not _NAME.fullmatch(name):
        raise RepositoryFault(f"{image!r} has an unusable image name")
    if "/" not in name:
        name = f"library/{name}"  # Docker official images live under library/
    return Reference(name, tag, digest, written)


def pinned_references(dockerfile: str) -> list[Reference]:
    """Every ``FROM`` with a digest, validated; raises ``RepositoryFault``."""
    refs = []
    for number, line in enumerate(dockerfile.splitlines(), start=1):
        image = _from_image(line)
        if image is not None and "@" in image:
            refs.append(replace(_parse_reference(image), line=number))
        elif image is not None and "$" in image:
            raise RepositoryFault(f"{image!r} is built from an ARG; pin a literal reference")
    if not refs:
        raise RepositoryFault("no FROM carries a tag and an @sha256: digest")
    return refs


_COMPOSE_IMAGE = re.compile(r"""^\s*image:\s*(?:"([^"]*)"|'([^']*)'|([^\s#]+))\s*(?:#.*)?$""")


def pinned_compose_references(text: str) -> list[Reference]:
    """Every ``image:`` with a digest, found line by line; raises ``RepositoryFault``."""
    refs = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _COMPOSE_IMAGE.match(line)
        if match is None:
            continue
        image = next(g for g in match.groups() if g is not None)
        if "@" in image:
            refs.append(replace(_parse_reference(image), line=number, compose=True))
    if not refs:
        raise RepositoryFault("no image: carries a tag and an @sha256: digest")
    return refs


def _read_pins(path: Path) -> list[Reference]:
    text = path.read_text()
    if path.suffix in (".yml", ".yaml"):
        return pinned_compose_references(text)
    return pinned_references(text)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _pull_token(name: str, fetch: Fetch) -> str:
    query = urllib.parse.urlencode(
        {"service": TOKEN_SERVICE, "scope": f"repository:{name}:pull"}, safe=":/"
    )
    status, _, body = fetch("GET", f"{TOKEN_URL}?{query}", {}, TIMEOUT_SEC)
    if status != 200:
        raise RuntimeError(f"token request answered HTTP {status}")
    try:
        token = json.loads(body)["token"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise RuntimeError("token reply is not the expected JSON") from None
    if not isinstance(token, str) or not token:
        raise TypeError("token reply has no token string")
    return token


def upstream_digest(ref: Reference, fetch: Fetch) -> str:
    """The digest the registry serves for the tag; raises on any outage."""
    token = _pull_token(ref.name, fetch)
    status, headers, _ = fetch(
        "HEAD",
        f"{REGISTRY_URL}/v2/{ref.name}/manifests/{ref.tag}",
        {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
        TIMEOUT_SEC,
    )
    if status != 200:
        raise RuntimeError(f"manifest request answered HTTP {status}")
    digest = _header(headers, "Docker-Content-Digest")
    if digest is None or not _DIGEST.fullmatch(digest):
        raise RuntimeError(f"Docker-Content-Digest is missing or malformed ({digest!r})")
    return digest


_LINE_TAG = re.compile(r"(\d+)\.(\d+)-(.+)")
_LINK_NEXT = re.compile(r"""<([^>]*)>\s*;\s*rel=(?:"next"|next(?![\w-]))""")


@dataclass(frozen=True)
class StableLine:
    """A pinned reference of an opted-in repository, split into its line parts."""

    ref: Reference
    major: int
    minor: int
    variant: str


def _split_line_tag(tag: str) -> tuple[int, int, str] | None:
    match = _LINE_TAG.fullmatch(tag)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(3)


def stable_line_pins(refs: Sequence[Reference], repositories: Sequence[str]) -> list[StableLine]:
    """The opted-in references, validated; raises ``RepositoryFault``."""
    pins: list[StableLine] = []
    for repository in repositories:
        matching = [r for r in refs if r.written == repository]
        if not matching:
            raise RepositoryFault(f"--stable-line {repository}: no digest-pinned FROM names it")
        for ref in matching:
            parts = _split_line_tag(ref.tag)
            if parts is None:
                raise RepositoryFault(
                    f"{ref.written}:{ref.tag} is not a <major>.<minor>-<variant> tag; "
                    "pin a stable line such as 1.30-alpine"
                )
            major, minor, variant = parts
            if minor % 2:
                raise RepositoryFault(
                    f"{ref.written}:{ref.tag} is a mainline (odd minor) tag, not a stable line"
                )
            if any(c.isdigit() for c in variant):
                raise RepositoryFault(
                    f"{ref.written}:{ref.tag} has a digit in the variant {variant!r}; "
                    "the newer line's variant would never match it"
                )
            if not any(p.ref.name == ref.name and p.ref.tag == ref.tag for p in pins):
                pins.append(StableLine(ref, major, minor, variant))
    return pins


def newest_stable_line(pin: StableLine, tags: Sequence[str]) -> str | None:
    """The newest tag that is a stable line newer than the pinned one, if any."""
    best = (pin.major, pin.minor)
    found: str | None = None
    for tag in tags:
        parts = _split_line_tag(tag)
        if parts is None or parts[2] != pin.variant or parts[1] % 2:
            continue
        if parts[:2] > best:
            best, found = parts[:2], tag
    return found


def upstream_tags(name: str, fetch: Fetch, max_pages: int = MAX_TAG_PAGES) -> list[str]:
    """The repository's complete tag list; raises on any outage or unusable reply."""
    token = _pull_token(name, fetch)
    prefix = f"/v2/{name}/tags/list"
    path = prefix
    tags: list[str] = []
    for _ in range(max_pages):
        status, headers, body = fetch(
            "GET", f"{REGISTRY_URL}{path}", {"Authorization": f"Bearer {token}"}, TIMEOUT_SEC
        )
        if status != 200:
            raise RuntimeError(f"tag list request answered HTTP {status}")
        if len(body) > MAX_BODY_BYTES:
            raise RuntimeError("tag list reply is too large")
        try:
            page = json.loads(body)["tags"]
        except (ValueError, KeyError, TypeError, RecursionError):
            raise RuntimeError("tag list reply is not the expected JSON") from None
        if not isinstance(page, list) or not page or not all(isinstance(t, str) for t in page):
            raise RuntimeError("tag list reply has no usable tags")
        tags.extend(page)
        link = _header(headers, "Link")
        match = _LINK_NEXT.search(link) if link else None
        if match is None:
            return tags
        path = match.group(1)
        if not path.startswith(prefix) or path[len(prefix) :][:1] not in ("", "?"):
            raise RuntimeError(f"tag list Link {path!r} does not stay on {prefix}")
    raise RuntimeError(f"tag list has more than {max_pages} pages")


_EOL_OPTION = re.compile(r"(?P<repository>[^=]+):(?P<line>\d+(?:\.\d+)?)=(?P<date>[^=]*)")
_EOL_DATE = re.compile(r"(\d{4})-(\d{2})(?:-(\d{2}))?")
_TAG_LINE = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class EndOfLife:
    repository: str
    line: str
    end: date


def _eol_date(text: str, option: str) -> date:
    match = _EOL_DATE.fullmatch(text)
    try:
        if match is None:
            raise ValueError
        return date(int(match[1]), int(match[2]), int(match[3] or 1))
    except ValueError:
        raise RepositoryFault(f"--end-of-life {option}: {text!r} is not YYYY-MM-DD or YYYY-MM") from None


def end_of_life_pins(
    refs: Sequence[Reference], options: Sequence[str], stable_lines: Sequence[str]
) -> list[EndOfLife]:
    """The opted-in lines, validated; raises ``RepositoryFault``."""
    pins: dict[str, EndOfLife] = {}
    for option in options:
        match = _EOL_OPTION.fullmatch(option)
        if match is None:
            raise RepositoryFault(f"--end-of-life {option!r} is not <repository>:<line>=<date>")
        pin = EndOfLife(match["repository"], match["line"], _eol_date(match["date"], option))
        if pin.repository in stable_lines:
            raise RepositoryFault(f"{pin.repository} is named by both --stable-line and --end-of-life")
        if pins.setdefault(pin.repository, pin) != pin:
            raise RepositoryFault(f"--end-of-life names {pin.repository} twice with different values")
    for pin in pins.values():
        matching = [r for r in refs if r.written == pin.repository]
        if not matching:
            raise RepositoryFault(f"--end-of-life {pin.repository}: no digest-pinned FROM names it")
        for ref in matching:
            tag_line = ref.tag.split("-", 1)[0]
            if not _TAG_LINE.fullmatch(tag_line):
                raise RepositoryFault(f"{ref.written}:{ref.tag} has no numeric release line (want <major>[.<minor>]-...)")
            if tag_line != pin.line:
                raise RepositoryFault(
                    f"--end-of-life {pin.repository}:{pin.line} but {ref.written}:{ref.tag} pins line "
                    f"{tag_line}; update the date with the tag"
                )
    return list(pins.values())


def _check_end_of_life(pin: EndOfLife, today: date, window: int) -> bool:
    """Report one opted-in line; True when it is red."""
    left = (pin.end - today).days
    action = (
        f"Move to a supported {pin.repository} line with a fresh digest "
        f"(docker buildx imagetools inspect {pin.repository}:<new tag>, architecture.md 11.3) "
        "and update the date in the workflow's --end-of-life option."
    )
    if today >= pin.end:
        print(
            f"::error title=Base image line is past end of life::{pin.repository} line {pin.line} "
            f"reached end of life on {pin.end.isoformat()} ({-left} days ago). {action}"
        )
        return True
    if left <= window:
        print(
            f"::error title=Base image line nears end of life::{pin.repository} line {pin.line} "
            f"reaches end of life on {pin.end.isoformat()} ({left} days left). {action}"
        )
        return True
    return False


def _usage_error(message: str) -> int:
    print(
        "base-image-digest-check: usage: base_image_digest_check.py <file> [<file> ...] "
        f"[--stable-line <repository>]... [--end-of-life <repository>:<line>=<date>]... ({message})",
        file=sys.stderr,
    )
    return 2


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RepositoryFault(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch = urllib_fetch,
    max_pages: int = MAX_TAG_PAGES,
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    window: int = EOL_WINDOW_DAYS,
) -> int:
    parser = _Parser(prog="base_image_digest_check.py", add_help=False)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--stable-line", action="append", default=[], dest="stable_lines")
    parser.add_argument("--end-of-life", action="append", default=[], dest="end_of_life")
    try:
        parsed = parser.parse_intermixed_args(sys.argv[1:] if argv is None else list(argv))
    except RepositoryFault as err:
        return _usage_error(str(err))
    if not parsed.files:
        return _usage_error("no file given")
    found: list[tuple[Path, Reference]] = []
    for arg in parsed.files:  # every file is parsed before the registry is contacted
        path = Path(arg)
        try:
            found.extend((path, ref) for ref in _read_pins(path))
        except (RepositoryFault, OSError, UnicodeDecodeError) as err:
            print(f"base-image-digest-check: cannot read the pins from {path}: {err}", file=sys.stderr)
            return 2
    try:
        line_pins = stable_line_pins([ref for _, ref in found], parsed.stable_lines)
        eol_pins = end_of_life_pins([ref for _, ref in found], parsed.end_of_life, parsed.stable_lines)
    except RepositoryFault as err:
        print(f"base-image-digest-check: {err}", file=sys.stderr)
        return 2
    lookups: dict[tuple[str, str], str | Exception] = {}
    stale = False
    for path, ref in found:
        key = (ref.name, ref.tag)
        if key not in lookups:
            try:
                lookups[key] = upstream_digest(ref, fetch)
            except Exception as err:  # noqa: BLE001 - any outage must end quietly with exit 0
                lookups[key] = err
                print(
                    f"base-image-digest-check: Docker Hub unavailable for {ref.name}:{ref.tag} "
                    f"({type(err).__name__}: {err}); will retry on the next run",
                    file=sys.stderr,
                )
        latest = lookups[key]
        if isinstance(latest, Exception) or latest == ref.digest:
            continue
        stale = True
        replacement = f"image: {ref.pinned(latest)}" if ref.compose else ref.pinned(latest)
        print(
            f"::error file={path},line={ref.line},title=Base image digest is behind::"
            f"{ref.written}:{ref.tag} is pinned to {ref.digest} but upstream is {latest}. "
            f"Replace the reference with {replacement} and redeploy (architecture.md 11.3)."
        )
    for pin in line_pins:
        stale = _check_line(pin, fetch, max_pages) or stale
    for eol in eol_pins:
        stale = _check_end_of_life(eol, today(), window) or stale
    return 1 if stale else 0


def _check_line(pin: StableLine, fetch: Fetch, max_pages: int) -> bool:
    """Report one opted-in reference; True when it is red."""
    ref = pin.ref
    try:
        tags = upstream_tags(ref.name, fetch, max_pages)
    except Exception as err:  # noqa: BLE001 - any outage must end quietly with exit 0
        print(
            f"base-image-digest-check: Docker Hub tag list unavailable for {ref.name} "
            f"({type(err).__name__}: {err}); will retry on the next run",
            file=sys.stderr,
        )
        return False
    newest = newest_stable_line(pin, tags)
    move = (
        f"Move the FROM to {ref.written}:{newest} with a fresh digest "
        f"(docker buildx imagetools inspect {ref.written}:{newest}), following architecture.md 11.3."
        if newest
        else ""
    )
    if ref.tag not in tags:
        print(
            "::error title=Base image line withdrawn upstream::upstream no longer publishes "
            f"{ref.written}:{ref.tag}." + (f" The newest stable line is {ref.written}:{newest}. {move}" if newest else "")
        )
        return True
    if newest:
        print(
            f"::error title=Base image stable line superseded::{ref.written}:{ref.tag} is pinned "
            f"but upstream publishes the newer stable line {ref.written}:{newest}. {move}"
        )
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
