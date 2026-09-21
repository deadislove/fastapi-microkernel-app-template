from __future__ import annotations

from abc import ABC
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession


class AbstractFacade(ABC):  # noqa: B024 - `name` is checked at registration time (FacadeRegistry.register), not via an abstractmethod; ABC here documents intent, not enforcement
    """
    Base contract every facade must satisfy.

    A facade coordinates two or more plugins' capabilities (resolved via
    `service_registry`, never by importing a plugin's classes directly)
    behind a single cross-cutting API. Unlike plugins, facades aren't
    dynamically discovered at startup; they're plain importable classes.
    Whatever module owns the facade's router calls
    `facade_registry.register(FacadeClass, router=router)` at its bottom
    (see `app/facades/router.py`); a facade with no HTTP surface registers
    with `router=None` from wherever it's defined instead. Either way,
    `app/facades/__init__.py` imports that module so the registration side
    effect runs, and `main.py` mounts every router `facade_registry.routers()`
    returns without needing to import any specific facade by name. This
    gives the kernel one place to enumerate what cross-plugin coordination
    exists, mirroring what `plugin_registry` does for plugins.

    Dependency direction stays one-way: facades depend on plugins (through
    `service_registry`), plugins must never depend on a facade.

    Why facades don't receive a `KernelContext`: `ctx` exists so a plugin's
    lifecycle hooks don't have to reach for kernel singletons directly, but
    that only works because `PluginLoader` calls those hooks and can build a
    fresh `ctx` for each one. Facades aren't driven through any such
    lifecycle (no `register()`/`boot()`/`shutdown()`, no loader driving
    them), so there's no natural moment to construct and hand one over.

    What this means in practice: a facade (see `CatalogFacade`) resolves a
    plugin capability by importing the bare `service_registry` singleton and
    calling `resolve()` directly, instead of going through `ctx.service_registry`
    the way a plugin would.

    Solution / why that's still safe: the `owner` tagging that `ctx.service_registry`
    exists to provide is only needed by `provide()`, so hot-reload knows whose
    capability to revoke. `resolve()` only reads and registers nothing; there
    is no `owner` to lose by skipping the wrapper, so a facade importing the
    singleton directly is not a gap to close, just a consequence of facades
    having no lifecycle to hang a `ctx` on.
    """

    # Unique slug used as the facade's identity in `facade_registry`
    name: ClassVar[str] = ""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
