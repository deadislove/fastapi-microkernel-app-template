# fastapi-microkernel-app-template

Modular FastAPI Core system based on **Microkernel Architecture** with dynamic Plugin loader, JWT Security, Rate Limiting, API Versioning, Result Pattern, Global Error Handling, Swagger UI (OpenAPI), Facade pattern, and Docker deployment setup.

---

## Architecture Overview

```
app/
├── core/           # Microkernel engine
│   ├── plugin_base.py      # AbstractPlugin interface
│   ├── registry.py         # Plugin catalog (singleton)
│   ├── loader.py           # Dynamic plugin discovery & lifecycle
│   ├── hooks.py            # Async EventBus for cross-plugin comms
│   ├── errors.py           # PluginError + PluginErrorCode
│   ├── error_handler.py    # Global exception middleware
│   ├── rate_limiter.py     # slowapi limiter singleton
│   └── security.py         # JWT FastAPI dependencies
│
├── infrastructure/ # Shared drivers
│   ├── database.py         # Async SQLAlchemy factory + Base
│   ├── jwt.py              # JWTService (encode/decode)
│   └── password.py         # bcrypt helpers
│
├── plugins/        # Dynamic plugins (auto-discovered)
│   ├── user_plugin/        # Auth, profiles, roles
│   └── product_plugin/     # Catalog, inventory
│
├── facades/        # Cross-plugin coordination
│   ├── catalog_facade.py   # User ↔ Product operations (resolved via service_registry)
│   └── router.py           # GET/POST /api/v1/catalog/*: lives beside its facade,
│                            #   not in api/v1/, so the kernel route layer never
│                            #   carries a compile-time dependency on a plugin's DTOs
│
├── api/v1/         # Versioned HTTP endpoints owned by the kernel itself
│   └── health.py           # GET /api/v1/health: kernel + per-plugin lifecycle state
│
├── config.py       # Pydantic Settings (reads .env)
└── main.py         # FastAPI app factory + lifespan
```

---

## Quick Start

```bash
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and configure environment
cp .env.example .env

# 4. Run the application
uvicorn app.main:app --reload
```

Swagger UI: **http://localhost:8000/docs**

---

## Commands

| Command | Description |
|---|---|
| `uvicorn app.main:app --reload` | Run with hot-reload |
| `pytest` | Run all tests |
| `ruff check . --fix` | Lint and auto-fix |
| `alembic upgrade head` | Apply DB migrations |
| `docker compose up -d --build` | Start full stack in Docker |

---

## Plugin System

Every plugin lives in `app/plugins/<name>/` and must:

1. Expose a top-level `plugin` attribute (an `AbstractPlugin` instance) in its `__init__.py`
2. Implement `register(app, ctx)`, `boot(app, ctx)`, and `shutdown(app, ctx)` hooks; `ctx` is a `KernelContext` carrying `event_bus`, `settings`, `service_registry`, and `db_factory`, so a plugin's dependency on the kernel is visible in its signature instead of hidden behind `from app.core... import ...`
3. Import its SQLAlchemy models inside `register()` so the DB factory picks them up (use `import ... as _models`, not a bare `import app.plugins...`; the latter rebinds the `app` parameter to the `app` package and breaks `app.include_router(...)`)
4. Declare any other plugins it depends on via `dependencies`, and publish anything it wants other plugins/facades to use via `ctx.service_registry` in `boot()`, never by letting another module `import` this plugin's `Service`/model classes directly
5. Subscribe to other plugins' events via `ctx.event_bus.subscribe(...)` in `boot()` (not `register()`; every plugin has finished `register()` by the time `boot()` runs) instead of importing the emitting plugin at all

```python
# app/plugins/my_plugin/__init__.py
from app.plugins.my_plugin.plugin import MyPlugin
plugin = MyPlugin()

# app/plugins/my_plugin/plugin.py
from app.core.plugin_base import AbstractPlugin, KernelContext
from app.plugins.my_plugin.service import MyService

class MyPlugin(AbstractPlugin):
    name = "my_plugin"
    dependencies = ["user_plugin"]  # optional: must register()+boot() before this one

    async def register(self, app, ctx: KernelContext):
        import app.plugins.my_plugin.models as _models  # register models
        from app.plugins.my_plugin.router import router
        app.include_router(router, prefix="/api/v1")

    async def boot(self, app, ctx: KernelContext):
        # Publish this plugin's capability so facades/other plugins can reach
        # it via `ctx.service_registry.resolve("my_service_factory")` instead
        # of importing MyService directly.
        ctx.service_registry.provide("my_service_factory", lambda session: MyService(session))

        # React to another plugin's event without importing it at all.
        ctx.event_bus.subscribe("user.created", self._on_user_created)

    async def shutdown(self, app, ctx: KernelContext): ...

    @staticmethod
    async def _on_user_created(**kwargs) -> None: ...
```

