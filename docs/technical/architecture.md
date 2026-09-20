# Architecture

This project is a single-process, single-database implementation of the **Microkernel (plugin-in-process) architecture pattern** on top of FastAPI. A small, stable *kernel* owns startup/shutdown, cross-cutting infrastructure, and a handful of communication primitives. Everything domain-specific — authentication, catalog management, and anything you add — lives in *plugins* that the kernel discovers and drives through a fixed lifecycle, without ever importing them by name.

> Scope note: this document covers the in-process design. It does not assume, and does not require, splitting plugins into separately deployed services — see [operations.md](./operations.md) for the single-process deployment model.

## Layers

```
app/
├── core/            # The kernel: contracts, registries, the loader, cross-cutting infra
│   ├── plugin_base.py    # AbstractPlugin contract + KernelContext
│   ├── registry.py       # PluginRegistry, ServiceRegistry (+ scoped views)
│   ├── hooks.py          # EventBus (+ scoped view)
│   ├── loader.py         # PluginLoader: discovery, ordering, lifecycle, hot-reload
│   ├── errors.py         # PluginError / PluginErrorCode
│   ├── error_handler.py  # Global exception -> JSON 500 middleware
│   ├── security.py       # JWT auth FastAPI dependencies
│   └── rate_limiter.py   # slowapi Limiter singleton
│
├── infrastructure/  # Shared drivers plugins are built on top of
│   ├── database.py       # Async SQLAlchemy engine/session factory + shared Base
│   ├── jwt.py             # JWTService
│   └── password.py        # bcrypt hashing helpers
│
├── plugins/         # Dynamically discovered, independently loadable units
│   ├── user_plugin/       # Auth, profiles, roles
│   └── product_plugin/    # Catalog, inventory
│
├── facades/         # Cross-plugin coordination, kept separate from any one plugin
│   ├── __init__.py        # Imports every facade's router module (triggers self-registration)
│   ├── base.py             # AbstractFacade contract
│   ├── registry.py         # FacadeRegistry (facades + their routers)
│   ├── catalog_facade.py   # Combines the user + product capabilities
│   └── router.py           # HTTP surface for the facade; self-registers at the bottom
│
├── api/v1/          # Routes owned directly by the kernel (not by any plugin)
│   ├── health.py          # GET /api/v1/health — plugin introspection
│   └── admin.py           # POST /api/v1/admin/plugins/{name}/reload — hot reload
│
├── config.py        # Settings (env-driven)
└── main.py          # FastAPI app factory + lifespan
```

