"""
Integration tests for the UserPlugin: registration, login, profile management.
Uses the in-memory SQLite DB wired up in conftest.py.
"""
from __future__ import annotations

import pytest

from app.plugins.user_plugin.schemas import LoginRequest, UserCreate, UserUpdate
from app.plugins.user_plugin.service import UserService


@pytest.mark.asyncio
async def test_register_new_user(session):
    svc = UserService(session)
    data = UserCreate(username="alice", email="alice@example.com", password="password123")
    result = await svc.register(data)
    assert result.is_ok()
    user = result.unwrap()
    assert user.username == "alice"
    assert user.email == "alice@example.com"
    assert user.hashed_password != "password123"  # must be hashed


@pytest.mark.asyncio
async def test_register_duplicate_username(session):
    svc = UserService(session)
    data = UserCreate(username="bob", email="bob@example.com", password="password123")
    await svc.register(data)

    result = await svc.register(
        UserCreate(username="bob", email="bob2@example.com", password="password123")
    )
    assert result.is_err()
    from app.core.errors import PluginErrorCode
    assert result.unwrap_err().code == PluginErrorCode.ALREADY_EXISTS


@pytest.mark.asyncio
async def test_authenticate_success(session):
    svc = UserService(session)
    await svc.register(UserCreate(username="carol", email="carol@example.com", password="mypassword"))

    result = await svc.authenticate(LoginRequest(username="carol", password="mypassword"))
    assert result.is_ok()
    tokens = result.unwrap()
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "bearer"


@pytest.mark.asyncio
async def test_authenticate_wrong_password(session):
    svc = UserService(session)
    await svc.register(UserCreate(username="dave", email="dave@example.com", password="correctpw"))

    result = await svc.authenticate(LoginRequest(username="dave", password="wrong"))
    assert result.is_err()
    from app.core.errors import PluginErrorCode
    assert result.unwrap_err().code == PluginErrorCode.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_get_by_id_not_found(session):
    svc = UserService(session)
    result = await svc.get_by_id("nonexistent-id")
    assert result.is_err()
    from app.core.errors import PluginErrorCode
    assert result.unwrap_err().code == PluginErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_update_profile(session):
    svc = UserService(session)
    reg = await svc.register(
        UserCreate(username="eve", email="eve@example.com", password="password123")
    )
    user = reg.unwrap()

    result = await svc.update_profile(user.id, UserUpdate(full_name="Eve Smith"))
    assert result.is_ok()
    assert result.unwrap().full_name == "Eve Smith"


@pytest.mark.asyncio
async def test_change_password(session):
    svc = UserService(session)
    reg = await svc.register(
        UserCreate(username="frank", email="frank@example.com", password="oldpassword")
    )
    user = reg.unwrap()

    result = await svc.change_password(user.id, "oldpassword", "newpassword")
    assert result.is_ok()

    # Old password should no longer work
    auth = await svc.authenticate(LoginRequest(username="frank", password="oldpassword"))
    assert auth.is_err()

    # New password should work
    auth2 = await svc.authenticate(LoginRequest(username="frank", password="newpassword"))
    assert auth2.is_ok()
