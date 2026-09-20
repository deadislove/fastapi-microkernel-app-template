# Operations

This document covers configuring, observing, and operating a running instance: the settings reference, the health/introspection endpoint, plugin activation, hot-reloading a single plugin, and deployment.

## Configuration reference

Settings are defined in `app/config.py` (a `pydantic-settings` model) and read from environment variables / a `.env` file (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `fastapi-microkernel-app-template` | Shown in the OpenAPI docs title. |
| `APP_ENV` | `development` | Free-form environment label. |
| `DEBUG` | `true` | Enables SQL echo logging and `DEBUG`-level app logs. |
| `SECRET_KEY` | *(placeholder — change in production)* | JWT signing key. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token TTL. |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token TTL. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` | Any SQLAlchemy async URL (`asyncpg` for Postgres, `aiomysql` for MySQL, etc.). |
| `PLUGIN_LOAD_MODE` | `fail_fast` | `fail_fast` aborts startup if any plugin's `register()`/`boot()` raises; `best_effort` isolates the failing plugin (and anything depending on it) and still starts the rest of the kernel. |
| `ENABLED_PLUGINS` | *(unset = load everything)* | JSON array of plugin directory names to load, e.g. `["user_plugin"]`. Lets the same codebase run different plugin sets per environment without touching code. |
| `RATE_LIMIT_DEFAULT` | `100/minute` | Default `slowapi` rate limit (per-route decorators can override this). |
| `ALLOWED_ORIGINS` | `["http://localhost:3000","http://localhost:8000"]` | CORS allow-list, JSON array. |

## Health & introspection

```
GET /api/v1/health
```

```json
{
  "status": "ok",
  "plugins": [
    {
      "name": "product_plugin",
      "state": "BOOTED",
      "version": "0.0.0",
      "api_version": "1.0",
      "dependencies": [],
      "error": null
    },
    {
      "name": "user_plugin",
      "state": "BOOTED",
      "version": "0.0.0",
      "api_version": "1.0",
      "dependencies": [],
      "error": null
    }
  ],
  "facades": ["catalog_facade"],
  "capabilities": ["product_service_factory", "user_service_factory"],
  "event_subscriptions": {
    "product.created": ["user_plugin"]
  }
}
```

`state` is one of `PENDING` (discovered, hooks not run yet), `REGISTERED`, `BOOTED`, `FAILED` (see `error` for why), or `SHUTDOWN`. This is the fastest way to tell, in any environment, exactly which plugin — and which lifecycle stage — is responsible for a degraded deployment, instead of grepping logs for a stack trace.

`version` is the plugin's own, purely informational release version (`"0.0.0"` if it doesn't track one) — not to be confused with `api_version`, which is about kernel-contract compatibility (see [plugin-development.md](./plugin-development.md#api_version)) and is checked by the loader; `version` isn't inspected by anything, it's just useful once a plugin has its own release cadence.

`facades` lists every registered facade name (`facade_registry.names()`) — the cross-plugin coordination points that exist in this process, alongside the plugins they coordinate.

`capabilities` (`service_registry.names()`) and `event_subscriptions` (`event_bus.subscriptions()`, event name → subscribing plugin names) turn the two core cross-plugin collaboration primitives from a black box into something you can actually see at runtime — e.g. confirming that disabling a plugin (via `enabled_plugins`) really did remove the capability/subscription you expected, without reading logs or source.

## Controlling which plugins load

Set `ENABLED_PLUGINS` to a JSON array to restrict startup to a subset:

```bash
ENABLED_PLUGINS='["user_plugin"]' uvicorn app.main:app
```

Any plugin not in the list is skipped at discovery time (logged, not an error). This is also how [plugin-development.md](./plugin-development.md) and the test suite prove that an event subscriber plugin doesn't hard-depend on the plugin it's listening to — `user_plugin` boots fine with `product_plugin` excluded; it just never receives `product.created`.

## Plugin load failure behavior

`PLUGIN_LOAD_MODE=fail_fast` (default) is the safer choice for production: a broken plugin should stop the deployment rather than silently serve a half-working app. `PLUGIN_LOAD_MODE=best_effort` is useful in development or for large plugin counts where isolating one broken plugin and still exercising the rest is more valuable than an all-or-nothing startup.

A **missing or circular dependency** in the `dependencies` graph (see [plugin-development.md](./plugin-development.md#dependencies)) always aborts startup, regardless of this setting — that's a static configuration error, not a plugin failing at runtime.

## Hot-reloading a plugin

```
POST /api/v1/admin/plugins/{name}/reload
Authorization: Bearer <admin JWT>
```

Returns `200` on success, `404` if `name` isn't currently loaded, `401`/`403` if the caller isn't authenticated as an admin.

```json
{
  "reloaded": "product_plugin",
  "dependents_may_need_reload": []
}
```

This reloads a single plugin **on the already-running process** — no restart:

1. Calls the current instance's `shutdown()`.
2. Removes every route it owns from the live FastAPI router.
3. Revokes everything it published to `service_registry` and every `event_bus` subscription it made (tracked by plugin-name ownership — see [architecture.md](./architecture.md#hot-reload)).
4. Re-imports its `service.py`, `router.py`, and `plugin.py` modules (`importlib.reload`).
5. Instantiates the fresh `plugin` object and runs `register()`+`boot()` again.

**Deliberately does not reload `models.py`/`schemas.py`.** SQLAlchemy's `Base.metadata` is a process-wide registry that doesn't support redefining a table's mapped class — a schema change (new column, new table) always needs a full process restart. This endpoint is for iterating on route/service logic only.

**Does not cascade to dependents.** `dependents_may_need_reload` lists every other currently-loaded plugin that declares the reloaded one in its `dependencies` — `reload_one()` doesn't reload them automatically, it's advisory information for the caller. A plugin that only resolves its dependency's capability per-call (the pattern every shipped plugin uses) keeps working correctly with no action needed; this only matters for a plugin that cached something from its dependency back in `boot()`.

## Deployment

### Docker

```bash
docker compose up -d --build      # Postgres + app
docker compose exec app alembic upgrade head
```

`docker-compose.yml` runs Postgres (`postgres:16-alpine`) alongside the app container, wiring `DATABASE_URL` to it automatically. Override `SECRET_KEY`, `POSTGRES_*` via your shell environment or an `.env` file next to `docker-compose.yml` before running in anything but local development.

### Migrations

Schema changes go through Alembic (`alembic/`), independent of the app's own runtime — `db_factory.create_all()` at startup is a convenience for fresh/dev databases (it's a no-op against tables that already exist), not a substitute for migrations in a real deployment.

```bash
alembic upgrade head
```

### Logging

`app/main.py` configures `logging.basicConfig` at `DEBUG` level when `DEBUG=true`, `INFO` otherwise. Every kernel component (`app.core.loader`, `app.core.hooks`, ...) and plugin logs through the standard `logging` module under its own module name, so log filtering/aggregation by logger name works out of the box.
