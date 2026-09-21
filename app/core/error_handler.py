from __future__ import annotations

import logging
import traceback

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)


class GlobalExceptionMiddleware(BaseHTTPMiddleware):
    """
    Last-resort safety net for the microkernel.

    Any unhandled exception that escapes a plugin's own error handling lands
    here.  We log the full traceback (so nothing is silently lost) and return
    a structured JSON 500 so the client always gets a parseable response.

    Intentionally does NOT catch HTTPException; FastAPI handles those itself.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception:
            # Full traceback in logs; only a safe summary goes to the client
            logger.error(
                "Unhandled exception on %s %s\n%s",
                request.method,
                request.url.path,
                traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred. Please try again later.",
                    "path": request.url.path,
                },
            )
