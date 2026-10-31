"""scripts/ci_changes.sh decides which CI jobs a change needs."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_changes.sh"


def jobs(*files: str) -> dict[str, bool]:
    out = subprocess.run(
        ["bash", str(SCRIPT)],
        input="\n".join(files) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k: v == "true" for k, v in (line.split("=") for line in out.split())}


@pytest.mark.parametrize(
    ("files", "python", "frontend", "images", "bakeoff"),
    [
        (("web/src/routes/Library.tsx",), False, True, False, False),
        (("web/src/api/schema.d.ts",), True, True, True, False),
        (("web/openapi.json",), True, True, True, False),
        (("common/queue.py", "tests/test_queue.py"), True, False, True, False),
        (("tests/test_queue.py",), True, False, False, False),
        (("tests/test_frontend_image.py",), True, False, True, False),
        (("ops/Dockerfile.backend",), True, False, True, False),
        (("_docs/team/pm.md", "docs/superpowers/plans/x.md"), False, False, False, False),
        (("README.md",), True, False, True, False),
        ((".github/workflows/ci.yml",), True, True, True, True),
        (("some/unknown/path.txt",), True, False, True, False),
        # The locks feed both the python job and the image builds (#145).
        (("requirements.backend.lock",), True, False, True, False),
        (("requirements.whisper.lock",), True, False, True, False),
        (("scripts/lock_requirements.py",), True, False, True, False),
        (
            ("tests/test_requirements_lock.py", "tests/scripts/test_lock_requirements.py"),
            True, False, False, False,
        ),
        # The bake-off set (#316) runs only its own job, never `images`.
        (("tests/test_whisper_cache_docker.py",), True, False, True, False),
        (("compose.yml",), True, False, True, False),
        (("ops/Dockerfile.whisper",), True, False, True, False),
        (("ops/Dockerfile.bakeoff",), True, False, False, True),
        (("ops/Dockerfile.bakeoff.dockerignore",), True, False, False, True),
        (("compose.bakeoff.yml",), True, False, False, True),
        (("scripts/phase0/bakeoff.py",), True, False, False, True),
        (("scripts/phase0/subtitle_coverage.py",), True, False, False, True),
        (("tests/test_bakeoff_image_docker.py",), True, False, False, True),
        (("tests/test_bakeoff_image.py",), True, False, False, True),
        (("ops/Dockerfile.bakeoff", "common/queue.py"), True, False, True, True),
        (("requirements.backend.txt", "ops/Dockerfile.bakeoff"), True, False, True, True),
        (("requirements.backend.txt",), True, False, True, False),
        (("tests/test_backend_image.py",), True, False, True, False),
        # Looks like a bake-off path but is not.
        (("scripts/phase0x/x.py",), True, False, True, False),
        (("pyproject.toml",), True, False, True, False),
        (("scripts/ci_changes.sh",), True, True, True, True),
        # #368: the publish workflow affects no image build, but a change to the
        # tag script changes what `images` computes (and it has python tests).
        ((".github/workflows/publish-deps-images.yml",), True, False, False, False),
        (("scripts/deps_image_tag.py",), True, False, True, False),
        (("tests/scripts/test_deps_image_tag.py",), True, False, True, False),  # "image" in the name
        (("tests/image_build.py",), True, False, True, False),
        ((), True, True, True, True),
    ],
)
def test_jobs_follow_the_changed_paths(
    files: tuple[str, ...], python: bool, frontend: bool, images: bool, bakeoff: bool
) -> None:
    assert jobs(*files) == {
        "python": python,
        "frontend": frontend,
        "images": images,
        "bakeoff": bakeoff,
    }


def test_empty_input_prints_the_four_keys_in_order() -> None:
    out = subprocess.run(
        ["bash", str(SCRIPT)], input="", capture_output=True, text=True, check=True
    ).stdout
    assert out.split() == [
        "python=true",
        "frontend=true",
        "images=true",
        "bakeoff=true",
    ]


TESTS = ROOT / "tests"


def _applies(tree: ast.Module, mark: str) -> bool:
    """True when the module's text applies `pytest.mark.<mark>` anywhere."""
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == mark
        and isinstance(n.value, ast.Attribute)
        and n.value.attr == "mark"
        for n in ast.walk(tree)
    )


def _marked_files(mark: str) -> list[Path]:
    # Only files that set the module-level `pytestmark` apply a marker to tests.
    out = []
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            ) and _applies(ast.Module(body=[node], type_ignores=[]), mark):
                out.append(path)
    return out


def _alone(path: Path) -> dict[str, bool]:
    return jobs(path.relative_to(ROOT).as_posix())


def test_every_bakeoff_marked_file_is_in_the_bakeoff_path_set() -> None:
    marked = _marked_files("bakeoff")
    assert marked == [TESTS / "test_bakeoff_image_docker.py"]
    for path in marked:
        assert _alone(path)["bakeoff"], path


def test_every_image_file_in_the_bakeoff_path_set_applies_the_bakeoff_marker() -> None:
    bakeoff = set(_marked_files("bakeoff"))
    for path in _marked_files("image"):
        if _alone(path)["bakeoff"]:
            assert path in bakeoff, f"{path} is a bake-off path but lacks the marker"


def test_tests_imported_by_a_bakeoff_file_are_in_the_bakeoff_path_set() -> None:
    for path in _marked_files("bakeoff"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                if node.module == "tests":
                    names += [f"tests.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if name == "tests" or not name.startswith("tests."):
                    continue
                helper = ROOT / (name.replace(".", "/") + ".py")
                if not helper.exists():
                    helper = ROOT / name.replace(".", "/") / "__init__.py"
                assert _alone(helper)["bakeoff"], f"{path} imports {name}, which is not in the bake-off set"
