from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """
    Shared declarative base: every plugin model must inherit from this.

    Why plugins share one Base/engine: this template targets a single
    process and a single database (see the top-level README's scope note),
    so there's no separate schema/connection per plugin to give each one its
    own `Base`. What that could tempt someone into: since every plugin's
    tables already live in the same metadata and the same database, it's an
    easy mistake to reach across plugins at the data layer: a `ForeignKey`
    from one plugin's table into another's, or a `select()` against another
    plugin's model class directly, since nothing at the SQL level stops it.
    Solution: don't do that regardless. A plugin's models must never declare
    a `ForeignKey` into another plugin's table, and no plugin may query
    another plugin's model class directly; cross-plugin data access always
    goes through the other plugin's Service (resolved via `service_registry`,
    never by importing its model class) or through an `EventBus` notification,
    the same as any other cross-plugin collaboration in this codebase.
    """
    pass


class DatabaseFactory:
    """
    Async SQLAlchemy connection pool factory.

    Supports any SQLAlchemy-compatible async driver:
      - SQLite  → aiosqlite  (dev/test)
      - Postgres → asyncpg   (production)
      - MySQL   → aiomysql
      - etc.

    Plugin models are registered by importing them before `create_all()` is
    called; the Base metadata collects them automatically.
    """

    def __init__(self, database_url: str = settings.database_url) -> None:
        # SQLite needs check_same_thread=False; other drivers ignore it
        connect_args: dict = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=settings.debug,
            connect_args=connect_args,
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> None:
        """Create all tables whose models have been imported by this point."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created/verified.")

    async def drop_all(self) -> None:
        """Tear down all tables: test isolation only, never in production."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("Database connection pool disposed.")

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """FastAPI dependency: yields a session and commits/rolls back."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


# Module-level singleton shared by all plugins
db_factory = DatabaseFactory()
