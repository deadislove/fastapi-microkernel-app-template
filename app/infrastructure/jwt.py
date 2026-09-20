from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pydantic import BaseModel
from result import Err, Ok, Result

from app.config import settings
from app.core.errors import PluginError


class TokenPayload(BaseModel):
    """Decoded JWT claims surfaced to route handlers and dependencies."""

    sub: str  # user identifier (UUID or username)
    roles: list[str] = []
    exp: datetime | None = None
    iat: datetime | None = None


class JWTService:
    """
    Stateless JWT encode/decode service.

    Success: Ok(TokenPayload) with validated claims.
    Failure: Err(PluginError) with TOKEN_EXPIRED or TOKEN_INVALID code.
    """

    def __init__(
        self,
        secret: str = settings.secret_key,
        algorithm: str = settings.jwt_algorithm,
        access_expire_minutes: int = settings.jwt_access_token_expire_minutes,
        refresh_expire_days: int = settings.jwt_refresh_token_expire_days,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._access_expire = timedelta(minutes=access_expire_minutes)
        self._refresh_expire = timedelta(days=refresh_expire_days)

    def create_access_token(self, subject: str, extra_claims: dict[str, Any] | None = None) -> str:
        return self._encode(subject, self._access_expire, extra_claims)

    def create_refresh_token(self, subject: str) -> str:
        return self._encode(subject, self._refresh_expire)

    def decode_access_token(self, token: str) -> Result[TokenPayload, PluginError]:
        return self._decode(token)

    def _encode(
        self,
        subject: str,
        expires_delta: timedelta,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = datetime.now(tz=UTC)
        payload: dict[str, Any] = {
            "sub": subject,
            "iat": now,
            "exp": now + expires_delta,
        }
        if extra:
            payload.update(extra)
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def _decode(self, token: str) -> Result[TokenPayload, PluginError]:
        try:
            raw = jwt.decode(token, self._secret, algorithms=[self._algorithm])
            return Ok(TokenPayload(**raw))
        except jwt.ExpiredSignatureError:
            return Err(PluginError.token_expired())
        except jwt.PyJWTError:
            return Err(PluginError.token_invalid())
