"""Alembic environment.

URL resolution order: an explicit ``sqlalchemy.url`` set programmatically
(used by the test suite), then ``DATABASE_URL`` from the environment/.env.
"""

from __future__ import annotations

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; cannot run migrations")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool,
                           connect_args={"application_name": "integrationlab-migrate"})
    with engine.connect() as connection:
        # Serialise concurrent `alembic upgrade` runs (e.g. several containers starting).
        connection.exec_driver_sql("SELECT pg_advisory_lock(726345001)")
        try:
            context.configure(connection=connection, target_metadata=target_metadata,
                              compare_type=True, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.exec_driver_sql("SELECT pg_advisory_unlock(726345001)")
            connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
