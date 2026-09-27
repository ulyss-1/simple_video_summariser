"""Alembic environment (architecture.md 6, 11.4; task #7, #71).

The DSN comes from ``common.config.get_settings().DATABASE_URL``, never from
``alembic.ini`` — the ini file carries no credentials. ``compose.yml`` sets
``DATABASE_URL`` with the ``postgresql://`` scheme, which SQLAlchemy would
otherwise resolve to the (uninstalled) psycopg2 driver, so it is rewritten to
``postgresql+psycopg://`` here. A URL already spelled with the psycopg
scheme is accepted unchanged.

Logging settings come from the same ``get_settings()`` call, never from
``alembic.ini``: the migrate command logs through
``configure_logging(get_settings())`` (#6) like every other service, so
``LOG_FORMAT=json`` gives JSON output. This also sidesteps the bug
``fileConfig`` had (#71): it disables every logger already instantiated in
the process that ``alembic.ini`` doesn't name, which broke logging for any
in-process caller (tests, and eventually a long-lived process that ever ran
a migration inline) the moment it imported a module with its own logger.

Migrations are hand-written SQL through ``op.execute(...)`` (architecture.md
6): ``target_metadata`` stays ``None`` and autogenerate is never used. This
module is only ever run by the ``alembic`` command; nothing in ``common/``,
``adapters/`` or ``services/`` imports it (AGENTS.md -> Rules).
"""

from __future__ import annotations

import logging
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

from common.config import get_settings
from common.logging import configure_logging

config = context.config


def _configure_process_logging() -> None:
    """Route this process's logging through ``configure_logging`` (#6, #71).

    Skipped when a programmatic caller sets
    ``config.attributes["configure_logger"] = False`` — Alembic's documented
    convention for a caller that has already configured logging itself and
    wants ``env.py`` to leave it alone.

    In offline mode (``--sql``), logs go to stderr so stdout stays pure SQL.
    """
    if config.attributes.get("configure_logger", True) is False:
        return
    stream = sys.stderr if context.is_offline_mode() else None
    configure_logging(get_settings(), stream=stream)
    # alembic.ini used to pin this logger at WARNING so a root level of INFO
    # (or a settings-configured root logger) wouldn't switch on SQLAlchemy's
    # own statement echo; keep that behaviour now that the ini's [loggers]
    # section is gone.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_configure_process_logging()

target_metadata = None


def _database_url() -> str:
    url = get_settings().DATABASE_URL.get_secret_value()
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    scheme = url.split("://", 1)[0] if "://" in url else url
    raise ValueError(
        "DATABASE_URL must use the postgresql:// or postgresql+psycopg:// "
        f"scheme for migrations, got: {scheme!r}"
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without opening a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
