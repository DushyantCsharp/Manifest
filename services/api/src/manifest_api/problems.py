"""RFC 9457 problem details: every REST error is ``application/problem+json``.

Problem ``type`` URIs sit under ``PROBLEM_TYPE_BASE``. ``.example`` is reserved (RFC 2606), so
the URIs are stable identifiers now and can become documentation pages once there is a real host.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from manifest_api.errors import (
    ConflictError,
    IdempotencyKeyInFlightError,
    IdempotencyKeyReusedError,
    InvalidCursorError,
    InvalidValueError,
    NotFoundError,
    NotWiredError,
    OverloadedError,
)
from manifest_api.idempotency import IDEMPOTENCY_KEY_HEADER

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://manifest.example/problems/"


class ValidationIssue(BaseModel):
    detail: str
    pointer: str | None = Field(
        default=None, description="JSON Pointer (RFC 6901, URI fragment form) into the body."
    )
    parameter: str | None = Field(
        default=None, description="Name of the offending query, path or header parameter."
    )


class Problem(BaseModel):
    """RFC 9457 problem details. Clients must ignore extension members they don't know."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="Problem type URI; `about:blank` when the status says it all.")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = Field(default=None, description="Path of the request that failed.")
    errors: list[ValidationIssue] | None = Field(
        default=None, description="Individual failures, on `validation-error` and friends."
    )


@dataclass(frozen=True)
class ProblemType:
    slug: str
    status: int
    title: str

    @property
    def uri(self) -> str:
        return PROBLEM_TYPE_BASE + self.slug


VALIDATION_ERROR = ProblemType("validation-error", 422, "Request is not valid")
IDEMPOTENCY_KEY_MISSING = ProblemType(
    "idempotency-key-missing", 400, "Idempotency-Key header is required"
)
IDEMPOTENCY_KEY_REUSED = ProblemType(
    "idempotency-key-reused", 422, "Idempotency-Key was already used for a different request"
)
IDEMPOTENCY_KEY_IN_FLIGHT = ProblemType(
    "idempotency-key-in-flight", 409, "A request with this Idempotency-Key is still in progress"
)
INVALID_CURSOR = ProblemType("invalid-cursor", 400, "Pagination cursor is not valid")
NOT_FOUND = ProblemType("not-found", 404, "Resource not found")
CONFLICT = ProblemType("conflict", 409, "Request conflicts with the current state of the resource")
OVERLOADED = ProblemType("overloaded", 429, "Too much work is pending; retry later")
NOT_IMPLEMENTED = ProblemType("not-implemented", 501, "Not implemented yet")
INTERNAL_ERROR = ProblemType("internal-error", 500, "Internal server error")

PROBLEM_TYPES = (
    VALIDATION_ERROR,
    IDEMPOTENCY_KEY_MISSING,
    IDEMPOTENCY_KEY_REUSED,
    IDEMPOTENCY_KEY_IN_FLIGHT,
    INVALID_CURSOR,
    NOT_FOUND,
    CONFLICT,
    OVERLOADED,
    NOT_IMPLEMENTED,
    INTERNAL_ERROR,
)

_PROBLEM_FOR_EXCEPTION: dict[type[Exception], ProblemType] = {
    NotFoundError: NOT_FOUND,
    ConflictError: CONFLICT,
    InvalidValueError: VALIDATION_ERROR,
    InvalidCursorError: INVALID_CURSOR,
    OverloadedError: OVERLOADED,
    IdempotencyKeyReusedError: IDEMPOTENCY_KEY_REUSED,
    IdempotencyKeyInFlightError: IDEMPOTENCY_KEY_IN_FLIGHT,
    NotWiredError: NOT_IMPLEMENTED,
}


def problem_response(
    request: Request,
    *,
    type_uri: str,
    title: str,
    status: int,
    detail: str | None = None,
    errors: Sequence[ValidationIssue] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=type_uri,
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        errors=list(errors) if errors else None,
    )
    return JSONResponse(
        problem.model_dump(mode="json", exclude_none=True),
        status_code=status,
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _typed(
    request: Request,
    problem_type: ProblemType,
    detail: str | None = None,
    errors: Sequence[ValidationIssue] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return problem_response(
        request,
        type_uri=problem_type.uri,
        title=problem_type.title,
        status=problem_type.status,
        detail=detail,
        errors=errors,
        headers=headers,
    )


async def _on_mapped_exception(
    problem_type: ProblemType, request: Request, exc: Exception
) -> JSONResponse:
    headers = None
    if isinstance(exc, OverloadedError):
        headers = {"Retry-After": str(exc.retry_after_seconds)}
    return _typed(request, problem_type, str(exc) or None, headers=headers)


def _issue(error: Mapping[str, Any]) -> ValidationIssue:
    location, *path = error["loc"]
    if location != "body":
        return ValidationIssue(detail=error["msg"], parameter=".".join(map(str, path)) or location)
    if error["type"] == "json_invalid":
        # loc holds a character offset here, not a path into the document.
        return ValidationIssue(detail=f"{error['msg']}: {error['ctx']['error']}", pointer="#")
    tokens = (str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return ValidationIssue(detail=error["msg"], pointer="/".join(("#", *tokens)))


async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    issues = [_issue(error) for error in errors]
    key_missing = any(
        error["type"] == "missing" and tuple(error["loc"]) == ("header", IDEMPOTENCY_KEY_HEADER)
        for error in errors
    )
    if key_missing:
        return _typed(request, IDEMPOTENCY_KEY_MISSING, errors=issues)
    return _typed(request, VALIDATION_ERROR, errors=issues)


_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")


def _allowed_methods(request: Request) -> str:
    """Every method the path serves. Starlette names only the first matching route's, but
    one path can have several routes (``GET`` and ``POST /v1/shipments``).
    """
    routes = request.app.router.routes
    return ", ".join(
        method
        for method in _METHODS
        if any(
            route.matches({**request.scope, "method": method})[0] is Match.FULL for route in routes
        )
    )


async def _on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    title = HTTPStatus(exc.status_code).phrase
    detail = str(exc.detail) if exc.detail and str(exc.detail) != title else None
    headers = dict(exc.headers or {})
    if exc.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        headers["Allow"] = _allowed_methods(request)  # RFC 9110 §15.5.6: required on 405
    return problem_response(
        request,
        type_uri="about:blank",
        title=title,
        status=exc.status_code,
        detail=detail,
        headers=headers or None,
    )


async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # No detail: internals stay in the server log (Starlette re-raises after responding).
    return _typed(request, INTERNAL_ERROR)


def install_problem_handlers(app: FastAPI) -> None:
    for exc_type, problem_type in _PROBLEM_FOR_EXCEPTION.items():
        app.add_exception_handler(exc_type, partial(_on_mapped_exception, problem_type))
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(Exception, _on_unhandled)


def problem_responses(descriptions: Mapping[int, str]) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries for a route's error statuses.

    FastAPI documents ``model`` under ``application/json``; ``manifest_api.openapi`` moves every
    4xx/5xx body to ``application/problem+json``.
    """
    responses: dict[int | str, dict[str, Any]] = {}
    for status, description in sorted(descriptions.items()):
        response: dict[str, Any] = {"model": Problem, "description": description}
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            response["headers"] = {
                "Retry-After": {
                    "description": "Seconds to wait before retrying with the same Idempotency-Key.",
                    "schema": {"type": "integer", "minimum": 1},
                }
            }
        responses[status] = response
    return responses
