"""
Phase 4 (3.12): hot-reload a single plugin without a full process restart.

Covers the low-level building blocks (ServiceRegistry.revoke_all_from,
EventBus.unsubscribe_all_from, Scoped* wrappers) and the end-to-end admin
endpoint that drives PluginLoader.reload_one() against the live app.
"""
from __future__ import annotations

import pytest

from app.core.hooks import EventBus, ScopedEventBus
from app.core.registry import ScopedServiceRegistry, ServiceRegistry
from app.infrastructure.jwt import JWTService


def _admin_headers() -> dict[str, str]:
    token = JWTService().create_access_token("admin-test-user", extra_claims={"roles": ["admin"]})
    return {"Authorization": f"Bearer {token}"}


# ── ServiceRegistry ownership (building block) ───────────────────────────────────

def test_service_registry_revoke_all_from_owner():
    reg = ServiceRegistry()
    reg.provide("a", lambda: 1, owner="p1")
    reg.provide("b", lambda: 2, owner="p2")

    reg.revoke_all_from("p1")

    assert reg.resolve("a").is_err()
    assert reg.resolve("b").is_ok()


def test_scoped_service_registry_tags_owner_automatically():
    reg = ServiceRegistry()
    scoped = ScopedServiceRegistry(reg, owner="p1")

    scoped.provide("x", lambda: 1)
    assert reg.resolve("x").is_ok()

    reg.revoke_all_from("p1")
    assert reg.resolve("x").is_err()


# ── EventBus ownership (building block) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_bus_unsubscribe_all_from_owner():
    bus = EventBus()
    received = []

    async def handler(**kwargs):
        received.append(kwargs)

    bus.subscribe("ev", handler, owner="p1")
    bus.unsubscribe_all_from("p1")
    await bus.emit("ev")

    assert received == []


@pytest.mark.asyncio
async def test_scoped_event_bus_tags_owner_automatically():
    bus = EventBus()
    received = []

    async def handler(**kwargs):
        received.append(kwargs)

    scoped = ScopedEventBus(bus, owner="p1")
    scoped.subscribe("ev", handler)
    bus.unsubscribe_all_from("p1")
    await bus.emit("ev")

    assert received == []


# ── End-to-end: admin reload endpoint ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reload_plugin_keeps_routes_working(client):
    resp = await client.get("/api/v1/products")
    assert resp.status_code == 200

    reload_resp = await client.post(
        "/api/v1/admin/plugins/product_plugin/reload", headers=_admin_headers()
    )
    assert reload_resp.status_code == 200
    body = reload_resp.json()
    assert body["reloaded"] == "product_plugin"
    # The two shipped plugins don't declare dependencies on each other.
    assert body["dependents_may_need_reload"] == []

    # The route must still work after the plugin was torn down and rebuilt
    # in-process, on the SAME running app.
    resp2 = await client.get("/api/v1/products")
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_reloaded_plugin_service_capability_still_resolvable(client):
    """
    After reload, the plugin's service_registry entry must have been
    re-provided by the fresh instance's boot() — not left stale/missing.
    Goes through the Catalog Facade (not the plugin's own router) because
    the facade is what actually depends on `service_registry.resolve(...)`.
    """
    await client.post(
        "/api/v1/auth/register",
        json={"username": "reloaduser", "email": "reloaduser@test.com", "password": "password123"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "reloaduser", "password": "password123"},
    )
    token = login_resp.json()["access_token"]

    reload_resp = await client.post(
        "/api/v1/admin/plugins/product_plugin/reload", headers=_admin_headers()
    )
    assert reload_resp.status_code == 200

    resp = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Reloaded", "sku": "RELOAD-001", "price": "2.50", "stock_quantity": 4},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_reload_reports_dependents_that_declare_it(client, monkeypatch):
    """
    reload_one() doesn't reload dependents automatically (see
    docs/spec/done/microkernel-architecture-refinements.md S4.5) — it
    reports them so the caller can decide. Temporarily declares user_plugin
    as depending on product_plugin (the two shipped plugins don't actually
    declare any dependency on each other) to exercise that reporting.
    """
    from app.plugins.user_plugin.plugin import UserPlugin

    monkeypatch.setattr(UserPlugin, "dependencies", ["product_plugin"])

    reload_resp = await client.post(
        "/api/v1/admin/plugins/product_plugin/reload", headers=_admin_headers()
    )
    assert reload_resp.status_code == 200
    assert reload_resp.json()["dependents_may_need_reload"] == ["user_plugin"]


@pytest.mark.asyncio
async def test_reload_plugin_requires_admin(client):
    resp = await client.post("/api/v1/admin/plugins/product_plugin/reload")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reload_unknown_plugin_returns_404(client):
    reload_resp = await client.post(
        "/api/v1/admin/plugins/does_not_exist/reload", headers=_admin_headers()
    )
    assert reload_resp.status_code == 404
