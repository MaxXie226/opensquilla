"""Public protocol-v3 transport envelope models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from opensquilla.contracts.client.errors import ErrorShape


class ReqFrame(BaseModel):
    """RPC request frame sent by client."""

    type: Literal["req"] = "req"
    id: str
    method: str
    params: Any | None = None


class ResFrame(BaseModel):
    """RPC response frame sent by server."""

    type: Literal["res"] = "res"
    id: str
    ok: bool
    payload: Any | None = None
    error: ErrorShape | None = None


class StateVersion(BaseModel):
    presence: int = 0
    health: int = 0


class EventFrame(BaseModel):
    """Server-pushed event frame."""

    type: Literal["event"] = "event"
    event: str
    payload: Any | None = None
    meta: dict[str, Any] | None = None
    seq: int | None = None
    state_version: StateVersion | None = None


class PingFrame(BaseModel):
    type: Literal["ping"] = "ping"


class PongFrame(BaseModel):
    type: Literal["pong"] = "pong"

__all__ = [
    "EventFrame",
    "PingFrame",
    "PongFrame",
    "ReqFrame",
    "ResFrame",
    "StateVersion",
]
