"""Guards against the #14 QA defect regressing (issue #14).

Every test module in this package needs a live Postgres, and pytest only
honors a module-level ``pytestmark`` inside the test module itself, not in
a sibling ``conftest.py``. This test has no Postgres dependency, so it
runs under ``pytest -m "not integration"`` and would fail fast if a future
module here forgot the marker (or someone tried moving it back into
``conftest.py``).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PACKAGE = "tests.common.repo"


def _test_module_names() -> list[str]:
    package_dir = Path(__file__).parent
    return sorted(
        f"{PACKAGE}.{path.stem}"
        for path in package_dir.glob("test_*.py")
        if path.name != Path(__file__).name
    )


@pytest.mark.parametrize("module_name", _test_module_names())
def test_module_carries_integration_marker(module_name: str) -> None:
    module = importlib.import_module(module_name)

    pytestmark = getattr(module, "pytestmark", None)
    marks = pytestmark if isinstance(pytestmark, list) else [pytestmark]

    assert any(
        mark is not None and mark.name == "integration" for mark in marks
    ), (
        f"{module_name} does not set `pytestmark = pytest.mark.integration` "
        "at module level; a conftest.py pytestmark does not apply to "
        "sibling test modules, so it must live in the module itself."
    )
