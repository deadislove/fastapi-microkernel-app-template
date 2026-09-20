from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str | None = Field(None, max_length=128)


class UserUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=128)
    email: EmailStr | None = None


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    full_name: str | None
    roles: list[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_user(cls, user: object) -> UserResponse:
        # Converts the comma-separated roles string to a list before validation
        return cls(
            id=user.id,  # type: ignore[attr-defined]
            username=user.username,  # type: ignore[attr-defined]
            email=user.email,  # type: ignore[attr-defined]
            full_name=user.full_name,  # type: ignore[attr-defined]
            roles=user.roles_list(),  # type: ignore[attr-defined]
            is_active=user.is_active,  # type: ignore[attr-defined]
            created_at=user.created_at,  # type: ignore[attr-defined]
        )


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)
