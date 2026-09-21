from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from app.facades.base import AbstractFacade


class FacadeRegistry:
    """
    Catalog of every facade class in the app, and, optionally, the
    APIRouter each one owns.

    Facades aren't lifecycle-managed like plugins (there's no dynamic
    discovery/boot step), so registration just happens once, at import time
    (each facade's router module calls `facade_registry.register(FacadeClass,
    router=router)` at its bottom; a facade with no HTTP surface registers
    with `router=None`). `app/facades/__init__.py` imports every facade's
    router module so this side effect runs; `main.py` then mounts whatever
    ends up in `routers()` without needing to import any specific facade by
    name: the same "add a module, don't touch the composition root" story
    plugins already have.
    """

    def __init__(self) -> None:
        self._facades: dict[str, type[AbstractFacade]] = {}
        self._routers: dict[str, APIRouter] = {}

    def register(self, facade_cls: type[AbstractFacade], *, router: APIRouter | None = None) -> None:
        if not facade_cls.name:
            raise ValueError(f"Facade {facade_cls.__name__} must define a non-empty `name`.")
        if facade_cls.name in self._facades:
            raise ValueError(f"Facade '{facade_cls.name}' is already registered.")
        self._facades[facade_cls.name] = facade_cls
        if router is not None:
            self._routers[facade_cls.name] = router

    def get(self, name: str) -> type[AbstractFacade] | None:
        return self._facades.get(name)

    def names(self) -> list[str]:
        return list(self._facades.keys())

    def routers(self) -> list[APIRouter]:
        return list(self._routers.values())


# Module-level singleton: facades self-register against this at import time
facade_registry = FacadeRegistry()
