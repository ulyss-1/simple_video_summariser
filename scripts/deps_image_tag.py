"""Tags, lookup and cleanup for the prebuilt dependency images on GHCR (#368).

Two packages, ``ghcr.io/ulyss-1/ytdigest-deps`` (the ``deps`` stage of
``ops/Dockerfile.backend``) and ``ghcr.io/ulyss-1/ytdigest-whisper-deps`` (the
``whisper-deps`` stage of ``ops/Dockerfile.whisper``), hold no application code
and no secrets. They are inputs of the CI ``images`` job only; production builds
from source and never reads them (architecture.md 11.3).

Used by ``.github/workflows/publish-deps-images.yml`` (publisher) and by the CI
``images`` job (consumer), so both compute the same tag from the same files.
Subcommands:

- ``tags [--month YYYY-MM]``: prints ``KEY=VALUE`` lines (usable as a
  ``$GITHUB_OUTPUT``): ``deps_tag``, ``whisper_tag`` (``sha-`` plus the first 16
  hex of the content hash), ``deps_hash``, ``whisper_hash`` (the full hashes).
- ``resolve``: for each tag, asks the registry whether it exists and, when it
  does and its ``org.opencontainers.image.source`` label is this repository,
  prints ``YTDIGEST_DEPS_IMAGE=<ref>@sha256:<digest>`` (and the whisper one),
  for ``$GITHUB_ENV``. Anything else (missing tag, registry unreachable,
  private package, wrong label) prints nothing for that image, so the build
  runs from source. It never falls back to ``latest`` or to another tag, and it
  always exits 0. Log lines go to stderr.
- ``prune --package NAME``: deletes old versions of one package (see
  ``select_deletions``). A refusal ends in a ``::warning`` and exit 0.

The content hash covers exactly what determines the image: the text of the
``deno``, ``builder`` and ``deps`` stages of ``ops/Dockerfile.backend`` (which
holds the digest-pinned ``python:3.14-slim`` and Deno ``FROM`` lines),
``requirements.backend.lock``, and a ``YYYY-MM`` stamp (the apt packages are not
version-pinned, so the tag changes monthly and the scheduled publish rebuilds).
The whisper hash adds the ``whisper-deps`` stage of ``ops/Dockerfile.whisper``,
``requirements.whisper.lock`` and the deps hash. Stages are hashed whole, so a
new instruction cannot be missed; comment and blank lines, line endings and
trailing blanks are ignored. Application code and the ``runtime`` stages are not
inputs. Standard library only; the clock, the registry and the package list are
injectable seams.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

OWNER = "ulyss-1"
REGISTRY = f"ghcr.io/{OWNER}"
DEPS_PACKAGE = "ytdigest-deps"
WHISPER_PACKAGE = "ytdigest-whisper-deps"
SOURCE_URL = "https://github.com/ulyss-1/simple_video_summariser"
DEPS_ENV = "YTDIGEST_DEPS_IMAGE"
WHISPER_ENV = "YTDIGEST_WHISPER_DEPS_IMAGE"
SOURCE_LABEL = "org.opencontainers.image.source"

BACKEND_DOCKERFILE = "ops/Dockerfile.backend"
WHISPER_DOCKERFILE = "ops/Dockerfile.whisper"
BACKEND_STAGES = ("deno", "builder", "deps")
WHISPER_STAGES = ("whisper-deps",)

KEEP_RECENT_TAGS = 5
KEEP_YOUNGER_THAN_DAYS = 30
LATEST = "latest"

_HASH_TAG = re.compile(r"sha-[0-9a-f]{16}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FROM = re.compile(r"^FROM\s+(?:--\S+\s+)*\S+(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)


class ScriptError(Exception):
    """A repository fault (a stage or file the hash needs is missing)."""


# ---- hashing -------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def stages(dockerfile: str) -> dict[str, str]:
    """Stage name (lower case) -> normalised text, from its FROM to the next FROM.

    Comment lines, blank lines and surrounding blanks are dropped, so a comment
    edit cannot change a tag while any instruction edit does.
    """
    result: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in _lines(dockerfile):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _FROM.match(line)
        if match:
            name = (match.group(1) or str(len(result))).lower()
            current = result.setdefault(name, [])
        if current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in result.items()}


def _stage_text(path: str, text: str, wanted: Sequence[str]) -> list[tuple[str, str]]:
    found = stages(text)
    missing = [name for name in wanted if name not in found]
    if missing:
        raise ScriptError(f"{path}: no stage named {', '.join(missing)}")
    return [(f"{path}:{name}", found[name]) for name in wanted]


def _digest(parts: Sequence[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for label, content in parts:
        data = content.encode()
        h.update(f"{label}\0{len(data)}\0".encode())
        h.update(data)
        h.update(b"\n")
    return h.hexdigest()


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_bytes().decode().replace("\r\n", "\n")
    except OSError as error:
        raise ScriptError(f"cannot read {relative}: {error}") from error


def deps_hash(root: Path, month: str) -> str:
    parts = _stage_text(BACKEND_DOCKERFILE, _read(root, BACKEND_DOCKERFILE), BACKEND_STAGES)
    parts.append(("requirements.backend.lock", _read(root, "requirements.backend.lock")))
    parts.append(("month", month))
    return _digest(parts)


def whisper_hash(root: Path, month: str) -> str:
    parts = _stage_text(WHISPER_DOCKERFILE, _read(root, WHISPER_DOCKERFILE), WHISPER_STAGES)
    parts.append(("requirements.whisper.lock", _read(root, "requirements.whisper.lock")))
    parts.append(("deps-hash", deps_hash(root, month)))
    parts.append(("month", month))
    return _digest(parts)


def tag_of(full_hash: str) -> str:
    return f"sha-{full_hash[:16]}"


@dataclass(frozen=True)
class Tags:
    deps_hash: str
    whisper_hash: str

    @property
    def deps_tag(self) -> str:
        return tag_of(self.deps_hash)

    @property
    def whisper_tag(self) -> str:
        return tag_of(self.whisper_hash)


def compute_tags(root: Path, month: str) -> Tags:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ScriptError(f"month must be YYYY-MM, got {month!r}")
    return Tags(deps_hash(root, month), whisper_hash(root, month))


# ---- resolve (CI consumer) -------------------------------------------------------


@dataclass(frozen=True)
class Found:
    digest: str
    source: str


Lookup = Callable[[str], Found]


def registry_lookup(ref: str) -> Found:
    """Digest and source label of ``ref`` from the registry (public: no login)."""
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")
    data = json.loads(result.stdout)
    labels = (data.get("image") or {}).get("config", {}).get("Labels") or {}
    return Found(data["manifest"]["digest"], labels.get(SOURCE_LABEL, ""))


def resolve(
    tags: Tags,
    lookup: Lookup,
    *,
    registry: str = REGISTRY,
    source_url: str = SOURCE_URL,
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> dict[str, str]:
    """Environment variable -> ``<package>@sha256:<digest>`` for each image found.

    Only an image whose computed tag exists, whose digest is well formed and
    whose source label is this repository is returned; everything else is
    absent, which means "build from source".
    """
    wanted = (
        (DEPS_ENV, DEPS_PACKAGE, tags.deps_tag),
        (WHISPER_ENV, WHISPER_PACKAGE, tags.whisper_tag),
    )
    env: dict[str, str] = {}
    for variable, package, tag in wanted:
        name = f"{registry}/{package}"
        try:
            found = lookup(f"{name}:{tag}")
        except Exception as error:  # noqa: BLE001 - any failure means "build from source"
            reason = str(error).strip().splitlines()[-1:] or ["lookup failed"]
            log(f"no prebuilt image for {package}:{tag} ({reason[0]}): building from source")
            continue
        if not _DIGEST.fullmatch(found.digest):
            log(f"no prebuilt image for {package}:{tag} (bad digest): building from source")
        elif found.source != source_url:
            log(
                f"no prebuilt image for {package}:{tag} (source label "
                f"{found.source!r} is not this repository): building from source"
            )
        else:
            env[variable] = f"{name}@{found.digest}"
            log(f"using {package}:{tag} = {env[variable]}")
    return env


# ---- prune (publisher) -----------------------------------------------------------


def _created(version: Mapping[str, object]) -> datetime:
    return datetime.fromisoformat(str(version["created_at"]))


def _tags(version: Mapping[str, object]) -> list[str]:
    metadata = version.get("metadata")
    container = metadata.get("container") if isinstance(metadata, dict) else None
    tags = container.get("tags") if isinstance(container, dict) else None
    return [str(t) for t in tags] if isinstance(tags, list) else []


def select_deletions(
    versions: Sequence[Mapping[str, object]],
    now: datetime,
    *,
    keep_recent: int = KEEP_RECENT_TAGS,
    keep_days: int = KEEP_YOUNGER_THAN_DAYS,
) -> list[Mapping[str, object]]:
    """The versions to delete from one package.

    Kept: the version tagged ``latest``; the versions carrying the ``keep_recent``
    most recently pushed hash tags; any version younger than ``keep_days``.
    Untagged manifests are deleted unless young.
    """
    hashed = sorted(
        (v for v in versions if any(_HASH_TAG.fullmatch(t) for t in _tags(v))),
        key=_created,
        reverse=True,
    )
    keep = {id(v) for v in hashed[:keep_recent]}
    cutoff = now - timedelta(days=keep_days)
    for v in versions:
        if LATEST in _tags(v) or _created(v) > cutoff:
            keep.add(id(v))
    return [v for v in versions if id(v) not in keep]


def _parse_paginated(text: str) -> list[dict[str, object]]:
    """``gh api --paginate`` prints one JSON array per page, back to back."""
    decoder = json.JSONDecoder()
    versions: list[dict[str, object]] = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        page, pos = decoder.raw_decode(text, pos)
        versions.extend(page)
    return versions


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout


def prune(package: str, owner: str = OWNER, now: datetime | None = None) -> int:
    base = f"/users/{owner}/packages/container/{package}/versions"
    try:
        versions = _parse_paginated(_gh("api", "--paginate", base))
        doomed = select_deletions(versions, now or datetime.now(UTC))
        for version in doomed:
            _gh("api", "--method", "DELETE", f"{base}/{version['id']}")
            print(f"deleted {package} version {version['id']} {_tags(version)}")
    except Exception as error:  # noqa: BLE001 - a refused token must not fail the publish
        print(
            f"::warning title=Cleanup of {package} skipped::{error}. Delete old "
            f"versions by hand in the package settings (keep latest, the five "
            f"newest sha- tags and anything younger than {KEEP_YOUNGER_THAN_DAYS} days)."
        )
        return 0
    print(f"{package}: kept {len(versions) - len(doomed)}, deleted {len(doomed)}")
    return 0


# ---- command line ----------------------------------------------------------------


def main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("tags", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("--month", default=None, help="YYYY-MM (default: this month, UTC)")
    pr = sub.add_parser("prune")
    pr.add_argument("--package", required=True, choices=(DEPS_PACKAGE, WHISPER_PACKAGE))
    args = parser.parse_args(argv)

    if args.command == "prune":
        return prune(args.package)
    repo = root or Path(__file__).resolve().parent.parent
    month = args.month or datetime.now(UTC).strftime("%Y-%m")
    try:
        tags = compute_tags(repo, month)
    except ScriptError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    if args.command == "tags":
        print(f"deps_tag={tags.deps_tag}")
        print(f"whisper_tag={tags.whisper_tag}")
        print(f"deps_hash={tags.deps_hash}")
        print(f"whisper_hash={tags.whisper_hash}")
        return 0
    for variable, value in resolve(tags, registry_lookup).items():
        print(f"{variable}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
