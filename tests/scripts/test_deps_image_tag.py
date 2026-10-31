"""scripts/deps_image_tag.py: tags, registry lookup and cleanup (#368).

Offline: the clock is a parameter, the registry lookup and the package list are
injected, and the hash runs on a copy of the real files in a temporary tree.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deps_image_tag.py"
_spec = importlib.util.spec_from_file_location("deps_image_tag", SCRIPT)
assert _spec and _spec.loader
tag = importlib.util.module_from_spec(_spec)
sys.modules["deps_image_tag"] = tag
_spec.loader.exec_module(tag)

BACKEND = "ops/Dockerfile.backend"
WHISPER = "ops/Dockerfile.whisper"
FILES = (BACKEND, WHISPER, "requirements.backend.lock", "requirements.whisper.lock")
MONTH = "2026-10"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def tags_of(root: Path, month: str = MONTH) -> tuple[str, str]:
    t = tag.compute_tags(root, month)
    return t.deps_tag, t.whisper_tag


def edit(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    text = path.read_text()
    assert old in text, f"{old!r} not in {name}"
    path.write_text(text.replace(old, new, 1))


def instruction_line(name: str, prefix: str) -> str:
    return next(
        line for line in (ROOT / name).read_text().splitlines() if line.startswith(prefix)
    )


# ---- determinism and format -------------------------------------------------------


def test_tags_are_deterministic_and_have_the_documented_form(tree: Path) -> None:
    first = tags_of(tree)
    assert first == tags_of(tree)
    assert all(re.fullmatch(r"sha-[0-9a-f]{16}", t) for t in first)
    assert first[0] != first[1]


def test_the_real_repository_files_produce_tags_through_the_command_line() -> None:
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "tags", "--month", MONTH],
        capture_output=True, text=True, check=True,
    ).stdout
    values = dict(line.split("=", 1) for line in out.splitlines())
    assert (values["deps_tag"], values["whisper_tag"]) == tags_of(ROOT)
    assert values["deps_tag"] == "sha-" + values["deps_hash"][:16]
    assert len(values["whisper_hash"]) == 64


@pytest.mark.parametrize("month", ["2026-13", "26-10", "2026-1", ""])
def test_a_malformed_month_is_a_repository_fault(tree: Path, month: str) -> None:
    with pytest.raises(tag.ScriptError):
        tag.compute_tags(tree, month)


def test_the_month_stamp_changes_both_tags(tree: Path) -> None:
    assert tags_of(tree, "2026-10")[0] != tags_of(tree, "2026-11")[0]
    assert tags_of(tree, "2026-10")[1] != tags_of(tree, "2026-11")[1]


# ---- inputs that must change a tag --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        (BACKEND, "FROM python:3.14-slim"),  # the python digest pin (builder and deps)
        (BACKEND, "FROM denoland/deno"),  # the Deno pin
    ],
)
def test_a_changed_base_image_line_changes_both_tags(tree: Path, name: str, prefix: str) -> None:
    before = tags_of(tree)
    line = instruction_line(name, prefix)
    edit(tree, name, line, line.replace("sha256:", "sha256:0", 1))
    after = tags_of(tree)
    assert after[0] != before[0]
    assert after[1] != before[1]  # the whisper tag includes the deps hash


def test_each_backend_stage_text_changes_the_deps_tag(tree: Path) -> None:
    before = tags_of(tree)
    for old, new in (
        ("build-essential", "build-essential gcc"),  # builder
        ("ffmpeg curl", "ffmpeg curl jq"),  # deps
    ):
        edit(tree, BACKEND, old, new)
        assert tags_of(tree)[0] != before[0], old
        edit(tree, BACKEND, new, old)
        assert tags_of(tree) == before


def test_the_backend_lock_changes_both_tags(tree: Path) -> None:
    before = tags_of(tree)
    (tree / "requirements.backend.lock").write_text(
        (tree / "requirements.backend.lock").read_text() + "x\n"
    )
    after = tags_of(tree)
    assert after[0] != before[0] and after[1] != before[1]


def test_the_whisper_lock_changes_only_the_whisper_tag(tree: Path) -> None:
    before = tags_of(tree)
    (tree / "requirements.whisper.lock").write_text(
        (tree / "requirements.whisper.lock").read_text() + "x\n"
    )
    after = tags_of(tree)
    assert after[0] == before[0]
    assert after[1] != before[1]


def test_the_whisper_deps_stage_changes_only_the_whisper_tag(tree: Path) -> None:
    before = tags_of(tree)
    edit(tree, WHISPER, "mkdir -p /models", "mkdir -p /models /extra")
    after = tags_of(tree)
    assert after[0] == before[0]
    assert after[1] != before[1]


# ---- inputs that must not change a tag ----------------------------------------------


def test_the_runtime_stages_and_application_copies_change_no_tag(tree: Path) -> None:
    before = tags_of(tree)
    edit(tree, BACKEND, "COPY --chown=app:app common/ ./common/",
         "COPY --chown=app:app common/ ./common/\nCOPY --chown=app:app extra/ ./extra/")
    edit(tree, WHISPER, "COPY --chown=app:app adapters/ ./adapters/",
         "COPY --chown=app:app adapters/ ./adapters/\nRUN echo runtime-only")
    assert tags_of(tree) == before


def test_comments_and_blank_lines_in_any_stage_change_no_tag(tree: Path) -> None:
    before = tags_of(tree)
    for name, stage_line in (
        (BACKEND, "FROM denoland/deno"),
        (BACKEND, "ENV PIP_DISABLE_PIP_VERSION_CHECK=1"),
        (BACKEND, "WORKDIR /app"),
        (WHISPER, "USER root"),
        (WHISPER, "WORKDIR /tmp"),
    ):
        edit(tree, name, stage_line, f"# a new comment\n\n{stage_line}")
    assert tags_of(tree) == before


def test_line_endings_and_trailing_blanks_change_no_tag(tree: Path) -> None:
    before = tags_of(tree)
    for name in FILES:
        path = tree / name
        text = path.read_text()
        if name.endswith("lock"):
            path.write_bytes(text.replace("\n", "\r\n").encode())
        else:
            path.write_bytes(text.replace("\n", "  \r\n").encode())
    assert tags_of(tree) == before


def test_source_files_and_docs_are_not_inputs(tree: Path) -> None:
    before = tags_of(tree)
    (tree / "common").mkdir()
    (tree / "common" / "config.py").write_text("x = 1\n")
    (tree / "_docs").mkdir()
    (tree / "_docs" / "architecture.md").write_text("changed\n")
    assert tags_of(tree) == before


# ---- the stage extraction against the real Dockerfiles ------------------------------


@pytest.mark.parametrize(
    ("dockerfile", "stage", "tag_index"),
    [
        (BACKEND, "deno", 0),
        (BACKEND, "builder", 0),
        (BACKEND, "deps", 0),
        (WHISPER, "whisper-deps", 1),
    ],
)
def test_any_new_instruction_in_a_hashed_stage_is_covered_by_the_hash(
    tree: Path, dockerfile: str, stage: str, tag_index: int
) -> None:
    text = (tree / dockerfile).read_text()
    body = tag.stages(text)
    assert stage in body, f"{dockerfile} has no stage {stage}"
    froms = [m for m in re.finditer(r"^FROM .*$", text, re.MULTILINE)]
    own = next(m for m in froms if re.search(rf"\bAS {stage}\s*$", m.group(0), re.IGNORECASE))
    following = [m.start() for m in froms if m.start() > own.start()]
    end = following[0] if following else len(text)
    # Insert as the last instruction of the stage, before the next stage's comments.
    block = text[own.start():end].rstrip("\n")
    (tree / dockerfile).write_text(
        text[: own.start()] + block + "\nRUN echo a-new-instruction\n\n" + text[end:]
    )
    before = tags_of(ROOT)
    after = tags_of(tree)
    assert after[tag_index] != before[tag_index]


def test_the_real_dockerfiles_have_every_stage_the_hash_reads() -> None:
    assert {"deno", "builder", "deps", "runtime"} <= set(tag.stages((ROOT / BACKEND).read_text()))
    assert {"whisper-deps", "runtime"} <= set(tag.stages((ROOT / WHISPER).read_text()))


def test_a_missing_stage_is_an_error_not_a_silent_empty_hash(tree: Path) -> None:
    edit(tree, WHISPER, "AS whisper-deps", "AS renamed")
    with pytest.raises(tag.ScriptError, match="whisper-deps"):
        tag.compute_tags(tree, MONTH)


# ---- resolve ------------------------------------------------------------------------

REPO = tag.SOURCE_URL
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
TAGS = tag.Tags("1" * 64, "2" * 64)


class FakeRegistry:
    """A lookup that answers from a table keyed by tag, or raises what it holds."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table
        self.seen: list[str] = []

    def __call__(self, ref: str) -> object:
        self.seen.append(ref)
        value = self.table[ref.rsplit(":", 1)[1]]
        if isinstance(value, Exception):
            raise value
        return value


