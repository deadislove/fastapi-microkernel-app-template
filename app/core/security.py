from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.infrastructure.jwt import JWTService, TokenPayload

# Points Swagger UI to the token endpoint so the "Authorize" button works
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

_jwt_service = JWTService()


async def get_current_user_payload(token: str = Depends(oauth2_scheme)) -> TokenPayload:
    """
    FastAPI dependency — validates the Bearer token and returns its payload.

    Raises HTTP 401 on any token problem so route handlers stay clean.
    """
    result = _jwt_service.decode_access_token(token)
    if result.is_err():
        err = result.unwrap_err()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=err.message,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return result.unwrap()


async def require_admin(payload: TokenPayload = Depends(get_current_user_payload)) -> TokenPayload:
    """Dependency that additionally enforces the 'admin' role."""
    if "admin" not in (payload.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return payload