If a plugin's `register()`/`boot()` raises, the kernel isolates it: `settings.plugin_load_mode` controls whether that aborts the whole startup (`fail_fast`, the default) or just skips that plugin (and anything depending on it) while the rest of the kernel still starts (`best_effort`). `GET /api/v1/health` reports each plugin's `state` (`PENDING`/`REGISTERED`/`BOOTED`/`FAILED`/`SHUTDOWN`), `api_version`, `dependencies`, and `error`.

### Hot-reloading a single plugin

`POST /api/v1/admin/plugins/{name}/reload` (admin-only) shuts one plugin down, drops its routes and its `service_registry`/`event_bus` registrations, re-imports its `service.py`/`router.py`/`plugin.py`, and runs a fresh instance's `register()`+`boot()`, all on the already-running app, no process restart. It deliberately does **not** reload `models.py`/`schemas.py`: SQLAlchemy's `Base.metadata` is a process-wide registry that can't redefine a table's mapped class, so schema changes always need a full restart.

### Data layer boundaries

All plugins share one `Base`/engine (single-process, single-database template): that's a deployment simplification, not an invitation to couple plugins at the data layer:

- A plugin's SQLAlchemy model must never declare a `ForeignKey` into another plugin's table.
- No plugin may `select()`/query another plugin's model class directly.
- Cross-plugin data access always goes through the other plugin's Service (resolved via `service_registry`, never by importing its model class) or through an `event_bus` notification.

The cross-plugin-import half of this (rule 1 below) is enforced automatically; see the next section. The FK/query discipline isn't statically checkable and is a code-review guideline.

### Architecture boundary checks

`python scripts/check_architecture_boundaries.py` (also wired into `pytest` via `tests/test_architecture_boundaries.py`, since this repo has no separate CI pipeline) statically enforces:

1. Plugins may not import another plugin's internals (schemas/service/models/router); only `service_registry`/`event_bus`.
2. `app/api/v1/*` (kernel routes) may not import any plugin's `schemas`/`service`/`models`.
3. `app/core/*` (the kernel) may not import `app.plugins.*` at all.

---

## Result Pattern

All service and facade methods return `Result[T, PluginError]` from the `result` library:

```python
from result import Ok, Err

async def get_user(user_id: str) -> Result[User, PluginError]:
    user = await session.get(User, user_id)
    if not user:
        return Err(PluginError.not_found("User", user_id))
    return Ok(user)

# At the router level:
result = await service.get_user(user_id)
if result.is_err():
    raise HTTPException(404, detail=result.unwrap_err().message)
return result.unwrap()
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-...` | JWT signing key |
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` | Async DB URL |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token TTL |
| `PLUGIN_LOAD_MODE` | `fail_fast` | `fail_fast` aborts startup on any plugin failure; `best_effort` isolates it |
| `ENABLED_PLUGINS` | *(unset = all)* | JSON array of plugin dir names to load, e.g. `["user_plugin"]` |
| `RATE_LIMIT_DEFAULT` | `100/minute` | Global rate limit |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | CORS origins |

---

## Docker

```bash
# Start PostgreSQL + app
docker compose up -d --build

# Run migrations inside the container
docker compose exec app alembic upgrade head
```

---

## Testing

```bash
pytest                    # all tests
pytest tests/test_core.py # core unit tests only
pytest -v                 # verbose output
```

Tests use an **in-memory SQLite** database, no external services required.

---

## Documentation

In-depth technical docs live in [`docs/technical/`](./docs/technical/): architecture, plugin development, operations, API conventions, and testing.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for dev setup, the pre-PR checklist, and how to scaffold a new plugin. Participation is governed by our [Code of Conduct](./CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](./SECURITY.md) to report a vulnerability privately, and for a list of known, intentional simplifications this template makes.

## License

MIT, see [LICENSE](./LICENSE). Copyright (c) 2026 [Ta-Wei Lin](https://www.linkedin.com/in/da-wei-lin-689a35107/).
