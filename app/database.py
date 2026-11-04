"""Connexion SQLite asynchrone (mode WAL), sessions et migrations Alembic automatiques."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import BASE_DIR, config

logger = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# NullPool : une connexion par session. Avec SQLite c'est peu coûteux et cela évite
# de partager des connexions aiosqlite entre boucles d'événements (tests, planificateur).
engine = create_async_engine(
    config.database_url,
    poolclass=NullPool,
    connect_args={"timeout": 30} if config.is_sqlite else {},
)

if config.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")      # lectures et écritures concurrentes
        cursor.execute("PRAGMA synchronous=NORMAL")    # recommandé avec WAL
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.sync_database_url)
    return cfg


def run_migrations() -> None:
    """Applique toutes les migrations Alembic en attente (appelé au démarrage)."""
    command.upgrade(alembic_config(), "head")
    logger.info("Schéma de base de données à jour.")
