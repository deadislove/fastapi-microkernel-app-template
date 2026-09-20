from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PluginErrorCode
from app.core.security import get_current_user_payload
from app.facades.catalog_facade import CatalogFacade
from app.facades.registry import facade_registry
from app.infrastructure.database import db_factory
from app.infrastructure.jwt import TokenPayload
from app.plugins.product_plugin.schemas import (
    ProductCreate,
    ProductResponse,
    StockAdjustment,
)

# This router lives beside its facade rather than in `app/api/v1/` — the
# core/kernel route layer should not carry a compile-time dependency on any
# single plugin's DTOs. `main.py` mounts it as an explicit, separate step from
# the core routes (health, etc.) to keep that boundary visible.
router = APIRouter(prefix="/catalog", tags=["Catalog (Facade)"])

_ERROR_STATUS_MAP = {
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PluginErrorCode.ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    PluginErrorCode.INSUFFICIENT_STOCK: status.HTTP_409_CONFLICT,
    PluginErrorCode.PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
    PluginErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _raise(err: object) -> None:
    from app.core.errors import PluginError
    assert isinstance(err, PluginError)
    http_status = _ERROR_STATUS_MAP.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=http_status, detail=err.message)


@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create product via Catalog Facade (validates user + product)",
)
async def create_product_via_facade(
    body: ProductCreate,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    """
    Demonstrates cross-plugin coordination: the facade verifies the user is
    active before delegating to the product service.
    """
    result = await CatalogFacade(session).create_product_as_user(payload.sub, body)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.get(
    "/products/{product_id}",
    response_model=ProductResponse,
    summary="Get product via Catalog Facade (validates requesting user)",
)
async def get_product_via_facade(
    product_id: str,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await CatalogFacade(session).get_product_with_owner_check(product_id, payload.sub)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.post(
    "/products/{product_id}/inventory",
    response_model=ProductResponse,
    summary="Adjust inventory via Catalog Facade",
)
async def adjust_inventory_via_facade(
    product_id: str,
    body: StockAdjustment,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await CatalogFacade(session).adjust_inventory(product_id, body.delta)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


# Self-register with facade_registry, including this router, so main.py can
# mount it via `facade_registry.routers()` without importing this module by
# name — see AbstractFacade's docstring for the full wiring.
facade_registry.register(CatalogFacade, router=router)
