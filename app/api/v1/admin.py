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
        "modules, then runs register()+boot() again on a fresh instance. "
        "Does NOT reload models.py/schemas.py — SQLAlchemy's Base.metadata "
        "is a process-wide singleton that doesn't support redefining a "
        "table's mapped class, so schema changes always require a full "
        "restart. See docs/spec/done/microkernel-architecture-improvements.md S3.12.\n\n"
        "`dependents_may_need_reload` lists every other currently-loaded "
        "plugin that declares this one as a dependency — reload_one() does "
        "not reload them automatically (see "
        "docs/spec/done/microkernel-architecture-refinements.md S4.5); "
        "it's advisory information for the caller."
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
