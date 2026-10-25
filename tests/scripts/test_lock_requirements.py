"""scripts/lock_requirements.py (issue #145). No network, no pip: both are injected."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

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

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
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
