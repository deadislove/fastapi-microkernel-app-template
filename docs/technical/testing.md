# Testing

```bash
pytest                    # all tests
pytest tests/test_core.py # core/kernel unit tests only
pytest -v                 # verbose output
```

Tests run against an **in-memory SQLite** database: no external services, no file left behind. This document maps out the test suite and the conventions behind it; for "how do I test a new plugin," see [plugin-development.md#testing-a-plugin](./plugin-development.md#testing-a-plugin) instead.

## Fixtures (`tests/conftest.py`)

| Fixture | Scope | Use for |
|---|---|---|
| `engine` | session | The shared in-memory SQLAlchemy engine. Dynamically imports every plugin's `models.py` under `app/plugins/*` before creating tables; mirrors `PluginLoader._discover()`'s scan, so a new plugin never needs this file edited. |
| `session` | function | A raw `AsyncSession` bound to `engine`, rolled back after each test. Use for unit-testing a `Service` class directly, without going through HTTP. |
| `client` | function | A full `httpx.AsyncClient` against a real `create_app()`, with the app's actual `lifespan` driven via `app.router.lifespan_context(app)`, so `PluginLoader` really runs and plugin/facade routes are really mounted. Also resets the shared rate limiter (`limiter.reset()`) so accumulated request counts from earlier tests don't 429 a later one. |

The `client` fixture deliberately exercises the *real* startup path rather than mounting routers by hand: that's what catches issues like a plugin's routes never getting registered, not just its business logic being individually correct.

## Test file map

| File | Covers |
|---|---|
| `test_core.py` | The kernel itself: `PluginRegistry`/`ServiceRegistry`/`EventBus` (including the `Scoped*` owner-tagging views), `PluginLoader` (dependency ordering, fail-fast/best-effort isolation, `enabled_plugins` filtering, injectable components), `KernelContext` wiring, `FacadeRegistry`, `AbstractPlugin.api_version`/`version`, `JWTService`, password hashing, `PluginError`. The largest file by far; most kernel behavior is unit-tested here without needing HTTP. |
| `test_api.py` | End-to-end HTTP: auth flow, user endpoints, product endpoints, `GET /api/v1/health`'s full response shape. |
| `test_facade.py` | The Catalog Facade's HTTP routes specifically (creation/lookup/inventory adjustment via the facade, not the plugin's own router). |
| `test_events.py` | Proves the `EventBus` end-to-end: a real HTTP request triggers `user_plugin`'s subscription to `product_plugin`'s event (checked via `caplog`), and `user_plugin` still boots with `product_plugin` excluded via `enabled_plugins`. |
| `test_hot_reload.py` | `ServiceRegistry.revoke_all_from`/`EventBus.unsubscribe_all_from`/`Scoped*` building blocks, plus the admin reload endpoint end-to-end (routes/capabilities survive a reload, dependents are reported, auth is enforced). |
| `test_architecture_boundaries.py` | Runs `scripts/check_architecture_boundaries.py` against the real codebase, **and** against synthetic fake `app/` trees (via `tmp_path`) built to deliberately violate each of the 4 rules, proving the checker actually catches something, not just that it passes trivially against already-clean code. |
| `test_scaffold.py` | `scripts/new_plugin.py`'s `_render()` function only: checks the generated source is syntactically valid and matches the current `AbstractPlugin` contract. Deliberately never calls the script's `main()`, which writes into the real `app/plugins/` directory; a test run must never do that as a side effect. |
| `test_product_plugin.py` / `test_user_plugin.py` | Each plugin's `Service` layer in isolation, via the `session` fixture. |

## Testing conventions

**Prove a check actually catches something.** A "no violations found" test (`test_no_architecture_boundary_violations`) is a necessary but weak assertion: nothing stops it from passing trivially if the check itself is broken. `test_architecture_boundaries.py` pairs it with tests that build a minimal fake `app/` tree, introduce one specific violation, and assert it's caught, one pair per rule. Apply the same standard to any other static check you add.

**Test the scaffold's output without touching the filesystem.** `scripts/new_plugin.py`'s `_render(name)` is a pure function returning `{filename: content}`; `test_scaffold.py` calls it directly and `compile()`s each result, instead of running the script and asserting on files it wrote into `app/plugins/`. Compiling isn't the same as running, though, so before trusting that split: the scaffold tool was manually verified end-to-end once (generating a real plugin into a scratch copy of the repo, running its generated test, running the full suite) while it was being built. That verification is not repeated as a permanent test: a one-off "does the mechanism actually work end-to-end" check is meant to be done once and its result recorded (e.g. in the PR description), not left as throwaway code that would otherwise write into `app/plugins/` on every `pytest` run. Apply the same pattern to any future one-off end-to-end verification: do it, record the result, then remove the scratch artifact.

**Fake plugins for loader tests share one autouse cleanup fixture.** Tests that inject fake `AbstractPlugin` subclasses into the *real* `plugin_registry`/`service_registry` singletons (to test `PluginLoader` behavior; see `test_core.py`'s loader section) name them with a `loader_test_` prefix and rely on an autouse fixture that scrubs anything matching that prefix after each test. Follow this convention for new loader tests, or use `PluginLoader`'s injectable `plugin_registry=`/`service_registry=` constructor parameters (see [architecture.md](./architecture.md#pluginloader-discovery-ordering-lifecycle)) to avoid touching the global singletons at all.
