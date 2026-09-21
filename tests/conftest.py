from __future__ import annotations

import os

# Must be set before the first `app.*` import, since `app.config.Settings()` is
# instantiated at import time. Without this, the kernel's own `db_factory`
# singleton (used by the real lifespan's create_all()/dispose() calls) would
# default to a real `./dev.db` file and leave it behind after every test run.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import contextlib
import importlib
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.rate_limiter import limiter
from app.infrastructure.database import Base, db_factory
from app.main import create_app

# In-memory SQLite for tests: no file left behind, no port conflicts
_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "app" / "plugins"


def _import_all_plugin_models() -> None:
    """
    Mirrors PluginLoader._discover()'s dynamic scan: every plugin under
    app/plugins/ gets its models imported so Base.metadata is fully
    populated, instead of hardcoding a fixed list of plugin names here.
    A newly scaffolded plugin (scripts/new_plugin.py) works in tests without
    editing this file.
    """
    for entry in sorted(_PLUGINS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        with contextlib.suppress(ImportError):
            # a plugin without models.py has nothing to register
            importlib.import_module(f"app.plugins.{entry.name}.models")


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Single engine for the whole test session; avoids repeated schema creation."""
    _import_all_plugin_models()

    eng = create_async_engine(_TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def session(engine):
    """Each test gets a fresh session that rolls back on teardown."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


@pytest_asyncio.fixture()
async def client(engine):
    """
    Full ASGI test client wired to the in-memory DB.

    Overrides the db_factory.get_session dependency so every request uses the
    test engine instead of the real one, and drives the app's real `lifespan`
    (via `app.router.lifespan_context`) so the PluginLoader actually runs:
    without this, plugin-provided routes (auth/users/products) never get
    mounted and every request to them 404s. Also resets the shared rate
    limiter; it's a process-wide singleton (app.core.rate_limiter.limiter),
    so without a reset, request counts accumulate across every test in the
    session and eventually 429 out endpoints like /auth/register.
    """
    limiter.reset()
    app = create_app()

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with factory() as sess:
            try:
                yield sess
                await sess.commit()
            except Exception:
                await sess.rollback()
                raise

    app.dependency_overrides[db_factory.get_session] = override_get_session

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        yield ac
