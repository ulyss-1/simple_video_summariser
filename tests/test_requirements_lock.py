"""The committed Python locks match their inputs (issues #145, #264). Offline.

`scripts/lock_requirements.py` writes `requirements.*.lock` from
`requirements.*.txt` (and `requirements.dev.lock` from pyproject.toml's dev
extra and build requires). The lock header records a digest of its input, so this
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


def dev_digest_problems(pyproject_text: str, lock_text: str) -> list[str]:
    if lock.header_digest(lock_text) == lock.dev_input_digest(pyproject_text):
        return []
    message = (
        "pyproject.toml (dev extra or [build-system].requires) changed since "
        "requirements.dev.lock was generated (or the lock has no input-sha256). "
        "Regenerate it: python scripts/lock_requirements.py dev"
    )
    return [message]


def dev_pin_problems(pyproject_text: str, locked: Lock) -> list[str]:
    problems = []
    for name, version in sorted(lock.dev_direct_pins(pyproject_text).items()):
        entry = locked.get(name)
        if entry is None:
            problems.append(f"{name}=={version} is pinned in pyproject.toml but missing from requirements.dev.lock")
        elif entry[0] != version:
            problems.append(f"{name}: pyproject.toml pins {version}, requirements.dev.lock locks {entry[0]}")
    return problems


def dev_backend_overlap_problems(dev: Lock, backend: Lock) -> list[str]:
    return [
        f"{name} is in both requirements.dev.lock and requirements.backend.lock; "
        "regenerate dev: python scripts/lock_requirements.py dev"
        for name in sorted(set(dev) & set(backend))
    ]


def dev_whisper_problems(dev: Lock, whisper: Lock) -> list[str]:
    problems = []
    for name in sorted(set(dev) & set(whisper)):
        if dev[name][0] != whisper[name][0]:
            problems.append(
                f"{name}: requirements.dev.lock has {dev[name][0]}, requirements.whisper.lock "
                f"has {whisper[name][0]}; regenerate dev: python scripts/lock_requirements.py dev"
            )
        elif dev[name][1] != whisper[name][1]:
            problems.append(
                f"{name}=={dev[name][0]}: different hash sets in requirements.dev.lock and "
                "requirements.whisper.lock; regenerate dev: python scripts/lock_requirements.py dev"
            )
    return problems


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


PYPROJECT = (
    '[build-system]\nrequires = ["setuptools==80.0.0"]\n'
    '[project.optional-dependencies]\ndev = ["pytest==9.0"]\n'
)


def test_dev_digest_check_fails_when_a_dev_or_build_pin_changed_naming_the_command() -> None:
    text = f"# input-sha256: {lock.dev_input_digest(PYPROJECT)}\n"
    assert dev_digest_problems(PYPROJECT, text) == []
    assert dev_digest_problems(PYPROJECT + "\n[tool.ruff]\nline-length = 9\n", text) == []
    for changed in (PYPROJECT.replace("9.0", "9.1"), PYPROJECT.replace("80.0.0", "81.0.0")):
        problems = dev_digest_problems(changed, text)
        assert len(problems) == 1
        assert "requirements.dev.lock" in problems[0]
        assert "python scripts/lock_requirements.py dev" in problems[0]
    assert dev_digest_problems(PYPROJECT, "a==1\n")


def test_dev_pin_check_fails_on_a_missing_or_different_direct_or_build_pin() -> None:
    locked = {"pytest": ("9.0", (H,)), "setuptools": ("80.0.0", (H,))}
    assert dev_pin_problems(PYPROJECT, locked) == []
    missing = dev_pin_problems(PYPROJECT, {"pytest": ("9.0", (H,))})
    assert len(missing) == 1 and "setuptools" in missing[0]
    different = dev_pin_problems(PYPROJECT, {**locked, "pytest": ("8.0", (H,))})
    assert len(different) == 1
    assert "pytest" in different[0] and "9.0" in different[0] and "8.0" in different[0]


def test_dev_overlap_check_fails_when_a_name_is_also_in_the_backend_lock() -> None:
    problems = dev_backend_overlap_problems({"a": ("1", (H,)), "b": ("1", (H,))}, {"b": ("1", (H,))})
    assert len(problems) == 1 and "b" in problems[0]
    assert "python scripts/lock_requirements.py dev" in problems[0]
    assert dev_backend_overlap_problems({"a": ("1", (H,))}, {"b": ("1", (H,))}) == []


def test_dev_whisper_check_fails_on_a_different_version_or_hash_set() -> None:
    other = "b" * 64
    assert dev_whisper_problems({"a": ("1", (H,))}, {"a": ("1", (H,)), "z": ("1", (H,))}) == []
    version = dev_whisper_problems({"a": ("1", (H,))}, {"a": ("2", (H,))})
    assert len(version) == 1 and "a" in version[0] and "1" in version[0] and "2" in version[0]
    assert "python scripts/lock_requirements.py dev" in version[0]
    hashes = dev_whisper_problems({"a": ("1", (H,))}, {"a": ("1", (H, other))})
    assert len(hashes) == 1 and "a" in hashes[0] and "hash" in hashes[0]
    assert "python scripts/lock_requirements.py dev" in hashes[0]


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


# ---- the dev lock, real files (#264) ----


def _dev() -> Lock:
    parsed: Lock = lock.parse_lock(_read("requirements.dev.lock"))
    return parsed


def test_the_dev_lock_was_generated_from_the_current_pyproject() -> None:
    assert (ROOT / "requirements.dev.lock").is_file()
    assert dev_digest_problems(_read("pyproject.toml"), _read("requirements.dev.lock")) == []
    assert lock.header_digest(_read("requirements.dev.lock")) is not None


def test_every_direct_dev_and_build_pin_is_locked_at_the_same_version() -> None:
    assert lock.dev_direct_pins(_read("pyproject.toml"))
    assert dev_pin_problems(_read("pyproject.toml"), _dev()) == []


def test_the_dev_lock_holds_the_dev_tools_and_the_build_backend() -> None:
    dev = _dev()
    assert len(dev) > 0
    for name in ("pytest", "setuptools", "hypothesis", "ruff", "mypy", "testcontainers", "httpx"):
        assert name in dev
    assert "ytdigest" not in dev


def test_the_dev_lock_shares_nothing_with_the_backend_lock() -> None:
    backend, _ = _locks()
    assert dev_backend_overlap_problems(_dev(), backend) == []


def test_what_the_dev_and_whisper_locks_share_has_one_version_and_hash_set() -> None:
    _, whisper = _locks()
    assert dev_whisper_problems(_dev(), whisper) == []


def test_every_dev_lock_line_is_an_exact_pin_with_well_formed_hashes() -> None:
    text = _read("requirements.dev.lock")
    assert lock.lock_problems(text) == []
    assert any(line and not line.startswith("#") for line in text.splitlines())
    assert "ytdigest==" not in text
