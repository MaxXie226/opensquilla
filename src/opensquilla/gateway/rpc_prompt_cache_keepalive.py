"""RPC surface for the opt-in session prompt-cache keepalive lease."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.gateway.prompt_cache_keepalive import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
)
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage

_d = get_dispatcher()


async def _resolve_session(params: Any, ctx: RpcContext) -> str:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    key = params.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("params.key must be a complete session key")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not available")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    return str(session.session_key)


def _service(ctx: RpcContext) -> Any:
    service = ctx.prompt_cache_keepalive_service
    if service is None:
        raise RpcUnavailableError("Prompt-cache keepalive is not available")
    return service


@_d.method("sessions.promptCacheKeepalive.status", scope="operator.read")
async def _status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    key = await _resolve_session(params, ctx)
    return cast(dict[str, Any], _service(ctx).status(key))


@_d.method("sessions.promptCacheKeepalive.set", scope="operator.write")
async def _set(params: Any, ctx: RpcContext) -> dict[str, Any]:
    key = await _resolve_session(params, ctx)
    assert isinstance(params, dict)
    enabled = params.get("enabled")
    if type(enabled) is not bool:
        raise ValueError("params.enabled must be a boolean")
    ttl = params.get("ttlSeconds", DEFAULT_TTL_SECONDS)
    if type(ttl) is not int:
        raise ValueError("params.ttlSeconds must be an integer")
    if enabled and not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise ValueError(
            f"params.ttlSeconds must be between {MIN_TTL_SECONDS} and "
            f"{MAX_TTL_SECONDS}"
        )
    return cast(
        dict[str, Any],
        await _service(ctx).set_enabled(
            key,
            enabled=enabled,
            ttl_seconds=ttl,
        ),
    )


__all__: list[str] = []
