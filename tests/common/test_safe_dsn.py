"""``safe_dsn`` renders a Postgres DSN without its password (issue #207)."""

from __future__ import annotations

import pytest

from common.safe_url import DSN_PASSWORD_PLACEHOLDER, safe_dsn

PW = "SENTINEL-pw"


def test_password_is_replaced_by_the_placeholder() -> None:
    shown = safe_dsn(f"postgresql://user:{PW}@db.example:5433/app")
    assert shown == f"postgresql://user:{DSN_PASSWORD_PLACEHOLDER}@db.example:5433/app"


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_both_postgres_schemes_are_accepted(scheme: str) -> None:
    assert safe_dsn(f"{scheme}://u@h/db") == f"{scheme}://u@h/db"


def test_query_string_secrets_are_dropped() -> None:
    shown = safe_dsn(f"postgresql://u:x@h/db?password={PW}&sslmode=require")
    assert PW not in shown
    assert "?" not in shown


@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://user:{PW}@[bad/db",  # QA's malformed input
        f"postgresql://user:{PW}@h:notaport/db",
        f"http://user:{PW}@h/db",
        f"host=h password={PW}",
        "",
    ],
)
def test_unparseable_input_gives_the_placeholder_without_the_password(dsn: str) -> None:
    shown = safe_dsn(dsn)
    assert shown == "<invalid DSN>"
    assert PW not in shown


def test_special_characters_in_the_password_do_not_survive() -> None:
    shown = safe_dsn("postgresql://user:p%40ss%2Fw%3Ard%23@h/db")
    assert shown == f"postgresql://user:{DSN_PASSWORD_PLACEHOLDER}@h/db"


def test_ipv6_host_keeps_its_brackets() -> None:
    assert safe_dsn("postgresql://u:x@[::1]:5432/db") == (
        f"postgresql://u:{DSN_PASSWORD_PLACEHOLDER}@[::1]:5432/db"
    )
