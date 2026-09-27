"""Alembic environment. Migrations are hand-written SQL (database.md §13): autogenerate is not used."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings


def run_migrations_online() -> None:
    engine = create_engine(settings.database_url_owner_sync, poolclass=pool.NullPool)
    with engine.connect() as connection:
        # database.md §13: lock_timeout in every migration so the SOS path is never blocked.
        connection.exec_driver_sql("SET lock_timeout = '3s'")
        connection.commit()  # session setting kept; don't leave an autobegun tx that alembic would never commit
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
