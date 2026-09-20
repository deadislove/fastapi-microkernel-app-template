from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

# Key function: rate-limit by client IP.
# Swap to a user-identity extractor if you need per-user limits.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
)
