"""
Proves the EventBus enables real cross-plugin collaboration: user_plugin
subscribes to product_plugin's `product.created` event in boot() without
ever importing anything from app.plugins.product_plugin, and that
user_plugin still boots fine when product_plugin isn't loaded at all.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from app.config import settings
from app.core.loader import PluginLoader
from app.core.registry import PluginState, plugin_registry


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
async def test_product_created_event_reaches_user_plugin_handler(client, caplog):
    token = await _register_and_login(client, "eventbususer")

    with caplog.at_level(logging.INFO, logger="app.plugins.user_plugin.plugin"):
        resp = await client.post(
            "/api/v1/catalog/products",
            json={"name": "Thingamajig", "sku": "EVT-001", "price": "1.00", "stock_quantity": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201

    audit_lines = [r.message for r in caplog.records if "Audit: product.created" in r.message]
    assert audit_lines, "user_plugin's product.created handler never fired"
    assert "EVT-001" in audit_lines[0]


@pytest.mark.asyncio
async def test_user_plugin_boots_without_product_plugin(monkeypatch):
    """
    user_plugin's plugin.py contains zero references to app.plugins.product_plugin,
    subscribing by event name only. This proves that structurally: with
    product_plugin excluded via `enabled_plugins`, user_plugin still reaches
    BOOTED (it just never receives the event).
    """
    monkeypatch.setattr(settings, "enabled_plugins", ["user_plugin"])
    loader = PluginLoader(FastAPI())

    await loader.load_all()
    try:
        assert plugin_registry.state_of("user_plugin") == PluginState.BOOTED
        assert plugin_registry.get("product_plugin") is None
    finally:
        await loader.unload_all()
