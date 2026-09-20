from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.hooks import event_bus
from app.core.registry import PluginState, plugin_registry, service_registry
from app.facades.registry import facade_registry

router = APIRouter(tags=["Health"])


class PluginHealth(BaseModel):
    name: str
    state: str
    version: str
    api_version: str
    dependencies: list[str]
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    plugins: list[PluginHealth]
    facades: list[str]
    capabilities: list[str]
    event_subscriptions: dict[str, list[str]]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Kernel health check",
    description=(
        "Returns the kernel status, the lifecycle state of every loaded "
        "plugin (PENDING/REGISTERED/BOOTED/FAILED/SHUTDOWN) with its own "
        "version, api_version/dependencies/error, the names of every "
        "registered facade (cross-plugin coordination point), every "
        "capability currently published to service_registry, and every "
        "event with active event_bus subscribers (and who subscribed)."
    ),
)
async def health_check() -> HealthResponse:
    plugins = []
    for name in plugin_registry.names():
        plugin = plugin_registry.get(name)
        state = plugin_registry.state_of(name) or PluginState.PENDING
        plugins.append(
            PluginHealth(
                name=name,
                state=state.value,
                version=plugin.version if plugin else "",
                api_version=plugin.api_version if plugin else "",
                dependencies=list(plugin.dependencies) if plugin else [],
                error=plugin_registry.error_of(name),
            )
        )
    return HealthResponse(
        status="ok",
        plugins=plugins,
        facades=facade_registry.names(),
        capabilities=service_registry.names(),
        event_subscriptions=event_bus.subscriptions(),
    )
