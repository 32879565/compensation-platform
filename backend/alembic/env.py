from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  确保所有模型注册进 metadata
from alembic import context
from app.core.config import get_settings
from app.db.alembic_url import escape_alembic_config_value
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    escape_alembic_config_value(get_settings().database_url),
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Integration tests and deployment tooling can pass an idle live
    # connection through Alembic's standard config attribute.
    # Keep it separate from the normal engine path so production startup still
    # obtains its URL exclusively from application configuration.
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        # An Alembic autocommit block must commit the current transaction
        # before it can run DDL such as CREATE INDEX CONCURRENTLY.  Never let
        # that transition commit a transaction owned by the caller (for
        # example an ``engine.begin()`` context); require an idle connection
        # so the migration context owns every transaction it creates.
        if supplied_connection.in_transaction():
            raise RuntimeError(
                "Alembic cannot use a supplied connection with an active transaction; "
                "pass an idle connection or let Alembic create its own connection."
            )
        try:
            context.configure(
                connection=supplied_connection,
                target_metadata=target_metadata,
                compare_type=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()

            # A no-op migration still queries alembic_version, which autobegins
            # a transaction without a revision step to close it.  The idle-entry
            # guard makes that residual transaction Alembic-owned and safe to end.
            if supplied_connection.in_transaction():
                supplied_connection.commit()
        except BaseException:
            if supplied_connection.in_transaction():
                supplied_connection.rollback()
            raise
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
