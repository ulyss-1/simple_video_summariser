"""scripts/lock_requirements.py (issue #145). No network, no pip: both are injected."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Self

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "lock_requirements.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lock_requirements", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lock = _load()

A, B, C, D, E = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)


def _item(name: str, version: str, sha: str) -> dict[str, object]:
    return {
        "metadata": {"name": name, "version": version},
        "download_info": {
            "url": f"https://files.example/{name}",
            "archive_info": {"hashes": {"sha256": sha}},
        },
    }


def _report(*items: dict[str, object]) -> dict[str, object]:
    return {"version": "1", "install": list(items)}


# ---- names and input digest ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PyYAML", "pyyaml"),
        ("faster_whisper", "faster-whisper"),
        ("Faster.Whisper", "faster-whisper"),
        ("a__b--c..d", "a-b-c-d"),
        ("yt-dlp", "yt-dlp"),
    ],
)
def test_canonical_name_follows_pep_503(raw: str, expected: str) -> None:
    assert lock.canonical_name(raw) == expected


def test_normalize_drops_comments_and_blanks_canonicalizes_and_sorts() -> None:
    text = "# header\n\nPyYAML == 6.0  # why\n  faster_whisper==1.2.1\n\t\npsycopg[Binary]==3.3.6\n"
    assert lock.normalize_input(text) == [
        "faster-whisper==1.2.1",
        "psycopg[binary]==3.3.6",
        "pyyaml==6.0",
    ]


def test_digest_ignores_comments_blanks_whitespace_and_order() -> None:
    one = "# a\nx==1\ny==2\n"
    two = "y == 2\n\n# other comment\nx==1  # trailing\n"
    assert lock.input_digest(one) == lock.input_digest(two)
    assert len(lock.input_digest(one)) == 64


@pytest.mark.parametrize(
    "other",
    ["x==1\ny==3\n", "x==1\n", "x==1\ny==2\nz==1\n", "x==1\ny[e]==2\n"],
)
def test_digest_changes_when_a_pin_changes_is_added_or_removed(other: str) -> None:
    assert lock.input_digest("x==1\ny==2\n") != lock.input_digest(other)


def test_direct_pins_map_canonical_name_to_version_without_extras() -> None:
    text = "psycopg[binary]==3.3.6\nPyYAML==6.0\n# c\n"
    assert lock.direct_pins(text) == {"psycopg": "3.3.6", "pyyaml": "6.0"}


# ---- report -> lock text ----


def _render(report: dict[str, object], digests: Mapping[tuple[str, str], set[str]], **kw: object) -> str:
    text: str = lock.lock_text(
        report,
        digests,
        input_name="requirements.backend.txt",
        command="python scripts/lock_requirements.py backend",
        digest=A,
        **kw,
    )
    return text


def test_lock_text_has_header_and_sorted_pins_with_every_published_hash() -> None:
    report = _report(_item("Zeta_Pkg", "2.0", C), _item("alpha", "1.0", B))
    digests = {("zeta-pkg", "2.0"): {E, C}, ("alpha", "1.0"): {B, D, A}}
    text = _render(report, digests)
    lines = text.splitlines()
    assert lines[0].startswith("# ") and "do not edit" in lines[0].lower()
    assert "# Input: requirements.backend.txt" in lines
    assert "# Regenerate: python scripts/lock_requirements.py backend" in lines
    assert f"# input-sha256: {A}" in lines
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    assert body == [
        f"alpha==1.0 --hash=sha256:{A} --hash=sha256:{B} --hash=sha256:{D}",
        f"zeta-pkg==2.0 --hash=sha256:{C} --hash=sha256:{E}",
    ]
    assert text.endswith("\n")


def test_header_has_no_timestamp_and_no_pip_or_python_version() -> None:
    text = _render(_report(_item("a", "1", A)), {("a", "1"): {A}})
    header = "\n".join(ln for ln in text.splitlines() if ln.startswith("#")).lower()
    assert "pip" not in header.replace("lock_requirements", "")
    assert "python " not in header.replace("python scripts/", "")
    assert "20" not in header  # no year
    assert "3.14" not in header


def test_a_package_with_an_sdist_and_wheels_lists_all_of_them() -> None:
    digests = {("a", "1"): {A, B, C, D}}  # sdist + three wheels
    text = _render(_report(_item("a", "1", C)), digests)
    assert text.count("--hash=sha256:") == 4


def test_extras_are_not_in_the_lock_and_extra_packages_get_their_own_line() -> None:
    report = _report(_item("psycopg", "3.3.6", A), _item("psycopg-binary", "3.3.6", B))
    digests = {("psycopg", "3.3.6"): {A}, ("psycopg-binary", "3.3.6"): {B}}
    body = [ln for ln in _render(report, digests).splitlines() if ln and ln[0] != "#"]
    assert [ln.split(" ")[0] for ln in body] == ["psycopg==3.3.6", "psycopg-binary==3.3.6"]
    assert "[" not in "".join(body)


def test_output_is_identical_for_the_same_input_in_a_different_order() -> None:
    items = [_item("a", "1", A), _item("b", "2", B), _item("c", "3", C)]
    digests = {("a", "1"): {A, D}, ("b", "2"): {B}, ("c", "3"): {C, E}}
    forward = _render(_report(*items), digests)
    backward = _render(_report(*reversed(items)), digests)
    assert forward == backward


def test_a_reported_hash_missing_from_pypi_is_rejected_naming_the_package() -> None:
    report = _report(_item("a", "1", A), _item("Weird_Pkg", "9", B))
    digests = {("a", "1"): {A}, ("weird-pkg", "9"): {C, D}}
    with pytest.raises(lock.LockError) as err:
        _render(report, digests)
    assert "weird-pkg" in str(err.value)
    assert "9" in str(err.value)


def test_a_package_pypi_knows_nothing_about_is_rejected() -> None:
    with pytest.raises(lock.LockError, match="a"):
        _render(_report(_item("a", "1", A)), {})


def test_exclude_leaves_out_packages_of_the_backend_lock() -> None:
    report = _report(_item("shared", "1", A), _item("only-here", "2", B))
    digests = {("shared", "1"): {A}, ("only-here", "2"): {B}}
    body = [ln for ln in _render(report, digests, exclude={"shared"}).splitlines() if ln and ln[0] != "#"]
    assert [ln.split(" ")[0] for ln in body] == ["only-here==2"]


def test_a_report_hash_in_the_hash_form_is_understood() -> None:
    item = _item("a", "1", A)
    item["download_info"] = {"url": "u", "archive_info": {"hash": f"sha256={A}"}}
    assert lock.reported_packages(_report(item)) == {("a", "1"): A}


def test_a_report_item_without_a_sha256_is_rejected() -> None:
    item = _item("a", "1", A)
    item["download_info"] = {"url": "u"}
    with pytest.raises(lock.LockError, match="a"):
        lock.reported_packages(_report(item))


# ---- lock text -> parsed ----


def test_parse_lock_round_trips_the_generated_text() -> None:
    report = _report(_item("a", "1", A), _item("b", "2", B))
    digests = {("a", "1"): {A, D}, ("b", "2"): {B}}
    parsed = lock.parse_lock(_render(report, digests))
    assert parsed == {"a": ("1", (A, D)), "b": ("2", (B,))}


def test_header_digest_is_read_back() -> None:
    assert lock.header_digest(_render(_report(_item("a", "1", A)), {("a", "1"): {A}})) == A
    assert lock.header_digest("a==1 --hash=sha256:" + A + "\n") is None


@pytest.mark.parametrize(
    "line",
    [
        "a==1",  # no hash
        "a>=1 --hash=sha256:" + "a" * 64,
        "a==1 --hash=sha256:" + "a" * 63,
        "a==1 --hash=sha256:" + "A" * 64,
        "a==1 --hash=md5:" + "a" * 32,
        "a[extra]==1 --hash=sha256:" + "a" * 64,
        "A==1 --hash=sha256:" + "a" * 64,
        "a_b==1 --hash=sha256:" + "a" * 64,
        "a==1 ; python_version < '3.14' --hash=sha256:" + "a" * 64,
        "a==1 --hash=sha256:" + "a" * 64 + " \\",
        "-r other.txt",
    ],
)
def test_lock_problems_flags_a_line_that_is_not_an_exact_hashed_pin(line: str) -> None:
    problems = lock.lock_problems(f"# header\n\nok==1 --hash=sha256:{A}\n{line}\n")
    assert len(problems) == 1
    assert problems[0].startswith("line 4: ")
    with pytest.raises(lock.LockError):
        lock.parse_lock(f"{line}\n")


def test_lock_problems_flags_unsorted_names_and_duplicates() -> None:
    out_of_order = f"b==1 --hash=sha256:{A}\na==1 --hash=sha256:{A}\n"
    assert lock.lock_problems(out_of_order)
    duplicate = f"a==1 --hash=sha256:{A}\na==2 --hash=sha256:{B}\n"
    assert lock.lock_problems(duplicate)


def test_lock_problems_flags_unsorted_hashes() -> None:
    assert lock.lock_problems(f"a==1 --hash=sha256:{B} --hash=sha256:{A}\n")


def test_lock_problems_is_empty_for_a_clean_lock() -> None:
    assert lock.lock_problems(f"# c\n\na==1 --hash=sha256:{A} --hash=sha256:{B}\n") == []


# ---- installed versus lock ----


def test_check_installed_names_the_package_and_both_versions() -> None:
    locked = {"typing-extensions": ("4.15.0", (A,)), "packaging": ("26.0", (B,))}
    problems = lock.check_installed(
        {"typing-extensions": "4.16.0", "packaging": "26.0", "pytest": "9"}, locked
    )
    assert len(problems) == 1
    assert "typing-extensions" in problems[0]
    assert "4.15.0" in problems[0] and "4.16.0" in problems[0]


def test_check_installed_flags_a_locked_package_that_is_not_installed() -> None:
    problems = lock.check_installed({}, {"a": ("1", (A,))})
    assert len(problems) == 1 and "a" in problems[0]


def test_check_installed_accepts_a_match_and_ignores_extra_packages() -> None:
    assert lock.check_installed({"a": "1", "pytest": "9"}, {"a": ("1", (A,))}) == []


# ---- platform ----


def test_the_target_platform_is_accepted() -> None:
    assert lock.platform_problem("linux", "x86_64", "cpython", (3, 14)) is None


@pytest.mark.parametrize(
    ("system", "machine", "impl", "version"),
    [
        ("win32", "x86_64", "cpython", (3, 14)),
        ("darwin", "arm64", "cpython", (3, 14)),
        ("linux", "aarch64", "cpython", (3, 14)),
        ("linux", "x86_64", "pypy", (3, 14)),
        ("linux", "x86_64", "cpython", (3, 13)),
        ("linux", "x86_64", "cpython", (3, 15)),
    ],
)
def test_any_other_platform_is_refused_naming_the_requirement(
    system: str, machine: str, impl: str, version: tuple[int, int]
) -> None:
    message = lock.platform_problem(system, machine, impl, version)
    assert message is not None
    assert "Linux x86_64" in message and "3.14" in message


# ---- generate(), with pip and PyPI faked ----

BACKEND_TXT = "# c\nfoo==1.0\nbar[x]==2.0\n"
WHISPER_TXT = "# c\nw==3.0\n"


class FakePip:
    """Stands in for `python -m pip install --dry-run --report`."""

    def __init__(self, items: list[dict[str, object]], rc: int = 0) -> None:
        self.items = items
        self.rc = rc
        self.calls: list[list[str]] = []
        self.constraints: list[str] = []
        self.requirements: list[str] = []  # text of every -r file, read while it exists

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        self.requirements += [
            Path(args[i + 1]).read_text() for i, a in enumerate(args) if a == "-r"
        ]
        if "-c" in args:
            self.constraints.append(Path(args[args.index("-c") + 1]).read_text())
        if self.rc:
            return self.rc, "ERROR: ResolutionImpossible"
        Path(args[args.index("--report") + 1]).write_text(json.dumps(_report(*self.items)))
        return 0, ""


class FakePyPI:
    """Stands in for the PyPI JSON API; records the URLs asked for."""

    def __init__(self, digests: Mapping[tuple[str, str], list[str]]) -> None:
        self.digests = digests
        self.requested: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        parts = url.split("/")
        found = self.digests[(parts[-3], parts[-2])]
        return json.dumps({"urls": [{"digests": {"sha256": h}} for h in found]}).encode()


def _fetch_for(digests: Mapping[tuple[str, str], list[str]]) -> FakePyPI:
    return FakePyPI(digests)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.backend.txt").write_text(BACKEND_TXT)
    (tmp_path / "requirements.whisper.txt").write_text(WHISPER_TXT)
    return tmp_path


def _backend_items() -> list[dict[str, object]]:
    return [_item("foo", "1.0", A), _item("bar", "2.0", B), _item("dep", "5", C)]


BACKEND_DIGESTS = {("foo", "1.0"): [A, D], ("bar", "2.0"): [B], ("dep", "5"): [C]}


def test_generate_backend_writes_the_lock_and_asks_pypi_json_api(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    pip, fetch = FakePip(_backend_items()), _fetch_for(BACKEND_DIGESTS)
    lock.generate("backend", root=root, run_pip=pip, fetch=fetch)
    parsed = lock.parse_lock((root / "requirements.backend.lock").read_text())
    assert parsed == {"bar": ("2.0", (B,)), "dep": ("5", (C,)), "foo": ("1.0", (A, D))}
    assert lock.header_digest((root / "requirements.backend.lock").read_text()) == lock.input_digest(BACKEND_TXT)
    assert "https://pypi.org/pypi/foo/1.0/json" in fetch.requested
    args = pip.calls[0]
    assert args[1:3] == ["-m", "pip"]
    for flag in ("--dry-run", "--ignore-installed", "--quiet", "--report"):
        assert flag in args
    assert "requirements.backend.txt" in " ".join(args)


def test_regenerating_with_no_change_produces_the_same_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    lock.generate("backend", root=root, run_pip=FakePip(_backend_items()), fetch=_fetch_for(BACKEND_DIGESTS))
    first = (root / "requirements.backend.lock").read_bytes()
    lock.generate("backend", root=root, run_pip=FakePip(_backend_items()), fetch=_fetch_for(BACKEND_DIGESTS))
    assert (root / "requirements.backend.lock").read_bytes() == first


def _backend_locked(root: Path) -> None:
    lock.generate("backend", root=root, run_pip=FakePip(_backend_items()), fetch=_fetch_for(BACKEND_DIGESTS))


def test_existing_entries_are_constraints_but_a_bumped_direct_pin_wins(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    (root / "requirements.backend.txt").write_text("foo==1.1\nbar[x]==2.0\n")
    pip = FakePip([_item("foo", "1.1", A), _item("bar", "2.0", B), _item("dep", "5", C)])
    lock.generate("backend", root=root, run_pip=pip, fetch=_fetch_for({**BACKEND_DIGESTS, ("foo", "1.1"): [A]}))
    constraints = pip.constraints[0].splitlines()
    assert "dep==5" in constraints
    assert not any(c.startswith("foo==") for c in constraints)  # direct pin edited


def test_upgrade_drops_all_constraints(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    pip = FakePip(_backend_items())
    lock.generate("backend", root=root, upgrade=True, run_pip=pip, fetch=_fetch_for(BACKEND_DIGESTS))
    assert pip.constraints == [] or pip.constraints[0].strip() == ""


def test_upgrade_package_drops_only_that_pin(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    (root / "requirements.backend.lock").write_text(
        (root / "requirements.backend.lock").read_text()
        + f"zzz==1 --hash=sha256:{E}\n"
    )
    pip = FakePip(_backend_items())
    lock.generate(
        "backend", root=root, upgrade_packages=["DEP", "zzz"], run_pip=pip, fetch=_fetch_for(BACKEND_DIGESTS)
    )
    constraints = pip.constraints[0].splitlines() if pip.constraints else []
    assert not any(c.startswith(("dep==", "zzz==")) for c in constraints)


def test_a_pip_failure_keeps_the_lock_byte_for_byte_and_suggests_upgrading(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    before = (root / "requirements.backend.lock").read_bytes()
    with pytest.raises(lock.LockError) as err:
        lock.generate("backend", root=root, run_pip=FakePip([], rc=1), fetch=_fetch_for({}))
    assert "ResolutionImpossible" in str(err.value)
    assert "--upgrade-package" in str(err.value) and "--upgrade" in str(err.value)
    assert (root / "requirements.backend.lock").read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == [
        "requirements.backend.lock",
        "requirements.backend.txt",
        "requirements.whisper.txt",
    ]


def test_a_hash_mismatch_keeps_the_lock_and_names_the_package(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    before = (root / "requirements.backend.lock").read_bytes()
    bad = {**BACKEND_DIGESTS, ("dep", "5"): [D]}
    with pytest.raises(lock.LockError, match="dep"):
        lock.generate("backend", root=root, run_pip=FakePip(_backend_items()), fetch=_fetch_for(bad))
    assert (root / "requirements.backend.lock").read_bytes() == before


def test_an_unreachable_pypi_keeps_the_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    before = (root / "requirements.backend.lock").read_bytes()

    def down(url: str) -> bytes:
        raise OSError("network is unreachable")

    with pytest.raises(lock.LockError, match="PyPI"):
        lock.generate("backend", root=root, run_pip=FakePip(_backend_items()), fetch=down)
    assert (root / "requirements.backend.lock").read_bytes() == before


WHISPER_ITEMS = [*_backend_items(), _item("w", "3.0", A), _item("wdep", "7", B)]
WHISPER_DIGESTS = {**BACKEND_DIGESTS, ("w", "3.0"): [A], ("wdep", "7"): [B]}


def test_whisper_resolves_on_the_backend_lock_and_writes_only_what_it_adds(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _backend_locked(root)
    pip, fetch = FakePip(WHISPER_ITEMS), _fetch_for(WHISPER_DIGESTS)
    lock.generate("whisper", root=root, run_pip=pip, fetch=fetch)
    text = (root / "requirements.whisper.lock").read_text()
    assert set(lock.parse_lock(text)) == {"w", "wdep"}
    assert "# Input: requirements.whisper.txt" in text
    assert lock.header_digest(text) == lock.input_digest(WHISPER_TXT)
    args = pip.calls[0]
    assert "--only-binary=:all:" in args
    joined = " ".join(args)
    assert "requirements.backend.txt" in joined and "requirements.whisper.txt" in joined
    assert {"foo==1.0", "bar==2.0", "dep==5"} <= set(pip.constraints[0].splitlines())
    # Nothing is fetched for packages the backend lock already covers.
    assert not any("/foo/" in u or "/dep/" in u for u in fetch.requested)


def test_whisper_needs_the_backend_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(lock.LockError, match="requirements.backend.lock"):
        lock.generate("whisper", root=root, run_pip=FakePip(WHISPER_ITEMS), fetch=_fetch_for(WHISPER_DIGESTS))


def test_main_refuses_an_unsupported_platform_before_doing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    pip = FakePip(_backend_items())
    rc = lock.main(["backend"], root=root, run_pip=pip, fetch=_fetch_for({}), platform=("win32", "AMD64", "cpython", (3, 14)))
    assert rc != 0
    assert "Linux x86_64" in capsys.readouterr().err
    assert pip.calls == []
    assert not (root / "requirements.backend.lock").exists()


def test_main_check_installed_reports_drift_and_succeeds_on_a_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "x.lock"
    path.write_text(f"a==1 --hash=sha256:{A}\n")
    assert lock.main(["--check-installed", str(path)], installed={"a": "1"}) == 0
    assert lock.main(["--check-installed", str(path)], installed={"a": "2"}) != 0
    err = capsys.readouterr().err
    assert "a" in err and "1" in err and "2" in err


def test_main_rejects_upgrade_combined_with_upgrade_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        lock.main(["backend", "--upgrade", "--upgrade-package", "x"])


# ---- the dev lock (issue #264) ----

PYPROJECT = """\
[build-system]
requires = ["setuptools==80.0.0"]  # build
build-backend = "setuptools.build_meta"

