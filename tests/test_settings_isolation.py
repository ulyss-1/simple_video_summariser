"""The autouse ``_isolate_database_settings`` fixture (tests/conftest.py, #305).

An inner pytest session runs two tests in a forced order: A fills the
``get_settings()`` cache, B must find it empty. Without the fixture B fails,
which is the negative control. No Docker, no sleep.
"""

from __future__ import annotations

import pytest

_INNER = """
import os

from common.config import get_settings

DSN = "postgresql://u:p@localhost:5432/leaky"


def test_a_fills_the_settings_cache(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    assert get_settings().DATABASE_URL.get_secret_value() == DSN
    assert get_settings.cache_info().currsize == 1


def test_b_starts_with_no_settings_and_no_database_url():
    assert get_settings.cache_info().currsize == 0
    assert "DATABASE_URL" not in os.environ
"""

_ORDER = ("--no-shuffle", "-p", "tests.shuffle_plugin", "-p", "no:cacheprovider")


@pytest.fixture
def inner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    # An ambient DATABASE_URL, as on a developer machine; the fixture must remove it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/ambient")
    pytester.makepyfile(test_inner=_INNER)
    return pytester


def test_the_fixture_gives_the_next_test_a_clean_cache_and_environment(
    inner: pytest.Pytester,
) -> None:
    result = inner.runpytest(*_ORDER, "-p", "tests.conftest")

    result.assert_outcomes(passed=2)


def test_without_the_fixture_the_second_test_sees_the_leaked_cache(
    inner: pytest.Pytester,
) -> None:
    # Negative control: no tests.conftest, so nothing clears the cache between A and B.
    result = inner.runpytest(*_ORDER)

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*test_b_starts_with_no_settings_and_no_database_url*"])
