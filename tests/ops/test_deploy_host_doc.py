"""Static consistency checks for ``_docs/deploy-host.md`` section 12 (#263).

Section 12 is a manual operator smoke check against real YouTube and the host
Ollama. It is never run here: these tests read repo files only and start no
process, no Docker and no network. Each check is a pure function over text that
returns a list of problems, shown failing on a crafted bad section before it is
asserted clean on the real file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from common.config import Settings
from common.errors import ErrorClass

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "_docs" / "deploy-host.md"
OPENAPI = ROOT / "web" / "openapi.json"
FIXTURE = ROOT / "tests" / "adapters" / "youtube" / "fixtures" / "metadata" / "manual_en.json"

H11 = "## 11. First deploy"
H12 = "## 12. Smoke check"
H_PUBLIC = "## Public access"
API_BASE = "http://127.0.0.1:8080/api/"
CALLS = (
    ("POST", "/videos"),
    ("GET", "/videos/{video_id}"),
    ("DELETE", "/videos/{video_id}"),
    ("GET", "/ops/jobs"),
)
STATUSES = ("done", "failed", "unavailable")
REQUIRED_ERROR_CLASSES = (
    "TRANSIENT_NETWORK",
    "RATE_LIMITED",
    "PERMANENT_SOURCE",
    "LLM_UNAVAILABLE",
    "LLM_INVALID_OUTPUT",
)
REQUIRED_WORDS = (
    "SUMMARIZER",
    "docker compose logs",
    "transcriber",
    "analyzer",
    "error_class",
    "last_error",
)
# UPPER_SNAKE tokens in the section that are settings, not error classes.
NOT_ERROR_CLASSES = {"OLLAMA_MODEL", "COMPOSE_FILE"}
FORBIDDEN = {
    "SUMMARIZER=anthropic": r"SUMMARIZER=anthropic",
    "ANTHROPIC_API_KEY": r"ANTHROPIC_API_KEY",
    "api.anthropic.com": r"api\.anthropic\.com",
    "cat .env": r"cat\s+\.env",
    "bare printenv/env": r"(?m)\bprintenv[ \t]*(?:$|[|;&)])|(?:^|[;&|(])[ \t]*env[ \t]*(?:$|[|;&)])",
    "unfiltered docker compose config": r"docker compose(?:\s+-f\s+\S+)?\s+config(?!\s*\|)",
    "jq": r"\bjq\b",
}


def _openapi() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(OPENAPI.read_text(encoding="utf-8"))
    return data


def _video_id() -> str:
    return str(json.loads(FIXTURE.read_text(encoding="utf-8"))["id"])


def slice_section(doc: str) -> str:
    """Text from the section 12 heading to the next ``## `` heading."""
    match = re.search(r"(?m)^## 12\. Smoke check$", doc)
    if match is None:
        return ""
    rest = doc[match.start() :]
    nxt = re.search(r"(?m)^## ", rest[3:])
    return rest if nxt is None else rest[: nxt.start() + 3]


def check_placement(doc: str) -> list[str]:
    problems: list[str] = []
    count = len(re.findall(r"(?m)^## 12\. Smoke check$", doc))
    if count != 1:
        problems.append(f"'{H12}' must appear exactly once, found {count}")
        return problems
    p11, p12, pub = doc.find(H11), doc.find(H12), doc.find(H_PUBLIC)
    if p11 < 0 or pub < 0 or not p11 < p12 < pub:
        problems.append(f"'{H12}' must sit between '{H11}' and '{H_PUBLIC}'")
    return problems


def check_video_id(section: str, video_id: str) -> list[str]:
    ids = re.findall(r"https://www\.youtube\.com/watch\?v=([^\s'\"`&)]+)", section)
    if not ids:
        return ["section has no https://www.youtube.com/watch?v=<id> URL"]
    if any(i != video_id for i in ids):
        return [f"watch URL id {sorted(set(ids))} differs from fixture id {video_id!r}"]
    return []


