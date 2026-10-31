"""Literal copies of the stack's base-image pins agree with their source (issue #267).

The test harness and the deploy-host preflight each keep a literal
`tag@sha256:` copy of an image the stack pins (architecture.md 11.3). This
guard reads the stack pins with the parser of `scripts/base_image_digest_check.py`
and compares them with the copies, read as text. No Docker, no network.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = REPO_ROOT / "scripts" / "base_image_digest_check.py"
_PINNED = re.compile(r".+:[A-Za-z0-9_][A-Za-z0-9_.-]*@sha256:[0-9a-f]{64}")


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("base_image_digest_check", _CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Copy:
    path: str
    pattern: str  # matches the assignment line; group 1 is the value
    source: str  # stack file that holds the pin
    which: str  # "compose-db" or "runtime-from"


COPIES = [
    Copy("tests/conftest.py", r'^_POSTGRES_IMAGE = "([^"]*)"\s*$', "compose.yml", "compose-db"),
    Copy("tests/test_frontend_image_docker.py", r'^STUB_IMAGE = "([^"]*)"\s*$', "ops/Dockerfile.backend", "runtime-from"),
    Copy("tests/test_frontend_image_docker.py", r'^BASE = "([^"]*)"\s*$', "ops/Dockerfile.frontend", "runtime-from"),
    Copy("ops/host/preflight.sh", r'^probe_image="([^"]*)"\s*$', "ops/Dockerfile.backend", "runtime-from"),
]


def _expected(copy: Copy) -> str:
    module = _checker()
    text = (REPO_ROOT / copy.source).read_text()
    if copy.which == "compose-db":
        refs = module.pinned_compose_references(text)
        db = [r for r in refs if r.written == "postgres"]
        assert len(db) == 1, f"{copy.source}: want exactly one pinned postgres image, got {len(db)}"
        return str(db[0].pinned())
    return str(module.pinned_references(text)[-1].pinned())  # the last digest-pinned FROM is the `deps` stage; `runtime` is `FROM deps`


def check_copy(copy: Copy, text: str) -> str | None:
    """None when the copy in `text` agrees with its source, else the failure message."""
    match = re.search(copy.pattern, text, re.MULTILINE)
    if match is None:
        return f"{copy.path}: no assignment line matching {copy.pattern!r}"
    value, expected = match.group(1), _expected(copy)
    if not _PINNED.fullmatch(value):
        return f"{copy.path}: {value!r} is not tag@sha256:<64 hex>; expected {expected!r}"
    if value != expected:
        return f"{copy.path}: copy is {value!r}, expected {expected!r} (from {copy.source})"
    return None


@pytest.mark.parametrize("copy", COPIES, ids=lambda c: f"{c.path}:{c.pattern[:12]}")
def test_copy_equals_the_stack_pin(copy: Copy) -> None:
    assert check_copy(copy, (REPO_ROOT / copy.path).read_text()) is None


def test_mismatch_message_names_file_copy_and_expected() -> None:
    copy = COPIES[3]
    old = "python:3.14-slim@sha256:" + "0" * 64
    message = check_copy(copy, f'probe_image="{old}"\n')
    assert message is not None
    assert "ops/host/preflight.sh" in message
    assert old in message
    assert _expected(copy) in message


def test_missing_assignment_fails_naming_the_file() -> None:
    message = check_copy(COPIES[0], "# no constant here\n")
    assert message is not None
    assert "tests/conftest.py" in message


def test_bare_tag_fails() -> None:
    message = check_copy(COPIES[2], 'BASE = "nginx/nginx-unprivileged:1.30-alpine"\n')
    assert message is not None
    assert "sha256" in message
