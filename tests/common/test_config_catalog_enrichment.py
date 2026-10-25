"""CATALOG_ENRICH_INTERVAL_SEC and CATALOG_ENRICH_BATCH_SIZE (issue #242)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from common.config import Settings

DB = "postgresql://u:p@h/db"
NAMES = ["CATALOG_ENRICH_INTERVAL_SEC", "CATALOG_ENRICH_BATCH_SIZE"]


def _settings(**values: Any) -> Settings:
    return Settings(**{"DATABASE_URL": DB, **values})


def test_the_defaults_are_thirty_seconds_and_ten_rows() -> None:
    settings = _settings()

    assert (settings.CATALOG_ENRICH_INTERVAL_SEC, settings.CATALOG_ENRICH_BATCH_SIZE) == (30, 10)


def test_they_can_be_set_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATALOG_ENRICH_INTERVAL_SEC", "5")
    monkeypatch.setenv("CATALOG_ENRICH_BATCH_SIZE", "3")

    settings = Settings(DATABASE_URL=DB)  # type: ignore[arg-type]

    assert (settings.CATALOG_ENRICH_INTERVAL_SEC, settings.CATALOG_ENRICH_BATCH_SIZE) == (5, 3)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", [0, -1, "abc", "", 1.5])
def test_zero_negative_and_non_integer_values_are_rejected(name: str, bad: Any) -> None:
    with pytest.raises(ValidationError) as info:
        _settings(**{name: bad})

    assert name in str(info.value)


@pytest.mark.parametrize("name", NAMES)
def test_they_are_documented_in_the_architecture_config_table(name: str) -> None:
    text = (Path(__file__).resolve().parents[2] / "_docs" / "architecture.md").read_text("utf-8")

    assert any(line.startswith("|") and f"`{name}`" in line for line in text.splitlines())
