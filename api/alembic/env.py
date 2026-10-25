"""Alembic environment. Migrations are raw SQL. SQLAlchemy + psycopg are used only here, to run
them (psycopg accepts multi-statement SQL such as trigger definitions); the app uses asyncpg."""

import os

from alembic import context
from sqlalchemy import create_engine

url = os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation").replace(
    "postgresql://", "postgresql+psycopg://", 1
)

engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
engine.dispose()
