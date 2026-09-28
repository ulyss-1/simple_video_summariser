"""``build_summarizer`` (issue #33): backend selection with no network and no I/O beyond prompts."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from adapters.summarize.factory import SummarizerConfigError, build_summarizer
from common.config import Settings

FAKE_KEY = "sk-ant-fake-for-tests"


def make_settings(**values: Any) -> Settings:
    # ``Any`` lets plain strings stand in for the SecretStr fields.
    return Settings(**{"DATABASE_URL": "postgresql://u:p@localhost/db", **values})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SUMMARIZER", "ANTHROPIC_API_KEY", "OLLAMA_HOST", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("build_summarizer made a network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_ollama_backend_is_built_for_ollama() -> None:
    settings = make_settings(SUMMARIZER="ollama", OLLAMA_MODEL="qwen-x")

    summarizer = build_summarizer(settings)

    assert summarizer.name == "ollama"
    assert summarizer.model == "qwen-x"


def test_anthropic_backend_is_built_for_anthropic() -> None:
    settings = make_settings(
        SUMMARIZER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY, ANTHROPIC_MODEL="claude-x"
    )

    summarizer = build_summarizer(settings)

    assert summarizer.name == "anthropic"
    assert summarizer.model == "claude-x"


@pytest.mark.parametrize("backend", ["ollama", "anthropic"])
def test_returned_name_equals_the_summarizer_setting(backend: str) -> None:
    settings = make_settings(SUMMARIZER=backend, ANTHROPIC_API_KEY=FAKE_KEY)

    assert build_summarizer(settings).name == settings.SUMMARIZER


@pytest.mark.usefixtures("no_network")
def test_building_ollama_with_an_unreachable_host_makes_no_network_call() -> None:
    settings = make_settings(SUMMARIZER="ollama", OLLAMA_HOST="http://192.0.2.1:9")

    assert build_summarizer(settings).name == "ollama"


@pytest.mark.usefixtures("no_network")
def test_building_anthropic_with_a_fake_key_makes_no_network_call() -> None:
    settings = make_settings(SUMMARIZER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)

    assert build_summarizer(settings).name == "anthropic"


@pytest.mark.parametrize("key", [None, "", "   ", "\t\n"])
def test_anthropic_without_a_usable_key_fails_naming_the_variable(key: str | None) -> None:
    settings = make_settings(SUMMARIZER="anthropic", ANTHROPIC_API_KEY=key)

    with pytest.raises(SummarizerConfigError) as raised:
        build_summarizer(settings)

    assert "ANTHROPIC_API_KEY" in str(raised.value)


def test_ollama_does_not_need_an_api_key() -> None:
    settings = make_settings(SUMMARIZER="ollama", ANTHROPIC_API_KEY=None)

    assert build_summarizer(settings).name == "ollama"
