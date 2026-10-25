"""The committed Python locks match their inputs (issue #145). Offline.

`scripts/lock_requirements.py` writes `requirements.*.lock` from
`requirements.*.txt`. The lock header records a digest of its input, so this
test fails when an input changes without a regeneration, and never because
PyPI published a new release (that is the point of not re-resolving in CI).
Each checker is pure and shown failing on crafted input before it is run on
the real files.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lock_requirements", ROOT / "scripts" / "lock_requirements.py")
assert _spec is not None and _spec.loader is not None
lock = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lock
_spec.loader.exec_module(lock)

PAIRS = (
    ("backend", "requirements.backend.txt", "requirements.backend.lock"),
    ("whisper", "requirements.whisper.txt", "requirements.whisper.lock"),
)
H = "a" * 64
Lock = Mapping[str, tuple[str, tuple[str, ...]]]


def digest_problems(req_name: str, req_text: str, lock_name: str, lock_text: str, target: str) -> list[str]:
    if lock.header_digest(lock_text) == lock.input_digest(req_text):
        return []
    message = (
        f"{req_name} changed since {lock_name} was generated (or the lock has no "
        f"input-sha256). Regenerate it: python scripts/lock_requirements.py {target}"
    )
    return [message]


def pin_problems(req_text: str, locked: Lock, lock_name: str) -> list[str]:
    problems = []
    for name, version in sorted(lock.direct_pins(req_text).items()):
        entry = locked.get(name)
        if entry is None:
            problems.append(f"{name}=={version} is pinned directly but missing from {lock_name}")
        elif entry[0] != version:
            problems.append(f"{name}: requirements pin {version}, {lock_name} locks {entry[0]}")
    return problems


def overlap_problems(backend: Lock, whisper: Lock) -> list[str]:
    return [f"{name} is in both locks" for name in sorted(set(backend) & set(whisper))]


def _lock(req_text: str, body: str = "") -> str:
    return f"# GENERATED\n# input-sha256: {lock.input_digest(req_text)}\n\n{body}"


# ---- crafted bad input: each check fails ----


def test_digest_check_fails_when_a_pin_is_bumped_added_or_removed() -> None:
    old = "a==1\nb==2\n"
    text = _lock(old, f"a==1 --hash=sha256:{H}\n")
    assert digest_problems("r.txt", old, "r.lock", text, "backend") == []
    for changed in ("a==1\nb==3\n", "a==1\nb==2\nc==1\n", "a==1\n"):
        problems = digest_problems("r.txt", changed, "r.lock", text, "backend")
        assert len(problems) == 1
        assert "r.txt" in problems[0] and "r.lock" in problems[0]
        assert "python scripts/lock_requirements.py backend" in problems[0]


def test_digest_check_ignores_a_comment_only_edit() -> None:
    text = _lock("a==1\n")
    assert digest_problems("r.txt", "# new comment\na==1  # why\n", "r.lock", text, "backend") == []


def test_digest_check_fails_on_a_lock_without_a_digest() -> None:
    assert digest_problems("r.txt", "a==1\n", "r.lock", f"a==1 --hash=sha256:{H}\n", "backend")


def test_pin_check_fails_on_a_missing_or_different_version_naming_both() -> None:
    locked = {"a": ("1", (H,))}
    assert pin_problems("a==1\n", locked, "r.lock") == []
    missing = pin_problems("a==1\nb==2\n", locked, "r.lock")
    assert len(missing) == 1 and "b" in missing[0]
    different = pin_problems("a==2\n", locked, "r.lock")
    assert len(different) == 1
    assert "a" in different[0] and "1" in different[0] and "2" in different[0]


def test_pin_check_treats_extras_and_case_as_the_same_package() -> None:
    assert pin_problems("PSYCOPG[binary]==3\n", {"psycopg": ("3", (H,))}, "r.lock") == []


def test_overlap_check_fails_when_a_name_is_in_both_locks() -> None:
    both = {"x": ("1", (H,)), "y": ("1", (H,))}
    assert overlap_problems(both, {"y": ("1", (H,)), "z": ("1", (H,))}) == ["y is in both locks"]
    assert overlap_problems(both, {"z": ("1", (H,))}) == []


@pytest.mark.parametrize(
    "line",
    ["a", "a>=1", "a==1", "a==1 --hash=sha256:xyz", f"a==1 --hash=sha1:{'a' * 40}", f"a==1 --hash=sha256:{'A' * 64}"],
)
def test_line_check_fails_when_a_line_is_not_an_exact_pin_with_a_sha256(line: str) -> None:
    assert lock.lock_problems(f"{line}\n")


# ---- the real files ----


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _locks() -> tuple[Lock, Lock]:
    return lock.parse_lock(_read("requirements.backend.lock")), lock.parse_lock(_read("requirements.whisper.lock"))


@pytest.mark.parametrize(("target", "req", "lock_file"), PAIRS)
def test_each_lock_was_generated_from_the_current_input(target: str, req: str, lock_file: str) -> None:
    assert (ROOT / lock_file).is_file()
    assert digest_problems(req, _read(req), lock_file, _read(lock_file), target) == []
    assert lock.header_digest(_read(lock_file)) is not None  # not vacuous


@pytest.mark.parametrize(("target", "req", "lock_file"), PAIRS)
def test_every_direct_pin_is_locked_at_the_same_version(target: str, req: str, lock_file: str) -> None:
    locked = lock.parse_lock(_read(lock_file))
    assert len(locked) > 0 and lock.direct_pins(_read(req))
    assert pin_problems(_read(req), locked, lock_file) == []


def test_no_package_is_in_both_locks() -> None:
    backend, whisper = _locks()
    assert backend and whisper
    assert overlap_problems(backend, whisper) == []


@pytest.mark.parametrize(("target", "req", "lock_file"), PAIRS)
def test_every_lock_line_is_an_exact_pin_with_well_formed_hashes(target: str, req: str, lock_file: str) -> None:
    text = _read(lock_file)
    assert lock.lock_problems(text) == []
    assert any(line and not line.startswith("#") for line in text.splitlines())


def test_the_backend_lock_holds_the_transitive_packages() -> None:
    backend, _ = _locks()
    for name in ("pydantic", "pydantic-core", "sqlalchemy", "starlette", "psycopg", "psycopg-binary"):
        assert name in backend


def test_the_whisper_lock_holds_the_whisper_tree() -> None:
    _, whisper = _locks()
    for name in ("faster-whisper", "av", "ctranslate2", "onnxruntime", "numpy", "tokenizers", "huggingface-hub"):
        assert name in whisper
