from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from result import Err, Ok, Result

from app.core.errors import PluginError, PluginErrorCode
from app.core.hooks import event_bus
from app.plugins.product_plugin.models import Product
from app.plugins.product_plugin.schemas import ProductCreate, ProductUpdate


class ProductService:
    """
    Domain logic for product catalog and inventory management.

    All public methods return Result[T, PluginError].
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ProductCreate) -> Result[Product, PluginError]:
        existing = await self._session.scalar(select(Product).where(Product.sku == data.sku))
        if existing:
            return Err(PluginError.already_exists("Product", f"sku={data.sku}"))

        product = Product(
            name=data.name,
            description=data.description,
            sku=data.sku,
            price=float(data.price),
            stock_quantity=data.stock_quantity,
            is_available=data.is_available,
        )
        self._session.add(product)
        await self._session.flush()

        await event_bus.emit("product.created", product_id=product.id, sku=product.sku)
        return Ok(product)

    async def get_by_id(self, product_id: str) -> Result[Product, PluginError]:
        product = await self._session.get(Product, product_id)
        if not product:
            return Err(PluginError.not_found("Product", product_id))
        return Ok(product)

    async def get_by_sku(self, sku: str) -> Result[Product, PluginError]:
        product = await self._session.scalar(select(Product).where(Product.sku == sku))
        if not product:
            return Err(PluginError.not_found("Product", f"sku={sku}"))
        return Ok(product)

    async def list_products(
        self,
        skip: int = 0,
        limit: int = 50,
        available_only: bool = False,
    ) -> list[Product]:
        stmt = select(Product).offset(skip).limit(limit)
        if available_only:
            stmt = stmt.where(Product.is_available.is_(True))
        rows = await self._session.scalars(stmt)
        return list(rows.all())

    async def update(
        self, product_id: str, data: ProductUpdate
    ) -> Result[Product, PluginError]:
        result = await self.get_by_id(product_id)
        if result.is_err():
            return result

        product = result.unwrap()
        if data.name is not None:
            product.name = data.name
        if data.description is not None:
            product.description = data.description
        if data.price is not None:
            product.price = float(data.price)
        if data.is_available is not None:
            product.is_available = data.is_available

        await self._session.flush()
        return Ok(product)

    async def adjust_stock(
        self, product_id: str, delta: int
    ) -> Result[Product, PluginError]:
        result = await self.get_by_id(product_id)
        if result.is_err():
            return result

        product = result.unwrap()
        new_qty = product.stock_quantity + delta

        if new_qty < 0:
            return Err(
                PluginError(
                    PluginErrorCode.INSUFFICIENT_STOCK,
                    f"Cannot reduce stock below zero. Current: {product.stock_quantity}, delta: {delta}",
                )
            )

        product.stock_quantity = new_qty
        await self._session.flush()

        await event_bus.emit(
            "product.stock_adjusted",
            product_id=product.id,
            delta=delta,
            new_quantity=new_qty,
        )
        return Ok(product)

    async def delete(self, product_id: str) -> Result[None, PluginError]:
        result = await self.get_by_id(product_id)
        if result.is_err():
            return result  # type: ignore[return-value]

        await self._session.delete(result.unwrap())
        await self._session.flush()
        return Ok(None)
