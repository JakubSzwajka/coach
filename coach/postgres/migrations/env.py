"""Alembic environment for Garmin Coach PostgreSQL migrations."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from coach.postgres.config import DatabaseSettings

config = context.config
target_metadata = None


def _settings() -> DatabaseSettings:
    configured = config.attributes.get("database_settings")
    if isinstance(configured, DatabaseSettings):
        return configured
    return DatabaseSettings.from_env()


def run_migrations_offline() -> None:
    context.configure(
        url=_settings().sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _settings().sqlalchemy_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
