"""
Integration tests for the ProductPlugin: CRUD and stock management.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import PluginErrorCode
from app.plugins.product_plugin.schemas import ProductCreate, ProductUpdate
from app.plugins.product_plugin.service import ProductService


def _make_product(sku: str = "SKU-001") -> ProductCreate:
    return ProductCreate(
        name="Test Widget",
        description="A test product",
        sku=sku,
        price=Decimal("9.99"),
        stock_quantity=100,
    )


@pytest.mark.asyncio
async def test_create_product(session):
    svc = ProductService(session)
    result = await svc.create(_make_product())
    assert result.is_ok()
    p = result.unwrap()
    assert p.sku == "SKU-001"
    assert p.stock_quantity == 100


@pytest.mark.asyncio
async def test_create_duplicate_sku(session):
    svc = ProductService(session)
    await svc.create(_make_product("DUP-001"))
    result = await svc.create(_make_product("DUP-001"))
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.ALREADY_EXISTS


@pytest.mark.asyncio
async def test_get_by_id(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("GET-001"))).unwrap()
    result = await svc.get_by_id(created.id)
    assert result.is_ok()
    assert result.unwrap().id == created.id


@pytest.mark.asyncio
async def test_get_by_id_not_found(session):
    svc = ProductService(session)
    result = await svc.get_by_id("nonexistent")
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_update_product(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("UPD-001"))).unwrap()
    result = await svc.update(created.id, ProductUpdate(name="Updated Widget"))
    assert result.is_ok()
    assert result.unwrap().name == "Updated Widget"


@pytest.mark.asyncio
async def test_adjust_stock_add(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("STK-001"))).unwrap()
    result = await svc.adjust_stock(created.id, 50)
    assert result.is_ok()
    assert result.unwrap().stock_quantity == 150


@pytest.mark.asyncio
async def test_adjust_stock_remove(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("STK-002"))).unwrap()
    result = await svc.adjust_stock(created.id, -30)
    assert result.is_ok()
    assert result.unwrap().stock_quantity == 70


@pytest.mark.asyncio
async def test_adjust_stock_below_zero(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("STK-003"))).unwrap()
    result = await svc.adjust_stock(created.id, -200)
    assert result.is_err()
    assert result.unwrap_err().code == PluginErrorCode.INSUFFICIENT_STOCK


@pytest.mark.asyncio
async def test_delete_product(session):
    svc = ProductService(session)
    created = (await svc.create(_make_product("DEL-001"))).unwrap()
    del_result = await svc.delete(created.id)
    assert del_result.is_ok()

    get_result = await svc.get_by_id(created.id)
    assert get_result.is_err()


@pytest.mark.asyncio
async def test_list_products(session):
    svc = ProductService(session)
    await svc.create(_make_product("LST-001"))
    await svc.create(_make_product("LST-002"))
    products = await svc.list_products()
    skus = [p.sku for p in products]
    assert "LST-001" in skus
    assert "LST-002" in skus
