from __future__ import annotations

from result import Err, Ok, Result
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PluginError
from app.core.hooks import event_bus
from app.infrastructure.jwt import JWTService
from app.infrastructure.password import hash_password, verify_password
from app.plugins.user_plugin.models import User
from app.plugins.user_plugin.schemas import (
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserUpdate,
)

_jwt = JWTService()


class UserService:
    """
    Domain logic for user management and authentication.

    All public methods return Result[T, PluginError]; callers must unwrap
    before using the value.  Never raises unless something is truly broken.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, data: UserCreate) -> Result[User, PluginError]:
        # Check uniqueness before hashing to avoid wasted bcrypt work
        existing = await self._session.scalar(
            select(User).where(
                (User.username == data.username) | (User.email == data.email)
            )
        )
        if existing:
            field = "username" if existing.username == data.username else "email"
            return Err(PluginError.already_exists("User", f"{field}={getattr(existing, field)}"))

        user = User(
            username=data.username,
            email=data.email,
            hashed_password=hash_password(data.password),
            full_name=data.full_name,
        )
        self._session.add(user)
        await self._session.flush()  # get the generated id before commit

        await event_bus.emit("user.created", user_id=user.id, email=user.email)
        return Ok(user)

    async def authenticate(self, data: LoginRequest) -> Result[TokenResponse, PluginError]:
        user = await self._session.scalar(
            select(User).where(User.username == data.username)
        )
        if not user or not verify_password(data.password, user.hashed_password):
            return Err(PluginError.invalid_credentials())

        if not user.is_active:
            from app.core.errors import PluginErrorCode
            return Err(PluginError(PluginErrorCode.USER_INACTIVE, "Account is deactivated."))

        access = _jwt.create_access_token(user.id, extra_claims={"roles": user.roles_list()})
        refresh = _jwt.create_refresh_token(user.id)
        return Ok(TokenResponse(access_token=access, refresh_token=refresh))

    async def get_by_id(self, user_id: str) -> Result[User, PluginError]:
        user = await self._session.get(User, user_id)
        if not user:
            return Err(PluginError.not_found("User", user_id))
        return Ok(user)

    async def get_by_username(self, username: str) -> Result[User, PluginError]:
        user = await self._session.scalar(select(User).where(User.username == username))
        if not user:
            return Err(PluginError.not_found("User", username))
        return Ok(user)

    async def update_profile(
        self, user_id: str, data: UserUpdate
    ) -> Result[User, PluginError]:
        result = await self.get_by_id(user_id)
        if result.is_err():
            return result

        user = result.unwrap()
        if data.full_name is not None:
            user.full_name = data.full_name
        if data.email is not None:
            user.email = data.email

        await self._session.flush()
        return Ok(user)

    async def change_password(
        self, user_id: str, current_password: str, new_password: str
    ) -> Result[None, PluginError]:
        result = await self.get_by_id(user_id)
        if result.is_err():
            return result  # type: ignore[return-value]

        user = result.unwrap()
        if not verify_password(current_password, user.hashed_password):
            return Err(PluginError.invalid_credentials())

        user.hashed_password = hash_password(new_password)
        await self._session.flush()
        return Ok(None)

    async def list_users(self, skip: int = 0, limit: int = 50) -> list[User]:
        rows = await self._session.scalars(select(User).offset(skip).limit(limit))
        return list(rows.all())
