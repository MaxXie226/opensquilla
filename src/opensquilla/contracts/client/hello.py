"""Public protocol-v3 connect and Hello models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opensquilla.contracts.client.envelope import StateVersion

PROTOCOL_VERSION = 3

MAX_PAYLOAD_BYTES = 26_214_400
MAX_BUFFERED_BYTES = 52_428_800
MAX_PREAUTH_PAYLOAD_BYTES = 65_536

TICK_INTERVAL_MS = 30_000
HEALTH_REFRESH_INTERVAL_MS = 60_000
PREAUTH_TIMEOUT_MS = 10_000
DEDUPE_TTL_MS = 300_000
DEDUPE_MAX_ENTRIES = 1000

WS_CLOSE_SERVICE_RESTART = 1012


class ClientInfo(BaseModel):
    id: str
    display_name: str | None = None
    version: str
    platform: str
    device_family: str | None = None
    model_identifier: str | None = None
    mode: str
    instance_id: str | None = None


class ConnectParams(BaseModel):
    """Live parser for the tolerant wire-level ``connect.params`` object."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    min_protocol: int = Field(default=1, alias="minProtocol", strict=True)
    max_protocol: int = Field(default=PROTOCOL_VERSION, alias="maxProtocol", strict=True)
    client: Any | None = None
    caps: Any | None = None
    commands: Any | None = None
    permissions: Any | None = None
    path_env: Any | None = None
    role: str = "operator"
    scopes: Any | None = None
    auth: dict[str, Any] = Field(default_factory=dict)
    locale: Any | None = None
    user_agent: Any | None = None


class ServerInfo(BaseModel):
    version: str
    conn_id: str


class FeaturesInfo(BaseModel):
    methods: list[str]
    events: list[str]


class SnapshotInfo(BaseModel):
    presence: list[Any] = []
    health: Any = None
    state_version: StateVersion = StateVersion()
    uptime_ms: int = 0
    config_path: str | None = None
    state_dir: str | None = None
    auth_mode: str | None = None


class PolicyInfo(BaseModel):
    max_payload: int = MAX_PAYLOAD_BYTES
    max_buffered_bytes: int = MAX_BUFFERED_BYTES
    tick_interval_ms: int = TICK_INTERVAL_MS
    concurrent_history_reads: bool = False
    agent_stream_heartbeat_interval_ms: int = 15_000
    agent_stream_idle_timeout_ms: int = 600_000
    webui_stream_idle_grace_ms: int = 630_000
    client_ws_keepalive_timeout_ms: int = 120_000


class ContractInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(alias="schemaVersion")
    digest: str
    generated_from: str = Field(alias="generatedFrom")


class RuntimeInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    core_version: str = Field(alias="coreVersion")
    build_commit: str | None = Field(default=None, alias="buildCommit")
    platform: str
    arch: str


class ProtocolRangeInfo(BaseModel):
    min: int
    max: int


class HelloOk(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["hello-ok"] = "hello-ok"
    id: str | None = None
    protocol: int
    server: ServerInfo
    features: FeaturesInfo
    snapshot: SnapshotInfo
    policy: PolicyInfo
    auth: dict[str, Any] | None = None
    contract: ContractInfo | None = None
    runtime: RuntimeInfo | None = None
    protocol_range: ProtocolRangeInfo | None = Field(default=None, alias="protocolRange")
    capabilities: list[str] | None = None
    extensions: list[str] | None = None

__all__ = [
    "DEDUPE_MAX_ENTRIES",
    "DEDUPE_TTL_MS",
    "HEALTH_REFRESH_INTERVAL_MS",
    "MAX_BUFFERED_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_PREAUTH_PAYLOAD_BYTES",
    "PREAUTH_TIMEOUT_MS",
    "PROTOCOL_VERSION",
    "TICK_INTERVAL_MS",
    "WS_CLOSE_SERVICE_RESTART",
    "ClientInfo",
    "ConnectParams",
    "ContractInfo",
    "FeaturesInfo",
    "HelloOk",
    "PolicyInfo",
    "ProtocolRangeInfo",
    "RuntimeInfo",
    "ServerInfo",
    "SnapshotInfo",
]
