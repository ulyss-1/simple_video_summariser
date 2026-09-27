"""Alembic environment (architecture.md 6, 11.4; task #7).

The DSN comes from ``common.config.get_settings().DATABASE_URL``, never from
``alembic.ini`` — the ini file carries no credentials. ``compose.yml`` sets
``DATABASE_URL`` with the ``postgresql://`` scheme, which SQLAlchemy would
otherwise resolve to the (uninstalled) psycopg2 driver, so it is rewritten to
``postgresql+psycopg://`` here. A URL already spelled with the psycopg
scheme is accepted unchanged.

Migrations are hand-written SQL through ``op.execute(...)`` (architecture.md
6): ``target_metadata`` stays ``None`` and autogenerate is never used. This
module is only ever run by the ``alembic`` command; nothing in ``common/``,
``adapters/`` or ``services/`` imports it (AGENTS.md -> Rules).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from common.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
