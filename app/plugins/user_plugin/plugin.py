from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.plugin_base import AbstractPlugin, KernelContext
from app.plugins.user_plugin.service import UserService

logger = logging.getLogger(__name__)


async def _log_product_created(product_id: str = "", sku: str = "", **_: object) -> None:
    """
    Demonstrates real cross-plugin collaboration through the EventBus: this
    handler lives in user_plugin, yet reacts to an event emitted by
    product_plugin; user_plugin never imports anything from
    `app.plugins.product_plugin`. If product_plugin is disabled/removed
    (see `settings.enabled_plugins`), user_plugin still boots fine; it just
    never receives this event.
    """
    logger.info(
        "Audit: product.created observed by user_plugin (product_id=%s, sku=%s)",
        product_id,
        sku,
    )


class UserPlugin(AbstractPlugin):
    name = "user_plugin"

    async def register(self, app: FastAPI, ctx: KernelContext) -> None:
        # Import models so SQLAlchemy Base picks them up before create_all().
        # `as _models` avoids rebinding the `app` parameter to the `app`
        # package: a bare `import app.plugins...` would shadow it and break
        # the `app.include_router(...)` call below.
        import app.plugins.user_plugin.models as _models  # noqa: F401
        from app.plugins.user_plugin.router import auth_router, router

        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")
        logger.info("UserPlugin: routes registered.")

    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        # Publish the user capability so facades/other plugins can reach it via
        # `ctx.service_registry.resolve(...)` instead of importing this class
        # directly: that's what lets this plugin be removed without breaking
        # anyone else's imports.
        ctx.service_registry.provide(
            "user_service_factory", lambda session: UserService(session)
        )

        # Subscribing here (not in register()) is safe because every
        # plugin has finished register() by the time boot() runs, so the
        # event name contract is stable even though we never import the
        # emitting plugin.
        ctx.event_bus.subscribe("product.created", _log_product_created)
        logger.info("UserPlugin: booted.")

    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        logger.info("UserPlugin: shut down.")
