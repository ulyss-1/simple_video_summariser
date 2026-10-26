"""Generate and check the hash-pinned Python locks (issue #145).

    python scripts/lock_requirements.py backend|whisper|dev [--upgrade | --upgrade-package NAME]
    python scripts/lock_requirements.py --check-installed requirements.backend.lock requirements.dev.lock
    python scripts/lock_requirements.py --report-outdated

``requirements.backend.lock`` and ``requirements.whisper.lock`` are generated
from ``requirements.backend.txt`` and ``requirements.whisper.txt``, and
``requirements.dev.lock`` (issue #264) from ``pyproject.toml``'s ``dev`` extra
plus its ``[build-system].requires``, resolved against the other two locks as
constraints and holding only what the dev layer adds. Order: ``backend``, then
``whisper``, then ``dev``. A lock holds: every
package the install would put into the image, transitive ones included, as an
exact ``name==version`` with the sha256 of every file PyPI publishes for that
version (so the same lock holds on WSL, the CI runner and ``python:3.14-slim``,
which can pick different wheels). The lock describes Linux x86_64 / CPython
3.14 only; anywhere else the script refuses to run.

Resolution is ``python -m pip install --dry-run --ignore-installed --report``,
so nothing is installed. By default the existing lock entries are passed to pip
as constraints (a direct pin edited in the input always wins), so a
regeneration changes only what the input change forces. Hashes come from
PyPI's JSON API. Any failure leaves the existing lock untouched: the new text
is written to a temporary file and renamed only on success.

The header records ``input-sha256``, a digest of the normalized input lines.
An offline test (``tests/test_requirements_lock.py``) compares it, so a lock
that no longer matches its input fails CI, while a new upstream release never
does. Install-time enforcement is pip's ``--require-hashes``.

``--report-outdated`` (issue #266) is the report-only weekly check: it resolves what
``backend --upgrade``, ``whisper --upgrade`` and ``dev --upgrade`` would resolve (each
against the upgraded results before it), compares that with the committed locks and
never writes a file or asks PyPI for hashes. Exit ``0`` and no stdout when nothing would
change; ``1`` and one ``::error`` annotation plus one line per entry when something
would; ``0`` with one stderr line when pip fails or times out (an outage); ``2`` when a
file is missing or unparseable or the platform is refused.

Standard library only; pip is invoked as a subprocess.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform as _platform
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = {
    "backend": ("requirements.backend.txt", "requirements.backend.lock"),
    "whisper": ("requirements.whisper.txt", "requirements.whisper.lock"),
    "dev": (
        "pyproject.toml [project.optional-dependencies].dev and [build-system].requires",
        "requirements.dev.lock",
    ),
}
# Locks a target is resolved against as read-only constraints, in the order to regenerate.
BASES = {"backend": (), "whisper": ("backend",), "dev": ("backend", "whisper")}
# Distributions that may be installed without being in a lock.
UNLOCKED_ALLOWED = ("pip", "ytdigest")

RunPip = Callable[[list[str]], tuple[int, str]]
Fetch = Callable[[str], bytes]
Lock = dict[str, tuple[str, tuple[str, ...]]]

_REQ = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,\s-]+)\])?"
    r"\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9._!+-]*)"
)
_LOCK_LINE = re.compile(
    r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._!+-]*)"
    r"(?P<hashes>(?: --hash=sha256:[0-9a-f]{64})+)"
)
_DIGEST_LINE = re.compile(r"# input-sha256: ([0-9a-f]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LockError(Exception):
    """A failure the user can act on; main() prints it and exits non-zero."""


def canonical_name(name: str) -> str:
    """PEP 503: lower case, runs of `-`, `_` and `.` collapsed to one `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_lines(text: str) -> list[tuple[str, str, str]]:
    """(canonical name, extras, version) for each requirement line."""
    found: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        match = _REQ.fullmatch(line)
        if match is None:
            raise LockError(f"not an exact pin in a requirements file: {raw.strip()!r}")
        extras = ",".join(
            sorted(e.strip().lower() for e in (match["extras"] or "").split(",") if e.strip())
        )
        found.append((canonical_name(match["name"]), extras, match["version"]))
    return found


def normalize_input(text: str) -> list[str]:
    """Requirement lines without comments, blanks and spacing, names canonical, sorted."""
    return sorted(
        f"{name}[{extras}]=={version}" if extras else f"{name}=={version}"
        for name, extras, version in _requirement_lines(text)
    )


def input_digest(text: str) -> str:
    joined = "\n".join(normalize_input(text)) + "\n"
    return hashlib.sha256(joined.encode()).hexdigest()


def direct_pins(text: str) -> dict[str, str]:
    """{canonical name: version} of the pins an input declares (extras dropped)."""
    return {name: version for name, _, version in _requirement_lines(text)}


def pyproject_requirements(text: str) -> tuple[list[str], list[str]]:
    """(dev extra entries, `[build-system].requires` entries) of a pyproject.toml text."""
    data = tomllib.loads(text)
    dev = list(data.get("project", {}).get("optional-dependencies", {}).get("dev", []))
    build = list(data.get("build-system", {}).get("requires", []))
    return dev, build


def dev_requirements_text(pyproject_text: str) -> str:
    """The dev and build entries as a requirements file, for pip to resolve."""
    dev, build = pyproject_requirements(pyproject_text)
    return "\n".join([*dev, *build]) + "\n"


def dev_input_digest(pyproject_text: str) -> str:
    """Digest of the dev and build entries, each set kept apart (`dev:` / `build:`)."""
    dev, build = pyproject_requirements(pyproject_text)
    lines = sorted(
        [f"dev:{n}" for n in normalize_input("\n".join(dev))]
        + [f"build:{n}" for n in normalize_input("\n".join(build))]
    )
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def dev_direct_pins(pyproject_text: str) -> dict[str, str]:
    return direct_pins(dev_requirements_text(pyproject_text))


def conflict_message(input_name: str, stderr: str, base_locks: Sequence[str]) -> str:
    """What to tell the user when pip cannot resolve an input."""
    if not base_locks:
        return (
            f"pip could not resolve {input_name}:\n{stderr.strip()}\n"
            "If an old lock entry is in the way, retry with `--upgrade-package <name>` "
            "for that package, or `--upgrade` to re-resolve everything."
        )
    names = " and ".join(base_locks)
    return (
        f"pip could not resolve {input_name} against {names} (used as constraints):\n"
        f"{stderr.strip()}\n"
        f"{names} and the lock being written were left unchanged; this script never edits "
        "a constraint lock. Change the dev pins to fit it, or bump the shared package "
        "in its own input and regenerate in order: backend, whisper, dev."
    )


def follow_up(target: str) -> str | None:
    """Which dependent locks to regenerate after `target` was written."""
    later = [t for t in TARGETS if target in BASES[t]]
    if not later:
        return None
    steps = ", then ".join(f"`python scripts/lock_requirements.py {t}`" for t in later)
    return f"{TARGETS[target][1]} changed; the dependent locks may now be stale. Regenerate: {steps}"


# ---- report and PyPI digests -> lock text ----


def reported_packages(report: Mapping[str, object]) -> dict[tuple[str, str], str]:
    """{(canonical name, version): sha256 of the file pip chose} from a pip report."""
    packages: dict[tuple[str, str], str] = {}
    items = report.get("install")
    if not isinstance(items, list):
        raise LockError("pip report has no 'install' list")
    for item in items:
        meta = item["metadata"]
        name, version = canonical_name(meta["name"]), meta["version"]
        archive = item.get("download_info", {}).get("archive_info", {})
        sha = archive.get("hashes", {}).get("sha256")
        if sha is None and str(archive.get("hash", "")).startswith("sha256="):
            sha = archive["hash"].split("=", 1)[1]
        if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
            raise LockError(f"pip reported no sha256 for {name}=={version}")
        packages[(name, version)] = sha
    return packages


def lock_text(
    report: Mapping[str, object],
    digests: Mapping[tuple[str, str], Collection[str]],
    *,
    input_name: str,
    command: str,
    digest: str,
    exclude: Collection[str] = (),
) -> str:
    """The lock file text. Rejects a package whose chosen file PyPI does not list."""
    entries: list[str] = []
    for (name, version), chosen in sorted(reported_packages(report).items()):
        if name in exclude:
            continue
        published = digests.get((name, version), ())
        if chosen not in published:
            raise LockError(
                f"{name}=={version}: the file pip chose (sha256 {chosen}) is not among "
                f"the {len(published)} file hash(es) PyPI lists for that version"
            )
        hashes = " ".join(f"--hash=sha256:{h}" for h in sorted(set(published)))
        entries.append(f"{name}=={version} {hashes}")
    header = [
        "# GENERATED FILE - do not edit by hand.",
        f"# Input: {input_name}",
        f"# Regenerate: {command}",
        "# Every entry lists the sha256 of every file PyPI publishes for that version.",
        f"# input-sha256: {digest}",
        "",
    ]
    return "\n".join([*header, *sorted(entries, key=lambda e: e.split("==")[0])]) + "\n"


def header_digest(text: str) -> str | None:
    for line in text.splitlines():
        match = _DIGEST_LINE.fullmatch(line)
        if match:
            return match.group(1)
    return None


def lock_problems(text: str) -> list[str]:
    """One message per lock line that is not a sorted, exact, hashed pin."""
    problems: list[str] = []
    previous: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            problems.append(f"line {number}: not an exact name==version pin with sha256 hashes: {line!r}")
            continue
        name = match["name"]
        if previous is not None and name <= previous:
            problems.append(f"line {number}: {name!r} is duplicated or not sorted after {previous!r}")
        previous = name
        hashes = [h.split(":", 1)[1] for h in match["hashes"].split()]
        if hashes != sorted(set(hashes)):
            problems.append(f"line {number}: hashes of {name!r} are not sorted and unique")
    return problems


def parse_lock(text: str) -> Lock:
    problems = lock_problems(text)
    if problems:
        raise LockError("; ".join(problems))
    parsed: Lock = {}
    for line in text.splitlines():
        match = _LOCK_LINE.fullmatch(line)
        if match:
            hashes = tuple(h.split(":", 1)[1] for h in match["hashes"].split())
            parsed[match["name"]] = (match["version"], hashes)
    return parsed


def check_installed(installed: Mapping[str, str], locked: Lock) -> list[str]:
    """Messages for locked packages that are missing or at another version."""
    problems = []
    for name, (version, _) in sorted(locked.items()):
        have = installed.get(name)
        if have is None:
            problems.append(f"{name}: locked at {version}, not installed")
        elif have != version:
            problems.append(f"{name}: locked at {version}, installed {have}")
    return problems


def unlocked_installed(
    installed: Mapping[str, str], locked: Lock, allowed: Collection[str] = UNLOCKED_ALLOWED
) -> list[str]:
    """Messages for installed distributions that no lock lists (bar `allowed`)."""
    return [
        f"{name}: installed {version}, in no lock"
        for name, version in sorted(installed.items())
        if name not in locked and name not in allowed
    ]


def platform_problem(
    system: str, machine: str, implementation: str, version: tuple[int, int]
) -> str | None:
    if (system, machine, implementation, tuple(version[:2])) == ("linux", "x86_64", "cpython", (3, 14)):
        return None
    return (
        "the lock describes exactly Linux x86_64 / CPython 3.14; this is "
        f"{system} {machine} {implementation} {version[0]}.{version[1]}. "
        "Run it in WSL with the project's Python 3.14 venv."
    )


# ---- generation ----


def default_run_pip(args: list[str]) -> tuple[int, str]:
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    return done.returncode, done.stderr


def default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        data: bytes = response.read()
    return data


def _published_hashes(name: str, version: str, fetch: Fetch) -> set[str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        payload = json.loads(fetch(url))
        return {f["digests"]["sha256"] for f in payload["urls"]}
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise LockError(f"could not read the file hashes of {name}=={version} from PyPI ({url}): {err}") from err


def _constraint_lines(
    target: str, root: Path, drop: Collection[str], upgrade: bool, direct: Collection[str]
) -> list[str]:
    own = TARGETS[target][1]
    sources: list[tuple[str, bool]] = [(TARGETS[base][1], True) for base in BASES[target]]
    sources.append((own, False))
    lines: list[str] = []
    for filename, always in sources:
        path = root / filename
        if not path.exists():
            if filename != own:
                base = next(t for t, (_, lock_name) in TARGETS.items() if lock_name == filename)
                raise LockError(f"{filename} is needed first: run `python scripts/lock_requirements.py {base}`")
            continue
        if upgrade and not always:
            continue
        for name, (version, _) in parse_lock(path.read_text()).items():
            if not always and (name in drop or name in direct):
                continue
            lines.append(f"{name}=={version}")
    return lines


def _write_atomically(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _resolve(
    target: str, root: Path, own_text: str, constraints: Sequence[str], run_pip: RunPip
) -> tuple[dict[str, object] | None, str]:
    """Run pip's dry-run resolve for `target`; (report, "") or (None, pip's stderr)."""
    input_name = TARGETS[target][0]
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        args = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                "--report", str(report_path)]
        if target == "whisper":
            args += ["--only-binary=:all:", "-r", str(root / TARGETS["backend"][0])]
        if target == "dev":
            args += ["-r", str(root / TARGETS["backend"][0])]
            own_path = Path(tmp) / "dev-requirements.txt"
            own_path.write_text(own_text)
            args += ["-r", str(own_path)]
        else:
            args += ["-r", str(root / input_name)]
        if constraints:
            constraint_path = Path(tmp) / "constraints.txt"
            constraint_path.write_text("\n".join(constraints) + "\n")
            args += ["-c", str(constraint_path)]
        returncode, stderr = run_pip(args)
        if returncode != 0:
            return None, stderr
        report: dict[str, object] = json.loads(report_path.read_text())
    return report, ""


def generate(
    target: str,
    *,
    root: Path = ROOT,
    upgrade: bool = False,
    upgrade_packages: Iterable[str] = (),
    run_pip: RunPip = default_run_pip,
    fetch: Fetch = default_fetch,
) -> Path:
    """Write the lock for `target` (atomically) and return its path."""
    input_name, lock_name = TARGETS[target]
    backend_text = (root / TARGETS["backend"][0]).read_text()
    if target == "dev":
        pyproject_text = (root / "pyproject.toml").read_text()
        own_text = dev_requirements_text(pyproject_text)
        digest = dev_input_digest(pyproject_text)
    else:
        own_text = (root / input_name).read_text()
        digest = input_digest(own_text)
    direct = set(direct_pins(backend_text)) | set(direct_pins(own_text))
    drop = {canonical_name(n) for n in upgrade_packages}
    constraints = _constraint_lines(target, root, drop, upgrade, direct)
    command = f"python scripts/lock_requirements.py {target}"
    base_locks = [TARGETS[b][1] for b in BASES[target]] if target == "dev" else []

    report, stderr = _resolve(target, root, own_text, constraints, run_pip)
    if report is None:
        raise LockError(conflict_message(input_name, stderr, base_locks))

    exclude: set[str] = set()
    if target != "backend":
        exclude = set(parse_lock((root / TARGETS["backend"][1]).read_text()))
    digests: dict[tuple[str, str], set[str]] = {}
    for name, version in reported_packages(report):
        if name not in exclude:
            digests[(name, version)] = _published_hashes(name, version, fetch)
    text = lock_text(
        report, digests, input_name=input_name, command=command, digest=digest, exclude=exclude,
    )

    path = root / lock_name
    _write_atomically(path, text)
    return path


# ---- --report-outdated (issue #266) ----

#: How many changed entries the annotation lists inline.
INLINE_LIMIT = 10
#: Seconds one pip resolve may take in --report-outdated mode.
PIP_TIMEOUT = 240.0
REPORT_TITLE = "Python locks are behind PyPI"
REGENERATE_STEPS = (
    "python scripts/lock_requirements.py backend --upgrade",
    "python scripts/lock_requirements.py whisper --upgrade",
    "python scripts/lock_requirements.py dev --upgrade",
    "pytest -m image",
    "then open a PR (architecture.md 11.3)",
)
_LEADING_INT = re.compile(r"(\d+)(?![\d!])")


@dataclass(frozen=True)
class Change:
    """One lock entry an upgrade would move: `old`/`new` is None when added/removed."""

    lock: str
    name: str
    old: str | None
    new: str | None

    @property
    def major(self) -> bool:
        return self.old is not None and self.new is not None and is_major(self.old, self.new)

    def text(self) -> str:
        if self.old is None:
            return f"{self.lock} {self.name} added {self.new}"
        if self.new is None:
            return f"{self.lock} {self.name} removed (was {self.old})"
        label = " (major)" if self.major else ""
        return f"{self.lock} {self.name} {self.old} -> {self.new}{label}"


def leading_int(version: str) -> int | None:
    """The first integer of the release segment, or None (epoch, no digits)."""
    match = _LEADING_INT.match(version)
    return int(match.group(1)) if match else None


def is_major(old: str, new: str) -> bool:
    before, after = leading_int(old), leading_int(new)
    return before is not None and after is not None and before != after


def order_changes(changes: Iterable[Change]) -> list[Change]:
    """`major` entries first, then by name (then lock)."""
    return sorted(changes, key=lambda c: (not c.major, c.name, c.lock))


def inline_summary(changes: Sequence[Change], limit: int) -> str:
    """The first `limit` entries (already ordered) joined, and `and N more` for the rest."""
    shown = "; ".join(c.text() for c in changes[:limit])
    rest = len(changes) - limit
    return f"{shown}; and {rest} more" if rest > 0 else shown


def compare_locks(lock_name: str, old: Mapping[str, str], new: Mapping[str, str]) -> list[Change]:
    """The entries that differ between a committed lock and an upgraded resolution."""
    return [
        Change(lock_name, name, old.get(name), new.get(name))
        for name in sorted(set(old) | set(new))
        if old.get(name) != new.get(name)
    ]


def behind_annotation(changes: Sequence[Change], limit: int) -> str:
    """The single `::error` line; `changes` must be ordered."""
    majors = sum(1 for c in changes if c.major)
    steps = ", ".join(REGENERATE_STEPS)
    return (
        f"::error title={REPORT_TITLE}::{len(changes)} lock entries would change on --upgrade "
        f"({majors} major): {inline_summary(changes, limit)}. Run: {steps}"
    )


def reported_versions(report: Mapping[str, object]) -> dict[str, str]:
    """{canonical name: version} of a pip report (no hashes needed)."""
    items = report.get("install")
    if not isinstance(items, list):
        raise LockError("pip report has no 'install' list")
    return {canonical_name(i["metadata"]["name"]): i["metadata"]["version"] for i in items}


def make_run_pip(timeout: float) -> RunPip:
    def run(args: list[str]) -> tuple[int, str]:
        done = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
        return done.returncode, done.stderr

    return run


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "no error output"


class _Outage(Exception):
    """A resolve step failed in a way that is likely transient."""


def _read(root: Path, name: str) -> str:
    try:
        return (root / name).read_text()
    except (OSError, UnicodeDecodeError) as err:
        raise LockError(f"{name}: cannot read ({err})") from err


def report_outdated(
    *, root: Path = ROOT, run_pip: RunPip, inline_limit: int = INLINE_LIMIT
) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of the weekly report. Never writes a file."""
    try:
        inputs = {
            "backend": _read(root, TARGETS["backend"][0]),
            "whisper": _read(root, TARGETS["whisper"][0]),
        }
        try:
            inputs["dev"] = dev_requirements_text(_read(root, "pyproject.toml"))
        except (tomllib.TOMLDecodeError, TypeError) as err:
            raise LockError(f"pyproject.toml: cannot parse ({err})") from err
        for text in inputs.values():
            direct_pins(text)  # raises naming a requirement that is not an exact pin
        committed: dict[str, dict[str, str]] = {}
        for target in TARGETS:
            lock_name = TARGETS[target][1]
            try:
                parsed = parse_lock(_read(root, lock_name))
            except LockError as err:
                if str(err).startswith(lock_name):
                    raise
                raise LockError(f"{lock_name}: {err}") from err
            committed[target] = {n: v for n, (v, _) in parsed.items()}
    except LockError as err:
        return 2, "", f"lock_requirements: {err}\n"

    upgraded: dict[str, dict[str, str]] = {}
    constraints: list[str] = []
    changes: list[Change] = []
    try:
        for target in TARGETS:
            try:
                report, stderr = _resolve(target, root, inputs[target], constraints, run_pip)
            except (subprocess.TimeoutExpired, OSError) as err:
                raise _Outage(f"{target} resolve: pip did not finish ({type(err).__name__})") from err
            if report is None:
                raise _Outage(f"{target} resolve: pip failed: {_last_line(stderr)}")
            versions = reported_versions(report)
            if target != "backend":
                for name in upgraded["backend"]:
                    versions.pop(name, None)
            upgraded[target] = versions
            constraints += [f"{n}=={v}" for n, v in versions.items()]
            changes += compare_locks(TARGETS[target][1], committed[target], versions)
    except _Outage as err:
        return 0, "", f"lock_requirements: {err}; will retry on the next run\n"
    except (LockError, ValueError, KeyError, TypeError) as err:
        return 2, "", f"lock_requirements: unusable pip report: {err}\n"

    if not changes:
        return 0, "", ""
    ordered = order_changes(changes)
    lines = [behind_annotation(ordered, inline_limit), *(c.text() for c in ordered)]
    return 1, "\n".join(lines) + "\n", ""


def _installed() -> dict[str, str]:
    return {
        canonical_name(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    run_pip: RunPip | None = None,
    fetch: Fetch = default_fetch,
    platform: tuple[str, str, str, tuple[int, int]] | None = None,
    installed: Mapping[str, str] | None = None,
    inline_limit: int = INLINE_LIMIT,
    pip_timeout: float = PIP_TIMEOUT,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("target", nargs="?", choices=sorted(TARGETS))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--upgrade", action="store_true", help="re-resolve from scratch")
    group.add_argument("--upgrade-package", action="append", default=[], metavar="NAME",
                       help="drop this package's old pin (repeatable)")
    parser.add_argument("--check-installed", nargs="+", metavar="LOCK",
                        help="fail when an installed package differs from the locks or is in none")
    parser.add_argument("--report-outdated", action="store_true",
                        help="report-only: exit 1 when an --upgrade regeneration would change a lock")
    args = parser.parse_args(argv)
    if args.report_outdated and (args.target or args.upgrade or args.upgrade_package
                                 or args.check_installed):
        parser.error("--report-outdated takes no other argument")

    if args.report_outdated:
        system, machine, impl, version = platform or (
            sys.platform, _platform.machine(), sys.implementation.name, sys.version_info[:2]
        )
        refused = platform_problem(system, machine, impl, version)
        if refused:
            print(f"lock_requirements: {refused}", file=sys.stderr)
            return 2
        code, out, err = report_outdated(
            root=root, run_pip=run_pip or make_run_pip(pip_timeout), inline_limit=inline_limit
        )
        sys.stdout.write(out)
        sys.stderr.write(err)
        return code
    run_pip = run_pip or default_run_pip

    try:
        if args.check_installed:
            have = _installed() if installed is None else installed
            locked: Lock = {}
            problems: list[str] = []
            for lock_file in args.check_installed:
                one = parse_lock(Path(lock_file).read_text())
                problems += [f"lock drift in {lock_file}: {p}" for p in check_installed(have, one)]
                locked.update(one)
            problems += [f"lock drift: {p}" for p in unlocked_installed(have, locked)]
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1 if problems else 0
        if args.target is None:
            parser.error("give backend, whisper or dev, or --check-installed LOCK...")
        system, machine, impl, version = platform or (
            sys.platform, _platform.machine(), sys.implementation.name, sys.version_info[:2]
        )
        refused = platform_problem(system, machine, impl, version)
        if refused:
            raise LockError(refused)
        path = generate(
            args.target, root=root, upgrade=args.upgrade, upgrade_packages=args.upgrade_package,
            run_pip=run_pip, fetch=fetch,
        )
    except LockError as err:
        print(f"lock_requirements: {err}", file=sys.stderr)
        return 1
    print(f"wrote {path.name}")
    note = follow_up(args.target)
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