lookup_with = FakeRegistry


def test_resolve_exports_the_digest_reference_of_each_tag_found() -> None:
    lookup = lookup_with(
        {TAGS.deps_tag: tag.Found(DIGEST, REPO), TAGS.whisper_tag: tag.Found(OTHER, REPO)}
    )
    logs: list[str] = []
    env = tag.resolve(TAGS, lookup, log=logs.append)
    assert env == {
        "YTDIGEST_DEPS_IMAGE": f"ghcr.io/ulyss-1/ytdigest-deps@{DIGEST}",
        "YTDIGEST_WHISPER_DEPS_IMAGE": f"ghcr.io/ulyss-1/ytdigest-whisper-deps@{OTHER}",
    }
    assert lookup.seen == [
        f"ghcr.io/ulyss-1/ytdigest-deps:{TAGS.deps_tag}",
        f"ghcr.io/ulyss-1/ytdigest-whisper-deps:{TAGS.whisper_tag}",
    ]
    assert any(TAGS.deps_tag in line and DIGEST in line for line in logs)


def test_resolve_leaves_a_missing_image_unset_and_says_it_builds_from_source() -> None:
    lookup = lookup_with(
        {TAGS.deps_tag: tag.Found(DIGEST, REPO), TAGS.whisper_tag: RuntimeError("not found")}
    )
    logs: list[str] = []
    env = tag.resolve(TAGS, lookup, log=logs.append)
    assert set(env) == {"YTDIGEST_DEPS_IMAGE"}
    assert any(
        f"no prebuilt image for ytdigest-whisper-deps:{TAGS.whisper_tag}" in line
        and "building from source" in line
        for line in logs
    )


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("unauthorized"), TimeoutError("timed out"), OSError("no route to host"),
     KeyError("manifest"), ValueError("bad json")],
)
def test_resolve_treats_every_lookup_failure_as_build_from_source(failure: Exception) -> None:
    lookup = lookup_with({TAGS.deps_tag: failure, TAGS.whisper_tag: failure})
    assert tag.resolve(TAGS, lookup, log=lambda _: None) == {}