[project]
name = "x"
version = "0.1.0"

[project.optional-dependencies]
dev = [
    "pytest==9.0",
    # a comment
    "Testcontainers[postgres]==4.0",
    "My_Tool==1.0",
]

[tool.ruff]
line-length = 100
"""


def test_the_dev_and_build_entries_are_read_with_tomllib_extras_and_case_kept() -> None:
    dev, build = lock.pyproject_requirements(PYPROJECT)
    assert dev == ["pytest==9.0", "Testcontainers[postgres]==4.0", "My_Tool==1.0"]
    assert build == ["setuptools==80.0.0"]
    assert lock.dev_direct_pins(PYPROJECT) == {
        "pytest": "9.0", "testcontainers": "4.0", "my-tool": "1.0", "setuptools": "80.0.0",
    }


def test_a_pyproject_without_a_dev_extra_has_no_dev_entries() -> None:
    assert lock.pyproject_requirements('[project]\nname = "x"\n') == ([], [])


def test_the_dev_digest_ignores_order_comments_and_unrelated_settings() -> None:
    base = lock.dev_input_digest(PYPROJECT)
    assert len(base) == 64
    reordered = PYPROJECT.replace('"pytest==9.0",', '"My_Tool==1.0",', 1).replace(
        '    "My_Tool==1.0",\n]', '    "pytest==9.0",\n]'
    )
    assert reordered != PYPROJECT
    assert lock.dev_input_digest(reordered) == base
    other = PYPROJECT.replace("line-length = 100", "line-length = 88\n# note").replace(
        'version = "0.1.0"', 'version = "9.9.9"'
    )
    assert lock.dev_input_digest(other) == base


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"pytest==9.0"', '"pytest==9.1"'),
        ('    "pytest==9.0",\n', ""),
        ('"pytest==9.0",', '"pytest==9.0",\n    "extra==1",'),
        ("setuptools==80.0.0", "setuptools==81.0.0"),
    ],
)
def test_the_dev_digest_changes_when_a_dev_or_build_pin_is_added_removed_or_bumped(old: str, new: str) -> None:
    changed = PYPROJECT.replace(old, new)
    assert changed != PYPROJECT
    assert lock.dev_input_digest(changed) != lock.dev_input_digest(PYPROJECT)


def test_moving_a_pin_from_dev_to_build_changes_the_dev_digest() -> None:
    moved = PYPROJECT.replace('requires = ["setuptools==80.0.0"]', 'requires = ["setuptools==80.0.0", "pytest==9.0"]')
    moved = moved.replace('    "pytest==9.0",\n', "")
    assert lock.dev_direct_pins(moved) == lock.dev_direct_pins(PYPROJECT)
    assert lock.dev_input_digest(moved) != lock.dev_input_digest(PYPROJECT)


def test_a_backend_lock_entry_is_removed_from_the_dev_report() -> None:
    report = _report(_item("foo", "1.0", A), _item("pytest", "9.0", B), _item("pluggy", "1", C))
    digests = {("pytest", "9.0"): {B}, ("pluggy", "1"): {C}}
    text = lock.lock_text(
        report, digests, input_name="i", command="c", digest="0" * 64, exclude={"foo"}
    )
    assert set(lock.parse_lock(text)) == {"pytest", "pluggy"}


def test_the_conflict_message_names_each_constraint_lock_and_pips_error() -> None:
    message = lock.conflict_message(
        "pyproject.toml dev", "ERROR: typing-extensions conflict", ["requirements.backend.lock", "requirements.whisper.lock"]
    )
    assert "ERROR: typing-extensions conflict" in message
    assert "requirements.backend.lock" in message and "requirements.whisper.lock" in message
    assert "unchanged" in message


def test_the_backend_conflict_message_is_the_upgrade_hint() -> None:
    message = lock.conflict_message("requirements.backend.txt", "boom", [])
    assert "--upgrade-package" in message and "lock" not in message.split("boom")[0]


@pytest.mark.parametrize(
    ("target", "expected"),
    [("backend", ["whisper", "dev"]), ("whisper", ["dev"]), ("dev", [])],
)
def test_follow_up_names_the_dependent_locks_in_order(target: str, expected: list[str]) -> None:
    note = lock.follow_up(target)
    if not expected:
        assert note is None
        return
    assert note is not None
    positions = [note.index(f"lock_requirements.py {name}") for name in expected]
    assert positions == sorted(positions)
    assert "lock_requirements.py backend" not in note


def test_unlocked_installed_flags_a_package_in_no_lock_bar_pip_and_ytdigest() -> None:
    locked = {"a": ("1", (A,))}
    problems = lock.unlocked_installed({"a": "1", "pip": "26", "ytdigest": "0.1.0", "stray": "2"}, locked)
    assert len(problems) == 1 and "stray" in problems[0] and "2" in problems[0]


def _dev_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text(PYPROJECT)
    _backend_locked(root)
    lock.generate("whisper", root=root, run_pip=FakePip(WHISPER_ITEMS), fetch=_fetch_for(WHISPER_DIGESTS))
    return root


DEV_ITEMS = [
    *WHISPER_ITEMS,
    _item("pytest", "9.0", A),
    _item("pluggy", "1.6", B),
    _item("setuptools", "80.0.0", C),
]
DEV_DIGESTS = {**WHISPER_DIGESTS, ("pytest", "9.0"): [A], ("pluggy", "1.6"): [B, D], ("setuptools", "80.0.0"): [C]}


def test_generate_dev_resolves_on_both_locks_and_writes_only_what_it_adds(tmp_path: Path) -> None:
    root = _dev_repo(tmp_path)
    pip, fetch = FakePip(DEV_ITEMS), _fetch_for(DEV_DIGESTS)
    lock.generate("dev", root=root, run_pip=pip, fetch=fetch)
    text = (root / "requirements.dev.lock").read_text()
    parsed = lock.parse_lock(text)
    # Backend entries are left out; a package shared with whisper is kept, with all its hashes.
    assert set(parsed) == {"pytest", "pluggy", "setuptools", "w", "wdep"}
    assert parsed["pluggy"] == ("1.6", (B, D))
    assert lock.header_digest(text) == lock.dev_input_digest(PYPROJECT)
    assert "[project.optional-dependencies].dev and [build-system].requires" in text
    assert "ytdigest" not in text
    args, joined = pip.calls[-1], " ".join(pip.calls[-1])
    assert "requirements.backend.txt" in joined
    assert "-e" not in args and "." not in args  # the project itself is not resolved
    assert not any("/foo/" in u or "/dep/" in u for u in fetch.requested)
    constraints = set(pip.constraints[-1].splitlines())
    assert {"foo==1.0", "w==3.0", "wdep==7"} <= constraints  # both locks are constraints
    resolved = pip.requirements[-1]
    assert "Testcontainers[postgres]==4.0" in resolved and "setuptools==80.0.0" in resolved


def test_dev_keeps_its_own_entries_as_constraints_and_upgrade_package_drops_one(tmp_path: Path) -> None:
    root = _dev_repo(tmp_path)
    lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS))
    pip = FakePip(DEV_ITEMS)
    lock.generate("dev", root=root, upgrade_packages=["Pluggy"], run_pip=pip, fetch=_fetch_for(DEV_DIGESTS))
    constraints = pip.constraints[0].splitlines()
    assert "pytest==9.0" not in constraints  # a direct pin comes from the input, not the old lock
    assert not any(c.startswith("pluggy==") for c in constraints)
    pip = FakePip(DEV_ITEMS)
    lock.generate("dev", root=root, upgrade=True, run_pip=pip, fetch=_fetch_for(DEV_DIGESTS))
    assert not any(c.startswith("pluggy==") for c in pip.constraints[0].splitlines())
    assert "foo==1.0" in pip.constraints[0].splitlines()  # the base locks are never dropped


def test_dev_needs_the_backend_and_whisper_locks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text(PYPROJECT)
    with pytest.raises(lock.LockError, match=r"requirements\.backend\.lock.*backend"):
        lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS))
    _backend_locked(root)
    with pytest.raises(lock.LockError, match=r"requirements\.whisper\.lock.*whisper"):
        lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS))


def test_a_dev_conflict_exits_nonzero_names_the_constraint_locks_and_changes_no_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _dev_repo(tmp_path)
    lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS))
    names = ("requirements.backend.lock", "requirements.whisper.lock", "requirements.dev.lock")
    before = {n: (root / n).read_bytes() for n in names}
    rc = lock.main(
        ["dev"], root=root, run_pip=FakePip([], rc=1), fetch=_fetch_for({}),
        platform=("linux", "x86_64", "cpython", (3, 14)),
    )
    err = capsys.readouterr().err
    assert rc != 0
    assert "ResolutionImpossible" in err
    assert "requirements.backend.lock" in err and "requirements.whisper.lock" in err
    assert {n: (root / n).read_bytes() for n in names} == before
    assert not [p for p in root.iterdir() if p.name.endswith(".tmp")]


def test_a_dev_hash_mismatch_keeps_the_dev_lock(tmp_path: Path) -> None:
    root = _dev_repo(tmp_path)
    lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS))
    before = (root / "requirements.dev.lock").read_bytes()
    bad = {**DEV_DIGESTS, ("pytest", "9.0"): [E]}
    with pytest.raises(lock.LockError, match="pytest"):
        lock.generate("dev", root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(bad))
    assert (root / "requirements.dev.lock").read_bytes() == before


def test_main_prints_which_dependent_locks_to_regenerate_after_backend_and_whisper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    target = ("linux", "x86_64", "cpython", (3, 14))
    lock.main(["backend"], root=root, run_pip=FakePip(_backend_items()), fetch=_fetch_for(BACKEND_DIGESTS), platform=target)
    out = capsys.readouterr().out
    assert out.index("lock_requirements.py whisper") < out.index("lock_requirements.py dev")
    lock.main(["whisper"], root=root, run_pip=FakePip(WHISPER_ITEMS), fetch=_fetch_for(WHISPER_DIGESTS), platform=target)
    out = capsys.readouterr().out
    assert "lock_requirements.py dev" in out and "lock_requirements.py whisper" not in out
    (root / "pyproject.toml").write_text(PYPROJECT)
    lock.main(["dev"], root=root, run_pip=FakePip(DEV_ITEMS), fetch=_fetch_for(DEV_DIGESTS), platform=target)
    assert "regenerate" not in capsys.readouterr().out.lower()


def test_main_check_installed_takes_several_locks_and_flags_a_package_in_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one, two = tmp_path / "one.lock", tmp_path / "two.lock"
    one.write_text(f"a==1 --hash=sha256:{A}\n")
    two.write_text(f"b==2 --hash=sha256:{B}\n")
    have = {"a": "1", "b": "2", "pip": "26", "ytdigest": "0.1.0"}
    assert lock.main(["--check-installed", str(one), str(two)], installed=have) == 0
    assert lock.main(["--check-installed", str(one), str(two)], installed={**have, "stray": "3"}) != 0
    assert "stray" in capsys.readouterr().err
    assert lock.main(["--check-installed", str(one), str(two)], installed={"a": "1", "pip": "26"}) != 0
    assert "b" in capsys.readouterr().err


# ---- --report-outdated (issue #266) ----

Items = list[dict[str, object]]
LOCK_FILES = ("requirements.backend.lock", "requirements.whisper.lock", "requirements.dev.lock")
INPUT_FILES = ("requirements.backend.txt", "requirements.whisper.txt", "pyproject.toml")
LINUX = ("linux", "x86_64", "cpython", (3, 14))


class ScriptedPip:
    """Fake pip for the three resolves of the report: one answer per call, in order.

    An answer is a list of report items, an exception to raise, or an (rc, stderr) pair.
    """

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []
        self.constraints: list[set[str]] = []
        self.requirements: list[str] = []
        self.temp_dirs: list[Path] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        self.temp_dirs.append(Path(args[args.index("--report") + 1]).parent)
        self.requirements.append(
            " ".join(Path(args[i + 1]).read_text() for i, a in enumerate(args) if a == "-r")
        )
        self.constraints.append(
            set(Path(args[args.index("-c") + 1]).read_text().split()) if "-c" in args else set()
        )
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            return answer
        items: Items = list(answer)  # type: ignore[call-overload]
        Path(args[args.index("--report") + 1]).write_text(json.dumps(_report(*items)))
        return 0, ""


def _locked_repo(tmp_path: Path) -> Path:
    root = _dev_repo(tmp_path)
    lock.generate("dev", root=root, run_pip=FakePip([*_backend_items(), *DEV_ONLY]), fetch=_fetch_for(DEV_DIGESTS))
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in (*LOCK_FILES, *INPUT_FILES)}


class LatestPyPI:
    """Stands in for `/pypi/<name>/json`: newest `info.version` per canonical name."""

    def __init__(self, latest: Mapping[str, object]) -> None:
        self.latest = dict(latest)
        self.requested: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        answer = self.latest[url.split("/")[-2]]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return answer
        return json.dumps({"info": {"version": answer}}).encode()


#: What the fixture repo pins (BACKEND_TXT, WHISPER_TXT, PYPROJECT): every pin is current.
CURRENT_PINS = {
    "foo": "1.0", "bar": "2.0", "w": "3.0", "pytest": "9.0", "testcontainers": "4.0",
    "my-tool": "1.0", "setuptools": "80.0.0",
}


def _report_run(
    root: Path, pip: ScriptedPip, *, pypi: FakePyPI | LatestPyPI | None = None, **kw: int
) -> int:
    code: int = lock.main(
        ["--report-outdated"], root=root, run_pip=pip, fetch=pypi or LatestPyPI(CURRENT_PINS),
        platform=LINUX, **kw,
    )
    return code


DEV_ONLY = DEV_ITEMS[len(WHISPER_ITEMS) :]


def _tree(backend: Items, w_version: str = "3.0", dev: Items = DEV_ONLY) -> list[Items]:
    """The three resolves: backend, backend+whisper, backend+whisper+dev."""
    whisper = [*backend, _item("w", w_version, A), _item("wdep", "7", B)]
    return [backend, whisper, [*backend, *dev]]


def _bump(items: Items, name: str, version: str) -> Items:
    return [_item(name, version, "f" * 64) if i["metadata"]["name"] == name else i for i in items]  # type: ignore[index]


def test_current_locks_exit_zero_silently_and_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    before = _snapshot(root)
    before_names = {p.name for p in root.iterdir()}
    pip, pypi = ScriptedPip(*_tree(_backend_items())), LatestPyPI(CURRENT_PINS)
    assert _report_run(root, pip, pypi=pypi) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert _snapshot(root) == before
    assert {p.name for p in root.iterdir()} == before_names
    # exactly one version-list call per checked direct pin, no per-version (hash) URL
    assert sorted(pypi.requested) == sorted(f"https://pypi.org/pypi/{n}/json" for n in CURRENT_PINS)
    assert len(pip.calls) == 3 and all(not d.exists() for d in pip.temp_dirs)


def test_the_resolves_are_what_upgrade_would_run_each_on_the_upgraded_results_before(
    tmp_path: Path,
) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip(*_tree(_bump(_backend_items(), "foo", "1.1"), w_version="3.1"))
    assert _report_run(root, pip) == 1
    first, second, third = pip.calls
    for call in pip.calls:
        assert call[1:4] == ["-m", "pip", "install"]
        assert "--dry-run" in call and "--ignore-installed" in call and "--report" in call
    assert first.count("-r") == 1 and first[first.index("-r") + 1].endswith("requirements.backend.txt")
    assert "-c" not in first and pip.constraints[0] == set()  # not the old lock
    assert "--only-binary=:all:" not in first
    assert [Path(second[i + 1]).name for i, a in enumerate(second) if a == "-r"] == [
        "requirements.backend.txt",
        "requirements.whisper.txt",
    ]
    assert "--only-binary=:all:" in second
    # constrained by the upgraded backend result (foo 1.1), not the committed lock (foo 1.0)
    assert "foo==1.1" in pip.constraints[1] and "foo==1.0" not in pip.constraints[1]
    assert not any(c.startswith("w==") for c in pip.constraints[1])
    # dev is resolved on both upgraded results
    assert {"foo==1.1", "w==3.1"} <= pip.constraints[2]
    assert "pytest==9.0" in pip.requirements[2] and "Testcontainers[postgres]==4.0" in pip.requirements[2]
    assert "--only-binary=:all:" not in third


def test_a_changed_entry_is_listed_with_old_and_new_and_unchanged_ones_are_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(*_tree(_bump(_backend_items(), "dep", "6")))) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == ["requirements.backend.lock dep 5 -> 6 (major)"]
    assert "foo 1.0" not in lines[0] and "bar 2.0" not in lines[0] and "pytest 9.0" not in lines[0]


def test_a_downgrade_is_a_change_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(*_tree(_bump(_backend_items(), "foo", "0.9.5")))) == 1
    assert capsys.readouterr().out.splitlines()[1:] == ["requirements.backend.lock foo 1.0 -> 0.9.5 (major)"]


def test_an_added_transitive_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(*_tree([*_backend_items(), _item("newdep", "1.0", A)]))) == 1
    assert capsys.readouterr().out.splitlines()[1:] == ["requirements.backend.lock newdep added 1.0"]


def test_a_removed_package_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    backend = [i for i in _backend_items() if i["metadata"]["name"] != "bar"]  # type: ignore[index]
    assert _report_run(root, ScriptedPip(*_tree(backend))) == 1
    assert capsys.readouterr().out.splitlines()[1:] == ["requirements.backend.lock bar removed (was 2.0)"]


def test_an_entry_that_moves_between_locks_is_reported_once_per_lock_it_changes_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    # whisper's wdep is now pulled in by the backend tree
    backend = [*_backend_items(), _item("wdep", "7", B)]
    whisper = [*backend, _item("w", "3.0", A)]
    pip = ScriptedPip(backend, whisper, [*backend, *DEV_ONLY])
    assert _report_run(root, pip) == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "requirements.backend.lock wdep added 7",
        "requirements.whisper.lock wdep removed (was 7)",
    ]


def test_a_dev_only_change_is_reported_against_the_dev_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(*_tree(_backend_items(), dev=_bump(DEV_ONLY, "pytest", "10.0")))) == 1
    assert capsys.readouterr().out.splitlines()[1:] == ["requirements.dev.lock pytest 9.0 -> 10.0 (major)"]


@pytest.mark.parametrize(
    ("old", "new", "major"),
    [
        ("2.11.4", "3.0.0", True),
        ("2.11.4", "2.12.0", False),
        ("0.9", "1.0", True),
        ("2.0rc1", "2.0", False),
        ("1.0.post1", "2.0", True),
        ("1!2.0", "2.0", False),  # an epoch is not read: no label, no crash
        ("2.0", "3!1.0", False),
        ("abc", "1.0", False),
        ("", "1.0", False),
        ("10.0", "9.0", True),
        ("1.0+local", "1.0", False),
    ],
)
def test_major_compares_the_leading_integer_and_never_crashes(old: str, new: str, major: bool) -> None:
    assert lock.is_major(old, new) is major


def test_major_entries_come_first_then_by_name_and_the_annotation_summarizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    backend = _bump(_bump(_bump(_backend_items(), "bar", "2.1"), "foo", "2.0"), "dep", "5.1")
    assert _report_run(root, ScriptedPip(*_tree(backend)), inline_limit=2) == 1
    lines = capsys.readouterr().out.splitlines()
    annotation = lines[0]
    assert annotation.startswith("::error title=Python locks are behind PyPI::")
    assert lines[1:] == [
        "requirements.backend.lock foo 1.0 -> 2.0 (major)",
        "requirements.backend.lock bar 2.0 -> 2.1",
        "requirements.backend.lock dep 5 -> 5.1",
    ]
    assert "3 lock entries" in annotation and "1 major" in annotation
    assert "foo 1.0 -> 2.0" in annotation and "bar 2.0 -> 2.1" in annotation
    assert "dep 5 -> 5.1" not in annotation and "and 1 more" in annotation
    steps = [
        "python scripts/lock_requirements.py backend --upgrade",
        "python scripts/lock_requirements.py whisper --upgrade",
        "python scripts/lock_requirements.py dev --upgrade",
        "pytest -m image",
        "PR",
    ]
    positions = [annotation.index(step) for step in steps]
    assert positions == sorted(positions)
    assert sum(ln.startswith("::error") for ln in lines) == 1


def test_the_inline_cap_defaults_to_ten() -> None:
    changes = [lock.Change("l", f"p{i:02}", "1", "1.1") for i in range(12)]
    text = lock.behind_annotation(lock.order_changes(changes), lock.INLINE_LIMIT)
    assert "p09" in text and "p10" not in text and "and 2 more" in text


def test_a_mixed_run_names_only_the_whisper_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(*_tree(_backend_items(), w_version="3.1"))) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "1 lock entries" in lines[0]
    assert lines[1:] == ["requirements.whisper.lock w 3.0 -> 3.1"]


def _assert_outage(
    root: Path, pip: ScriptedPip, capsys: pytest.CaptureFixture[str], step: str, calls: int
) -> None:
    before = _snapshot(root)
    assert _report_run(root, pip) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert len(out.err.strip().splitlines()) == 1
    assert step in out.err and out.err.strip().endswith("will retry on the next run")
    assert len(pip.calls) == calls
    assert _snapshot(root) == before
    assert all(not d.exists() for d in pip.temp_dirs)


def test_a_failing_backend_resolve_is_an_outage_and_whisper_is_not_attempted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip((1, "WARNING: x\nERROR: Could not reach pypi.org\n\n"))
    _assert_outage(root, pip, capsys, "backend", 1)


def test_pips_last_error_line_is_in_the_outage_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    assert _report_run(root, ScriptedPip(_backend_items(), (1, "first\nERROR: connection reset\n"))) == 0
    err = capsys.readouterr().err
    assert "whisper" in err and "ERROR: connection reset" in err and "first" not in err


def test_a_failing_dev_resolve_is_an_outage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip(*_tree(_backend_items())[:2], (1, "ERROR: ResolutionImpossible"))
    _assert_outage(root, pip, capsys, "dev", 3)


def test_a_pip_timeout_is_an_outage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip(_backend_items(), subprocess.TimeoutExpired(["pip"], 1.0))
    _assert_outage(root, pip, capsys, "whisper", 2)


def test_the_default_pip_seam_applies_the_injected_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lock.subprocess, "run", fake_run)
    assert lock.make_run_pip(7.5)(["pip"]) == (0, "")
    assert seen["timeout"] == 7.5


@pytest.mark.parametrize("missing", [*LOCK_FILES, *INPUT_FILES])
def test_a_missing_file_is_a_repository_fault_naming_it_without_calling_pip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    root = _locked_repo(tmp_path)
    (root / missing).unlink()
    pip = ScriptedPip()
    assert _report_run(root, pip) == 2
    out = capsys.readouterr()
    assert out.out == "" and missing in out.err and len(out.err.strip().splitlines()) == 1
    assert pip.calls == []


def test_an_unparseable_lock_is_a_repository_fault_naming_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    (root / "requirements.whisper.lock").write_text("not a pin\n")
    pip = ScriptedPip()
    assert _report_run(root, pip) == 2
    assert "requirements.whisper.lock" in capsys.readouterr().err
    assert pip.calls == []


def test_an_unpinned_requirement_is_a_repository_fault_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    (root / "requirements.whisper.txt").write_text("loose>=1\n")
    pip = ScriptedPip()
    assert _report_run(root, pip) == 2
    assert "loose>=1" in capsys.readouterr().err
    assert pip.calls == []


def test_an_unparseable_pyproject_is_a_repository_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    (root / "pyproject.toml").write_text("[broken\n")
    pip = ScriptedPip()
    assert _report_run(root, pip) == 2
    assert "pyproject.toml" in capsys.readouterr().err and pip.calls == []


def test_an_unsupported_platform_is_a_repository_fault_without_calling_pip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip()
    rc = lock.main(["--report-outdated"], root=root, run_pip=pip, platform=("win32", "AMD64", "cpython", (3, 14)))
    assert rc == 2 and "Linux x86_64" in capsys.readouterr().err and pip.calls == []


def test_report_outdated_takes_no_other_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        lock.main(["--report-outdated", "backend"])
    with pytest.raises(SystemExit):
        lock.main(["--report-outdated", "--upgrade"])
    assert "--report-outdated" in capsys.readouterr().err


# ---- direct pins behind PyPI (issue #278) ----

DIRECT = "Direct Python pins are behind PyPI"
HELD = "Held Python pins"


def _inputs(root: Path, *, backend: str | None = None, whisper: str | None = None,
            pyproject: str | None = None) -> Path:
    for name, text in (("requirements.backend.txt", backend), ("requirements.whisper.txt", whisper),
                       ("pyproject.toml", pyproject)):
        if text is not None:
            (root / name).write_text(text)
    return root


def _direct_run(
    root: Path, latest: Mapping[str, object], *, pip: ScriptedPip | None = None, **kw: int
) -> tuple[int, LatestPyPI, ScriptedPip]:
    pip = pip or ScriptedPip(*_tree(_backend_items()))
    pypi = LatestPyPI({**CURRENT_PINS, **latest})
    return _report_run(root, pip, pypi=pypi, **kw), pypi, pip


def _annotations(out: str, level: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"::{level} ")]


def test_requirement_pins_drop_extras_and_markers_and_use_canonical_names() -> None:
    pins = lock.requirements_direct_pins(
        "r.txt", "# c\npsycopg[binary]==3.3.6\nPyYAML==6.0\nfaster_whisper==1.2.1\nfoo==1.0 ; python_version>'3'\n"
    )
    assert [(p.name, p.version, p.line) for p in pins] == [
        ("psycopg", "3.3.6", 2), ("pyyaml", "6.0", 3), ("faster-whisper", "1.2.1", 4), ("foo", "1.0", 5),
    ]


def test_pyproject_pins_come_from_the_dev_extra_and_build_requires_with_extras_dropped() -> None:
    pins = lock.pyproject_direct_pins("pyproject.toml", PYPROJECT)
    assert [(p.name, p.version) for p in pins] == [
        ("setuptools", "80.0.0"), ("pytest", "9.0"), ("testcontainers", "4.0"), ("my-tool", "1.0"),
    ]


def test_a_non_exact_entry_is_skipped_without_a_fetch_or_a_report_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), pyproject=PYPROJECT.replace("==80.0.0", ">=77"))
    rc, pypi, _ = _direct_run(root, {})
    assert rc == 0 and capsys.readouterr().out == ""
    assert "https://pypi.org/pypi/setuptools/json" not in pypi.requested


@pytest.mark.parametrize("spelling", ["yt-dlp", "yt_dlp", "YT-DLP"])
def test_yt_dlp_is_never_checked_or_mentioned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=f"{BACKEND_TXT}{spelling}==2024.1.1\n")
    pip = ScriptedPip(*_tree([*_backend_items(), _item("yt-dlp", "2024.1.1", A)]))
    lock_path = root / "requirements.backend.lock"
    lock_path.write_text(lock_path.read_text() + f"yt-dlp==2024.1.1 --hash=sha256:{A}\n")
    rc, pypi, _ = _direct_run(root, {"yt-dlp": "2099.1.1"}, pip=pip)
    out = capsys.readouterr()
    assert not any("yt-dlp" in u or "yt_dlp" in u.lower() for u in pypi.requested)
    assert "yt" not in out.out.lower() and "yt" not in out.err.lower()
    assert rc == 0 and "https://pypi.org/pypi/foo/json" in pypi.requested


def test_the_exclusion_is_one_named_constant() -> None:
    assert lock.YTDLP_EXCLUDED == "yt-dlp"


@pytest.mark.parametrize(
    ("pinned", "newest"),
    [("1.0", "1.0.0"), ("1.0.0", "1.0"), ("2.0", "2.0"), ("2.1", "2.0"), ("10.0", "9.9")],
)
def test_a_pin_equal_to_or_newer_than_pypi_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pinned: str, newest: str
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=BACKEND_TXT.replace("foo==1.0", f"foo=={pinned}"))
    rc, _, _ = _direct_run(root, {"foo": newest})
    assert rc == 0 and capsys.readouterr().out == ""


def test_a_pin_behind_pypi_is_reported_with_file_name_and_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    before = _snapshot(root)
    rc, pypi, pip = _direct_run(root, {"foo": "1.1", "pytest": "9.1"})
    out = capsys.readouterr()
    assert rc == 1 and out.err == ""
    lines = out.out.splitlines()
    assert len(_annotations(out.out, "error")) == 1 and lines[0].startswith(f"::error title={DIRECT}::")
    assert lines[1:] == [
        "pyproject.toml pytest 9.0 -> 9.1",
        "requirements.backend.txt foo 1.0 -> 1.1",
    ]
    assert "2 direct pins are behind PyPI (0 major)" in lines[0]
    assert "python scripts/lock_requirements.py backend or whisper" in lines[0]
    assert "pytest -m image" in lines[0] and "AGENTS.md" in lines[0]
    assert _snapshot(root) == before and len(pip.calls) == 3
    assert pypi.requested.count("https://pypi.org/pypi/foo/json") == 1


def test_major_pins_come_first_then_by_file_and_name_and_the_cap_is_injectable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    rc, _, _ = _direct_run(root, {"foo": "1.1", "bar": "2.1", "pytest": "10.0"}, inline_limit=2)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert lines[1:] == [
        "pyproject.toml pytest 9.0 -> 10.0 (major)",
        "requirements.backend.txt bar 2.0 -> 2.1",
        "requirements.backend.txt foo 1.0 -> 1.1",
    ]
    assert "3 direct pins are behind PyPI (1 major)" in lines[0]
    assert "pyproject.toml pytest 9.0 -> 10.0 (major); requirements.backend.txt bar 2.0 -> 2.1; and 1 more" in lines[0]
    assert "foo 1.0 -> 1.1." not in lines[0]


@pytest.mark.parametrize("newest", ["1.0.post1", "2!1.0", "1.0rc1"])
def test_a_non_numeric_version_that_differs_is_reported_unordered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], newest: str
) -> None:
    root = _locked_repo(tmp_path)
    rc, _, _ = _direct_run(root, {"foo": newest})
    assert rc == 1
    assert f"requirements.backend.txt foo 1.0 -> {newest} (unordered)" in capsys.readouterr().out


def test_a_non_numeric_pin_equal_to_pypi_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=BACKEND_TXT.replace("foo==1.0", "foo==1.0rc1"))
    rc, _, _ = _direct_run(root, {"foo": "1.0rc1"})
    assert rc == 0 and capsys.readouterr().out == ""


def test_a_prerelease_label_can_be_major_and_unordered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=BACKEND_TXT.replace("foo==1.0", "foo==26.1.0rc1"))
    rc, _, _ = _direct_run(root, {"foo": "27.0"})
    assert rc == 1 and "foo 26.1.0rc1 -> 27.0 (major, unordered)" in capsys.readouterr().out


def test_a_name_pinned_in_two_inputs_is_reported_per_file_and_fetched_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), whisper="w==3.0\nFoo==1.0.1\n")
    rc, pypi, _ = _direct_run(root, {"foo": "1.1"})
    lines = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert lines[1:] == [
        "requirements.backend.txt foo 1.0 -> 1.1",
        "requirements.whisper.txt foo 1.0.1 -> 1.1",
    ]
    assert pypi.requested.count("https://pypi.org/pypi/foo/json") == 1


# -- holds --


def test_a_hold_is_read_from_a_trailing_comment_in_a_requirements_file_and_in_the_dev_array() -> None:
    (pin,) = lock.requirements_direct_pins("r.txt", "av==18.1.0  # hold: PyAV 19 breaks it (#76)\n")
    assert pin.hold == "PyAV 19 breaks it (#76)"
    text = PYPROJECT.replace('"pytest==9.0",', '"pytest==9.0",  # hold:  pinned for #99  ')
    pins = {p.name: p for p in lock.pyproject_direct_pins("pyproject.toml", text)}
    assert pins["pytest"].hold == "pinned for #99" and pins["setuptools"].hold is None
    text = PYPROJECT.replace('requires = ["setuptools==80.0.0"]  # build',
                             'requires = ["setuptools==80.0.0"]  # hold: build pin (#1)')
    assert lock.pyproject_direct_pins("pyproject.toml", text)[0].hold == "build pin (#1)"


def test_the_shipped_av_pin_is_held_with_a_reason_citing_76() -> None:
    text = (REPO_ROOT / "requirements.whisper.txt").read_text()
    pins = {p.name: p for p in lock.requirements_direct_pins("requirements.whisper.txt", text)}
    assert pins["av"].hold is not None and "#76" in pins["av"].hold
    assert pins["faster-whisper"].hold is None
    assert "PyAV 19 removed that argument" in text  # the explanatory comment stays


def test_a_hold_does_not_change_the_input_digests() -> None:
    plain, held = "foo==1.0\nbar==2.0\n", "foo==1.0  # hold: x (#1)\nbar==2.0\n"
    assert lock.input_digest(plain) == lock.input_digest(held)
    assert lock.dev_input_digest(PYPROJECT) == lock.dev_input_digest(
        PYPROJECT.replace('"pytest==9.0",', '"pytest==9.0",  # hold: x (#1)')
    )
    assert lock.dev_input_digest(PYPROJECT) == lock.dev_input_digest(
        PYPROJECT.replace('requires = ["setuptools==80.0.0"]  # build', 'requires = ["setuptools==80.0.0"]  # hold: y')
    )


HELD_BACKEND = "foo==1.0  # hold: compat (#7)\nbar[x]==2.0\n"


def test_a_held_pin_that_is_behind_is_a_notice_and_the_run_stays_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=HELD_BACKEND)
    before = _snapshot(root)
    rc, _, _ = _direct_run(root, {"foo": "2.0"})
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert rc == 0 and out.err == ""
    assert _annotations(out.out, "error") == []
    assert len(_annotations(out.out, "notice")) == 1
    assert lines[0].startswith(f"::notice title={HELD}::") and "foo 1.0 (latest 2.0): compat (#7)" in lines[0]
    assert lines[1:] == ["foo 1.0 (latest 2.0): compat (#7)"]
    assert _snapshot(root) == before


def test_a_held_pin_that_is_current_is_not_mentioned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=HELD_BACKEND)
    rc, _, _ = _direct_run(root, {})
    assert rc == 0 and capsys.readouterr().out == ""


def test_the_notice_is_printed_next_to_a_red_run_without_being_its_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=HELD_BACKEND)
    rc, _, _ = _direct_run(root, {"foo": "2.0", "bar": "2.1"})
    out = capsys.readouterr().out
    assert rc == 1 and len(_annotations(out, "error")) == 1 and len(_annotations(out, "notice")) == 1
    assert out.index("::error") < out.index("::notice")
    assert "bar" in _annotations(out, "error")[0] and "foo" not in _annotations(out, "error")[0]


def test_annotation_text_escapes_percent_cr_and_lf_and_keeps_commas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend="foo==1.0  # hold: 100% sure, really (#7)\nbar[x]==2.0\n")
    rc, _, _ = _direct_run(root, {"foo": "2.0"})
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert "100%25 sure, really (#7)" in lines[0]
    assert lines[1] == "foo 1.0 (latest 2.0): 100% sure, really (#7)"
    assert lock.workflow_escape("a%b\r\nc") == "a%25b%0D%0Ac"


@pytest.mark.parametrize(
    "comment", ["# Hold: x (#1)", "# hold x (#1)", "# held: x (#1)", "#hold x"]
)
def test_a_misspelled_hold_is_an_ordinary_comment_so_the_pin_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], comment: str
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend=f"foo==1.0  {comment}\nbar[x]==2.0\n")
    rc, _, _ = _direct_run(root, {"foo": "2.0"})
    out = capsys.readouterr().out
    assert rc == 1
    assert "requirements.backend.txt foo 1.0 -> 2.0 (major)" in out and "::notice" not in out


FAULTS = [
    ("requirements.backend.txt", "foo==1.0  # hold:\nbar[x]==2.0\n", 1),
    ("requirements.backend.txt", "foo==1.0  # hold:    \nbar[x]==2.0\n", 1),
    ("requirements.backend.txt", "foo==1.0\n# hold: above the pin (#1)\nbar[x]==2.0\n", 2),
    ("requirements.backend.txt", "foo==1.0 bar==2.0  # hold: two (#1)\n", 1),
    ("requirements.backend.txt", "-r other.txt  # hold: option (#1)\nfoo==1.0\n", 1),
    ("requirements.backend.txt", "foo==1.0\nbar>=2  # hold: not exact (#1)\n", 2),
    ("requirements.backend.txt", "foo==1.0\nyt-dlp==2024.1.1  # hold: stale (#1)\n", 2),
    ("requirements.whisper.txt", "w==3.0\nyt_dlp==2024.1.1  # hold: stale (#1)\n", 2),
]


@pytest.mark.parametrize(("name", "text", "line"), FAULTS)
def test_a_malformed_or_misplaced_hold_is_a_repository_fault_before_any_pip_or_pypi_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, text: str, line: int
) -> None:
    root = _locked_repo(tmp_path)
    (root / name).write_text(text)
    rc, pypi, pip = _direct_run(root, {})
    err = capsys.readouterr().err
    assert rc == 2
    assert len(err.strip().splitlines()) == 1 and f"{name}:{line}:" in err
    assert pip.calls == [] and pypi.requested == []


def test_a_hold_on_yt_dlp_says_68_checks_it_daily(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _inputs(_locked_repo(tmp_path), backend="foo==1.0\nyt-dlp==2024.1.1  # hold: x (#1)\n")
    assert _direct_run(root, {})[0] == 2
    assert "#68" in capsys.readouterr().err


PYPROJECT_FAULTS = [
    '"pytest==9.0",  # hold:',
    '"pytest==9.0", "x==1",  # hold: two (#1)',
    '"pytest>=9",  # hold: not exact (#1)',
]


@pytest.mark.parametrize("entry", PYPROJECT_FAULTS)
def test_a_bad_hold_in_the_dev_array_is_a_repository_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    root = _inputs(_locked_repo(tmp_path), pyproject=PYPROJECT.replace('"pytest==9.0",', entry))
    rc, pypi, pip = _direct_run(root, {})
    assert rc == 2 and "pyproject.toml:11:" in capsys.readouterr().err
    assert pip.calls == [] and pypi.requested == []


@pytest.mark.parametrize(
    "replacement",
    ["[tool.ruff]  # hold: nope (#1)", "# hold: a comment-only line (#1)\n[tool.ruff]"],
)
def test_a_hold_outside_the_dev_and_build_arrays_is_a_repository_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], replacement: str
) -> None:
    root = _inputs(_locked_repo(tmp_path), pyproject=PYPROJECT.replace("[tool.ruff]", replacement))
    rc, pypi, pip = _direct_run(root, {})
    assert rc == 2 and "pyproject.toml:" in capsys.readouterr().err
    assert pip.calls == [] and pypi.requested == []


# -- exit codes and sections --


def test_locks_current_and_a_pin_behind_exits_one_with_only_the_direct_pin_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _, _ = _direct_run(_locked_repo(tmp_path), {"w": "4.0"})
    out = capsys.readouterr().out
    assert rc == 1 and f"title={DIRECT}" in out and "Python locks are behind PyPI" not in out


def test_locks_behind_and_pins_current_exits_one_with_only_the_lock_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pip = ScriptedPip(*_tree(_bump(_backend_items(), "foo", "1.1")))
    rc, _, _ = _direct_run(_locked_repo(tmp_path), {}, pip=pip)
    out = capsys.readouterr().out
    assert rc == 1 and "title=Python locks are behind PyPI" in out and DIRECT not in out


def test_both_sections_behind_print_the_lock_section_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pip = ScriptedPip(*_tree(_bump(_backend_items(), "foo", "1.1")))
    rc, _, _ = _direct_run(_locked_repo(tmp_path), {"w": "4.0"}, pip=pip)
    out = capsys.readouterr().out
    assert rc == 1 and out.index("Python locks are behind") < out.index(DIRECT)
    assert len(_annotations(out, "error")) == 2


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection refused"),
        TimeoutError("timed out"),
        OSError("PyPI answered HTTP 503"),
        b"not json",
        json.dumps({"info": {}}).encode(),
        json.dumps({"info": {"version": 3}}).encode(),
        json.dumps([]).encode(),
    ],
)
def test_a_pypi_outage_reports_nothing_names_the_package_and_leaves_the_lock_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: object
) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip(*_tree(_bump(_backend_items(), "foo", "1.1")))
    pypi = LatestPyPI({**CURRENT_PINS, "bar": failure})
    rc = _report_run(root, pip, pypi=pypi)
    out = capsys.readouterr()
    assert rc == 1
    assert len(_annotations(out.out, "error")) == 1 and "title=Python locks are behind" in out.out
    assert DIRECT not in out.out and "::notice" not in out.out
    assert len(out.err.strip().splitlines()) == 1
    assert "bar" in out.err and out.err.strip().endswith("will retry on the next run")


def test_an_oversized_answer_counts_as_an_outage(tmp_path: Path) -> None:
    root = _locked_repo(tmp_path)
    big = json.dumps({"info": {"version": "9" * 200}}).encode()
    pypi = LatestPyPI({**CURRENT_PINS, "foo": big})
    code, out, err = lock.report_outdated(
        root=root, run_pip=ScriptedPip(*_tree(_backend_items())), fetch=pypi, max_body=100
    )
    assert code == 0 and out == "" and "foo" in err and err.endswith("will retry on the next run\n")


def test_an_outage_with_nothing_else_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _locked_repo(tmp_path)
    pypi = LatestPyPI({**CURRENT_PINS, "w": OSError("down")})
    assert _report_run(root, ScriptedPip(*_tree(_backend_items())), pypi=pypi) == 0
    out = capsys.readouterr()
    assert out.out == "" and "w:" in out.err


def test_a_pip_outage_does_not_stop_the_direct_pin_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _locked_repo(tmp_path)
    pip = ScriptedPip((1, "ERROR: Could not reach pypi.org\n"))
    rc, _, _ = _direct_run(root, {"foo": "1.5"}, pip=pip)
    out = capsys.readouterr()
    assert rc == 1 and f"title={DIRECT}" in out.out and "Python locks are behind" not in out.out
    assert "backend resolve" in out.err and "will retry on the next run" in out.err


def test_a_pip_outage_with_current_pins_stays_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pip = ScriptedPip((1, "ERROR: down\n"))
    rc, _, _ = _direct_run(_locked_repo(tmp_path), {}, pip=pip)
    assert rc == 0 and capsys.readouterr().out == ""


def test_the_default_fetch_seam_applies_the_timeout_and_the_body_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status, self.body = status, body

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            seen["read"] = n
            return self.body[:n]

    answer = Response(200, b"x" * 10)

    def fake_urlopen(url: str, timeout: float) -> Response:
        seen["timeout"] = timeout
        return answer

    monkeypatch.setattr(lock.urllib.request, "urlopen", fake_urlopen)
    assert lock.make_fetch(7.5, 20)("u") == b"x" * 10
    assert seen == {"timeout": 7.5, "read": 21}
    with pytest.raises(OSError, match="larger"):
        lock.make_fetch(7.5, 5)("u")
    answer.status = 503
    with pytest.raises(OSError, match="503"):
        lock.make_fetch(7.5)("u")
    assert lock.FETCH_TIMEOUT == 20.0


def test_main_builds_the_fetch_seam_with_the_injected_timeout_when_none_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[float] = []

    def fake_make_fetch(timeout: float, max_bytes: int = 0) -> LatestPyPI:
        made.append(timeout)
        return LatestPyPI(CURRENT_PINS)

    monkeypatch.setattr(lock, "make_fetch", fake_make_fetch)
    root = _locked_repo(tmp_path)
    pip = ScriptedPip(*_tree(_backend_items()))
    assert lock.main(["--report-outdated"], root=root, run_pip=pip, platform=LINUX, fetch_timeout=3.0) == 0
    assert made == [3.0]


def test_the_workflow_header_names_this_issue_and_the_run_line_is_unchanged() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "lock-update-check.yml").read_text()
    header = text.split("name:", 1)[0]
    assert "#278" in header and "hold" in header and "#68" in header
    assert "run: python scripts/lock_requirements.py --report-outdated" in text
