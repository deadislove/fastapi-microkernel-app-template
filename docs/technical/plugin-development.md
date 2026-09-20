# Plugin Development Guide

A plugin is a self-contained unit of domain logic — routes, a SQLAlchemy model, a service layer — that the kernel discovers and drives without ever importing it by name. This guide walks through the contract and conventions, using the two shipped plugins (`user_plugin`, `product_plugin`) as reference implementations.

Read [architecture.md](./architecture.md) first if you haven't — this document assumes you know what `KernelContext`, `service_registry`, and `event_bus` are for.

## Fastest path: the scaffold generator

```bash
python scripts/new_plugin.py billing_plugin
```

This creates `app/plugins/billing_plugin/{__init__,plugin,router,service,models,schemas}.py` and a `tests/test_billing_plugin.py`, already wired to the current `AbstractPlugin` contract (`KernelContext`, `dependencies`, `api_version`). Rename the generated entity/table, adjust the schema fields, and go — this section explains what each generated piece does and why, so you can go beyond the scaffold's single-entity CRUD shape.

## Directory layout

```
app/plugins/<name>/
├── __init__.py   # exposes the `plugin` singleton instance — this is what the loader discovers
├── plugin.py     # the AbstractPlugin subclass: register()/boot()/shutdown()
├── models.py     # SQLAlchemy models (inherit from app.infrastructure.database.Base)
├── schemas.py    # Pydantic request/response models
├── service.py    # domain logic, returns Result[T, PluginError]
└── router.py     # FastAPI APIRouter, thin HTTP mapping over the service
```

`__init__.py` must expose a module-level `plugin` attribute — this is the only thing `PluginLoader._discover()` looks for:

```python
# app/plugins/billing_plugin/__init__.py
from app.plugins.billing_plugin.plugin import BillingPlugin

plugin = BillingPlugin()
```

## The plugin class

```python
# app/plugins/billing_plugin/plugin.py
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.plugin_base import AbstractPlugin, KernelContext
from app.plugins.billing_plugin.service import BillingService

logger = logging.getLogger(__name__)


class BillingPlugin(AbstractPlugin):
    name = "billing_plugin"
    dependencies = ["user_plugin"]  # optional — omit if you depend on nothing

    async def register(self, app: FastAPI, ctx: KernelContext) -> None:
        import app.plugins.billing_plugin.models as _models  # noqa: F401
        from app.plugins.billing_plugin.router import router
        app.include_router(router, prefix="/api/v1")
        logger.info("BillingPlugin: routes registered.")

    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        ctx.service_registry.provide(
            "billing_service_factory", lambda session: BillingService(session)
        )
        logger.info("BillingPlugin: booted.")

    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        logger.info("BillingPlugin: shut down.")
```

### `register()` vs. `boot()`

The loader runs `register()` on *every* plugin before running `boot()` on *any* plugin. That means:

- **`register()`** is for declaring things that don't need other plugins to exist yet: importing your models (so `Base.metadata` picks them up before `create_all()`), and mounting your router.
- **`boot()`** is for anything that assumes the rest of the kernel is up: publishing to `service_registry`, subscribing to `event_bus` events, or resolving another plugin's capability.

Subscribing to an event from `register()` is a bug waiting to happen — the plugin whose event you want might not exist yet at that point in the two-pass loop (it does by `boot()`, guaranteed).

### The `as _models` import — don't drop this

```python
import app.plugins.billing_plugin.models as _models  # noqa: F401
```

A bare `import app.plugins.billing_plugin.models` (no `as`) rebinds the local name `app` to the top-level `app` *package* — which shadows the `app: FastAPI` parameter of `register()`. The very next line, `app.include_router(...)`, would then fail with `AttributeError: module 'app' has no attribute 'include_router'`. This bit a real version of this template in production, not just in a test — always alias the import.

### `dependencies`

```python
class BillingPlugin(AbstractPlugin):
    dependencies = ["user_plugin"]
```

Declares that `user_plugin` must finish `register()`+`boot()` before `BillingPlugin` starts. The loader topologically sorts on this; a dependency that doesn't exist, or a cycle, aborts startup with a clear `PluginLoadError` — regardless of `plugin_load_mode` (see [operations.md](./operations.md)), since that's a configuration bug, not a runtime failure of one plugin. If `user_plugin` itself fails to boot (in `best_effort` mode), `BillingPlugin` is automatically skipped too, rather than starting against a dependency that never actually finished.

### `api_version`

```python
class BillingPlugin(AbstractPlugin):
    api_version: ClassVar[str] = "1.0"  # defaults to the current kernel version; usually omit this
```

Defaults to `KERNEL_API_VERSION` (`app/core/plugin_base.py`), so you don't normally set it. It exists so that if the kernel contract ever makes a breaking change (a new required `KernelContext` field, a changed hook signature) and bumps its major version, a plugin still declaring the old major version is skipped with a warning at discovery time instead of crashing on some unrelated `AttributeError` later.

