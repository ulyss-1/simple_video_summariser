"""Analyzer startup with SUMMARIZER=free and the local fallback (#392): log fields, warning, probe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adapters.summarize.ollama import HttpResponse
from common.config import Settings
from services.analyzer import main as analyzer_main
from tests.services.analyzer.fake_db import FakeDatabase
from tests.services.analyzer.fakes import FakeSummarizer
from tests.services.analyzer.test_main import (
    _isolated_process_state,  # noqa: F401 - autouse fixture shared with test_main
    log_lines,
    make_settings,
    spy_handler_factory,
    worker_options,
)


class FreeFake(FakeSummarizer):
    """What main reads from the free-tier adapter."""

    provider_names = ("groq", "local-ollama")
    local_model = "gemma4:e4b"


class Probe:
    def __init__(self, result: HttpResponse | BaseException) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, url: str, timeout: float) -> HttpResponse:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def run(tmp_path: Path, settings: Settings, probe: Probe) -> int:
    handler_factory, _ = spy_handler_factory()
    return analyzer_main.main(
        settings=settings,
        connect=FakeDatabase().connect,
        summarizer_factory=lambda s: FreeFake(name="free"),
        handler_factory=handler_factory,
        worker_options=worker_options(tmp_path),
        max_iterations=1,
        probe_transport=probe,
    )


def free(**values: Any) -> Settings:
    return make_settings(SUMMARIZER="free", GROQ_API_KEY="k", **values)


def named(lines: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [line for line in lines if line["event"] == event]


def test_flag_on_logs_fields_probes_the_local_model_and_warns_on_small_context(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = json.dumps({"models": [{"name": "gemma4:e4b"}]}).encode()
    probe = Probe(HttpResponse(200, body))
    assert run(tmp_path, free(FREE_LLM_LOCAL_FALLBACK=True, OLLAMA_NUM_CTX=8192), probe) == 0
    lines = log_lines(capsys)
    [started] = named(lines, "analyzer.started")
    assert started["free_local_fallback"] is True
    assert started["free_providers"] == ["groq", "local-ollama"]
    [ready] = named(lines, "analyzer.ollama_ready")
    assert ready["model"] == "gemma4:e4b"
    [warn] = named(lines, "free_tier_local_context_small")
    assert warn["level"] == "warning"


def test_unreachable_probe_does_not_stop_the_worker(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(tmp_path, free(FREE_LLM_LOCAL_FALLBACK=True, OLLAMA_NUM_CTX=16384), Probe(OSError("x")))
    lines = log_lines(capsys)
    assert code == 0
    assert named(lines, "analyzer.ollama_unreachable")
    assert not named(lines, "free_tier_local_context_small")


def test_flag_off_logs_false_and_does_not_probe(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    probe = Probe(OSError("never"))
    run(tmp_path, free(), probe)
    [started] = named(log_lines(capsys), "analyzer.started")
    assert started["free_local_fallback"] is False
    assert probe.calls == 0
