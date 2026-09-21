from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.admin import router as admin_router
from app.api.v1.health import router as health_router
from app.config import settings
from app.core.error_handler import GlobalExceptionMiddleware
from app.core.loader import PluginLoader
from app.core.rate_limiter import limiter
from app.facades.registry import facade_registry
from app.infrastructure.database import db_factory

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup: load plugins → create DB tables → register core routes.
    Shutdown: unload plugins → dispose DB pool.
    """
    loader = PluginLoader(app)
    app.state.plugin_loader = loader
    await loader.load_all()

    # Tables are created after all plugin models have been imported via register()
    await db_factory.create_all()
    logger.info("Microkernel startup complete.")

    yield

    await loader.unload_all()
    await db_factory.dispose()
    logger.info("Microkernel shutdown complete.")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=(
            "Modular FastAPI Microkernel with dynamic Plugin loader, "
            "JWT Security, Rate Limiting, API Versioning, Result Pattern, "
            "Global Error Handling, and Swagger UI."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
        # OAuth2 Bearer scheme shown in Swagger UI Authorize dialog
        swagger_ui_init_oauth={"usePkceWithAuthorizationCodeGrant": True},
    )

    # ── Middleware (order matters: outermost = first to receive requests) ──────
    app.add_middleware(GlobalExceptionMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate limiter ──────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Core (non-plugin) routes ──────────────────────────────────────────────
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")

    # ── Facade routes ────────────────────────────────────────────────────────
    # Mounted as an explicit, separate step from the core routes above: a
    # facade coordinates plugins, so its routes belong beside its facade
    # (app/facades/), not in the kernel's own api/v1 layer. Importing
    # `facade_registry` above already imported `app.facades` (running every
    # facade's self-registration), so this loop doesn't need to know any
    # facade's name, the same way the plugin loader doesn't.
    for facade_router in facade_registry.routers():
        app.include_router(facade_router, prefix="/api/v1")

    return app


app = create_app()
