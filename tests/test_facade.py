"""
HTTP tests for the Catalog Facade router (app/facades/router.py).

Exercises the full chain: the facade route (living beside its facade in
app/facades/, not in app/api/v1/ — see docs/technical/architecture.md,
"Module boundaries") calling CatalogFacade, which resolves the User/Product
services through `service_registry` instead of importing them directly.
"""
from __future__ import annotations

import pytest


async def _register_and_login(client, username: str, password: str = "password123") -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@test.com", "password": password},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return resp.json()["access_token"]


@pytest.mark.asyncio
async def test_create_product_via_facade(client):
    token = await _register_and_login(client, "facadeuser1")
    resp = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Gadget", "sku": "FAC-001", "price": "19.99", "stock_quantity": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["sku"] == "FAC-001"
    assert body["stock_quantity"] == 5


@pytest.mark.asyncio
async def test_create_product_via_facade_requires_auth(client):
    resp = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Gadget", "sku": "FAC-002", "price": "19.99", "stock_quantity": 5},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_product_via_facade(client):
    token = await _register_and_login(client, "facadeuser2")
    create_resp = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Widget", "sku": "FAC-003", "price": "9.99", "stock_quantity": 3},
        headers={"Authorization": f"Bearer {token}"},
    )
    product_id = create_resp.json()["id"]

    get_resp = await client.get(
        f"/api/v1/catalog/products/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == product_id


@pytest.mark.asyncio
async def test_get_product_via_facade_not_found(client):
    token = await _register_and_login(client, "facadeuser3")
    resp = await client.get(
        "/api/v1/catalog/products/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_adjust_inventory_via_facade(client):
    token = await _register_and_login(client, "facadeuser4")
    create_resp = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Gizmo", "sku": "FAC-004", "price": "5.00", "stock_quantity": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    product_id = create_resp.json()["id"]

    adjust_resp = await client.post(
        f"/api/v1/catalog/products/{product_id}/inventory",
        json={"delta": -3},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert adjust_resp.status_code == 200
    assert adjust_resp.json()["stock_quantity"] == 7
