from __future__ import annotations

from enum import StrEnum


class PluginErrorCode(StrEnum):
    """Canonical error codes shared across all plugins and facades."""

    # Generic
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNAUTHENTICATED = "UNAUTHENTICATED"

    # Auth / User
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    USER_INACTIVE = "USER_INACTIVE"

    # Product
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    PRODUCT_UNAVAILABLE = "PRODUCT_UNAVAILABLE"

    # Infrastructure
    DATABASE_ERROR = "DATABASE_ERROR"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"


class PluginError(Exception):
    """
    Structured error returned (not raised) by plugin services and facades.

    Wrap this in `result.Err(PluginError(...))` so callers can pattern-match
    without try/except chains.  Only raise when something is truly unexpected.
    """

    def __init__(
        self,
        code: PluginErrorCode,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __repr__(self) -> str:
        return f"PluginError(code={self.code!r}, message={self.message!r})"

    # Convenience constructors: keeps call sites terse
    @classmethod
    def not_found(cls, resource: str, identifier: object = None) -> PluginError:
        msg = f"{resource} not found"
        if identifier is not None:
            msg += f": {identifier}"
        return cls(PluginErrorCode.NOT_FOUND, msg)

    @classmethod
    def already_exists(cls, resource: str, identifier: object = None) -> PluginError:
        msg = f"{resource} already exists"
        if identifier is not None:
            msg += f": {identifier}"
        return cls(PluginErrorCode.ALREADY_EXISTS, msg)

    @classmethod
    def invalid_credentials(cls) -> PluginError:
        return cls(PluginErrorCode.INVALID_CREDENTIALS, "Invalid username or password.")

    @classmethod
    def permission_denied(cls, reason: str = "") -> PluginError:
        return cls(PluginErrorCode.PERMISSION_DENIED, reason or "Permission denied.")

    @classmethod
    def unauthenticated(cls) -> PluginError:
        return cls(PluginErrorCode.UNAUTHENTICATED, "Authentication required.")

    @classmethod
    def token_expired(cls) -> PluginError:
        return cls(PluginErrorCode.TOKEN_EXPIRED, "Token has expired.")

    @classmethod
    def token_invalid(cls) -> PluginError:
        return cls(PluginErrorCode.TOKEN_INVALID, "Token is invalid.")

    @classmethod
    def database_error(cls, detail: str = "") -> PluginError:
        return cls(PluginErrorCode.DATABASE_ERROR, detail or "A database error occurred.")

    @classmethod
    def validation_error(cls, detail: str = "") -> PluginError:
        return cls(PluginErrorCode.VALIDATION_ERROR, detail or "Validation failed.")
