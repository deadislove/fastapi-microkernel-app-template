from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Handler type: async callable that receives arbitrary keyword arguments
Handler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """
    Lightweight async pub/sub bus for cross-plugin communication.

    Plugins MUST use this bus instead of importing each other directly:
    that's the only way the kernel can guarantee isolation and safe hot-reload.

    Usage:
        # Subscribe (typically in boot(), once every plugin has register()'d)
        event_bus.subscribe("user.created", my_async_handler)

        # Emit (fire-and-forget; all handlers run concurrently)
        await event_bus.emit("user.created", user_id=42, email="a@b.com")

    Success: all handlers complete without raising.
    Failure: individual handler exceptions are logged and swallowed so one bad
             subscriber never breaks the emitter's flow.

    Each subscription may optionally be tagged with an `owner` (the
    subscribing plugin's name) so a hot-reload can selectively
    `unsubscribe_all_from(owner)` instead of wiping every plugin's
    subscriptions via `clear()`.

    Scope of the `ctx`-only rule (why `subscribe()` and `emit()` are treated
    differently):

    Why: hot-reload needs to remove exactly one plugin's subscriptions
    without touching anyone else's; only possible if every subscription is
    tagged with which plugin made it.

    What: `subscribe()` is what creates that tag, so it must go through
    `ctx.event_bus` (a `ScopedEventBus`) rather than importing this module's
    `event_bus` singleton directly: the plain singleton has no plugin name to
    attach. `emit()`, by contrast, is a stateless, fire-and-forget broadcast:
    it registers nothing, so it needs no tag and no scoping. A plugin's
    `service.py` (constructed per-request, with no access to any `ctx`; see
    `AbstractPlugin`/`KernelContext` in `plugin_base.py`) calling
    `event_bus.emit(...)` directly is therefore intentional and safe, not a
    violation of the "reach the kernel only through `ctx`" rule.

    Solution: `scripts/check_architecture_boundaries.py`'s Rule 4 encodes
    exactly this split automatically: it only flags a bare `event_bus`
    import inside a plugin's `plugin.py` (where `subscribe()` is called), not
    inside `service.py` (where only `emit()` is legitimate).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[Handler, str | None]]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler, *, owner: str | None = None) -> None:
        self._handlers[event].append((handler, owner))
        logger.debug("Subscribed %s to event '%s'", handler.__qualname__, event)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        # Safe removal: no KeyError if the event was never registered
        entries = self._handlers.get(event, [])
        self._handlers[event] = [(h, o) for (h, o) in entries if h != handler]

    def unsubscribe_all_from(self, owner: str) -> None:
        """Removes every subscription made with `owner=owner`; used by hot-reload."""
        for event in list(self._handlers.keys()):
            self._handlers[event] = [
                (h, o) for (h, o) in self._handlers[event] if o != owner
            ]

    def subscriptions(self) -> dict[str, list[str]]:
        """
        Read-only introspection view: event name -> owners currently
        subscribed to it.

        Why: without this, "who is listening for what" is a black box you'd
        have to grep source code to answer; there's no runtime way to
        confirm a plugin's `boot()` actually subscribed to the event you
        expect, or that disabling a plugin really removed its subscription.

        Solution: `GET /api/v1/health` calls this method and exposes the
        result directly (see `app/api/v1/health.py`), so that question has a
        runtime answer instead of a source-reading one.

        A subscription made without an `owner` (bypassing `ctx.event_bus`)
        is reported as `"<unowned>"`. Events with zero current subscribers
        are omitted.
        """
        return {
            event: [owner if owner is not None else "<unowned>" for _, owner in entries]
            for event, entries in self._handlers.items()
            if entries
        }

    async def emit(self, event: str, **kwargs: Any) -> None:
        entries = self._handlers.get(event, [])
        if not entries:
            return

        handlers = [h for h, _ in entries]
        results = await asyncio.gather(
            *(h(**kwargs) for h in handlers),
            return_exceptions=True,
        )

        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                logger.exception(
                    "Handler '%s' raised on event '%s': %s",
                    handler.__qualname__,
                    event,
                    result,
                )

    def clear(self, event: str | None = None) -> None:
        """Remove all handlers for a specific event, or all events if None."""
        if event:
            self._handlers.pop(event, None)
        else:
            self._handlers.clear()


# Shared singleton: import this in plugins and facades
event_bus = EventBus()


class ScopedEventBus:
    """
    View of `EventBus` bound to one plugin's name.

    `ctx.event_bus.subscribe(event, handler)` inside a plugin's `boot()`
    automatically tags `owner=<plugin name>` on the underlying bus via this
    wrapper: plugin code never has to pass its own name, and the loader can
    later call `event_bus.unsubscribe_all_from(name)` to cleanly tear down
    just that plugin's subscriptions during a hot-reload.
    """

    def __init__(self, bus: EventBus, owner: str) -> None:
        self._bus = bus
        self._owner = owner

    def subscribe(self, event: str, handler: Handler) -> None:
        self._bus.subscribe(event, handler, owner=self._owner)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        self._bus.unsubscribe(event, handler)

    async def emit(self, event: str, **kwargs: Any) -> None:
        await self._bus.emit(event, **kwargs)
