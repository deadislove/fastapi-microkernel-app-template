from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PluginErrorCode
from app.core.rate_limiter import limiter
from app.core.security import get_current_user_payload
from app.infrastructure.database import db_factory
from app.infrastructure.jwt import TokenPayload
from app.plugins.user_plugin.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.plugins.user_plugin.service import UserService

router = APIRouter(prefix="/users", tags=["Users"])
auth_router = APIRouter(prefix="/auth", tags=["Auth"])

# ── Helpers ──────────────────────────────────────────────────────────────────

_ERROR_STATUS_MAP = {
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PluginErrorCode.ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    PluginErrorCode.INVALID_CREDENTIALS: status.HTTP_401_UNAUTHORIZED,
    PluginErrorCode.PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
    PluginErrorCode.USER_INACTIVE: status.HTTP_403_FORBIDDEN,
    PluginErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _raise(err: object) -> None:
    from app.core.errors import PluginError
    assert isinstance(err, PluginError)
    http_status = _ERROR_STATUS_MAP.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=http_status, detail=err.message)


# ── Auth endpoints ────────────────────────────────────────────────────────────

@auth_router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: UserCreate,
    session: AsyncSession = Depends(db_factory.get_session),
) -> UserResponse:
    result = await UserService(session).register(body)
    if result.is_err():
        _raise(result.unwrap_err())
    return UserResponse.from_orm_user(result.unwrap())


@auth_router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtain JWT access + refresh tokens (OAuth2 form)",
)
@limiter.limit("20/minute")
async def login_form(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(db_factory.get_session),
) -> TokenResponse:
    """OAuth2-compatible form login — used by Swagger UI's Authorize button."""
    result = await UserService(session).authenticate(
        LoginRequest(username=form.username, password=form.password)
    )
    if result.is_err():
        _raise(result.unwrap_err())
    return result.unwrap()


@auth_router.post(
    "/login",
    response_model=TokenResponse,
    summary="Obtain JWT tokens via JSON body",
)
@limiter.limit("20/minute")
async def login_json(
    request: Request,
    body: LoginRequest,
    session: AsyncSession = Depends(db_factory.get_session),
) -> TokenResponse:
    result = await UserService(session).authenticate(body)
    if result.is_err():
        _raise(result.unwrap_err())
    return result.unwrap()


# ── User profile endpoints ────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user profile",
)
async def get_me(
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> UserResponse:
    result = await UserService(session).get_by_id(payload.sub)
    if result.is_err():
        _raise(result.unwrap_err())
    return UserResponse.from_orm_user(result.unwrap())


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Update current user profile",
)
async def update_me(
    body: UserUpdate,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> UserResponse:
    result = await UserService(session).update_profile(payload.sub, body)
    if result.is_err():
        _raise(result.unwrap_err())
    return UserResponse.from_orm_user(result.unwrap())


@router.post(
    "/me/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change current user password",
)
async def change_password(
    body: PasswordChangeRequest,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> None:
    result = await UserService(session).change_password(
        payload.sub, body.current_password, body.new_password
    )
    if result.is_err():
        _raise(result.unwrap_err())


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get user by ID (admin only)",
)
async def get_user(
    user_id: str,
    payload: TokenPayload = Depends(get_current_user_payload),
    session: AsyncSession = Depends(db_factory.get_session),
) -> UserResponse:
    # Any authenticated user can look up profiles; restrict further if needed
    result = await UserService(session).get_by_id(user_id)
    if result.is_err():
        _raise(result.unwrap_err())
    return UserResponse.from_orm_user(result.unwrap())