Don't confuse this with `version` (also on `AbstractPlugin`, defaults to `"0.0.0"`): `api_version` is about compatibility with the kernel contract and is actually checked by the loader; `version` is purely informational — your plugin's own release version, shown by `GET /api/v1/health` and inspected by nothing. Only set `version` once your plugin has its own release cadence worth tracking.

## Models

```python
# app/plugins/billing_plugin/models.py
from app.infrastructure.database import Base

class Invoice(Base):
    __tablename__ = "billing_plugin_invoices"
    ...
```

All plugins share one `Base`/engine — a deliberate single-process/single-database simplification, not license to couple at the data layer:

- Never declare a `ForeignKey` into another plugin's table.
- Never `select()`/query another plugin's model class directly.

Cross-plugin data access always goes through the other plugin's `Service` (resolved via `service_registry`) or an `event_bus` notification — see [architecture.md](./architecture.md#module-boundaries).

## Service layer: the `Result` pattern

Every public service method returns `Result[T, PluginError]` (from the [`result`](https://pypi.org/project/result/) library) instead of raising for expected failure cases:

```python
from result import Err, Ok, Result
from app.core.errors import PluginError

async def get_by_id(self, item_id: str) -> Result[Invoice, PluginError]:
    invoice = await self._session.get(Invoice, item_id)
    if not invoice:
        return Err(PluginError.not_found("Invoice", item_id))
    return Ok(invoice)
```

See [api-conventions.md](./api-conventions.md) for the full `PluginError`/`PluginErrorCode` reference and how routers map errors to HTTP status codes.

### Where `ctx` is required, and where it isn't

`KernelContext` is only threaded into `AbstractPlugin.register/boot/shutdown` — it doesn't reach `service.py` or `router.py`, which are constructed per-request and have no `ctx` to receive. In practice:

- **`plugin.py` (lifecycle hooks) must use `ctx.service_registry.provide(...)` and `ctx.event_bus.subscribe(...)`** — never import the raw `service_registry`/`event_bus` singletons. Both calls register something with an `owner` tag, which hot-reload depends on to clean up correctly. `scripts/check_architecture_boundaries.py` enforces this (Rule 4) on `plugin.py` specifically.
- **`service.py`/`router.py` may import `service_registry`/`event_bus` directly** to call `.resolve(...)`/`.emit(...)`. Neither of those registers anything or needs an `owner` tag, so there's nothing unsafe about it — see `product_plugin/service.py`'s `event_bus.emit("product.created", ...)` call for a real example. This is a deliberate scope boundary, not an oversight; see [docs/spec/done/microkernel-architecture-refinements.md](../spec/done/microkernel-architecture-refinements.md) S4.1 if you want the full reasoning.

The same applies to facades: they never receive a `ctx` at all (they aren't lifecycle-managed), so a facade calling `service_registry.resolve(...)` directly is the expected pattern, not a gap — see [architecture.md](./architecture.md#abstractfacade-and-facaderegistry).

## Publishing a capability

```python
# in boot()
ctx.service_registry.provide(
    "billing_service_factory", lambda session: BillingService(session)
)
```

Anything else — a facade, another plugin — resolves it the same way:

```python
factory_result = ctx.service_registry.resolve("billing_service_factory")
if factory_result.is_err():
    return factory_result  # BillingPlugin isn't loaded; degrade gracefully
billing_svc = factory_result.unwrap()(session)
```

Never `from app.plugins.billing_plugin.service import BillingService` from anywhere outside `billing_plugin` itself — that's exactly the coupling `service_registry` exists to prevent (and `scripts/check_architecture_boundaries.py` will fail your build if you do).

## Reacting to another plugin's event

```python
# in boot(), e.g. user_plugin subscribing to product_plugin's event
async def _log_product_created(product_id: str = "", sku: str = "", **_: object) -> None:
    logger.info("Audit: product.created observed (product_id=%s, sku=%s)", product_id, sku)

ctx.event_bus.subscribe("product.created", _log_product_created)
```

This works even if `product_plugin` is disabled — `user_plugin` never imports it, it just never receives the event. Handler exceptions are logged and swallowed by the bus, so one bad subscriber can't break the emitting plugin's flow.

Two conventions worth following, since `emit()`/`subscribe()` have no schema to enforce them for you:

- **Name events `resource.past_tense_verb`** (`product.created`, `user.created`, `product.stock_adjusted`) — consistent naming makes `event_subscriptions` in `GET /api/v1/health` readable at a glance.
- **Give every subscriber parameter a default, and end the signature with `**_: object`**, exactly like `_log_product_created(product_id: str = "", sku: str = "", **_: object)` above. The emitting plugin can then add a new kwarg, or a subscriber can ignore fields it doesn't care about, without either side breaking — there's no payload schema, so this defensive shape is the only thing standing between "the emitter changed something" and a `TypeError` at runtime.

## Contributing an exception handler (optional)

A plugin isn't limited to adding routes — `register(app, ctx)` receives the real `FastAPI` instance, so a plugin with its own domain-specific exception type can register a handler for it directly:

```python
class InvoiceLimitExceeded(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit


async def register(self, app: FastAPI, ctx: KernelContext) -> None:
    ...
    async def _handle_invoice_limit(request: Request, exc: InvoiceLimitExceeded):
        return JSONResponse(status_code=409, content={"error": "INVOICE_LIMIT_EXCEEDED", "limit": exc.limit})

    app.add_exception_handler(InvoiceLimitExceeded, _handle_invoice_limit)
```

This is safe because FastAPI dispatches exception handlers **by exception type**, not registration order — two plugins each registering a handler for their own distinct exception type never conflict or shadow each other.

**Don't do the same for `app.add_middleware(...)`.** Unlike exception handlers, middleware order *is* significant (`app/main.py` documents this: "outermost = first to receive requests"), and FastAPI/Starlette apply middleware in reverse-registration order. Multiple plugins each calling `app.add_middleware()` from their own `register()` — with no visibility into what order plugins load in, or what other plugins already added — is a real way to get subtly wrong request handling that's hard to debug. If a plugin genuinely needs middleware-like behavior, prefer a dependency (`Depends(...)`) on its own router instead, which is scoped to that plugin's own routes and has no cross-plugin ordering risk. If you truly need app-wide middleware, add it in `app/main.py` alongside the existing ones, where all the ordering is visible in one place — not from inside a plugin.

## Plugin-specific settings (optional)

`app/config.py`'s `Settings` only holds kernel-level configuration (`plugin_load_mode`, `enabled_plugins`, JWT/rate-limit/CORS settings, ...). It is **not** the place to add a plugin's own options — putting `BILLING_INVOICE_PREFIX` there would mean the kernel's config file grows (and needs a code review) every time any plugin wants a new setting, and that setting lingers in `app/config.py` even after the plugin is removed.

If your plugin needs its own configuration, define a separate `pydantic-settings` class instead, scoped to that plugin:

```python
# app/plugins/billing_plugin/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class BillingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLING_", env_file=".env", extra="ignore")
    invoice_prefix: str = "INV-"

billing_settings = BillingSettings()
```

Read it directly wherever the plugin needs it (`service.py`, `router.py`) — there's no dedicated kernel mechanism for this (`KernelContext.settings` is still the one shared kernel `Settings` instance), so this is just a plain Python import local to your plugin. This is optional and has a real trade-off: settings become discoverable by reading the kernel's `.env.example` alone *plus* every plugin's own `settings.py`, instead of one file. For a small number of plugins, keeping everything in `app/config.py` is often simpler — reach for a per-plugin settings class once a plugin's option count and independence justify it.

## Testing a plugin

`tests/conftest.py` provides three fixtures:

- **`session`** — a raw `AsyncSession` against an in-memory SQLite database, for unit-testing a `Service` class directly.
- **`client`** — a full `httpx.AsyncClient` wired to the same in-memory database, with the real FastAPI `lifespan` driven (so `PluginLoader` actually runs and your routes are mounted) — use this for HTTP-level tests.
- **`engine`** — session-scoped; dynamically imports every plugin's `models.py` under `app/plugins/*` so `Base.metadata` is fully populated before tests run. You don't need to register a newly scaffolded plugin's models here yourself.

```python
@pytest.mark.asyncio
async def test_create_invoice(client):
    resp = await client.post("/api/v1/billing-plugin", json={"name": "Example"})
    assert resp.status_code == 201
```

Before committing, run the architecture boundary check (also part of `pytest`, but useful standalone while iterating):

```bash
python scripts/check_architecture_boundaries.py
```

## Introspecting a loaded plugin

`GET /api/v1/health` reports every loaded plugin's `state` (`PENDING`/`REGISTERED`/`BOOTED`/`FAILED`/`SHUTDOWN`), `version`, `api_version`, `dependencies`, and last `error` — useful while developing a new plugin to see exactly which lifecycle stage failed. The same response also lists registered facades and, separately, every capability published to `service_registry` and every event with active `event_bus` subscribers — handy for confirming your plugin's `boot()` actually published/subscribed to what you expect. See [operations.md](./operations.md#health--introspection).

## If your plugin starts background tasks

Neither shipped plugin does this, but if yours needs an `asyncio.create_task(...)` (a poller, a scheduled job) or any other resource that isn't a route/service/subscription, `reload_one()` (see [operations.md](./operations.md#hot-reloading-a-plugin)) has no way to know about it — it only tears down routes, `service_registry` entries, and `event_bus` subscriptions. An unmanaged background task survives a hot-reload, and `boot()` running again starts a second one, silently duplicating work.

`reload_one()` *does* still call the old instance's `shutdown()` before rebuilding — so the fix is entirely in your plugin: hold on to anything you start, and cancel/close it in `shutdown()`:

```python
class BillingPlugin(AbstractPlugin):
    name = "billing_plugin"

    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        self._poll_task = asyncio.create_task(self._poll_invoices())

    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        self._poll_task.cancel()
```

This is a plugin-author convention, not something the kernel enforces or does for you.
