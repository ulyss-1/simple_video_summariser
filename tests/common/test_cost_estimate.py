"""The shared analysis cost estimator (issue #153): pure arithmetic with fake numbers."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from common.cost_estimate import estimate_cost_usd

REPO = Path(__file__).resolve().parents[2]


def test_ten_thousand_chars_at_two_micro_dollars_is_two_cents() -> None:
    assert estimate_cost_usd(10_000, 2e-6, 3) == pytest.approx(0.02)


def test_zero_chars_cost_zero() -> None:
    assert estimate_cost_usd(0, 2e-6, 3) == 0.0


@pytest.mark.parametrize("per_char", [None, math.nan, math.inf, -math.inf, -1e-6, 0.0])
def test_a_missing_or_unusable_per_char_figure_is_unknown(per_char: float | None) -> None:
    assert estimate_cost_usd(1000, per_char, 3) is None


@pytest.mark.parametrize("chars", [None, -1])
def test_a_missing_or_negative_length_is_unknown(chars: int | None) -> None:
    assert estimate_cost_usd(chars, 2e-6, 3) is None


@pytest.mark.parametrize("samples", [0, -1])
def test_no_samples_is_unknown(samples: int) -> None:
    assert estimate_cost_usd(1000, 2e-6, samples) is None


def test_an_overflowing_product_is_unknown() -> None:
    assert estimate_cost_usd(10**400, 1e300, 1) is None
    assert estimate_cost_usd(10**300, 1e300, 1) is None

