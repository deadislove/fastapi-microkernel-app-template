from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from result import Err, Ok, Result

from app.core.errors import PluginError, PluginErrorCode

if TYPE_CHECKING:
    from app.core.plugin_base import AbstractPlugin


class PluginState(StrEnum):
    """
    Lifecycle state of a single plugin, tracked by PluginRegistry.

    PENDING    -> discovered and catalogued, lifecycle hooks not run yet.
    REGISTERED -> `register()` completed (routes/models declared).
    BOOTED     -> `boot()` completed (safe to call other plugins/facades).
    FAILED     -> `register()` or `boot()` raised; see `error_of(name)`.
    SHUTDOWN   -> `shutdown()` completed during teardown.
    """

    PENDING = "PENDING"
    REGISTERED = "REGISTERED"
    BOOTED = "BOOTED"
    FAILED = "FAILED"
    SHUTDOWN = "SHUTDOWN"


class PluginRegistry:
    """
    Central catalog of every loaded plugin.

    Plugins register themselves by name; the loader queries this registry to
    drive the boot and shutdown sequences.  Keeping it separate from the loader
    means tests can inject fake plugins without touching the filesystem.

    Also tracks each plugin's lifecycle state (and last error, if any) so
    callers — the loader's fail-fast/best-effort decision, the health
    endpoint — can see exactly which plugin, and which stage, is responsible
    for a failure instead of just an opaque startup crash.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, AbstractPlugin] = {}
        self._states: dict[str, PluginState] = {}
        self._errors: dict[str, str] = {}

    def register(self, plugin: AbstractPlugin) -> None:
        if not plugin.name:
            raise ValueError(f"Plugin {type(plugin).__name__} must define a non-empty `name`.")
        if plugin.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.name}' is already registered.")
        self._plugins[plugin.name] = plugin
        self._states[plugin.name] = PluginState.PENDING
        self._errors.pop(plugin.name, None)

    def get(self, name: str) -> AbstractPlugin | None:
        return self._plugins.get(name)

    def all(self) -> list[AbstractPlugin]:
        return list(self._plugins.values())

    def names(self) -> list[str]:
        return list(self._plugins.keys())

    def set_state(self, name: str, state: PluginState, error: str | None = None) -> None:
        self._states[name] = state
        if error is not None:
            self._errors[name] = error
        elif state != PluginState.FAILED:
            self._errors.pop(name, None)

    def state_of(self, name: str) -> PluginState | None:
        return self._states.get(name)

    def error_of(self, name: str) -> str | None:
        return self._errors.get(name)

    def states(self) -> dict[str, PluginState]:
        return dict(self._states)

    def unregister(self, name: str) -> None:
        # Used during hot-reload; caller is responsible for calling shutdown() first
        self._plugins.pop(name, None)
        self._states.pop(name, None)
        self._errors.pop(name, None)


# Module-level singleton — the kernel and plugins share this instance
plugin_registry = PluginRegistry()


ServiceFactory = Callable[..., Any]


class ServiceRegistry:
    """
    Capability registry for cross-plugin collaboration.

    Plugins publish factories here (typically from `boot()`) instead of being
    imported directly by facades or other plugins.  This is what keeps a
    missing/removed plugin from breaking import-time wiring elsewhere in the
    app — the only sanctioned way to reach another plugin's service is
    through here, mirroring the EventBus's role for pub/sub communication.

    Entries may optionally be tagged with an `owner` (the publishing plugin's
    name) so a hot-reload can selectively `revoke_all_from(owner)` instead of
    wiping every plugin's capabilities via `clear()`.

    Scope of the `ctx`-only rule (why `provide()` and `resolve()` are treated
    differently):

    Why: hot-reload needs to revoke exactly one plugin's published
    capabilities without touching anyone else's — that's only possible if
    every entry is tagged with which plugin owns it.

    What: `provide()` is what creates that tag, so it must go through
    `ctx.service_registry` (a `ScopedServiceRegistry`) rather than importing
    this module's `service_registry` singleton directly — the plain singleton
    has no plugin name to attach. `resolve()`, by contrast, only reads and
    registers nothing, so it needs no tag and no scoping. A facade, or a
    plugin's own `service.py`/`router.py` (neither of which ever receives a
    `ctx` — see `AbstractPlugin`/`KernelContext` in `plugin_base.py`),
    importing this singleton to call `resolve(...)` directly is therefore
    intentional and safe, not a violation of the "reach the kernel only
    through `ctx`" rule.

    Solution: `scripts/check_architecture_boundaries.py`'s Rule 4 encodes
    exactly this split automatically — it only flags a bare
    `service_registry` import inside a plugin's `plugin.py` (where `provide()`
    is called), not inside `service.py`/`router.py` (where only `resolve()`
    is legitimate).
    """

    def __init__(self) -> None:
        self._providers: dict[str, ServiceFactory] = {}
        self._owners: dict[str, str] = {}

    def provide(self, name: str, factory: ServiceFactory, *, owner: str | None = None) -> None:
        if name in self._providers:
            raise ValueError(f"Service '{name}' is already provided.")
        self._providers[name] = factory
        if owner is not None:
            self._owners[name] = owner

    def resolve(self, name: str) -> Result[ServiceFactory, PluginError]:
        factory = self._providers.get(name)
        if factory is None:
            return Err(PluginError(PluginErrorCode.NOT_FOUND, f"Service '{name}' is not available."))
        return Ok(factory)

    def names(self) -> list[str]:
        return list(self._providers.keys())

    def revoke_all_from(self, owner: str) -> None:
        """Removes every entry published with `owner=owner` — used by hot-reload."""
        for name in [n for n, o in self._owners.items() if o == owner]:
            self._providers.pop(name, None)
            self._owners.pop(name, None)

    def clear(self) -> None:
        self._providers.clear()
        self._owners.clear()


# Module-level singleton — plugins provide capabilities, facades/consumers resolve them
service_registry = ServiceRegistry()


class ScopedServiceRegistry:
    """
    View of `ServiceRegistry` bound to one plugin's name.

    `ctx.service_registry.provide(name, factory)` inside a plugin's `boot()`
    automatically tags `owner=<plugin name>` on the underlying registry via
    this wrapper — plugin code never has to pass its own name, and the loader
    can later call `service_registry.revoke_all_from(name)` to cleanly tear
    down just that plugin's capabilities during a hot-reload.
    """

    def __init__(self, registry: ServiceRegistry, owner: str) -> None:
        self._registry = registry
        self._owner = owner

    def provide(self, name: str, factory: ServiceFactory) -> None:
        self._registry.provide(name, factory, owner=self._owner)

    def resolve(self, name: str) -> Result[ServiceFactory, PluginError]:
        return self._registry.resolve(name)

    def names(self) -> list[str]:
        return self._registry.names()
