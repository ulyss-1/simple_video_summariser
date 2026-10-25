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
FRONTEND_DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile.frontend"


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
        repo = next(r for r in self.answers if f"/{r}/" in url or f"repository:{r}:" in url)
        token, manifest = self.answers[repo]
        answer = token if "auth.docker.io" in url else manifest
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetch(manifest: Response | Exception, token: Response | Exception | None = None) -> Fetch:
    return Fetch({IMAGE: (token or _token(), manifest)})


def _run(
    tmp_path: Path, text: str, fetch: Fetch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    path = tmp_path / "Dockerfile"
    path.write_bytes(text.encode())
    code = check.main([str(path)], fetch=fetch)
    out = capsys.readouterr()
    assert path.read_bytes() == text.encode()  # the check reports; it never edits
    return code, out.out, out.err


# -- the parser ----------------------------------------------------------


def test_parser_finds_only_digest_pinned_froms_and_tolerates_flags_and_as() -> None:
    text = (
        "FROM node:24-alpine AS build\n"
        f"FROM --platform=linux/amd64 {IMAGE}:{TAG}@{PINNED} AS runtime\n"
    )

    refs = check.pinned_references(text)

    assert [(r.name, r.tag, r.digest) for r in refs] == [(IMAGE, TAG, PINNED)]


def test_parser_reads_the_real_frontend_dockerfile_without_network() -> None:
    refs = check.pinned_references(FRONTEND_DOCKERFILE.read_text())

    assert [(r.name, r.tag) for r in refs] == [(IMAGE, TAG)]


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
    assert len(lines) == 1 and lines[0].startswith("::error title=")
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
    "official_node": f"FROM node:24-alpine@{PINNED}\n",
    "official_python": f"FROM python:3.14-slim@{PINNED}\n",
    "fault_beside_valid_pin": f"FROM {IMAGE}:{TAG}@{PINNED}\nFROM node:24@{PINNED}\n",
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


def test_workflow_runs_the_script_on_the_frontend_dockerfile() -> None:
    assert "python3 scripts/base_image_digest_check.py ops/Dockerfile.frontend" in _workflow()