def check_api_calls(section: str, spec: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for method, path in CALLS:
        if method.lower() not in spec["paths"].get(path, {}):
            problems.append(f"{method} {path} is absent from web/openapi.json")
        if f"{method} /api{path}" not in section:
            problems.append(f"section does not reference `{method} /api{path}`")
    return problems


def check_urls(section: str, video_id: str) -> list[str]:
    watch = f"https://www.youtube.com/watch?v={video_id}"
    bad = []
    for url in re.findall(r"https?://[^\s'\"`)]+", section):
        if not (url == watch or url.startswith(API_BASE)):
            bad.append(url)
    return [f"URL outside the allowed set: {u}" for u in bad]


def check_statuses(section: str, spec: dict[str, Any]) -> list[str]:
    enum = spec["components"]["schemas"]["VideoDetail"]["properties"]["status"]["enum"]
    problems = []
    for status in STATUSES:
        if status not in enum:
            problems.append(f"{status!r} is not in the VideoStatus enum {enum}")
        if not re.search(rf"\b{status}\b", section):
            problems.append(f"section never mentions status {status!r}")
    return problems


def check_deadline(section: str, minimum_sec: int) -> list[str]:
    figures = re.findall(r"(\d+)\s+minutes", section)
    if len(figures) != 1:
        return [f"expected exactly one 'N minutes' deadline figure, found {figures}"]
    if int(figures[0]) * 60 < minimum_sec:
        return [f"deadline {figures[0]} minutes is below {minimum_sec} s of runtime caps"]
    return []


def check_error_classes(section: str) -> list[str]:
    members = {m.value for m in ErrorClass}
    named = set(re.findall(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b", section))
    named = {t for t in named if "MAX_RUNTIME" not in t and t not in NOT_ERROR_CLASSES}
    problems = [f"{t} is not a member of ErrorClass" for t in sorted(named - members)]
    problems += [f"triage does not name {t}" for t in REQUIRED_ERROR_CLASSES if t not in named]
    return problems


def check_forbidden(section: str) -> list[str]:
    return [f"forbidden content: {name}" for name, rx in FORBIDDEN.items() if re.search(rx, section)]


def check_required_words(section: str) -> list[str]:
    return [f"section lacks {w!r}" for w in REQUIRED_WORDS if w not in section]


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section(doc: str) -> str:
    return slice_section(doc)


@pytest.fixture(scope="module")
def caps_sec() -> int:
    settings = Settings.model_fields
    return int(settings["INGEST_MAX_RUNTIME_SEC"].default) + int(
        settings["ANALYZE_MAX_RUNTIME_SEC"].default
    )


# --- the real file -------------------------------------------------------


def test_section_is_not_vacuous(section: str) -> None:
    assert section, f"{H12} not found in {DOC}"
    assert "curl" in section
    assert "-X POST" in section
    assert "-H 'Content-Type: application/json'" in section
    assert '{"url": "https://www.youtube.com/watch?v=' in section


def test_intro_and_update_paragraph_point_at_the_smoke_check(doc: str) -> None:
    flat = " ".join(doc.split())
    assert "then run the smoke check (§12)." in flat
    assert "re-run §12" in flat


def test_real_section_placement(doc: str) -> None:
    assert check_placement(doc) == []


def test_real_section_video_id(section: str) -> None:
    assert check_video_id(section, _video_id()) == []


def test_real_section_api_calls(section: str) -> None:
    assert check_api_calls(section, _openapi()) == []


def test_real_section_urls(section: str) -> None:
    assert check_urls(section, _video_id()) == []


def test_real_section_statuses(section: str) -> None:
    assert check_statuses(section, _openapi()) == []


def test_real_section_deadline(section: str, caps_sec: int) -> None:
    assert check_deadline(section, caps_sec) == []


def test_real_section_error_classes(section: str) -> None:
    assert check_error_classes(section) == []


def test_real_section_forbidden(section: str) -> None:
    assert check_forbidden(section) == []


def test_real_section_required_words(section: str) -> None:
    assert check_required_words(section) == []


# --- each check fails on a crafted bad input ------------------------------

GOOD_DOC = f"{H11}\nx\n{H12}\nbody\n---\n{H_PUBLIC} (Tunnel)\n"


@pytest.mark.parametrize(
    ("doc_text", "needle"),
    [
        (GOOD_DOC.replace(H12, "## 12. Other"), "exactly once"),
        (GOOD_DOC + f"{H12}\n", "exactly once"),
        (f"{H12}\n{H11}\n{H_PUBLIC}\n", "between"),
        (f"{H11}\n{H_PUBLIC}\n{H12}\n", "between"),
    ],
)
def test_placement_check_fails(doc_text: str, needle: str) -> None:
    assert any(needle in p for p in check_placement(doc_text))


def test_placement_check_passes_on_good_doc() -> None:
    assert check_placement(GOOD_DOC) == []
    assert slice_section(GOOD_DOC).startswith(H12)
    assert H_PUBLIC not in slice_section(GOOD_DOC)


def test_video_id_check_fails() -> None:
    assert check_video_id("no url", "abc")
    assert check_video_id("https://www.youtube.com/watch?v=zzz", "abc")
    assert check_video_id("https://www.youtube.com/watch?v=abc", "abc") == []


def test_api_calls_check_fails() -> None:
    spec = _openapi()
    assert len(check_api_calls("nothing here", spec)) == len(CALLS)
    assert any("absent" in p for p in check_api_calls("x", {"paths": {}}))
    full = " ".join(f"{m} /api{p}" for m, p in CALLS)
    assert check_api_calls(full, spec) == []


def test_urls_check_fails() -> None:
    watch = "https://www.youtube.com/watch?v=abc"
    assert check_urls(f"{watch} {API_BASE}videos", "abc") == []
    assert check_urls("curl https://dev-ytdigest.devopsdojo.click/api/videos", "abc")
    assert check_urls("curl http://127.0.0.1:8080/videos", "abc")


def test_statuses_check_fails() -> None:
    spec = _openapi()
    assert check_statuses("done failed", spec)
    assert check_statuses("done failed unavailable", spec) == []
    broken = {"components": {"schemas": {"VideoDetail": {"properties": {"status": {"enum": ["done"]}}}}}}
    assert check_statuses("done failed unavailable", broken)


def test_deadline_check_fails() -> None:
    assert check_deadline("no figure", 4200)
    assert check_deadline("70 minutes and 80 minutes", 4200)
    assert check_deadline("60 minutes", 4200)
    assert check_deadline("70 minutes", 4200) == []


def test_error_class_check_fails() -> None:
    good = " ".join(REQUIRED_ERROR_CLASSES)
    assert check_error_classes(good) == []
    assert check_error_classes(good + " NOT_A_CLASS")
    assert check_error_classes("TRANSIENT_NETWORK RATE_LIMITED")


@pytest.mark.parametrize(
    "bad",
    [
        "SUMMARIZER=anthropic docker compose up",
        "export ANTHROPIC_API_KEY=x",
        "curl https://api.anthropic.com",
        "cat .env",
        "printenv",
        "docker compose exec analyzer printenv | sort",
        "env",
        "x=$(env)",
        "docker compose config",
        "curl x | jq .",
    ],
)
def test_forbidden_check_fails(bad: str) -> None:
    assert check_forbidden(bad), bad


@pytest.mark.parametrize(
    "ok",
    [
        "docker compose exec analyzer printenv SUMMARIZER OLLAMA_MODEL",
        "docker compose config | grep -E 'type: bind'",
        "environment variables",
    ],
)
def test_forbidden_check_allows(ok: str) -> None:
    assert check_forbidden(ok) == []


def test_required_words_check_fails() -> None:
    assert len(check_required_words("")) == len(REQUIRED_WORDS)
    assert check_required_words(" ".join(REQUIRED_WORDS)) == []