@pytest.mark.parametrize("digest", ["", "sha256:abc", "sha1:" + "a" * 40, "sha256:" + "A" * 64,
                                    DIGEST + "\n--build-arg x"])
def test_resolve_rejects_a_malformed_digest(digest: str) -> None:
    lookup = lookup_with(
        {TAGS.deps_tag: tag.Found(digest, REPO), TAGS.whisper_tag: tag.Found(digest, REPO)}
    )
    assert tag.resolve(TAGS, lookup, log=lambda _: None) == {}


@pytest.mark.parametrize("source", ["", "https://github.com/someone/else"])
def test_resolve_rejects_an_image_whose_source_label_is_not_this_repository(source: str) -> None:
    lookup = lookup_with(
        {TAGS.deps_tag: tag.Found(DIGEST, source), TAGS.whisper_tag: tag.Found(DIGEST, source)}
    )
    assert tag.resolve(TAGS, lookup, log=lambda _: None) == {}


def test_resolve_only_ever_asks_for_the_computed_tags_never_latest() -> None:
    lookup = lookup_with({TAGS.deps_tag: RuntimeError("x"), TAGS.whisper_tag: RuntimeError("x")})
    tag.resolve(TAGS, lookup, log=lambda _: None)
    for ref in lookup.seen:
        assert re.fullmatch(r"ghcr\.io/ulyss-1/[\w-]+:sha-[0-9a-f]{16}", ref), ref


def test_every_exported_value_is_a_digest_reference_or_absent() -> None:
    lookup = lookup_with(
        {TAGS.deps_tag: tag.Found(DIGEST, REPO), TAGS.whisper_tag: tag.Found(DIGEST, REPO)}
    )
    for value in tag.resolve(TAGS, lookup, log=lambda _: None).values():
        assert re.fullmatch(r"ghcr\.io/ulyss-1/[\w-]+@sha256:[0-9a-f]{64}", value)


