"""Request identification and structured FastAPI error handling."""

import re
from uuid import uuid4

import structlog.contextvars
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.schemas import ApiError, ApiErrorDetail, ApiErrorResponse
from app.services.research_api import (
    ResearchProviderUnavailableError,
    ResearchRunConflictError,
    ResearchRunNotFoundError,
)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def request_id(request: Request) -> str:
    """Return the middleware-assigned request ID."""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else uuid4().hex


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ApiErrorDetail] | None = None,
) -> JSONResponse:
    payload = ApiErrorResponse(
        error=ApiError(
            code=code,
            message=message,
            request_id=request_id(request),
            details=details or [],
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
    )


def install_error_handling(app: FastAPI) -> None:
    """Install request IDs and safe structured exception responses."""

    @app.middleware("http")
    async def identify_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        assigned = incoming if _SAFE_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        request.state.request_id = assigned
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=assigned)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = assigned
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        details = [
            ApiErrorDetail(
                location=list(item["loc"]),
                message=str(item["msg"]),
                error_type=str(item["type"]),
            )
            for item in error.errors()
        ]
        return _error_response(
            request,
            status_code=422,
            code="validation_error",
            message="The request did not pass validation.",
            details=details,
        )

    @app.exception_handler(ResearchRunNotFoundError)
    async def run_not_found(
        request: Request,
        error: ResearchRunNotFoundError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=404,
            code="research_run_not_found",
            message=str(error),
        )

    @app.exception_handler(ResearchRunConflictError)
    async def run_conflict(
        request: Request,
        error: ResearchRunConflictError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=409,
            code="research_run_conflict",
            message=str(error),
        )

    @app.exception_handler(ResearchProviderUnavailableError)
    async def provider_unavailable(
        request: Request,
        error: ResearchProviderUnavailableError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="provider_unavailable",
            message=str(error),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=error.status_code,
            code="http_error",
            message="The requested API operation could not be completed.",
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        structlog.get_logger(__name__).exception(
            "unhandled_api_error",
            error_type=type(error).__name__,
        )
        return _error_response(
            request,
            status_code=500,
            code="internal_error",
            message="An unexpected internal error occurred.",
        )
