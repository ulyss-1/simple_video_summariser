"""Compare the pinned yt-dlp with the latest PyPI release (issue #68).

Run by the scheduled workflow ``.github/workflows/ytdlp-update-check.yml``,
outside every application service (architecture.md 16.11: a broken extractor
is usually fixed upstream within days, so staleness is the bigger risk).

It only reports; it never edits ``requirements.backend.txt`` (a human bumps
the pin and redeploys). Standard library only.

Exit codes:

- ``0`` and no stdout: the pin is current (or newer than PyPI's latest).
- ``1`` and a GitHub Actions ``::error`` annotation naming the pinned and the
  latest version: the pin is behind. The scheduled run goes red, which is the
  visible signal; nothing else depends on it, so deploys are never blocked.
- ``0`` and one line on stderr: PyPI could not be reached or answered with
  something unusable. The check fails quietly and runs again next time.
- ``2``: ``requirements.backend.txt`` has no ``yt-dlp==<version>`` pin, a
  repository fault worth a loud failure.

With ``--dockerfile ops/Dockerfile.backend`` (issue #369) the companions are
checked too, after the yt-dlp check:

- ``yt-dlp-ejs`` must equal what the pinned yt-dlp requires in its ``default``
  extra. A mismatch is an ``::error`` (exit ``1``); agreement is silent.
- the Deno image pin must be at least the ``deno>=X`` minimum of that yt-dlp
  release. Below it is an ``::error`` (exit ``1``).
- a newer Deno release than the pin is only an ``::notice`` (exit unchanged).
- an outage or unusable answer for these checks is one stderr line, exit
  unchanged; a missing ``yt-dlp-ejs`` or Deno pin is exit ``2``.

The fetch is an injectable seam (``Fetch``), so tests make no network call.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
#: One release's metadata: the ``requires_dist`` of the pinned yt-dlp (#369).
PYPI_RELEASE_URL = "https://pypi.org/pypi/yt-dlp/{version}/json"
TIMEOUT_SEC = 20.0
#: The JSON lists every release of yt-dlp; cap what is read.
MAX_BODY_BYTES = 32 * 1024 * 1024

#: ``fetch(url, timeout_sec)`` returns ``(status, body)``. HTTP error statuses
#: are returned, not raised; any other failure raises.
type Fetch = Callable[[str, float], tuple[int, bytes]]

_PIN = re.compile(r"^\s*yt[-_.]dlp(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)", re.IGNORECASE | re.MULTILINE)


class PinNotFound(Exception):
    pass


def urllib_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as err:
        with err:
            return int(err.code), b""


def pinned_version(requirements: str) -> str:
    match = _PIN.search(requirements)
    if match is None:
        raise PinNotFound("no yt-dlp==<version> pin found")
    return match.group(1)


def _parse(version: str) -> tuple[int, ...]:
    """``2026.08.19`` -> ``(2026, 8, 19)``; numeric dotted versions only."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        raise ValueError(f"unusable version {version!r}")
    return tuple(int(part) for part in version.split("."))


