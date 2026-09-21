"""
Why this script exists: a new plugin has several conventions to get right
(the `AbstractPlugin` contract, the `ctx.service_registry`/`ctx.event_bus`
scoping rules, the `as _models` import alias, ...), and expecting every
author to remember all of them from documentation alone means some will
inevitably be missed. What this does about it: generates a skeleton that already
follows the current `AbstractPlugin` contract (`KernelContext`, `dependencies`,
`api_version`, `version`) and the Microkernel boundary rules, so a new plugin
starts out correct instead of starting out as a copy-paste with gaps. See
docs/technical/plugin-development.md for the full contract this mirrors.

Usage:
    python scripts/new_plugin.py <plugin_directory_name>

Example:
    python scripts/new_plugin.py billing_plugin
    # -> app/plugins/billing_plugin/{__init__,plugin,router,service,models,schemas}.py
    # -> tests/test_billing_plugin.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _REPO_ROOT / "app" / "plugins"
_TESTS_DIR = _REPO_ROOT / "tests"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _pascal_case(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _table_name(name: str) -> str:
    # "billing_plugin" -> "billing_plugin_items"; adjust after generation to
    # match your actual entity name.
    return f"{name}_items"


def _render(name: str) -> dict[str, str]:
    class_name = _pascal_case(name)
    table = _table_name(name)

    init_py = f'''from app.plugins.{name}.plugin import {class_name}

plugin = {class_name}()
'''

    plugin_py = f'''from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.plugin_base import AbstractPlugin, KernelContext
from app.plugins.{name}.service import {class_name}Service

logger = logging.getLogger(__name__)


class {class_name}(AbstractPlugin):
    name = "{name}"

    # Other plugin names that must finish register()+boot() before this one.
    # dependencies = ["user_plugin"]

    # This plugin's own release version: purely informational, shown by
    # GET /api/v1/health. Not to be confused with api_version (kernel-contract
    # compatibility, checked by the loader); this one is inspected by nothing.
    # version = "1.0.0"

    async def register(self, app: FastAPI, ctx: KernelContext) -> None:
        # `as _models` avoids rebinding the `app` parameter to the `app`
        # package: a bare `import app.plugins...` would shadow it and break
        # the `app.include_router(...)` call below.
        import app.plugins.{name}.models as _models  # noqa: F401

        from app.plugins.{name}.router import router

        app.include_router(router, prefix="/api/v1")
        logger.info("{class_name}: routes registered.")

    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        # Publish this plugin's capability so facades/other plugins can reach
        # it via `ctx.service_registry.resolve("{name}_service_factory")`
        # instead of importing {class_name}Service directly.
        ctx.service_registry.provide(
            "{name}_service_factory", lambda session: {class_name}Service(session)
        )

        # React to another plugin's event without importing it:
        # ctx.event_bus.subscribe("user.created", self._on_user_created)
        logger.info("{class_name}: booted.")

    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        logger.info("{class_name}: shut down.")
'''

    models_py = f'''from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class {class_name}Item(Base):
    __tablename__ = "{table}"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"<{class_name}Item id={{self.id!r}} name={{self.name!r}}>"
'''

    schemas_py = f'''from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class {class_name}ItemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class {class_name}ItemResponse(BaseModel):
    id: str
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {{"from_attributes": True}}
'''

    service_py = f'''from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from result import Err, Ok, Result

from app.core.errors import PluginError
from app.plugins.{name}.models import {class_name}Item
from app.plugins.{name}.schemas import {class_name}ItemCreate


class {class_name}Service:
    """Domain logic for {name}. All public methods return Result[T, PluginError]."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: {class_name}ItemCreate) -> Result[{class_name}Item, PluginError]:
        item = {class_name}Item(name=data.name)
        self._session.add(item)
        await self._session.flush()
        return Ok(item)

    async def get_by_id(self, item_id: str) -> Result[{class_name}Item, PluginError]:
        item = await self._session.get({class_name}Item, item_id)
        if not item:
            return Err(PluginError.not_found("{class_name}Item", item_id))
        return Ok(item)

    async def list_items(self, skip: int = 0, limit: int = 50) -> list[{class_name}Item]:
        rows = await self._session.scalars(
            select({class_name}Item).offset(skip).limit(limit)
        )
        return list(rows.all())
