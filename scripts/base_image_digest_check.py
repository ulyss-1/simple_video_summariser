"""Compare digest-pinned base images with what Docker Hub serves today (issue #241).

Run by the scheduled workflow ``.github/workflows/base-image-digest-check.yml``.
Usage: ``base_image_digest_check.py <Dockerfile>``. Every ``FROM`` that carries
both a tag and an ``@sha256:`` digest is checked; a ``FROM`` without a digest
is skipped. Nothing about the images is hard-coded here.

It only reports; it never edits the Dockerfile (a human bumps the pin, as with
yt-dlp, #68). Standard library only.

Exit codes:

- ``0`` and no stdout: every pin equals the registry's digest for its tag.
- ``1`` and one GitHub Actions ``::error`` line per stale reference, naming
  the pinned and upstream digest and the replacement ``FROM`` reference.
- ``0`` and one stderr line per reference whose lookup failed: the registry
  could not be reached or answered with something unusable; the check runs
  again next time. A real staleness elsewhere still gives exit ``1``.
- ``2``: a repository fault (file unreadable, no pin, malformed pin, an
  ``ARG`` in the reference, a registry other than Docker Hub, a one-component
  official-image name). The registry is never contacted.

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
from dataclasses import dataclass
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
    name: str  # repository path on Docker Hub, e.g. ``nginx/nginx-unprivileged``
    tag: str
    digest: str

    def pinned(self, digest: str | None = None) -> str:
        return f"{self.name}:{self.tag}@{digest or self.digest}"


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
    head, slash, tail = name.partition("/")
    if slash and (head == "docker.io" or "." in head or ":" in head or head == "localhost"):
        if head != "docker.io":
            raise RepositoryFault(f"{image!r} names registry {head}; only Docker Hub is supported")
        name = tail
    if "/" not in name:
        raise RepositoryFault(f"{image!r} is a one-component official image name; not supported yet (#249)")
    if not _NAME.fullmatch(name):
        raise RepositoryFault(f"{image!r} has an unusable image name")
    return Reference(name, tag, digest)


def pinned_references(dockerfile: str) -> list[Reference]:
    """Every ``FROM`` with a digest, validated; raises ``RepositoryFault``."""
    refs = []
    for line in dockerfile.splitlines():
        image = _from_image(line)
        if image is not None and "@" in image:
            refs.append(_parse_reference(image))
        elif image is not None and "$" in image:
            raise RepositoryFault(f"{image!r} is built from an ARG; pin a literal reference")
    if not refs:
        raise RepositoryFault("no FROM carries a tag and an @sha256: digest")
    return refs


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
    if len(args) != 1:
        print("base-image-digest-check: usage: base_image_digest_check.py <Dockerfile>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        refs = pinned_references(path.read_text())
    except (RepositoryFault, OSError, UnicodeDecodeError) as err:
        print(f"base-image-digest-check: cannot read the pins from {path}: {err}", file=sys.stderr)
        return 2
    stale = False
    for ref in refs:
        try:
            latest = upstream_digest(ref, fetch)
        except Exception as err:  # noqa: BLE001 - any outage must end quietly with exit 0
            print(
                f"base-image-digest-check: Docker Hub unavailable for {ref.name}:{ref.tag} "
                f"({type(err).__name__}: {err}); will retry on the next run",
                file=sys.stderr,
            )
            continue
        if latest != ref.digest:
            stale = True
            print(
                f"::error title=Base image digest is behind::{ref.name}:{ref.tag} is pinned to "
                f"{ref.digest} but upstream is {latest}. Replace the FROM reference with "
                f"{ref.pinned(latest)} and redeploy (architecture.md 11.3)."
            )
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
