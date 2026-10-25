"""Compare digest-pinned base images with what Docker Hub serves today (#241, #249).

Run by the scheduled workflow ``.github/workflows/base-image-digest-check.yml``.
Usage: ``base_image_digest_check.py <file> [<file> ...]``. A path ending in
``.yml`` or ``.yaml`` is read as a Compose file (line-based, no YAML parser: the
``!reset`` / ``!override`` tags of the overlay defeat a plain loader), any other
path as a Dockerfile. Every ``FROM`` or ``image:`` that carries both a tag and
an ``@sha256:`` digest is checked; one without a digest is skipped. Every file
is parsed before the registry is contacted, and each distinct (repository, tag)
is looked up once. Nothing about the images is hard-coded here.

It only reports; it never edits a file (a human bumps the pin, as with
yt-dlp, #68). Standard library only.

Exit codes:

- ``0`` and no stdout: every pin equals the registry's digest for its tag.
- ``1`` and one GitHub Actions ``::error`` line per stale reference, naming
  the file, the line, the pinned and upstream digest and the replacement
  reference (a ``FROM`` reference, or the full Compose ``image:`` value).
- ``0`` and one stderr line per reference whose lookup failed: the registry
  could not be reached or answered with something unusable; the check runs
  again next time. A real staleness elsewhere still gives exit ``1``.
- ``2``: a repository fault (no file given, file unreadable, no pin,
  malformed pin, an ``ARG`` or ``${}`` in the reference, a registry other than
  Docker Hub). The registry is never contacted.

The network is one injectable seam (``Fetch``), so tests make no network call.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

TOKEN_URL = "https://auth.docker.io/token"
TOKEN_SERVICE = "registry.docker.io"
REGISTRY_URL = "https://registry-1.docker.io"
TIMEOUT_SEC = 20.0
MAX_BODY_BYTES = 1024 * 1024
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


def upstream_digest(ref: Reference, fetch: Fetch) -> str:
    """The digest the registry serves for the tag; raises on any outage."""
    query = urllib.parse.urlencode(
        {"service": TOKEN_SERVICE, "scope": f"repository:{ref.name}:pull"}, safe=":/"
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


def main(argv: Sequence[str] | None = None, *, fetch: Fetch = urllib_fetch) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "base-image-digest-check: usage: base_image_digest_check.py <file> [<file> ...]",
            file=sys.stderr,
        )
        return 2
    found: list[tuple[Path, Reference]] = []
    for arg in args:  # every file is parsed before the registry is contacted
        path = Path(arg)
        try:
            found.extend((path, ref) for ref in _read_pins(path))
        except (RepositoryFault, OSError, UnicodeDecodeError) as err:
            print(f"base-image-digest-check: cannot read the pins from {path}: {err}", file=sys.stderr)
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
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