# ---- prune --------------------------------------------------------------------------

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def version(vid: int, age_days: float, *tags: str) -> dict[str, object]:
    created = (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": vid, "name": f"sha256:{vid:064x}", "created_at": created,
            "metadata": {"container": {"tags": list(tags)}}}


def hash_tag(n: int) -> str:
    return f"sha-{n:016x}"


def deleted_ids(versions: list[dict[str, object]], **kwargs: int) -> set[object]:
    return {v["id"] for v in tag.select_deletions(versions, NOW, **kwargs)}


def test_an_empty_package_deletes_nothing() -> None:
    assert tag.select_deletions([], NOW) == []


def test_old_untagged_versions_are_deleted_and_young_ones_kept() -> None:
    versions = [version(1, 40), version(2, 31), version(3, 29), version(4, 0.1)]
    assert deleted_ids(versions) == {1, 2}


def test_the_version_latest_points_to_is_never_deleted_however_old() -> None:
    versions = [version(1, 400, "latest", hash_tag(1)), version(2, 300, hash_tag(2))]
    assert deleted_ids(versions, keep_recent=0) == {2}


def test_the_n_newest_hash_tags_are_kept_and_older_ones_deleted() -> None:
    versions = [version(n, 100 + n, hash_tag(n)) for n in range(1, 9)]  # 1 is newest
    assert deleted_ids(versions, keep_recent=5) == {6, 7, 8}
    assert deleted_ids(versions, keep_recent=2) == {3, 4, 5, 6, 7, 8}


def test_the_counts_come_from_the_constants_by_default() -> None:
    assert (tag.KEEP_RECENT_TAGS, tag.KEEP_YOUNGER_THAN_DAYS) == (5, 30)
    versions = [version(n, 100 + n, hash_tag(n)) for n in range(1, 8)]
    assert deleted_ids(versions) == {6, 7}


def test_a_young_version_is_kept_even_when_it_would_otherwise_go() -> None:
    versions = [version(n, 100 + n, hash_tag(n)) for n in range(1, 4)] + [version(9, 5)]
    assert deleted_ids(versions, keep_recent=1) == {2, 3}
    assert deleted_ids(versions, keep_recent=1, keep_days=3) == {2, 3, 9}


def test_hash_tags_are_ranked_by_push_time_not_by_name() -> None:
    old = version(1, 200, "sha-ffffffffffffffff")
    new = version(2, 100, "sha-0000000000000000")
    assert deleted_ids([old, new], keep_recent=1) == {1}


def test_a_version_with_a_foreign_tag_is_not_a_hash_tag() -> None:
    versions = [version(1, 100, "v1"), version(2, 101, hash_tag(2))]
    assert deleted_ids(versions, keep_recent=5) == {1}


def test_paginated_gh_output_is_read_page_by_page() -> None:
    text = '[{"id": 1}, {"id": 2}]\n[{"id": 3}]\n'
    assert [v["id"] for v in tag._parse_paginated(text)] == [1, 2, 3]
    assert tag._parse_paginated("") == []


def test_a_refused_deletion_ends_in_a_warning_and_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if "DELETE" in args:
            raise RuntimeError("HTTP 403: Resource not accessible by integration")
        return '[{"id": 7, "created_at": "2020-01-01T00:00:00Z", "metadata": {}}]'

    monkeypatch.setattr(tag, "_gh", fake_gh)
    assert tag.prune("ytdigest-deps", now=NOW) == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning title=Cleanup of ytdigest-deps skipped::")
    assert "HTTP 403" in out and "by hand" in out
    assert any("DELETE" in c for c in calls)


def test_prune_deletes_exactly_the_selected_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []

    def fake_gh(*args: str) -> str:
        if "DELETE" in args:
            deleted.append(args[-1])
            return ""
        return (
            '[{"id": 1, "created_at": "2020-01-01T00:00:00Z", "metadata": {}},'
            ' {"id": 2, "created_at": "2020-01-01T00:00:00Z",'
            '  "metadata": {"container": {"tags": ["latest"]}}}]'
        )

    monkeypatch.setattr(tag, "_gh", fake_gh)
    assert tag.prune("ytdigest-whisper-deps", now=NOW) == 0
    assert deleted == ["/users/ulyss-1/packages/container/ytdigest-whisper-deps/versions/1"]
