from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.plugin_base import AbstractPlugin, KernelContext
from app.plugins.product_plugin.service import ProductService

logger = logging.getLogger(__name__)


class ProductPlugin(AbstractPlugin):
    name = "product_plugin"

    async def register(self, app: FastAPI, ctx: KernelContext) -> None:
        # Import models so SQLAlchemy Base picks them up before create_all().
        # `as _models` avoids rebinding the `app` parameter to the `app`
        # package — a bare `import app.plugins...` would shadow it and break
        # the `app.include_router(...)` call below.
        import app.plugins.product_plugin.models as _models  # noqa: F401
        from app.plugins.product_plugin.router import router

        app.include_router(router, prefix="/api/v1")
        logger.info("ProductPlugin: routes registered.")

    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        # Publish the product capability so facades/other plugins can reach it
        # via `ctx.service_registry.resolve(...)` instead of importing this
        # class directly — that's what lets this plugin be removed without
        # breaking anyone else's imports.
        ctx.service_registry.provide(
            "product_service_factory", lambda session: ProductService(session)
        )
        logger.info("ProductPlugin: booted.")

    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        logger.info("ProductPlugin: shut down.")
