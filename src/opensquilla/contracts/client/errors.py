"""Public protocol-v3 error model and currently declared error codes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

ERROR_NOT_LINKED = "NOT_LINKED"
ERROR_NOT_PAIRED = "NOT_PAIRED"
ERROR_AGENT_TIMEOUT = "AGENT_TIMEOUT"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
ERROR_UNAVAILABLE = "UNAVAILABLE"
ERROR_UNAUTHORIZED = "UNAUTHORIZED"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"


class ErrorShape(BaseModel):
    code: str
    message: str
    details: Any | None = None
    retryable: bool | None = None
    retry_after_ms: int | None = None
    accepted: bool | None = None

DECLARED_ERROR_CODES = (
    ERROR_AGENT_TIMEOUT,
    ERROR_APPROVAL_NOT_FOUND,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_NOT_FOUND,
    ERROR_NOT_LINKED,
    ERROR_NOT_PAIRED,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
)

__all__ = [
    "DECLARED_ERROR_CODES",
    "ERROR_AGENT_TIMEOUT",
    "ERROR_APPROVAL_NOT_FOUND",
    "ERROR_INVALID_REQUEST",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_NOT_FOUND",
    "ERROR_NOT_LINKED",
    "ERROR_NOT_PAIRED",
    "ERROR_UNAUTHORIZED",
    "ERROR_UNAVAILABLE",
    "ErrorShape",
]
