"""Unit tests for ``analyze_dedupe_key`` (issue #37). No database."""

from __future__ import annotations

import pytest

from common.queue import analyze_dedupe_key


def test_key_joins_prompt_version_and_summarizer_name_with_a_colon() -> None:
    assert analyze_dedupe_key("v3", "ollama") == "v3:ollama"


@pytest.mark.parametrize(
    ("prompt_version", "summarizer_name"),
    [("", "ollama"), ("v1", ""), ("", ""), ("v1:x", "ollama"), ("v1", "a:b"), ("v1", ":")],
)
def test_empty_or_colon_containing_parts_raise_value_error(
    prompt_version: str, summarizer_name: str
) -> None:
    with pytest.raises(ValueError):
        analyze_dedupe_key(prompt_version, summarizer_name)


def test_keyword_arguments_use_the_documented_names() -> None:
    assert analyze_dedupe_key(prompt_version="v1", summarizer_name="anthropic") == "v1:anthropic"
