"""yt-dlp-ejs and Deno companion checks (issue #369). No network: every fetch is canned."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ytdlp_update_check.py"
LOCK_SCRIPT = REPO_ROOT / "scripts" / "lock_requirements.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ytdlp-update-check.yml"
YT_DLP_RELEASE = "https://pypi.org/pypi/yt-dlp/2026.10.21/json"
DENO_LATEST = "https://api.github.com/repos/denoland/deno/releases/latest"
DIGEST = "b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load("ytdlp_update_check", SCRIPT)
lock = _load("lock_requirements", LOCK_SCRIPT)

REQS = "fastapi==0.1\nyt-dlp==2026.10.21\nyt-dlp-ejs==0.8.0\npsycopg==3.2\n"
DOCKERFILE = f"# deno\nFROM denoland/deno:bin-2.9.8@sha256:{DIGEST} AS deno\nFROM python:3.14-slim AS builder\n"
YT_DLP_DEFAULT_EXTRA = [
    'yt-dlp-ejs==0.8.0; extra == "default"',
    'deno>=2.9.8; extra == "deno"',
    'brotli; implementation_name == "cpython" and extra == "default"',
    'pycryptodomex; extra == "curl-cffi"',
    'deno==2.9.8; extra == "pin-deno"',
]


def _json(body: object) -> tuple[int, bytes]:
    return 200, json.dumps(body).encode()


class Router:
    """Canned answers by URL; an unexpected URL is recorded and refused."""

    def __init__(self, routes: dict[str, tuple[int, bytes] | Exception]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.unexpected: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        assert timeout > 0
        if url not in self.routes:
            self.unexpected.append(url)
            raise ConnectionError(f"no canned answer for {url}")
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _happy(**overrides: tuple[int, bytes] | Exception) -> Router:
    routes: dict[str, tuple[int, bytes] | Exception] = {
        "https://pypi.org/pypi/yt-dlp/json": _json({"info": {"version": "2026.10.21"}}),
        YT_DLP_RELEASE: _json({"info": {"requires_dist": YT_DLP_DEFAULT_EXTRA}}),
        DENO_LATEST: _json({"tag_name": "v2.9.8"}),
    }
    for key, value in overrides.items():
        routes[{"release": YT_DLP_RELEASE, "deno": DENO_LATEST}[key]] = value
    return Router(routes)


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fetch: Router,
    requirements: str = REQS,
    dockerfile: str = DOCKERFILE,
) -> tuple[int, str, str]:
    req = tmp_path / "requirements.backend.txt"
    docker = tmp_path / "Dockerfile.backend"
    req.write_text(requirements)
    docker.write_text(dockerfile)
    code = check.main([str(req), "--dockerfile", str(docker)], fetch=fetch)
    out = capsys.readouterr()
    assert req.read_text() == requirements and docker.read_text() == dockerfile  # never edits
    return code, out.out, out.err


# -- agreement is silent -----------------------------------------------------------


def test_matching_pins_and_current_deno_are_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(tmp_path, capsys, _happy())

    assert (code, out, err) == (0, "", "")


def test_the_companion_urls_are_fetched_only_when_a_dockerfile_is_given(tmp_path: Path) -> None:
    req = tmp_path / "requirements.backend.txt"
    req.write_text(REQS)
    fetch = _happy()

    assert check.main([str(req)], fetch=fetch) == 0
    assert fetch.urls == ["https://pypi.org/pypi/yt-dlp/json"]


# -- yt-dlp-ejs follows yt-dlp -----------------------------------------------------


def test_an_ejs_pin_that_differs_from_yt_dlp_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = REQS.replace("yt-dlp-ejs==0.8.0", "yt-dlp-ejs==0.7.1")

    code, out, _err = _run(tmp_path, capsys, _happy(), requirements=reqs)

    assert code == 1
    assert "::error title=yt-dlp-ejs pin does not match yt-dlp" in out
    assert "pinned yt-dlp-ejs==0.7.1" in out and "requires yt-dlp-ejs==0.8.0" in out


def test_an_ejs_requirement_with_a_range_is_checked_as_a_minimum(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extra = ['yt-dlp-ejs>=0.8.0; extra == "default"']
    fetch = _happy(release=_json({"info": {"requires_dist": extra}}))

    assert _run(tmp_path, capsys, fetch)[0] == 0
    newer = REQS.replace("yt-dlp-ejs==0.8.0", "yt-dlp-ejs==0.9.0")
    assert _run(tmp_path, capsys, fetch, requirements=newer)[0] == 0
    older = REQS.replace("yt-dlp-ejs==0.8.0", "yt-dlp-ejs==0.7.0")
    code, out, _err = _run(tmp_path, capsys, fetch, requirements=older)
    assert code == 1 and "::error title=yt-dlp-ejs" in out


# -- Deno meets yt-dlp's minimum ---------------------------------------------------


def test_a_deno_pin_below_the_minimum_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dockerfile = DOCKERFILE.replace("bin-2.9.8", "bin-2.9.7")

    code, out, _err = _run(tmp_path, capsys, _happy(), dockerfile=dockerfile)

    assert code == 1
    assert "::error title=Deno pin is below yt-dlp's minimum" in out
    assert "pinned deno 2.9.7" in out and "deno>=2.9.8" in out


def test_the_deno_minimum_is_read_from_the_deno_extra_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    default_only = _json({"info": {"requires_dist": ['deno>=2.10.0; extra == "default"']}})
    code, out, _err = _run(tmp_path, capsys, _happy(release=default_only), dockerfile=DOCKERFILE.replace("bin-2.9.8", "bin-2.9.7"))

    assert code == 0 and "::error" not in out


def test_a_deno_pin_at_or_above_the_minimum_is_not_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _run(tmp_path, capsys, _happy(), dockerfile=DOCKERFILE.replace("bin-2.9.8", "bin-2.10.0"))

    assert code == 0 and "::error" not in out


# -- Deno staleness is a notice ----------------------------------------------------


def test_a_newer_deno_release_is_a_notice_and_not_a_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fetch = _happy(deno=_json({"tag_name": "v2.10.1"}))

    code, out, _err = _run(tmp_path, capsys, fetch)

    assert code == 0
    assert "::notice title=Deno is behind its release::pinned deno 2.9.8, latest deno 2.10.1" in out
    assert "::error" not in out


# -- outages end quietly; repository faults are loud -------------------------------


@pytest.mark.parametrize("failure", [ConnectionError("down"), (503, b"")])
def test_a_yt_dlp_metadata_outage_is_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: tuple[int, bytes] | Exception
) -> None:
    fetch = _happy(release=failure)

    code, out, err = _run(tmp_path, capsys, fetch)

    assert code == 0 and out == ""
    assert "yt-dlp's metadata unavailable" in err
    assert "will retry on the next run" in err


def test_a_deno_outage_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(tmp_path, capsys, _happy(deno=TimeoutError("slow")))

    assert code == 0 and out == "" and "the Deno release unavailable" in err


@pytest.mark.parametrize(
    "reply",
    [
        (200, b"<html>not json</html>"),
        _json({"info": {}}),  # no requires_dist
        _json({"info": {"requires_dist": "yt-dlp-ejs==0.8.0"}}),  # not a list
    ],
)
def test_an_unusable_metadata_reply_is_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reply: tuple[int, bytes]
) -> None:
    code, out, err = _run(tmp_path, capsys, _happy(release=reply))

    assert code == 0 and out == "" and "yt-dlp's metadata unavailable" in err


def test_an_unusable_deno_tag_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(tmp_path, capsys, _happy(deno=_json({"tag_name": "2.10.0"})))

    assert code == 0 and out == "" and "the Deno release unavailable" in err


@pytest.mark.parametrize(
    ("requirements", "dockerfile", "missing"),
    [
        (REQS.replace("yt-dlp-ejs==0.8.0\n", ""), DOCKERFILE, "yt-dlp-ejs"),
        (REQS, DOCKERFILE.replace("FROM denoland/deno:bin-2.9.8@sha256:" + DIGEST, "FROM alpine"), "deno"),
    ],
)
def test_a_missing_companion_pin_is_a_repository_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], requirements: str, dockerfile: str, missing: str
) -> None:
    code, out, err = _run(tmp_path, capsys, _happy(), requirements=requirements, dockerfile=dockerfile)

    assert code == 2 and out == ""
    assert "cannot read the companion pins" in err and missing in err


def test_dockerfile_flag_without_a_path_is_a_fault(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    req = tmp_path / "requirements.backend.txt"
    req.write_text(REQS)

    assert check.main([str(req), "--dockerfile"], fetch=_happy()) == 2


def test_a_yt_dlp_pin_behind_pypi_still_fails_and_the_companions_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch = _happy()
    fetch.routes["https://pypi.org/pypi/yt-dlp/json"] = _json({"info": {"version": "2026.10.26"}})

    code, out, _err = _run(tmp_path, capsys, fetch)

    assert code == 1
    assert "::error title=yt-dlp pin is behind PyPI" in out
    assert "::error title=yt-dlp-ejs" not in out  # the pins themselves agree


# -- the lock report skips yt-dlp-ejs as it skips yt-dlp (#278, #369) -------------


def test_the_weekly_report_skips_yt_dlp_ejs() -> None:
    assert lock.YTDLP_EJS_EXCLUDED == "yt-dlp-ejs"
    assert lock._make_pin("requirements.backend.txt", 1, "yt-dlp-ejs==0.8.0", None) is None


def test_yt_dlp_ejs_cannot_be_held_either() -> None:
    with pytest.raises(lock.LockError, match="yt-dlp-ejs cannot be held"):
        lock._make_pin("requirements.backend.txt", 1, "yt-dlp-ejs==0.8.0", "waiting")


def test_other_direct_pins_are_still_reported() -> None:
    assert lock._make_pin("requirements.backend.txt", 1, "psycopg==3.2.1", None) is not None


# -- the workflow passes the Dockerfile ----------------------------------------------


def test_the_scheduled_workflow_checks_the_backend_dockerfile() -> None:
    text = WORKFLOW.read_text()

    assert re.search(r"run: python3 scripts/ytdlp_update_check\.py requirements\.backend\.txt --dockerfile ops/Dockerfile\.backend$", text, re.MULTILINE)


def test_the_repository_pins_satisfy_the_companion_rules() -> None:
    req = (REPO_ROOT / "requirements.backend.txt").read_text()
    docker = (REPO_ROOT / "ops" / "Dockerfile.backend").read_text()

    assert check.ejs_pin(req) and check.deno_pin(docker)
