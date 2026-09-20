from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PluginErrorCode
from app.core.rate_limiter import limiter
from app.core.security import require_admin
from app.infrastructure.database import db_factory
from app.infrastructure.jwt import TokenPayload
from app.plugins.product_plugin.schemas import (
    ProductCreate,
    ProductResponse,
    ProductUpdate,
    StockAdjustment,
)
from app.plugins.product_plugin.service import ProductService

router = APIRouter(prefix="/products", tags=["Products"])

_ERROR_STATUS_MAP = {
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PluginErrorCode.ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    PluginErrorCode.INSUFFICIENT_STOCK: status.HTTP_409_CONFLICT,
    PluginErrorCode.PRODUCT_UNAVAILABLE: status.HTTP_410_GONE,
    PluginErrorCode.PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
    PluginErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _raise(err: object) -> None:
    from app.core.errors import PluginError
    assert isinstance(err, PluginError)
    http_status = _ERROR_STATUS_MAP.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=http_status, detail=err.message)


@router.get(
    "",
    response_model=list[ProductResponse],
    summary="List products",
)
@limiter.limit("60/minute")
async def list_products(
    request: Request,
    skip: int = 0,
    limit: int = 50,
    available_only: bool = False,
    session: AsyncSession = Depends(db_factory.get_session),
) -> list[ProductResponse]:
    products = await ProductService(session).list_products(skip, limit, available_only)
    return [ProductResponse.model_validate(p) for p in products]


@router.get(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Get product by ID",
)
async def get_product(
    product_id: str,
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await ProductService(session).get_by_id(product_id)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new product (admin only)",
)
async def create_product(
    body: ProductCreate,
    payload: TokenPayload = Depends(require_admin),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await ProductService(session).create(body)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.patch(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Update product details (admin only)",
)
async def update_product(
    product_id: str,
    body: ProductUpdate,
    payload: TokenPayload = Depends(require_admin),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await ProductService(session).update(product_id, body)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.post(
    "/{product_id}/stock",
    response_model=ProductResponse,
    summary="Adjust product stock quantity (admin only)",
)
async def adjust_stock(
    product_id: str,
    body: StockAdjustment,
    payload: TokenPayload = Depends(require_admin),
    session: AsyncSession = Depends(db_factory.get_session),
) -> ProductResponse:
    result = await ProductService(session).adjust_stock(product_id, body.delta)
    if result.is_err():
        _raise(result.unwrap_err())
    return ProductResponse.model_validate(result.unwrap())


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a product (admin only)",
)
async def delete_product(
    product_id: str,
    payload: TokenPayload = Depends(require_admin),
    session: AsyncSession = Depends(db_factory.get_session),
) -> None:
    result = await ProductService(session).delete(product_id)
    if result.is_err():
        _raise(result.unwrap_err())
