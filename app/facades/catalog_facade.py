from __future__ import annotations

from result import Err, Ok, Result

from app.core.errors import PluginError, PluginErrorCode
from app.core.registry import service_registry
from app.facades.base import AbstractFacade
from app.plugins.product_plugin.models import Product
from app.plugins.product_plugin.schemas import ProductCreate, ProductUpdate


class CatalogFacade(AbstractFacade):
    """
    High-level facade combining User and Product plugin capabilities.

    Routers and external callers should use this facade instead of calling
    plugin services directly — it's the only sanctioned cross-plugin boundary.

    Product/User services are resolved through `service_registry` (published
    by each plugin's `boot()`) rather than imported directly — this is what
    lets either plugin be removed without breaking this module's imports;
    a missing capability simply surfaces as `Result.Err`.

    Why this imports `service_registry` directly instead of going through a
    `ctx` (see `AbstractFacade` for the full explanation): facades aren't
    lifecycle-managed like plugins, so there's no `PluginLoader`-built
    `KernelContext` to receive here. That's fine, not an inconsistency to
    fix, because `resolve()` only reads and registers nothing — it's
    `provide()`/`subscribe()` that need the `owner` tagging `ctx` exists to
    provide, and this facade never calls either of those.

    All methods return Result[T, PluginError] for clean HTTP mapping at the
    router level.
    """

    name = "catalog_facade"

    def _resolve(self, service_name: str) -> Result[object, PluginError]:
        factory_result = service_registry.resolve(service_name)
        if factory_result.is_err():
            return factory_result  # type: ignore[return-value]
        return Ok(factory_result.unwrap()(self._session))

    async def create_product_as_user(
        self, user_id: str, data: ProductCreate
    ) -> Result[Product, PluginError]:
        """
        Verify the requesting user exists and is active before creating a product.

        Success: Ok(Product)
        Failure: Err(PluginError) — NOT_FOUND if user missing, or product creation errors.
        """
        user_svc_result = self._resolve("user_service_factory")
        if user_svc_result.is_err():
            return user_svc_result  # type: ignore[return-value]

        user_result = await user_svc_result.unwrap().get_by_id(user_id)
        if user_result.is_err():
            return user_result  # type: ignore[return-value]

        user = user_result.unwrap()
        if not user.is_active:
            return Err(
                PluginError(PluginErrorCode.PERMISSION_DENIED, "Inactive users cannot create products.")
            )

        product_svc_result = self._resolve("product_service_factory")
        if product_svc_result.is_err():
            return product_svc_result  # type: ignore[return-value]

        return await product_svc_result.unwrap().create(data)

    async def get_product_with_owner_check(
        self, product_id: str, requesting_user_id: str
    ) -> Result[Product, PluginError]:
        """
        Fetch a product, confirming the requesting user is valid.

        Success: Ok(Product)
        Failure: Err(PluginError) — NOT_FOUND for either resource.
        """
        user_svc_result = self._resolve("user_service_factory")
        if user_svc_result.is_err():
            return user_svc_result  # type: ignore[return-value]

        # Confirm user exists — prevents leaking product data to ghost accounts
        user_result = await user_svc_result.unwrap().get_by_id(requesting_user_id)
        if user_result.is_err():
            return user_result  # type: ignore[return-value]

        product_svc_result = self._resolve("product_service_factory")
        if product_svc_result.is_err():
            return product_svc_result  # type: ignore[return-value]

        return await product_svc_result.unwrap().get_by_id(product_id)

    async def update_product(
        self, product_id: str, data: ProductUpdate
    ) -> Result[Product, PluginError]:
        product_svc_result = self._resolve("product_service_factory")
        if product_svc_result.is_err():
            return product_svc_result  # type: ignore[return-value]

        return await product_svc_result.unwrap().update(product_id, data)

    async def adjust_inventory(
        self, product_id: str, delta: int
    ) -> Result[Product, PluginError]:
        product_svc_result = self._resolve("product_service_factory")
        if product_svc_result.is_err():
            return product_svc_result  # type: ignore[return-value]

        return await product_svc_result.unwrap().adjust_stock(product_id, delta)
