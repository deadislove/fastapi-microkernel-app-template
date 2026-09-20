from __future__ import annotations

from abc import ABC
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession


class AbstractFacade(ABC):
    """
    Base contract every facade must satisfy.

    A facade coordinates two or more plugins' capabilities — resolved via
    `service_registry`, never by importing a plugin's classes directly —
    behind a single cross-cutting API. Unlike plugins, facades aren't
    dynamically discovered at startup; they're plain importable classes.
    Whatever module owns the facade's router calls
    `facade_registry.register(FacadeClass, router=router)` at its bottom
    (see `app/facades/router.py`) — a facade with no HTTP surface registers
    with `router=None` from wherever it's defined instead. Either way,
    `app/facades/__init__.py` imports that module so the registration side
    effect runs, and `main.py` mounts every router `facade_registry.routers()`
    returns without needing to import any specific facade by name. This
    gives the kernel one place to enumerate what cross-plugin coordination
    exists, mirroring what `plugin_registry` does for plugins.

    Dependency direction stays one-way: facades depend on plugins (through
    `service_registry`), plugins must never depend on a facade.

    Unlike `AbstractPlugin.register/boot/shutdown`, facade methods don't
    receive a `KernelContext` — facades aren't driven through a lifecycle, so
    there's no natural point to construct and pass one. Resolving a plugin
    capability via the bare `service_registry` singleton (see
    `CatalogFacade`) is safe here: `resolve()` only reads, it doesn't need
    the `owner` scoping that `provide()`/`subscribe()` require for
    hot-reload. See docs/spec/done/microkernel-architecture-refinements.md
    S4.2 for the full reasoning.
    """

    # Unique slug used as the facade's identity in `facade_registry`
    name: ClassVar[str] = ""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
