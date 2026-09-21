from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.errors import PluginErrorCode
from app.core.security import require_admin
from app.infrastructure.jwt import TokenPayload

router = APIRouter(prefix="/admin/plugins", tags=["Admin (Hot Reload)"])

_ERROR_STATUS_MAP = {
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
}


class ReloadResponse(BaseModel):
    reloaded: str
    dependents_may_need_reload: list[str]


@router.post(
    "/{name}/reload",
    response_model=ReloadResponse,
    summary="Hot-reload a single plugin's routes/service/lifecycle code (admin only)",
    description=(
        "Shuts the plugin down, drops its routes and service_registry/"
        "event_bus registrations, re-imports its service/router/plugin "
        "modules, then runs register()+boot() again on a fresh instance.\n\n"
        "Why models.py/schemas.py are NOT reloaded: SQLAlchemy's "
        "Base.metadata is a process-wide singleton that doesn't support "
        "redefining a table's already-registered mapped class. What that "
        "means: a schema change (new column, new table) can't be picked up "
        "by re-importing code alone, so it's out of scope here: a schema "
        "change always requires a full process restart.\n\n"
        "Why `dependents_may_need_reload` exists: reloading this plugin "
        "never touches any other plugin that declares it as a dependency, "
        "because this endpoint has no way to know whether that dependent's "
        "state is still valid; only the caller has that context. Solution: "
        "the field lists every other currently-loaded plugin in that "
        "situation so the caller can decide whether to reload them too; "
        "it's advisory information, not something this endpoint acts on."
    ),
)
async def reload_plugin(
    name: str,
    request: Request,
    payload: TokenPayload = Depends(require_admin),
) -> ReloadResponse:
    loader = request.app.state.plugin_loader
    result = await loader.reload_one(name)
    if result.is_err():
        err = result.unwrap_err()
        http_status = _ERROR_STATUS_MAP.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(status_code=http_status, detail=err.message)
    return ReloadResponse(reloaded=name, dependents_may_need_reload=result.unwrap())