def latest_version(fetch: Fetch) -> str:
    """The latest release on PyPI; raises on any outage or unusable reply."""
    status, body = fetch(PYPI_URL, TIMEOUT_SEC)
    if status != 200:
        raise RuntimeError(f"PyPI answered HTTP {status}")
    if len(body) > MAX_BODY_BYTES:
        raise RuntimeError("PyPI reply too large")
    try:
        version = json.loads(body)["info"]["version"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise RuntimeError("PyPI reply is not the expected JSON") from None
    if not isinstance(version, str):
        raise TypeError("PyPI reply has no version string")
    _parse(version)
    return version


def _check_ytdlp(path: Path, fetch: Fetch) -> int:
    try:
        pinned = pinned_version(path.read_text())
        _parse(pinned)
    except (PinNotFound, ValueError, OSError) as err:
        print(f"ytdlp-update-check: cannot read the yt-dlp pin from {path}: {err}", file=sys.stderr)
        return 2
    try:
        latest = latest_version(fetch)
    except Exception as err:  # noqa: BLE001 - any outage must end quietly with exit 0
        print(
            f"ytdlp-update-check: PyPI unavailable ({type(err).__name__}: {err}); "
            "will retry on the next run",
            file=sys.stderr,
        )
        return 0
    if _parse(latest) > _parse(pinned):
        print(
            f"::error title=yt-dlp pin is behind PyPI::pinned yt-dlp=={pinned}, "
            f"latest yt-dlp=={latest}. Bump the pin in requirements.backend.txt, "
            "regenerate requirements.backend.lock "
            "(python scripts/lock_requirements.py backend --upgrade-package yt-dlp) "
            "and redeploy (architecture.md 16.11)."
        )
        return 1
    return 0


# ---- companions: yt-dlp-ejs and Deno (issue #369, follow-up to #134) ----


#: ``yt-dlp-ejs==X`` in requirements.backend.txt.
_EJS_PIN = re.compile(
    r"^\s*yt[-_.]dlp[-_.]ejs\s*==\s*([^\s;#]+)", re.IGNORECASE | re.MULTILINE
)
#: The Deno image pin in ops/Dockerfile.backend, ``FROM denoland/deno:bin-X@sha256:...``.
_DENO_PIN = re.compile(r"^FROM\s+denoland/deno:bin-(\d+(?:\.\d+)*)@sha256:", re.MULTILINE)
#: One ``requires_dist`` entry of yt-dlp's default extra, e.g. ``yt-dlp-ejs==0.8.0; extra == "default"``.
#: yt-dlp-ejs is required by the ``default`` extra; the Deno minimum by the ``deno`` extra.
_EXTRA_REQUIREMENT = re.compile(
    r"\s*(yt[-_.]dlp[-_.]ejs|deno)\s*(==|>=)\s*([^\s;]+)\s*;\s*extra\s*==\s*[\"'](default|deno)[\"']\s*",
    re.IGNORECASE,
)
_EXTRA_FOR = {"yt-dlp-ejs": "default", "deno": "deno"}
DENO_RELEASE_URL = "https://api.github.com/repos/denoland/deno/releases/latest"


def ejs_pin(requirements: str) -> str:
    match = _EJS_PIN.search(requirements)
    if match is None:
        raise PinNotFound("no yt-dlp-ejs==<version> pin found")
    return match.group(1)


def deno_pin(dockerfile: str) -> str:
    match = _DENO_PIN.search(dockerfile)
    if match is None:
        raise PinNotFound("no FROM denoland/deno:bin-<version>@sha256 pin found")
    return match.group(1)


def _json_answer(fetch: Fetch, url: str) -> object:
    status, body = fetch(url, TIMEOUT_SEC)
    if status != 200:
        raise RuntimeError(f"{url} answered HTTP {status}")
    if len(body) > MAX_BODY_BYTES:
        raise RuntimeError(f"{url} reply too large")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise RuntimeError(f"{url} reply is not JSON") from None


def default_extra_requirements(fetch: Fetch, yt_dlp_version: str) -> dict[str, tuple[str, str]]:
    """What the pinned yt-dlp release requires: ``yt-dlp-ejs`` in its ``default``
    extra, the ``deno`` minimum in its ``deno`` extra.

    Maps ``yt-dlp-ejs`` / ``deno`` to ``(operator, version)``. Raises on an
    outage or a reply that is not PyPI's JSON shape.
    """
    url = PYPI_RELEASE_URL.format(version=yt_dlp_version)
    answer = _json_answer(fetch, url)
    info = answer.get("info") if isinstance(answer, dict) else None
    entries = info.get("requires_dist") if isinstance(info, dict) else None
    if entries is None:
        raise RuntimeError(f"{url} has no requires_dist")
    if not isinstance(entries, list):
        raise TypeError(f"{url} requires_dist is not a list")
    required: dict[str, tuple[str, str]] = {}
    for entry in entries:
        match = _EXTRA_REQUIREMENT.fullmatch(entry) if isinstance(entry, str) else None
        if match is None:
            continue
        name = match.group(1).lower().replace("_", "-").replace(".", "-")
        if _EXTRA_FOR[name] == match.group(4).lower():
            required[name] = (match.group(2), match.group(3))
    return required


def latest_deno(fetch: Fetch) -> str:
    answer = _json_answer(fetch, DENO_RELEASE_URL)
    tag = answer.get("tag_name") if isinstance(answer, dict) else None
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise RuntimeError("the Deno release tag is not vX.Y.Z")
    _parse(tag[1:])
    return tag[1:]


def _quiet(what: str, err: Exception) -> None:
    print(
        f"ytdlp-update-check: {what} unavailable ({type(err).__name__}: {err}); "
        "will retry on the next run",
        file=sys.stderr,
    )


def _check_companions(requirements: str, dockerfile: str, fetch: Fetch) -> int:
    """Check the yt-dlp-ejs and Deno pins against yt-dlp's metadata. Exit 0 or 1.

    Repository faults (no pin) raise ``PinNotFound`` or ``ValueError`` for the caller.
    """
    ejs = ejs_pin(requirements)
    deno = deno_pin(dockerfile)
    _parse(ejs)
    _parse(deno)
    yt_dlp = pinned_version(requirements)
    code = 0
    try:
        required = default_extra_requirements(fetch, yt_dlp)
    except Exception as err:  # noqa: BLE001 - an outage ends quietly
        _quiet("yt-dlp's metadata", err)
        required = {}
    if "yt-dlp-ejs" in required:
        op, want = required["yt-dlp-ejs"]
        if (op == "==" and ejs != want) or (op == ">=" and _parse(ejs) < _parse(want)):
            print(
                f"::error title=yt-dlp-ejs pin does not match yt-dlp::pinned yt-dlp-ejs=={ejs}, "
                f"but yt-dlp=={yt_dlp} requires yt-dlp-ejs{op}{want}. Move the two pins "
                "together in requirements.backend.txt and the lock (architecture.md 16.11)."
            )
            code = 1
    if "deno" in required:
        _op, minimum = required["deno"]
        if _parse(deno) < _parse(minimum):
            print(
                f"::error title=Deno pin is below yt-dlp's minimum::pinned deno {deno} in "
                f"ops/Dockerfile.backend, but yt-dlp=={yt_dlp} requires deno>={minimum}. "
                "Bump the FROM denoland/deno line and its digest (architecture.md 16.11)."
            )
            code = 1
    try:
        newest = latest_deno(fetch)
    except Exception as err:  # noqa: BLE001 - an outage ends quietly
        _quiet("the Deno release", err)
    else:
        if _parse(newest) > _parse(deno):
            print(
                f"::notice title=Deno is behind its release::pinned deno {deno}, "
                f"latest deno {newest}. Consider bumping ops/Dockerfile.backend (not blocking)."
            )
    return code


def _check_companions_files(path: Path, dockerfile: Path, fetch: Fetch) -> int:
    try:
        requirements = path.read_text()
        docker = dockerfile.read_text()
        return _check_companions(requirements, docker, fetch)
    except (PinNotFound, ValueError, OSError) as err:
        print(f"ytdlp-update-check: cannot read the companion pins: {err}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None, *, fetch: Fetch = urllib_fetch) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dockerfile: Path | None = None
    if "--dockerfile" in args:
        at = args.index("--dockerfile")
        if at + 1 >= len(args):
            print("ytdlp-update-check: --dockerfile needs a path", file=sys.stderr)
            return 2
        dockerfile = Path(args[at + 1])
        del args[at : at + 2]
    path = Path(args[0]) if args else Path("requirements.backend.txt")
    code = _check_ytdlp(path, fetch)
    if code == 2 or dockerfile is None:
        return code
    return max(code, _check_companions_files(path, dockerfile, fetch))


if __name__ == "__main__":
    sys.exit(main())
