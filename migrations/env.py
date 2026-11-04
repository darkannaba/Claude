"""Environnement Alembic : migrations exécutées avec un moteur SQLite synchrone."""
import importlib

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import config as app_config
from app.database import Base

importlib.import_module("app.models")  # enregistre tous les modèles auprès de Base.metadata

alembic_cfg = context.config
if not alembic_cfg.get_main_option("sqlalchemy.url"):
    alembic_cfg.set_main_option("sqlalchemy.url", app_config.sync_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=alembic_cfg.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        alembic_cfg.get_section(alembic_cfg.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
