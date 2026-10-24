"""Scheduled base-image digest check (issue #241). No network: the fetch is injected."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "base_image_digest_check.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "base-image-digest-check.yml"
YTDLP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ytdlp-update-check.yml"
OPS = REPO_ROOT / "ops"
CHECKED_FILES = [
    "ops/Dockerfile.frontend",
    "ops/Dockerfile.backend",
    "ops/Dockerfile.bakeoff",
    "compose.yml",
    "compose.override.yml",
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("base_image_digest_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

PINNED = "sha256:" + "a" * 64
UPSTREAM = "sha256:" + "b" * 64
IMAGE = "nginx/nginx-unprivileged"
TAG = "1.30-alpine"
OTHER = "acme/widget"
OTHER_DIGEST = "sha256:" + "c" * 64
OTHER_UPSTREAM = "sha256:" + "d" * 64

DOCKERFILE = f"FROM node:24-alpine AS build\nRUN true\nFROM {IMAGE}:{TAG}@{PINNED}\nUSER 101\n"

COMPOSE = f"services:\n  web:\n    image: {IMAGE}:{TAG}@{PINNED}\n  api:\n    image: ytdigest-backend:latest\n"

Response = tuple[int, Mapping[str, str], bytes]


def _token(value: str = "tok") -> Response:
    return 200, {}, json.dumps({"token": value}).encode()


def _manifest(digest: str | None, status: int = 200) -> Response:
    return status, ({} if digest is None else {"Docker-Content-Digest": digest}), b""


class Fetch:
    """Records every call; answers by repository: ``{repo: (token, manifest)}``."""

    def __init__(self, answers: Mapping[str, tuple[Response | Exception, Response | Exception]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], timeout: float) -> Response:
        self.calls.append((method, url, dict(headers), timeout))
        assert timeout > 0
        repo = next(r for r in self.answers if f"/{r}/manifests/" in url or f"repository:{r}:" in url)
        token, manifest = self.answers[repo]
        answer = token if "auth.docker.io" in url else manifest
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetch(manifest: Response | Exception, token: Response | Exception | None = None) -> Fetch:
    return Fetch({IMAGE: (token or _token(), manifest)})


def _run_files(
    tmp_path: Path, files: Mapping[str, str], fetch: Fetch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    paths = []
    for name, text in files.items():
        path = tmp_path / name
        path.write_bytes(text.encode())
        paths.append(path)
    code = check.main([str(p) for p in paths], fetch=fetch)
    out = capsys.readouterr()
    for path, text in zip(paths, files.values(), strict=True):
        assert path.read_bytes() == text.encode()  # the check reports; it never edits
    return code, out.out, out.err


def _run(
    tmp_path: Path, text: str, fetch: Fetch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    return _run_files(tmp_path, {"Dockerfile": text}, fetch, capsys)


# -- the parser ----------------------------------------------------------


def test_parser_finds_only_digest_pinned_froms_and_tolerates_flags_and_as() -> None:
    text = (
        "FROM node:24-alpine AS build\n"
        f"FROM --platform=linux/amd64 {IMAGE}:{TAG}@{PINNED} AS runtime\n"
    )

    refs = check.pinned_references(text)

    assert [(r.name, r.tag, r.digest) for r in refs] == [(IMAGE, TAG, PINNED)]


# -- the registry conversation -------------------------------------------


def test_asks_the_registry_with_an_anonymous_token_then_a_head_with_index_accept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(PINNED), _token("secret-token"))

    _run(tmp_path, DOCKERFILE, fetch, capsys)

    (m1, url1, _, _), (m2, url2, headers, _) = fetch.calls
    assert m1 == "GET"
    assert url1.startswith("https://auth.docker.io/token?")
    assert "service=registry.docker.io" in url1
    assert f"scope=repository:{IMAGE}:pull" in url1
    assert m2 == "HEAD"  # HEAD does not use up the pull rate limit
    assert url2 == f"https://registry-1.docker.io/v2/{IMAGE}/manifests/{TAG}"
    accepted = headers["Accept"]
    assert "application/vnd.oci.image.index.v1+json" in accepted
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in accepted
    assert headers["Authorization"] == "Bearer secret-token"


# -- the comparison ------------------------------------------------------


def test_current_is_silent_and_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(tmp_path, DOCKERFILE, _fetch(_manifest(PINNED)), capsys)

    assert (code, out, err) == (0, "", "")


def test_header_name_is_matched_case_insensitively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch((200, {"docker-content-digest": PINNED}, b""))

    assert _run(tmp_path, DOCKERFILE, fetch, capsys) == (0, "", "")


def test_stale_names_both_digests_and_the_replacement_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(tmp_path, DOCKERFILE, _fetch(_manifest(UPSTREAM)), capsys)

    assert code == 1
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("::error file=") and ",line=3,title=" in lines[0]
    assert str(tmp_path / "Dockerfile") in lines[0]
    for needle in (IMAGE, TAG, PINNED, UPSTREAM, f"{IMAGE}:{TAG}@{UPSTREAM}"):
        assert needle in out


# -- the registry is unreachable: fail quietly ---------------------------

_OUTAGES: list[tuple[Response | Exception | None, Response | Exception]] = [
    ((503, {}, b""), _manifest(PINNED)),
    ((200, {}, b"<html>"), _manifest(PINNED)),
    ((200, {}, b"{}"), _manifest(PINNED)),
    ((200, {}, b'{"token": 5}'), _manifest(PINNED)),
    (TimeoutError("timed out"), _manifest(PINNED)),
    (None, _manifest(UPSTREAM, 401)),
    (None, _manifest(UPSTREAM, 404)),
    (None, _manifest(UPSTREAM, 429)),
    (None, _manifest(UPSTREAM, 500)),
    (None, _manifest(UPSTREAM, 503)),
    (None, TimeoutError("timed out")),
    (None, ConnectionError("refused")),
    (None, _manifest(None)),
    (None, _manifest("")),
    (None, _manifest("sha256:" + "b" * 63)),
    (None, _manifest("sha256:" + "b" * 65)),
    (None, _manifest("sha256:" + "B" * 64)),
    (None, _manifest("sha512:" + "b" * 64)),
]


@pytest.mark.parametrize(("token", "manifest"), _OUTAGES)
def test_an_outage_or_junk_reply_exits_0_with_one_stderr_line(
    token: Response | Exception | None,
    manifest: Response | Exception,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(tmp_path, DOCKERFILE, _fetch(manifest, token), capsys)

    assert code == 0
    assert out == ""
    assert "::error" not in err
    assert err.count("\n") == 1
    assert f"{IMAGE}:{TAG}" in err


def test_a_stale_reference_is_not_hidden_by_an_unrelated_failed_lookup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = f"FROM {IMAGE}:{TAG}@{PINNED}\nFROM {OTHER}:2@{OTHER_DIGEST}\n"
    fetch = Fetch({IMAGE: (_token(), _manifest(UPSTREAM)), OTHER: (_token(), _manifest(None, 503))})

    code, out, err = _run(tmp_path, text, fetch, capsys)

    assert code == 1
    assert out.count("::error") == 1 and UPSTREAM in out
    assert err.count("\n") == 1 and OTHER in err


def test_two_stale_references_get_one_annotation_each(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = f"FROM {IMAGE}:{TAG}@{PINNED}\nFROM {OTHER}:2@{OTHER_DIGEST}\n"
    fetch = Fetch(
        {IMAGE: (_token(), _manifest(UPSTREAM)), OTHER: (_token(), _manifest(OTHER_UPSTREAM))}
    )

    code, out, _ = _run(tmp_path, text, fetch, capsys)

    assert code == 1
    assert out.count("::error") == 2


# -- a repository fault is loud and never touches the network ------------


def _d(n: int) -> str:
    return "sha256:" + "a" * n


_FAULTS = {
    "no_digest_anywhere": "FROM node:24-alpine AS build\nFROM nginx:1.30\n",
    "no_from": "RUN true\n",
    "empty": "",
    "digest_without_tag": f"FROM {IMAGE}@{PINNED}\n",
    "digest_63": f"FROM {IMAGE}:{TAG}@{_d(63)}\n",
    "digest_65": f"FROM {IMAGE}:{TAG}@{_d(65)}\n",
    "digest_uppercase": f"FROM {IMAGE}:{TAG}@sha256:{'A' * 64}\n",
    "digest_wrong_algorithm": f"FROM {IMAGE}:{TAG}@sha512:{'a' * 64}\n",
    "arg_in_name": f"ARG BASE\nFROM ${{BASE}}:{TAG}@{PINNED}\n",
    "arg_in_tag": f"FROM {IMAGE}:${{TAG}}@{PINNED}\n",
    "foreign_registry": f"FROM ghcr.io/{IMAGE}:{TAG}@{PINNED}\n",
    "registry_with_port": f"FROM localhost:5000/{IMAGE}:{TAG}@{PINNED}\n",
    "localhost_registry": f"FROM localhost/{IMAGE}:{TAG}@{PINNED}\n",
    "fault_beside_valid_pin": f"FROM {IMAGE}:{TAG}@{PINNED}\nFROM ghcr.io/x/y:1@{PINNED}\n",
}


@pytest.mark.parametrize("text", list(_FAULTS.values()), ids=list(_FAULTS))
def test_a_repository_fault_exits_2_without_contacting_the_registry(
    text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code, out, err = _run(tmp_path, text, fetch, capsys)

    assert (code, out) == (2, "")
    assert err.count("\n") == 1 and "Traceback" not in err
    assert fetch.calls == []


def test_a_missing_file_exits_2_without_contacting_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code = check.main([str(tmp_path / "nope")], fetch=fetch)

    out = capsys.readouterr()
    assert (code, out.out) == (2, "")
    assert out.err.count("\n") == 1
    assert fetch.calls == []


def test_an_unreadable_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code = check.main([str(tmp_path)], fetch=fetch)  # a directory cannot be read as text

    assert (code, capsys.readouterr().out) == (2, "")
    assert fetch.calls == []


def test_explicit_docker_io_host_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(PINNED))

    code, out, err = _run(tmp_path, f"FROM docker.io/{IMAGE}:{TAG}@{PINNED}\n", fetch, capsys)

    assert (code, out, err) == (0, "", "")
    assert fetch.calls[1][1].endswith(f"/v2/{IMAGE}/manifests/{TAG}")


# -- official images live under library/ ---------------------------------

NODE_D = "sha256:" + "1" * 64
NODE_UP = "sha256:" + "2" * 64
PY_D = "sha256:" + "3" * 64
PG_D = "sha256:" + "4" * 64


@pytest.mark.parametrize(
    "written", ["node", "library/node", "docker.io/node", "docker.io/library/node"]
)
def test_official_image_names_map_to_library_in_the_token_scope_and_manifest_url(
    written: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = Fetch({"library/node": (_token(), _manifest(NODE_D))})

    code, out, err = _run(tmp_path, f"FROM {written}:24-alpine@{NODE_D}\n", fetch, capsys)

    assert (code, out, err) == (0, "", "")
    (_, token_url, _, _), (_, manifest_url, _, _) = fetch.calls
    assert "scope=repository:library/node:pull" in token_url
    assert manifest_url == "https://registry-1.docker.io/v2/library/node/manifests/24-alpine"


@pytest.mark.parametrize("written", [IMAGE, f"docker.io/{IMAGE}"])
def test_a_two_component_name_is_used_unchanged(
    written: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(PINNED))

    _run(tmp_path, f"FROM {written}:{TAG}@{PINNED}\n", fetch, capsys)

    assert f"scope=repository:{IMAGE}:pull" in fetch.calls[0][1]
    assert fetch.calls[1][1] == f"https://registry-1.docker.io/v2/{IMAGE}/manifests/{TAG}"


def test_the_replacement_uses_the_name_as_written_not_library_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = Fetch({"library/node": (_token(), _manifest(NODE_UP))})

    code, out, _ = _run(tmp_path, f"FROM node:24-alpine@{NODE_D} AS build\n", fetch, capsys)

    assert code == 1
    assert f"node:24-alpine@{NODE_UP}" in out
    assert f"library/node:24-alpine@{NODE_UP}" not in out


@pytest.mark.parametrize("registry", ["ghcr.io/x/y", "localhost:5000/x", "localhost/x/y", "my.reg/x"])
def test_a_registry_other_than_docker_hub_is_a_fault_with_no_fetch(
    registry: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code, out, err = _run(tmp_path, f"FROM {registry}:1@{PINNED}\n", fetch, capsys)

    assert (code, out) == (2, "")
    assert err.count("\n") == 1
    assert fetch.calls == []


# -- Compose files: line-based parsing -------------------------------------


def _compose_refs(text: str) -> list[tuple[str, str, str, int]]:
    return [(r.name, r.tag, r.digest, r.line) for r in check.pinned_compose_references(text)]


@pytest.mark.parametrize(
    "line",
    [
        f"    image: {IMAGE}:{TAG}@{PINNED}",
        f"    image: {IMAGE}:{TAG}@{PINNED}   # >=18.6 (architecture.md 16.1)",
        f'    image: "{IMAGE}:{TAG}@{PINNED}"',
        f"    image: '{IMAGE}:{TAG}@{PINNED}'  # note",
    ],
    ids=["plain", "trailing_comment", "double_quoted", "single_quoted_comment"],
)
def test_compose_image_forms_are_found_without_quotes_or_comment(line: str) -> None:
    assert _compose_refs(f"services:\n  x:\n{line}\n") == [(IMAGE, TAG, PINNED, 3)]


def test_compose_commented_out_and_unpinned_images_are_skipped() -> None:
    text = (
        f"    # image: {IMAGE}:{TAG}@{PINNED}\n"
        "    image: ytdigest-backend:latest\n"
        f"    image: {OTHER}:2@{OTHER_DIGEST}\n"
    )

    assert _compose_refs(text) == [(OTHER, "2", OTHER_DIGEST, 3)]


@pytest.mark.parametrize(
    "text",
    [
        f"    image: ${{IMG}}:{TAG}@{PINNED}\n",
        f"    image: {IMAGE}:${{TAG}}@{PINNED}\n",
        f"    image: {IMAGE}@{PINNED}\n",
        "    image: ytdigest-backend:latest\n",
        "services: {}\n",
    ],
    ids=["var_in_name", "var_in_tag", "digest_without_tag", "no_pinned_image", "no_image_at_all"],
)
def test_compose_faults_exit_2_without_contacting_the_registry(
    text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code, out, err = _run_files(tmp_path, {"compose.yml": text}, fetch, capsys)

    assert (code, out) == (2, "")
    assert err.count("\n") == 1 and "compose.yml" in err
    assert fetch.calls == []


@pytest.mark.parametrize("name", ["compose.yml", "compose.yaml"])
def test_yml_and_yaml_paths_are_read_as_compose(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run_files(tmp_path, {name: COMPOSE}, _fetch(_manifest(PINNED)), capsys)

    assert (code, out, err) == (0, "", "")


def test_a_stale_compose_image_names_file_line_and_the_full_image_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run_files(tmp_path, {"compose.yml": COMPOSE}, _fetch(_manifest(UPSTREAM)), capsys)

    assert code == 1
    (line,) = out.splitlines()
    assert f"::error file={tmp_path / 'compose.yml'},line=3,title=" in line
    assert f"image: {IMAGE}:{TAG}@{UPSTREAM}" in line
    assert PINNED in line


# -- several files in one run ---------------------------------------------


def test_no_argument_exits_2_with_a_usage_line(capsys: pytest.CaptureFixture[str]) -> None:
    fetch = _fetch(_manifest(UPSTREAM))

    code = check.main([], fetch=fetch)

    out = capsys.readouterr()
    assert (code, out.out) == (2, "")
    assert out.err.count("\n") == 1 and "usage" in out.err.lower()
    assert fetch.calls == []


def test_a_fault_in_any_file_stops_everything_before_any_fetch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _fetch(_manifest(UPSTREAM))
    files = {"Dockerfile": DOCKERFILE, "compose.yml": "services: {}\n"}

    code, out, err = _run_files(tmp_path, files, fetch, capsys)

    assert (code, out) == (2, "")
    assert err.count("\n") == 1 and str(tmp_path / "compose.yml") in err
    assert str(tmp_path / "Dockerfile") not in err
    assert fetch.calls == []


def test_each_repository_and_tag_is_looked_up_once_but_every_reference_is_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = f"FROM python:3.14-slim@{PY_D} AS a\nFROM python:3.14-slim@{PY_D}\n"
    compose = f"    image: python:3.14-slim@{PY_D}\n    image: postgres:18-alpine@{PG_D}\n"
    fetch = Fetch(
        {
            "library/python": (_token(), _manifest(NODE_UP)),
            "library/postgres": (_token(), _manifest(PG_D)),
        }
    )

    files = {"Dockerfile": dockerfile, "compose.yml": compose}
    code, out, _ = _run_files(tmp_path, files, fetch, capsys)

    assert [c[0] for c in fetch.calls].count("HEAD") == 2
    assert code == 1
    assert out.count("::error") == 3  # the three python references, each on its own
    assert out.count("line=1,") == 2 and out.count("line=2,") == 1


def test_a_stale_dockerfile_and_a_failed_compose_lookup_give_exit_1_one_annotation_one_stderr_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = f"    image: {OTHER}:2@{OTHER_DIGEST}\n"
    fetch = Fetch({IMAGE: (_token(), _manifest(UPSTREAM)), OTHER: (_token(), _manifest(None, 503))})

    files = {"Dockerfile": DOCKERFILE, "compose.yml": compose}
    code, out, err = _run_files(tmp_path, files, fetch, capsys)

    assert code == 1
    assert out.count("::error") == 1 and err.count("\n") == 1 and OTHER in err


# -- coverage guard: the real repository files (no network) -----------------


def _real(path: str) -> list[object]:
    text = (REPO_ROOT / path).read_text()
    if path.endswith((".yml", ".yaml")):
        return list(check.pinned_compose_references(text))
    return list(check.pinned_references(text))


def test_the_repository_pins_exactly_the_seven_expected_references() -> None:
    refs = [r for path in CHECKED_FILES for r in _real(path)]

    found = sorted((r.name, r.tag) for r in refs)  # type: ignore[attr-defined]
    assert found == sorted(
        [(IMAGE, TAG)]
        + [("library/node", "24-alpine")] * 2
        + [("library/python", "3.14-slim")] * 3
        + [("library/postgres", "18-alpine")]
    )
    digests: dict[tuple[str, str], set[str]] = {}
    for r in refs:
        digests.setdefault((r.name, r.tag), set()).add(r.digest)  # type: ignore[attr-defined]
    assert all(len(d) == 1 for d in digests.values()), digests


def _from_images(text: str) -> list[str]:
    images, stages = [], set()
    for line in text.splitlines():
        tokens = line.split("#", 1)[0].split()
        if tokens and tokens[0].upper() == "FROM":
            rest = [t for t in tokens[1:] if not t.startswith("--")]
            images.append(rest[0])
            if len(rest) >= 3 and rest[1].upper() == "AS":
                stages.add(rest[2])
    return [i for i in images if i not in stages]


def _compose_images(text: str) -> list[str]:
    return re.findall(r"^\s*image:\s*[\"']?([^\s\"'#]+)", text, re.MULTILINE)


def test_every_upstream_base_image_is_digest_pinned_and_its_file_is_checked() -> None:
    images: dict[str, list[str]] = {}
    for dockerfile in OPS.glob("Dockerfile*"):
        if dockerfile.suffix != ".dockerignore":
            images[dockerfile.relative_to(REPO_ROOT).as_posix()] = _from_images(dockerfile.read_text())
    for compose in REPO_ROOT.glob("compose*.yml"):
        images[compose.name] = _compose_images(compose.read_text())

    command = re.search(r"scripts/base_image_digest_check\.py (.+)$", _workflow(), re.MULTILINE)
    assert command
    checked = command.group(1).split()
    for path, refs in images.items():
        for ref in (i for i in refs if not i.startswith("ytdigest-")):
            assert "@sha256:" in ref, f"{path}: {ref} is not digest-pinned"
        if any("@sha256:" in ref for ref in refs):
            assert path in checked, f"{path} has a pin but the workflow does not check it"


# -- the scheduled workflow ----------------------------------------------


def _workflow() -> str:
    return WORKFLOW.read_text()


def test_workflow_runs_daily_and_by_hand_only() -> None:
    text = _workflow()

    assert re.search(r"^on:\s*$", text, re.MULTILINE)
    assert re.search(r"^\s+schedule:\s*\n\s+- cron: ['\"]\S+ \S+ \* \* \*['\"]", text, re.MULTILINE)
    assert "workflow_dispatch:" in text
    assert "pull_request" not in text and "push:" not in text


def test_workflow_cron_minute_differs_from_the_ytdlp_check() -> None:
    pattern = r"cron: ['\"](\S+ \S+ \* \* \*)['\"]"
    mine = re.search(pattern, _workflow())
    theirs = re.search(pattern, YTDLP_WORKFLOW.read_text())

    assert mine and theirs
    assert mine.group(1).split()[0] != theirs.group(1).split()[0]


def test_workflow_is_read_only_secretless_and_time_boxed() -> None:
    text = _workflow()

    assert re.search(r"^permissions:\s*\n\s+contents: read\s*$", text, re.MULTILINE)
    assert "secrets." not in text
    assert re.search(r"timeout-minutes: \d+", text)
    assert "needs:" not in text


def test_workflow_uses_only_first_party_actions_pinned_to_a_sha_already_trusted() -> None:
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", _workflow(), re.MULTILINE)
    trusted: set[str] = set()
    for other in WORKFLOW.parent.glob("*.yml"):
        if other != WORKFLOW:
            trusted.update(re.findall(r"uses:\s*(\S+)", other.read_text()))

    assert uses
    for ref, rest in uses:
        assert re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", ref), ref
        assert "#" in rest
        assert ref in trusted, f"{ref} is not already used in another workflow"


def test_workflow_runs_the_script_once_on_every_pinned_file() -> None:
    runs = re.findall(r"^\s+run:\s*(.+)$", _workflow(), re.MULTILINE)

    assert runs == ["python3 scripts/base_image_digest_check.py " + " ".join(CHECKED_FILES)]
