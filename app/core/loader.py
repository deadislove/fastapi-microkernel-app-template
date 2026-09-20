from __future__ import annotations

import importlib
import logging
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI
from result import Err, Ok, Result

from app.config import Settings
from app.config import settings as _default_settings
from app.core.errors import PluginError, PluginErrorCode
from app.core.hooks import EventBus, ScopedEventBus
from app.core.hooks import event_bus as _default_event_bus
from app.core.plugin_base import (
    KERNEL_API_VERSION,
    AbstractPlugin,
    KernelContext,
    is_api_version_compatible,
)
from app.core.registry import (
    PluginRegistry,
    PluginState,
    ScopedServiceRegistry,
    ServiceRegistry,
)
from app.core.registry import (
    plugin_registry as _default_plugin_registry,
)
from app.core.registry import (
    service_registry as _default_service_registry,
)
from app.infrastructure.database import DatabaseFactory
from app.infrastructure.database import db_factory as _default_db_factory

logger = logging.getLogger(__name__)

# Plugins are discovered by scanning this package directory
_PLUGINS_PACKAGE = "app.plugins"
_PLUGINS_DIR = Path(__file__).parent.parent / "plugins"

# Submodules reloaded by `reload_one()`, in dependency order. Deliberately
# excludes `models`/`schemas` — see `reload_one()`'s docstring.
_HOT_RELOAD_SUBMODULES = ("service", "router", "plugin")


class PluginLoadError(RuntimeError):
    """
    Raised when the plugin set can't be loaded at all:
      - a plugin's register()/boot() failed in fail_fast mode, or
      - the declared dependency graph is missing a plugin or has a cycle.
    The latter is always fatal regardless of `plugin_load_mode` — it's a
    configuration error, not a single misbehaving plugin.
    """


