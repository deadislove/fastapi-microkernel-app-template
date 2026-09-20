from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Handler type: async callable that receives arbitrary keyword arguments
Handler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """
    Lightweight async pub/sub bus for cross-plugin communication.

    Plugins MUST use this bus instead of importing each other directly —
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

    Scope of the `ctx`-only rule: a plugin's `register()`/`boot()`/`shutdown()`
    MUST reach the bus through `ctx.event_bus` (a `ScopedEventBus`) — never by
    importing this module's `event_bus` singleton directly — because
    `subscribe()` needs the `owner` tag for hot-reload to work.
    `emit()`, however, is a stateless, fire-and-forget broadcast: it doesn't
    register anything, so it doesn't need scoping. A plugin's `service.py`
    (constructed per-request, with no access to any `ctx`) calling
    `event_bus.emit(...)` directly is intentional and safe, not a violation —
    see docs/spec/done/microkernel-architecture-refinements.md S4.1 for the
    full reasoning. `scripts/check_architecture_boundaries.py`'s Rule 4
    enforces the `ctx`-only requirement on `plugin.py` only, for this reason.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[Handler, str | None]]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler, *, owner: str | None = None) -> None:
        self._handlers[event].append((handler, owner))
        logger.debug("Subscribed %s to event '%s'", handler.__qualname__, event)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        # Safe removal — no KeyError if the event was never registered
        entries = self._handlers.get(event, [])
        self._handlers[event] = [(h, o) for (h, o) in entries if h != handler]

    def unsubscribe_all_from(self, owner: str) -> None:
        """Removes every subscription made with `owner=owner` — used by hot-reload."""
        for event in list(self._handlers.keys()):
            self._handlers[event] = [
                (h, o) for (h, o) in self._handlers[event] if o != owner
            ]

    def subscriptions(self) -> dict[str, list[str]]:
        """
        Read-only introspection view: event name -> owners currently
        subscribed to it (see docs/spec/done/microkernel-architecture-observability.md
        S5.1 — this is what lets `GET /api/v1/health` show which plugins are
        listening for which events instead of that being a black box).
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

        for handler, result in zip(handlers, results):
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


# Shared singleton — import this in plugins and facades
event_bus = EventBus()


class ScopedEventBus:
    """
    View of `EventBus` bound to one plugin's name.

    `ctx.event_bus.subscribe(event, handler)` inside a plugin's `boot()`
    automatically tags `owner=<plugin name>` on the underlying bus via this
    wrapper — plugin code never has to pass its own name, and the loader can
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