The direction of dependency is deliberate and enforced (see [Module boundaries](#module-boundaries) below):

```
app.api.v1  ──▶  app.facades  ──▶  app.plugins.*  ──▶  app.infrastructure
                                        ▲
app.core  (never imports app.plugins.* or app.facades.*)
```

- **The kernel (`app.core.*`) never imports a plugin.** It only knows about the `AbstractPlugin` contract, not any concrete plugin class.
- **Plugins never import each other.** Two plugins that need to collaborate do so through the kernel's `ServiceRegistry` and `EventBus` — never `from app.plugins.other_plugin.service import ...`.
- **Facades depend on plugins**, resolved the same way — via `service_registry`, never a direct import of a plugin's `Service` class. Plugins never depend on a facade.
- **The kernel's own `api/v1/*` routes never import plugin internals** (schemas, services, models). A route that needs plugin data belongs to a facade (`app/facades/router.py`), not to `app/api/v1/`.

## Kernel components

### `AbstractPlugin` and `KernelContext`

Every plugin is a class implementing three async hooks:

```python
class AbstractPlugin(ABC):
    name: str = ""
    api_version: ClassVar[str] = KERNEL_API_VERSION   # compatibility check, see below
    dependencies: ClassVar[list[str]] = []             # other plugin names, see ordering below

    async def register(self, app: FastAPI, ctx: KernelContext) -> None: ...
    async def boot(self, app: FastAPI, ctx: KernelContext) -> None: ...
    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None: ...
```

`KernelContext` is a small frozen dataclass carrying everything the kernel is willing to hand a plugin:

```python
@dataclass(frozen=True)
class KernelContext:
    event_bus: EventBus
    settings: Settings
    service_registry: ServiceRegistry
    db_factory: DatabaseFactory
```

Passing this explicitly — instead of a plugin doing `from app.core.hooks import event_bus` itself — means a plugin's dependency on the kernel shows up in its method signature, and a caller (a test, or the hot-reload path) can substitute different components without monkeypatching module globals.

`ctx.event_bus` and `ctx.service_registry` are not the raw singletons — they're views scoped to that specific plugin's name (see [Hot reload](#hot-reload) below for why).

### `PluginLoader`: discovery, ordering, lifecycle

`app/core/loader.py`'s `PluginLoader` is the only thing that touches `app/plugins/` by directory name. On startup it:

1. **Discovers** every sub-package of `app/plugins/` that exposes a top-level `plugin: AbstractPlugin` attribute (see `app/plugins/<name>/__init__.py`). A plugin whose directory isn't in `settings.enabled_plugins` (if that's set), or whose `api_version` doesn't match the kernel's major version, is skipped with a logged warning rather than crashing.
2. **Topologically sorts** the discovered plugins by their declared `dependencies` (Kahn's algorithm), so "A depends on B" always means B finishes `register()`+`boot()` before A starts. A missing dependency or a dependency cycle aborts startup immediately with a clear `PluginLoadError` — that's a configuration bug, not a plugin misbehaving at runtime.
3. Calls `register()` on every plugin (in order), then `boot()` on every plugin (in order) — two separate passes, so that by the time any plugin's `boot()` runs, every other plugin has already finished `register()`. Subscribing to another plugin's events (see below) is only safe from `boot()`, for this reason.
4. **Isolates failures.** If `register()`/`boot()` raises, that plugin (and, transitively, anything depending on it) is marked `FAILED` in the `PluginRegistry` with the error message attached. `settings.plugin_load_mode` decides what happens next: `fail_fast` (the default) re-raises and aborts the whole startup; `best_effort` isolates just the failing subtree and lets the rest of the kernel come up.

On shutdown, plugins are torn down in reverse order, then `service_registry`/`event_bus` are cleared — anything a plugin published is stale once every plugin has been unregistered.

### `PluginRegistry` and plugin state

`app/core/registry.py`'s `PluginRegistry` is the catalog of currently-loaded plugin *instances*, plus a lifecycle state machine per plugin:

```
PENDING → REGISTERED → BOOTED
              ↘           ↘
               FAILED     SHUTDOWN
```

`GET /api/v1/health` (see [operations.md](./operations.md)) exposes this directly — for every loaded plugin: its `state`, `version`, `api_version`, declared `dependencies`, and last `error` if it failed. The same endpoint also lists every registered facade, every capability currently published to `service_registry`, and every event with active `event_bus` subscribers — see the next two sections.

### `ServiceRegistry` — the only sanctioned way to reach another plugin's service

```python
# inside product_plugin's boot()
ctx.service_registry.provide("product_service_factory", lambda session: ProductService(session))

# inside a facade
result = ctx.service_registry.resolve("product_service_factory")   # Result[Factory, PluginError]
```

A plugin **publishes** a factory callable under a string key (typically in `boot()`); any other plugin or facade **resolves** that key to get an instance, instead of importing the class directly. If the publishing plugin isn't loaded, `resolve()` returns `Err(...)` — the caller degrades gracefully instead of an `ImportError` at process start.

`service_registry.names()` lists every currently-published capability — `GET /api/v1/health`'s `capabilities` field surfaces this, so what's published isn't a black box you'd otherwise have to read logs or source to see.

### `EventBus` — fire-and-forget pub/sub

```python
# inside user_plugin's boot()
ctx.event_bus.subscribe("product.created", handler)

# inside product_plugin's service layer
await event_bus.emit("product.created", product_id=product.id, sku=product.sku)
```

`emit()` runs every subscribed handler concurrently and swallows individual handler exceptions (logged, not propagated) so one bad subscriber never breaks the emitter. This is what lets `user_plugin` react to a `product_plugin` event without ever importing anything from `app.plugins.product_plugin` — disable `product_plugin` (via `enabled_plugins`) and `user_plugin` still boots fine; it just never receives that event.

`event_bus.subscriptions()` lists every event with active subscribers and who subscribed — `GET /api/v1/health`'s `event_subscriptions` field surfaces this, for the same reason `capabilities` surfaces `service_registry`.

### `AbstractFacade` and `FacadeRegistry`

A **facade** coordinates two or more plugins behind one API — e.g. `CatalogFacade` verifies a user is active (via `user_plugin`'s service) before creating a product (via `product_plugin`'s service). Facades:

- Resolve plugin capabilities through `service_registry`, exactly like any other consumer — never by importing a plugin's `Service` class. They don't receive a `KernelContext` the way plugins do (facades aren't lifecycle-managed), so this is a direct import of the `service_registry` singleton — safe, since `resolve()` doesn't register anything and needs no `owner` scoping.
- Self-register their class — and their router, if they have one — with `facade_registry` (`facade_registry.register(CatalogFacade, router=router)` at the bottom of `app/facades/router.py`), so the kernel has one place to enumerate what cross-plugin coordination exists in the app.
- Own their own HTTP router (`app/facades/router.py`) instead of living in the kernel's own `api/v1` routes — a facade's route depends on plugin DTOs, which the kernel's own routes must not.

`app/facades/__init__.py` imports every facade's router module (so the self-registration above actually runs), and `main.py` mounts whatever `facade_registry.routers()` returns — it never imports a specific facade module by name. Adding a second facade means adding one import line to `app/facades/__init__.py`, not editing `main.py`, mirroring how adding a plugin never requires editing `main.py`/`loader.py` either.

Dependency direction is one-way: facades depend on plugins, plugins never depend on a facade.

## Startup and shutdown lifecycle

```
create_app()
 └─ FastAPI(lifespan=lifespan)
      │
      ▼ (on startup)
 loader = PluginLoader(app); app.state.plugin_loader = loader
 loader.load_all()
   ├─ discover()               # scan app/plugins/*, filter by enabled_plugins + api_version
   ├─ resolve_load_order()     # topological sort by `dependencies`
   ├─ for each plugin: register(app, ctx)   # routes, SQLAlchemy models
   └─ for each plugin: boot(app, ctx)       # publish services, subscribe to events
 db_factory.create_all()       # tables created only after every register() has imported its models
      │
      ▼ (serving requests)
      │
      ▼ (on shutdown)
 loader.unload_all()
   ├─ for each plugin (reverse order): shutdown(app, ctx)
   └─ service_registry.clear(); event_bus.clear()
 db_factory.dispose()
```

## Module boundaries

The dependency directions above are not just convention — `scripts/check_architecture_boundaries.py` statically enforces them by walking every `.py` file under `app/` with `ast` and checking:

1. A file under `app.plugins.<a>` must not import anything from `app.plugins.<b>` for `a != b`.
2. A file under `app.api.v1.*` must not import `app.plugins.*.schemas` / `.service` / `.models`.
3. A file under `app.core.*` must not import `app.plugins.*` at all.
4. A plugin's `plugin.py` must not import `service_registry`/`event_bus` directly — it must reach them through `ctx` (see [Where `ctx` is required, and where it isn't](./plugin-development.md#where-ctx-is-required-and-where-it-isnt) in plugin-development.md). `service.py`/`router.py` are exempt — they never receive a `ctx`, and calling `.resolve(...)`/`.emit(...)` directly is safe.

It's wired into the test suite (`tests/test_architecture_boundaries.py`) so a violation fails `pytest`, not just a manual review. Run it directly with:

```bash
python scripts/check_architecture_boundaries.py
```

The same discipline extends to the data layer: plugins share one SQLAlchemy `Base`/engine (a single-database simplification), but a plugin's model must never declare a `ForeignKey` into another plugin's table, and no plugin may query another plugin's model class directly — that access always goes through the other plugin's `Service` (via `service_registry`) or an `EventBus` notification. This half of the rule isn't statically checkable and remains a code-review guideline.

## Hot reload

Because `ctx.service_registry`/`ctx.event_bus` are views scoped to the owning plugin's name (`ScopedServiceRegistry`/`ScopedEventBus`), the kernel can selectively tear down and rebuild a single plugin on a live app — see [operations.md](./operations.md#hot-reloading-a-plugin) for the endpoint and its limitations.

## Further reading

- [plugin-development.md](./plugin-development.md) — writing a new plugin against this contract.
- [operations.md](./operations.md) — configuration, health/introspection, hot reload, deployment.
- [api-conventions.md](./api-conventions.md) — the `Result`/`PluginError` pattern and HTTP conventions every endpoint follows.
- [testing.md](./testing.md) — the test suite: fixtures, per-file coverage map, and testing conventions.