class PluginLoader:
    """
    Discovers, instantiates, and drives the lifecycle of all plugins.

    Discovery strategy: every sub-package inside `app/plugins/` that exposes a
    `plugin` attribute (an AbstractPlugin instance) is treated as a plugin.
    This keeps the loader decoupled from concrete plugin names.

    Plugins are then ordered by their declared `AbstractPlugin.dependencies`
    (topological sort) so that "A depends on B" always means B finishes
    register()+boot() before A starts — instead of relying on directory /
    alphabetical ordering, which only worked by coincidence.

    Each plugin's `register()`/`boot()` call is isolated: a failure marks that
    plugin FAILED in `plugin_registry` and, depending on
    `settings.plugin_load_mode`, either aborts the whole startup (fail_fast —
    the default) or skips just that plugin so the rest of the kernel can still
    come up (best_effort). A plugin whose dependency already failed is skipped
    the same way, since its dependency will never actually finish booting.

    Every lifecycle hook receives an explicit `KernelContext` whose
    `event_bus`/`service_registry` are views scoped to that plugin's name (see
    `ScopedEventBus`/`ScopedServiceRegistry`) — this is what lets
    `reload_one()` selectively tear down just one plugin's routes, published
    capabilities, and event subscriptions.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        event_bus: EventBus = _default_event_bus,
        settings: Settings = _default_settings,
        service_registry: ServiceRegistry = _default_service_registry,
        db_factory: DatabaseFactory = _default_db_factory,
        plugin_registry: PluginRegistry = _default_plugin_registry,
    ) -> None:
        self._app = app
        self._event_bus = event_bus
        self._settings = settings
        self._service_registry = service_registry
        self._db_factory = db_factory
        self._plugin_registry = plugin_registry

    def _build_ctx_for(self, plugin_name: str) -> KernelContext:
        return KernelContext(
            event_bus=ScopedEventBus(self._event_bus, owner=plugin_name),
            settings=self._settings,
            service_registry=ScopedServiceRegistry(self._service_registry, owner=plugin_name),
            db_factory=self._db_factory,
        )

    async def load_all(self) -> None:
        """Scan the plugins directory, register, then boot each plugin."""
        discovered = self._discover()
        ordered = self._resolve_load_order(discovered)

        for plugin in ordered:
            self._plugin_registry.register(plugin)
            logger.info(
                "Discovered plugin: %s (dependencies=%s)", plugin.name, plugin.dependencies
            )

        failed: set[str] = set()

        for plugin in self._plugin_registry.all():
            if not self._can_proceed(plugin, failed):
                continue
            try:
                await plugin.register(self._app, self._build_ctx_for(plugin.name))
            except Exception as exc:  # noqa: BLE001 - isolate the failing plugin, report clearly
                self._handle_failure(plugin, "register", exc, failed)
                continue
            self._plugin_registry.set_state(plugin.name, PluginState.REGISTERED)
            logger.info("Plugin registered routes/hooks: %s", plugin.name)

        for plugin in self._plugin_registry.all():
            if not self._can_proceed(plugin, failed):
                continue
            try:
                await plugin.boot(self._app, self._build_ctx_for(plugin.name))
            except Exception as exc:  # noqa: BLE001 - isolate the failing plugin, report clearly
                self._handle_failure(plugin, "boot", exc, failed)
                continue
            self._plugin_registry.set_state(plugin.name, PluginState.BOOTED)
            logger.info("Plugin booted: %s", plugin.name)

    def _can_proceed(self, plugin: AbstractPlugin, failed: set[str]) -> bool:
        if plugin.name in failed:
            return False
        blocking = [dep for dep in plugin.dependencies if dep in failed]
        if blocking:
            message = f"skipped: dependency failed: {blocking}"
            self._plugin_registry.set_state(plugin.name, PluginState.FAILED, error=message)
            failed.add(plugin.name)
            logger.error("Plugin '%s' skipped — dependency failed: %s", plugin.name, blocking)
            return False
        return True

    def _handle_failure(
        self, plugin: AbstractPlugin, stage: str, exc: Exception, failed: set[str]
    ) -> None:
        message = f"{stage}() failed: {exc}"
        self._plugin_registry.set_state(plugin.name, PluginState.FAILED, error=message)
        failed.add(plugin.name)
        logger.exception("Plugin '%s' failed during %s()", plugin.name, stage)

        if self._settings.plugin_load_mode != "best_effort":
            raise PluginLoadError(
                f"Plugin '{plugin.name}' failed during {stage}(): {exc}"
            ) from exc

    async def unload_all(self) -> None:
        # Shutdown in reverse order — mirrors dependency graph teardown
        for plugin in reversed(self._plugin_registry.all()):
            try:
                await plugin.shutdown(self._app, self._build_ctx_for(plugin.name))
                self._plugin_registry.set_state(plugin.name, PluginState.SHUTDOWN)
                logger.info("Plugin shut down: %s", plugin.name)
            except Exception:
                logger.exception("Error shutting down plugin: %s", plugin.name)
            finally:
                self._plugin_registry.unregister(plugin.name)

        # All plugins are gone — any capability/subscription they published is
        # now stale.
        self._service_registry.clear()
        self._event_bus.clear()

    def _resolve_load_order(self, plugins: list[AbstractPlugin]) -> list[AbstractPlugin]:
        """
        Topologically sorts `plugins` by their declared `dependencies` (Kahn's
        algorithm). A missing or circular dependency always aborts startup
        (raises `PluginLoadError`) regardless of `plugin_load_mode` — it's a
        static configuration error, not a single plugin misbehaving at
        runtime.
        """
        by_name = {p.name: p for p in plugins}
        in_degree: dict[str, int] = {p.name: 0 for p in plugins}
        dependents: dict[str, list[str]] = defaultdict(list)

        for plugin in plugins:
            for dep in plugin.dependencies:
                if dep not in by_name:
                    raise PluginLoadError(self._missing_dependency_message(plugin.name, dep))
                in_degree[plugin.name] += 1
                dependents[dep].append(plugin.name)

        ready = deque(name for name, degree in in_degree.items() if degree == 0)
        ordered_names: list[str] = []
        while ready:
            name = ready.popleft()
            ordered_names.append(name)
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    ready.append(dependent)

        if len(ordered_names) != len(plugins):
            unresolved = sorted(set(by_name) - set(ordered_names))
            raise PluginLoadError(f"Circular plugin dependency detected among: {unresolved}")

        return [by_name[name] for name in ordered_names]

    def _missing_dependency_message(self, plugin_name: str, dep: str) -> str:
        """
        Distinguishes two very different situations behind the same "not
        discovered" symptom: a genuinely nonexistent plugin name (a typo, or
        a dependency that was never written) vs. a plugin that exists on
        disk but was deliberately excluded via `enabled_plugins` — the first
        is almost certainly a bug, the second is an environment/config
        choice that happens to conflict with a dependency declaration. See
        docs/spec/done/microkernel-architecture-refinements.md S4.6.
        """
        enabled = self._settings.enabled_plugins
        exists_on_disk = (_PLUGINS_DIR / dep).is_dir()
        if exists_on_disk and enabled is not None and dep not in enabled:
            return (
                f"Plugin '{plugin_name}' depends on '{dep}', which exists under "
                f"app/plugins/ but is disabled via enabled_plugins. Add '{dep}' "
                f"to enabled_plugins, or remove it from '{plugin_name}'.dependencies."
            )
        return f"Plugin '{plugin_name}' depends on '{dep}', which was not discovered under app/plugins/."

    def _discover(self) -> list[AbstractPlugin]:
        plugins: list[AbstractPlugin] = []

        if not _PLUGINS_DIR.exists():
            logger.warning("Plugins directory not found: %s", _PLUGINS_DIR)
            return plugins

        enabled = self._settings.enabled_plugins

        for entry in sorted(_PLUGINS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue

            if enabled is not None and entry.name not in enabled:
                logger.info("Plugin '%s' skipped (not in enabled_plugins).", entry.name)
                continue

            module_path = f"{_PLUGINS_PACKAGE}.{entry.name}"
            try:
                module = importlib.import_module(module_path)
            except ImportError:
                logger.exception("Failed to import plugin module: %s", module_path)
                continue

            plugin_instance = getattr(module, "plugin", None)
            if plugin_instance is None:
                logger.warning(
                    "Plugin package '%s' has no top-level `plugin` attribute — skipping.",
                    module_path,
                )
                continue

            if not isinstance(plugin_instance, AbstractPlugin):
                logger.warning(
                    "'%s.plugin' is not an AbstractPlugin subclass — skipping.",
                    module_path,
                )
                continue

            if not is_api_version_compatible(plugin_instance.api_version):
                logger.warning(
                    "'%s' declares api_version=%s, incompatible with kernel API %s — skipping.",
                    module_path,
                    plugin_instance.api_version,
                    KERNEL_API_VERSION,
                )
                continue

            plugins.append(plugin_instance)

        return plugins

    # ── Hot reload (Phase 4 / 3.12) ───────────────────────────────────────────

    def _drop_routes_for(self, name: str) -> int:
        """Removes every route owned by plugin `name` from the live app."""
        prefix = f"{_PLUGINS_PACKAGE}.{name}."
        routes = self._app.router.routes
        kept = [
            route
            for route in routes
            if not (
                getattr(route, "endpoint", None) is not None
                and route.endpoint.__module__.startswith(prefix)
            )
        ]
        removed = len(routes) - len(kept)
        routes[:] = kept
        return removed

    async def reload_one(self, name: str) -> Result[list[str], PluginError]:
        """
        Hot-reloads a single plugin: shuts it down, drops its routes and its
        service_registry/event_bus registrations, re-imports its
        `service`/`router`/`plugin` submodules, then runs a fresh instance's
        register()+boot().

        Deliberately does NOT reload `models.py`/`schemas.py`: SQLAlchemy's
        `Base.metadata` is a process-wide registry that doesn't support
        redefining a table's mapped class, so schema changes are out of scope
        for hot-reload and always require a full process restart — see
        docs/spec/done/microkernel-architecture-improvements.md S3.12.

        Does NOT reload — or even know how to safely reload — any other
        plugin that declares `name` in its own `dependencies`. On success,
        the returned list names every currently-loaded plugin in that
        situation, so the caller can decide whether to reload them too (see
        docs/spec/done/microkernel-architecture-refinements.md S4.5). This
        is advisory only: a dependent plugin that only resolves its
        dependency's capability per-call (the pattern every shipped plugin
        uses) keeps working correctly without any action — the list matters
        for plugins that cached something from their dependency at `boot()`.
        """
        plugin = self._plugin_registry.get(name)
        if plugin is None:
            return Err(PluginError.not_found("Plugin", name))

        dependents = sorted(
            p.name for p in self._plugin_registry.all() if p.name != name and name in p.dependencies
        )
        if dependents:
            logger.warning(
                "Reloading '%s' — plugin(s) %s declare it as a dependency and may need reloading too.",
                name,
                dependents,
            )

        old_ctx = self._build_ctx_for(name)
        try:
            await plugin.shutdown(self._app, old_ctx)
        except Exception:
            logger.exception("Error shutting down plugin '%s' during reload", name)

        removed_routes = self._drop_routes_for(name)
        self._service_registry.revoke_all_from(name)
        self._event_bus.unsubscribe_all_from(name)
        self._plugin_registry.unregister(name)
        logger.info("Reload '%s': dropped %d route(s), revoked capabilities/subscriptions", name, removed_routes)

        for submodule in _HOT_RELOAD_SUBMODULES:
            mod_path = f"{_PLUGINS_PACKAGE}.{name}.{submodule}"
            try:
                mod = importlib.import_module(mod_path)
                importlib.reload(mod)
            except ImportError:
                continue  # not every plugin necessarily has all of these submodules

        package_path = f"{_PLUGINS_PACKAGE}.{name}"
        try:
            package = importlib.import_module(package_path)
            importlib.reload(package)
        except ImportError as exc:
            return Err(PluginError(PluginErrorCode.UNKNOWN, f"Failed to reload plugin package: {exc}"))

        new_plugin = getattr(package, "plugin", None)
        if not isinstance(new_plugin, AbstractPlugin):
            return Err(
                PluginError(
                    PluginErrorCode.UNKNOWN,
                    f"Reloaded module '{package_path}' has no valid `plugin` attribute",
                )
            )

        self._plugin_registry.register(new_plugin)
        new_ctx = self._build_ctx_for(name)
        try:
            await new_plugin.register(self._app, new_ctx)
            self._plugin_registry.set_state(name, PluginState.REGISTERED)
            await new_plugin.boot(self._app, new_ctx)
            self._plugin_registry.set_state(name, PluginState.BOOTED)
        except Exception as exc:  # noqa: BLE001 - report clearly rather than crash the request
            self._plugin_registry.set_state(name, PluginState.FAILED, error=f"reload failed: {exc}")
            logger.exception("Plugin '%s' failed to reload", name)
            return Err(PluginError(PluginErrorCode.UNKNOWN, f"Plugin '{name}' failed to reload: {exc}"))

        logger.info("Plugin '%s' hot-reloaded successfully.", name)
        return Ok(dependents)
