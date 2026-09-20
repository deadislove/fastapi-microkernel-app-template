from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from fastapi import FastAPI

from app.config import Settings
from app.core.hooks import EventBus
from app.core.registry import ServiceRegistry
from app.infrastructure.database import DatabaseFactory


@dataclass(frozen=True)
class KernelContext:
    """
    Explicit bundle of kernel-provided capabilities, passed into every plugin
    lifecycle hook instead of each plugin reaching for `app.core.*` singletons
    itself via a bare `from ... import`.

    Two benefits over the implicit-import style:
      1. A plugin's dependency on the kernel is visible in its method
         signature, not hidden behind an import line.
      2. Callers (tests, or a future hot-reload path) can pass a `KernelContext`
         built from different components — a fake `event_bus`, a
         plugin-scoped view of `service_registry`, etc. — without
         monkeypatching module-level singletons.
    """

    event_bus: EventBus
    settings: Settings
    service_registry: ServiceRegistry
    db_factory: DatabaseFactory


# The kernel's own contract version. Bump the major segment only when a
# breaking change is made to the plugin lifecycle contract (e.g. adding a
# required KernelContext field, changing a hook's signature) — a plugin built
# against an incompatible major version is skipped by the loader instead of
# crashing at some arbitrary later point.
KERNEL_API_VERSION = "1.0"


def is_api_version_compatible(plugin_version: str, kernel_version: str = KERNEL_API_VERSION) -> bool:
    """Major-version compatibility check: '1.x' plugins work with kernel '1.y'."""
    return plugin_version.split(".")[0] == kernel_version.split(".")[0]


class AbstractPlugin(ABC):
    """
    Base contract every plugin must satisfy.

    Lifecycle:
      register() → called once at startup so the plugin can declare its routes,
                   models, and hooks before the kernel boots.
      boot()     → called after all plugins are registered; safe to reference
                   other plugins or shared infrastructure here.
      shutdown() → called on application teardown; release resources in reverse
                   registration order to avoid dangling references.

    Returns: None for all hooks — errors should raise, not silently swallow.
    """

    # Unique slug used as the plugin's identity in the registry
    name: str = ""

    # Kernel contract version this plugin was built against. Defaults to the
    # current kernel version so existing plugins don't need to opt in; only
    # declare a different value if you're deliberately testing compatibility
    # against another kernel version.
    api_version: ClassVar[str] = KERNEL_API_VERSION

    # The plugin's OWN release version — purely informational (shown by
    # `GET /api/v1/health`), and never inspected by the loader. Distinct from
    # `api_version`: that one is about compatibility with the kernel
    # contract; this one is "which build of this plugin is running", useful
    # once a plugin is maintained/released on its own cadence. Optional —
    # defaults to "0.0.0" for plugins that don't track a version.
    version: ClassVar[str] = "0.0.0"

    # Names of other plugins that must finish register()+boot() before this
    # one starts. The loader topologically sorts on this instead of relying on
    # directory/alphabetical order, and fails fast on missing/circular deps.
    dependencies: ClassVar[list[str]] = []

    @abstractmethod
    async def register(self, app: FastAPI, ctx: KernelContext) -> None:
        """Declare routes, models, and hooks. No cross-plugin calls yet."""
        ...

    @abstractmethod
    async def boot(self, app: FastAPI, ctx: KernelContext) -> None:
        """Post-registration init — safe to call facades or other plugins."""
        ...

    @abstractmethod
    async def shutdown(self, app: FastAPI, ctx: KernelContext) -> None:
        """Teardown — close connections, cancel tasks, unregister hooks."""
        ...