'''

    router_py = f'''from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PluginError, PluginErrorCode
from app.infrastructure.database import db_factory
from app.plugins.{name}.schemas import {class_name}ItemCreate, {class_name}ItemResponse
from app.plugins.{name}.service import {class_name}Service

router = APIRouter(prefix="/{name.replace('_', '-')}", tags=["{class_name}"])

_ERROR_STATUS_MAP = {{
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
}}


def _raise(err: PluginError) -> None:
    http_status = _ERROR_STATUS_MAP.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=http_status, detail=err.message)


@router.get("", response_model=list[{class_name}ItemResponse], summary="List items")
async def list_items(
    session: AsyncSession = Depends(db_factory.get_session),
) -> list[{class_name}ItemResponse]:
    items = await {class_name}Service(session).list_items()
    return [{class_name}ItemResponse.model_validate(i) for i in items]


@router.post(
    "",
    response_model={class_name}ItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an item",
)
async def create_item(
    body: {class_name}ItemCreate,
    session: AsyncSession = Depends(db_factory.get_session),
) -> {class_name}ItemResponse:
    result = await {class_name}Service(session).create(body)
    if result.is_err():
        _raise(result.unwrap_err())
    return {class_name}ItemResponse.model_validate(result.unwrap())


@router.get("/{{item_id}}", response_model={class_name}ItemResponse, summary="Get item by ID")
async def get_item(
    item_id: str,
    session: AsyncSession = Depends(db_factory.get_session),
) -> {class_name}ItemResponse:
    result = await {class_name}Service(session).get_by_id(item_id)
    if result.is_err():
        _raise(result.unwrap_err())
    return {class_name}ItemResponse.model_validate(result.unwrap())
'''

    test_py = f'''"""Smoke tests for the {name} scaffold; replace with real coverage."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_get_item(client):
    create_resp = await client.post("/api/v1/{name.replace('_', '-')}", json={{"name": "Example"}})
    assert create_resp.status_code == 201
    item_id = create_resp.json()["id"]

    get_resp = await client.get(f"/api/v1/{name.replace('_', '-')}/{{item_id}}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Example"
'''

    return {
        "__init__.py": init_py,
        "plugin.py": plugin_py,
        "models.py": models_py,
        "schemas.py": schemas_py,
        "service.py": service_py,
        "router.py": router_py,
        f"__test__{name}": test_py,  # special-cased below (goes to tests/, not the plugin dir)
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1

    name = argv[1]
    if not _NAME_RE.match(name):
        print(f"Invalid plugin name '{name}': use lower_snake_case, e.g. 'billing_plugin'.")
        return 1

    plugin_dir = _PLUGINS_DIR / name
    if plugin_dir.exists():
        print(f"Refusing to overwrite existing directory: {plugin_dir}")
        return 1

    files = _render(name)
    test_content = files.pop(f"__test__{name}")

    plugin_dir.mkdir(parents=True)
    for filename, content in files.items():
        (plugin_dir / filename).write_text(content, encoding="utf-8")

    test_path = _TESTS_DIR / f"test_{name}.py"
    if not test_path.exists():
        test_path.write_text(test_content, encoding="utf-8")

    print(f"Created app/plugins/{name}/ and tests/test_{name}.py")
    print("Next steps:")
    print(f"  1. Rename {_pascal_case(name)}Item to your real entity and adjust models.py/schemas.py.")
    print("  2. Set `dependencies = [...]` in plugin.py if this plugin needs another plugin's boot() to run first.")
    print("  3. Run `python scripts/check_architecture_boundaries.py` before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
