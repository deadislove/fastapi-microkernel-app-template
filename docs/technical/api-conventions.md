# API Conventions

Every endpoint in this app (kernel, facade, or plugin) follows the same conventions for error handling, authentication, and rate limiting. This document is the reference for all of them.

## The `Result` / `PluginError` pattern

Service and facade methods return `Result[T, PluginError]` (from the [`result`](https://pypi.org/project/result/) library) for *expected* failure cases (not found, already exists, invalid credentials) instead of raising exceptions for control flow:

```python
from result import Err, Ok, Result
from app.core.errors import PluginError

async def get_user(user_id: str) -> Result[User, PluginError]:
    user = await session.get(User, user_id)
    if not user:
        return Err(PluginError.not_found("User", user_id))
    return Ok(user)
```

Routers unwrap the result and map errors to HTTP responses:

```python
result = await service.get_user(user_id)
if result.is_err():
    _raise(result.unwrap_err())
return result.unwrap()
```

Genuinely unexpected failures (a bug, a driver-level exception) should still raise; they're caught by the global exception middleware (below) and turned into a `500` with no leaked internals.

### `PluginErrorCode`

The canonical set of error codes, shared across every plugin and facade (`app/core/errors.py`):

| Code | Typical meaning |
|---|---|
| `NOT_FOUND` | Requested resource doesn't exist. |
| `ALREADY_EXISTS` | Uniqueness constraint violated (duplicate username/email/SKU). |
| `VALIDATION_ERROR` | Domain-level validation failure (beyond what Pydantic already checks). |
| `PERMISSION_DENIED` | Authenticated, but not allowed to do this. |
| `UNAUTHENTICATED` | No valid credentials at all. |
| `INVALID_CREDENTIALS` | Login attempt with a wrong username/password. |
| `TOKEN_EXPIRED` / `TOKEN_INVALID` | JWT decode/validation failure. |
| `USER_INACTIVE` | Account exists but is deactivated. |
| `INSUFFICIENT_STOCK` / `PRODUCT_UNAVAILABLE` | Product-domain-specific errors. |
| `DATABASE_ERROR` / `EXTERNAL_SERVICE_ERROR` | Infrastructure-level failure. |
| `UNKNOWN` | Fallback / unclassified. |

`PluginError` has convenience constructors for the common cases: `PluginError.not_found(resource, id)`, `.already_exists(...)`, `.invalid_credentials()`, `.permission_denied(reason)`, `.unauthenticated()`, `.token_expired()`, `.token_invalid()`, `.validation_error(detail)`, `.database_error(detail)`. Use these instead of constructing `PluginError(code, message)` by hand where one fits.

### Mapping errors to HTTP status codes

Each router defines its own `_ERROR_STATUS_MAP: dict[PluginErrorCode, int]` and a small `_raise(err)` helper that looks the code up (defaulting to `500` for anything unmapped) and raises `HTTPException`. This keeps the mapping local to the endpoints that actually produce those errors instead of one giant global switch statement. Example (`app/plugins/product_plugin/router.py`):

```python
_ERROR_STATUS_MAP = {
    PluginErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    PluginErrorCode.ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    PluginErrorCode.INSUFFICIENT_STOCK: status.HTTP_409_CONFLICT,
    PluginErrorCode.PRODUCT_UNAVAILABLE: status.HTTP_410_GONE,
    PluginErrorCode.PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
    PluginErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_ENTITY,
}
```

When adding a new plugin, copy this pattern rather than reusing another plugin's map: a plugin importing another plugin's router module to reuse its error map would violate the [module boundary rules](./architecture.md#module-boundaries).

### The global exception safety net

`GlobalExceptionMiddleware` (`app/core/error_handler.py`) is the outermost middleware. It does **not** touch `HTTPException` (FastAPI already handles those), but catches anything else that escapes a router, logs the full traceback server-side, and returns a structured, non-leaking response:

```json
{
  "error": "INTERNAL_SERVER_ERROR",
  "message": "An unexpected error occurred. Please try again later.",
  "path": "/api/v1/products/abc"
}
```

If you see this in practice, it means a service raised instead of returning `Err(...)`; check the server log for the real traceback, then decide whether that path should have been a `PluginError` instead.

## Authentication

JWT bearer tokens, issued by `user_plugin`:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/register` | Create an account. |
| `POST /api/v1/auth/token` | OAuth2-compatible form login (what Swagger UI's "Authorize" button calls). |
| `POST /api/v1/auth/login` | Same as above, JSON body, for non-Swagger clients. |
| `GET /api/v1/users/me` | Current user's profile. |
| `PATCH /api/v1/users/me` | Update current user's profile. |
| `POST /api/v1/users/me/change-password` | Change password. |
| `GET /api/v1/users/{user_id}` | Look up any user's profile (any authenticated user, not just the owner). |

`app/core/security.py` provides two FastAPI dependencies used across every plugin and facade router:

- **`get_current_user_payload`**: decodes the bearer token via `JWTService`, raises `401` on any failure, otherwise returns a `TokenPayload` (`sub` = user id, `roles` = list of role strings from the token's claims).
- **`require_admin`**: depends on the above, additionally raises `403` unless `"admin" in payload.roles`. Real usage: `product_plugin`'s create/update/adjust-stock/delete routes, and the admin-only hot-reload endpoint (`POST /api/v1/admin/plugins/{name}/reload`, see [operations.md](./operations.md#hot-reloading-a-plugin)).

Both only inspect the JWT's own claims; they do **not** re-check the database on every request. A revoked/deactivated user keeps a valid token until it expires; if your deployment needs stronger revocation guarantees, that's a deliberate simplification to reconsider, not an oversight.

`JWTService` (`app/infrastructure/jwt.py`) wraps `pyjwt`: `create_access_token(user_id, extra_claims)`, `create_refresh_token(user_id)`, and `decode_access_token(token) -> Result[TokenPayload, PluginError]` (returning `TOKEN_EXPIRED`/`TOKEN_INVALID` as appropriate rather than raising).

## Rate limiting

Powered by [`slowapi`](https://github.com/laurentS/slowapi), keyed by client IP (`get_remote_address`). `settings.rate_limit_default` (`100/minute`) is the app-wide default; individual routes override it where a tighter limit matters:

```python
@router.post("/register")
@limiter.limit("10/minute")
async def register(request: Request, body: UserCreate, ...): ...
```

A route decorated with `@limiter.limit(...)` **must** take `request: Request` as a parameter; `slowapi` reads the client identity off it. Exceeding the limit returns `429` via the `RateLimitExceeded` exception handler registered in `main.py`.

Every route with a tighter-than-default limit today:

| Endpoint | Limit |
|---|---|
| `POST /api/v1/auth/register` | `10/minute` |
| `POST /api/v1/auth/token` | `20/minute` |
| `POST /api/v1/auth/login` | `20/minute` |
| `GET /api/v1/products` | `60/minute` |
| Everything else | `100/minute` (`RATE_LIMIT_DEFAULT`) |

## Versioning

All routes are mounted under `/api/v1`. There's no `v2` yet, but the convention (kernel routes and facade routes both prefixed the same way) means introducing one is a matter of adding a parallel `api/v2` tree and a new set of routers; it doesn't require touching plugins' internal service/schema layers.
