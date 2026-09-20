"""
End-to-end HTTP tests against the full ASGI app (in-memory DB).
Covers auth flow, user endpoints, product endpoints, and health check.
"""
from __future__ import annotations

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _register_and_login(client, username: str, password: str = "password123") -> str:
    """Register a user and return a valid access token."""
    await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@test.com", "password": password},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return resp.json()["access_token"]


# ── Health ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["plugins"], list)

    plugins_by_name = {p["name"]: p for p in body["plugins"]}
    assert "product_plugin" in plugins_by_name
    assert "user_plugin" in plugins_by_name
    for entry in plugins_by_name.values():
        # Both real plugins have no dependencies and no failures at this point
        assert entry["state"] == "BOOTED"
        assert entry["version"] == "0.0.0"  # neither shipped plugin overrides this
        assert entry["api_version"] == "1.0"
        assert entry["dependencies"] == []
        assert entry["error"] is None

    # capabilities/event_subscriptions (5.1): the two published service
    # factories, and user_plugin's real subscription to product.created.
    assert "product_service_factory" in body["capabilities"]
    assert "user_service_factory" in body["capabilities"]
    assert body["event_subscriptions"]["product.created"] == ["user_plugin"]

    assert "catalog_facade" in body["facades"]


# ── Auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_user(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": "testuser1", "email": "testuser1@test.com", "password": "password123"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "testuser1"
    assert "id" in body


@pytest.mark.asyncio
async def test_register_duplicate_returns_409(client):
    payload = {"username": "dupuser", "email": "dup@test.com", "password": "password123"}
    await client.post("/api/v1/auth/register", json=payload)
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login_success(client):
    await client.post(
        "/api/v1/auth/register",
        json={"username": "loginuser", "email": "login@test.com", "password": "password123"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "loginuser", "password": "password123"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(client):
    await client.post(
        "/api/v1/auth/register",
        json={"username": "wrongpw", "email": "wrongpw@test.com", "password": "correct"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "wrongpw", "password": "wrong"},
    )
    assert resp.status_code == 401


# ── User profile ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_me(client):
    token = await _register_and_login(client, "meuser")
    resp = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "meuser"


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client):
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_update_me(client):
    token = await _register_and_login(client, "updateme")
    resp = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Updated Name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Updated Name"


# ── Products ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_products_public(client):
    resp = await client.get("/api/v1/products")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_product_requires_admin(client):
    token = await _register_and_login(client, "regularuser")
    resp = await client.post(
        "/api/v1/products",
        json={"name": "Widget", "sku": "W-001", "price": "9.99", "stock_quantity": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Regular users don't have admin role
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_product_not_found(client):
    resp = await client.get("/api/v1/products/nonexistent-id")
    assert resp.status_code == 404
