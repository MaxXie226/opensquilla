"""Deterministic snapshot of the Gateway surface consumed by clients.

This module records protocol v3 as it behaves today. It deliberately does not
turn the snapshot into a runtime parser or change the wire contract. In
particular, the historical ``ConnectParams`` Pydantic model is stricter than
the hand-written WebSocket handshake parser; both facts are exported rather
than silently treating the model as the live parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import TypeAdapter
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.gateway.origin_guard import (
    SAME_CONFIGURED_OR_NO_ORIGIN,
    client_auth_policy,
    client_origin_policy,
)
from opensquilla.gateway.protocol import (
    DECLARED_EVENTS,
    DEDUPE_MAX_ENTRIES,
    DEDUPE_TTL_MS,
    ERROR_AGENT_TIMEOUT,
    ERROR_APPROVAL_NOT_FOUND,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_NOT_FOUND,
    ERROR_NOT_LINKED,
    ERROR_NOT_PAIRED,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
    HEALTH_REFRESH_INTERVAL_MS,
    MAX_BUFFERED_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_PREAUTH_PAYLOAD_BYTES,
    PREAUTH_TIMEOUT_MS,
    PROTOCOL_VERSION,
    TICK_INTERVAL_MS,
    WS_CLOSE_SERVICE_RESTART,
    ConnectParams,
    EventFrame,
    FeaturesInfo,
    HelloOk,
    PingFrame,
    PolicyInfo,
    PongFrame,
    ReqFrame,
    ResFrame,
    ServerInfo,
    SnapshotInfo,
    StateVersion,
    make_error_res,
)
from opensquilla.gateway.rpc import RpcContractEntry
from opensquilla.gateway.rpc import get_registry as get_rpc_registry
from opensquilla.sandbox.run_mode_policy import hello_auth_payload

CONTRACT_SCHEMA_VERSION = 1
CONTRACT_PROTOCOL_MIN = 1
CONTRACT_PROTOCOL_MAX = PROTOCOL_VERSION

CONTRACT_ARTIFACT_PATHS = (
    PurePosixPath("protocol.schema.json"),
    PurePosixPath("rpc-methods.json"),
    PurePosixPath("events.json"),
    PurePosixPath("http-routes.json"),
    PurePosixPath("golden/connect.json"),
    PurePosixPath("golden/hello-ok.json"),
    PurePosixPath("golden/error.json"),
)

EVENT_PATTERNS = (
    "session.event.*",
    "task.*",
)

# Exact non-internal events registered by the current Web UI through
# ``RpcClient.on``. The list is descriptive, not an exhaustive server promise.
OBSERVED_CLIENT_EVENTS = (
    "channel.status",
    "cron.run.finished",
    "exec.approval.requested",
    "exec.approval.resolved",
    "models.routing.changed",
    "plugin.approval.requested",
    "plugin.approval.resolved",
    "session.epoch_changed",
    "session.event.artifact",
    "session.event.compaction",
    "session.event.cron_result",
    "session.event.ensemble_progress",
    "session.event.meta_preflight",
    "session.event.meta_run_announced",
    "session.event.meta_run_completed",
    "session.event.meta_step_state",
    "session.event.router_control_replay",
    "session.event.router_decision",
    "session.event.run_heartbeat",
    "session.event.state_change",
    "session.event.subagent_completion",
    "session.event.task_group.done",
    "session.event.task_group.failed",
    "session.event.task_group.synthesizing",
    "session.event.task_group.waiting",
    "session.event.text_delta",
    "session.event.tool_result",
    "session.event.tool_use_delta",
    "session.event.tool_use_start",
    "session.event.warning",
    "sessions.changed",
    "task.queued",
    "task.running",
)

_DESKTOP_IDENTITY_PATH = "/api/desktop/identity"
_DESKTOP_SHUTDOWN_PATH = "/api/desktop/shutdown"
_UNSAFE_HTTP_METHODS = frozenset({"DELETE", "PATCH", "POST", "PUT"})
_SYNTHETIC_CONN_ID = "00000000-0000-0000-0000-000000000001"


class ClientContractError(RuntimeError):
    """Raised when a runtime surface cannot be exported safely or completely."""


@dataclass(frozen=True, slots=True)
class ClientContractSnapshot:
    """Read-only aggregate used to render the committed v3 artifacts."""

    protocol_schema: dict[str, Any]
    rpc_methods: dict[str, Any]
    events: dict[str, Any]
    http_routes: dict[str, Any]
    golden_connect: dict[str, Any]
    golden_hello: dict[str, Any]
    golden_error: dict[str, Any]


def render_json(payload: Any) -> bytes:
    """Render a stable UTF-8 JSON file with LF line endings."""

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def _protocol_schema() -> dict[str, Any]:
    wire_schema = TypeAdapter(
        ReqFrame | ResFrame | EventFrame | HelloOk | PingFrame | PongFrame
    ).json_schema()
    definitions = wire_schema["$defs"]
    frame_names = ("ReqFrame", "ResFrame", "EventFrame", "HelloOk", "PingFrame", "PongFrame")
    for name in frame_names:
        required = definitions[name].setdefault("required", [])
        if "type" not in required:
            required.append("type")
        required.sort()
    definitions["ClientFrame"] = {
        "oneOf": [
            {"$ref": "#/$defs/ReqFrame"},
            {"$ref": "#/$defs/PingFrame"},
            {"$ref": "#/$defs/PongFrame"},
        ]
    }
    definitions["ServerFrame"] = {
        "oneOf": [
            {"$ref": "#/$defs/ResFrame"},
            {"$ref": "#/$defs/EventFrame"},
            {"$ref": "#/$defs/HelloOk"},
            {"$ref": "#/$defs/PingFrame"},
            {"$ref": "#/$defs/PongFrame"},
        ]
    }
    definitions["Frame"] = {
        "oneOf": [
            {"$ref": "#/$defs/ReqFrame"},
            {"$ref": "#/$defs/ResFrame"},
            {"$ref": "#/$defs/EventFrame"},
            {"$ref": "#/$defs/HelloOk"},
            {"$ref": "#/$defs/PingFrame"},
            {"$ref": "#/$defs/PongFrame"},
        ]
    }
    legacy_connect = TypeAdapter(ConnectParams).json_schema(
        ref_template="#/$defs/Legacy{model}"
    )
    for name, definition in legacy_connect.pop("$defs", {}).items():
        definitions[f"Legacy{name}"] = definition
    definitions["LegacyConnectParams"] = legacy_connect
    error_codes = sorted(
        {
            ERROR_AGENT_TIMEOUT,
            ERROR_APPROVAL_NOT_FOUND,
            ERROR_INVALID_REQUEST,
            ERROR_METHOD_NOT_FOUND,
            ERROR_NOT_FOUND,
            ERROR_NOT_LINKED,
            ERROR_NOT_PAIRED,
            ERROR_UNAUTHORIZED,
            ERROR_UNAVAILABLE,
        }
    )
    metadata = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "protocol": {
            "current": PROTOCOL_VERSION,
            "maximum_supported": CONTRACT_PROTOCOL_MAX,
            "minimum_supported": CONTRACT_PROTOCOL_MIN,
        },
        "frames": {
            "client_to_server_declared": {"$ref": "#/$defs/ClientFrame"},
            "server_to_client": {"$ref": "#/$defs/ServerFrame"},
        },
        "connect": {
            "canonical_client_frame": {
                "additionalProperties": True,
                "properties": {
                    "id": {"type": "string"},
                    "method": {"const": "connect"},
                    "params": {
                        "additionalProperties": True,
                        "properties": {
                            "auth": {"type": "object"},
                            "client": {
                                "additionalProperties": True,
                                "type": "object",
                            },
                            "maxProtocol": {
                                "maximum": CONTRACT_PROTOCOL_MAX,
                                "minimum": CONTRACT_PROTOCOL_MIN,
                                "type": "integer",
                            },
                            "minProtocol": {
                                "maximum": CONTRACT_PROTOCOL_MAX,
                                "minimum": CONTRACT_PROTOCOL_MIN,
                                "type": "integer",
                            },
                            "role": {"type": "string"},
                            "scopes": {
                                "items": {"type": "string"},
                                "type": "array",
                            },
                        },
                        "type": "object",
                    },
                    "type": {"const": "req"},
                },
                "required": ["type", "id", "method", "params"],
                "type": "object",
            },
            "accepted_currently": {
                "auth": "non-object values are treated as an empty object",
                "client": "accepted but not validated or consumed",
                "id": "optional; scalar ids are stringified and other values use 'handshake'",
                "maxProtocol": (
                    f"optional integer excluding booleans; defaults to {PROTOCOL_VERSION}"
                ),
                "minProtocol": "optional integer excluding booleans; defaults to 1",
                "params": "non-object values are treated as an empty object",
                "required_frame_fields": ["method=connect", "type=req"],
                "role": "non-string values fall back to operator",
                "scope_claims": (
                    "accepted but authorization scopes come from the resolved principal"
                ),
                "version_lower_bound": (
                    "the current parser does not reject values below 1; canonical clients must use "
                    "the advertised supported range"
                ),
            },
            "legacy_declared_model": {
                "schema": {"$ref": "#/$defs/LegacyConnectParams"},
                "status": "not-used-by-websocket-parser",
                "wire_name_difference": "min_protocol/max_protocol vs minProtocol/maxProtocol",
            },
            "sequence": [
                "server event connect.challenge",
                "client request connect",
                "server hello-ok or error response followed by close",
            ],
        },
        "hello": {
            "features_events_completeness": "declared-stable-subset",
            "features_methods_source": "locked-rpc-registry",
            "schema": {"$ref": "#/$defs/HelloOk"},
            "snapshot_uptime_ms_current_semantics": "epoch-milliseconds-despite-field-name",
        },
        "limits": {
            "max_buffered_bytes": {
                "advertised_in_hello": True,
                "application_enforced": False,
                "value": MAX_BUFFERED_BYTES,
            },
            "max_payload_bytes": {
                "advertised_in_hello": True,
                "application_enforced": False,
                "value": MAX_PAYLOAD_BYTES,
            },
            "max_preauth_payload_bytes": {
                "advertised_in_hello": False,
                "application_enforced": False,
                "status": "defined-only",
                "value": MAX_PREAUTH_PAYLOAD_BYTES,
            },
            "transport_frame_limit": {
                "source": "ASGI-server/backend default; Gateway does not pin a value",
                "value": None,
            },
            "writer_queue": {
                "unit": "frames-not-bytes",
                "value_source": "GatewayConfig.ws_writer_queue_maxsize",
            },
        },
        "timing_defaults_ms": {
            "agent_stream_heartbeat": {
                "advertised_in_hello": True,
                "value": PolicyInfo().agent_stream_heartbeat_interval_ms,
            },
            "agent_stream_idle": {
                "advertised_in_hello": True,
                "value": PolicyInfo().agent_stream_idle_timeout_ms,
            },
            "client_ws_keepalive": {
                "advertised_in_hello": True,
                "enforced": True,
                "value": PolicyInfo().client_ws_keepalive_timeout_ms,
            },
            "dedupe_ttl": {
                "status": "defined-only",
                "value": DEDUPE_TTL_MS,
            },
            "health_refresh": {
                "status": "defined-only",
                "value": HEALTH_REFRESH_INTERVAL_MS,
            },
            "preauth_timeout": {
                "enforced": True,
                "value": PREAUTH_TIMEOUT_MS,
            },
            "tick_interval": {
                "advertised_in_hello": True,
                "enforced": True,
                "value": TICK_INTERVAL_MS,
            },
            "webui_stream_idle_grace": {
                "advertised_in_hello": True,
                "value": PolicyInfo().webui_stream_idle_grace_ms,
            },
        },
        "other_declared_defaults": {
            "dedupe_max_entries": {
                "status": "defined-only",
                "value": DEDUPE_MAX_ENTRIES,
            },
            "websocket_close_service_restart": WS_CLOSE_SERVICE_RESTART,
        },
        "errors": {
            "declared_codes": error_codes,
            "open_set": True,
            "schema": {"$ref": "#/$defs/ErrorShape"},
        },
        "runtime_validation": {
            "inbound": "hand-written JSON parsing; Pydantic request models are descriptive",
            "outbound": "Pydantic response/event/hello serialization",
        },
    }
    wire_schema.pop("anyOf", None)
    wire_schema.update(
        {
            "$id": "urn:opensquilla:client-contract:v3",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": "#/$defs/Frame",
            "description": (
                "Machine-valid protocol-v3 frame schema with descriptive baseline metadata."
            ),
            "title": "OpenSquilla Gateway client protocol v3 baseline",
            "x-opensquilla-contract": metadata,
        }
    )
    return wire_schema


def _rpc_contract_entries() -> tuple[RpcContractEntry, ...]:
    registry = get_rpc_registry()
    if not registry.registration_locked:
        raise ClientContractError("RPC package registration is not locked")
    return registry.contract_entries()


def _rpc_methods(entries: tuple[RpcContractEntry, ...]) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "completeness": "method-names-and-required-scopes-only",
        "method_count": len(entries),
        "methods": [
            {
                "name": entry.name,
                "required_scope": entry.required_scope,
            }
            for entry in entries
        ],
        "payload_schemas": "not-available-in-protocol-v3-baseline",
        "source": "locked-rpc-registry",
    }


def _events() -> dict[str, Any]:
    declared = tuple(DECLARED_EVENTS)
    observed_only = sorted(set(OBSERVED_CLIENT_EVENTS) - set(declared))
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "completeness": "open-event-set",
        "declared_events": list(declared),
        "event_patterns": list(EVENT_PATTERNS),
        "observed_client_events": list(OBSERVED_CLIENT_EVENTS),
        "observed_only": observed_only,
        "observed_source": "opensquilla-webui RpcClient.on registrations",
        "notes": [
            "declared_events is the stable subset advertised in Hello",
            "event_patterns and observed_only are descriptive, not exhaustive promises",
        ],
    }


def _http_auth_policy(
    path: str,
    transport: str,
    control_base_path: str,
    endpoint: Any = None,
) -> dict[str, Any]:
    if transport == "websocket":
        return {
            "scheme": "websocket-connect-handshake",
            "modes": {
                "none": {
                    "credential_transport": "none",
                    "status": "supported",
                },
                "password": {
                    "credential_transport": "none",
                    "status": "unsupported-by-principal-resolver",
                },
                "token": {
                    "credential_transport": "connect.params.auth.token",
                    "status": "supported",
                },
                "trusted-proxy": {
                    "credential_transport": "none",
                    "status": "unsupported-by-principal-resolver",
                },
            },
        }
    if path == _DESKTOP_IDENTITY_PATH:
        return {
            "caller_credential_required": False,
            "desktop_instance_required": True,
            "middleware": "bypassed",
            "network_constraints": ["loopback-bind", "loopback-peer"],
            "request_fields": ["challenge"],
            "scheme": "desktop-server-identity-challenge",
        }
    if path == _DESKTOP_SHUTDOWN_PATH:
        return {
            "caller_credential_required": True,
            "desktop_instance_required": True,
            "middleware": "bypassed",
            "network_constraints": ["loopback-bind", "loopback-peer"],
            "request_fields": ["challenge", "proof"],
            "scheme": "desktop-ownership-proof",
        }
    if path in AuthMiddleware.PUBLIC_PATHS:
        return {
            "middleware": "bypassed",
            "scheme": "public",
        }
    if path == control_base_path or path.startswith(f"{control_base_path}/"):
        return {
            "middleware": "bypassed",
            "scheme": "public-control-ui",
        }
    endpoint_policy = client_auth_policy(endpoint)
    token_transport = endpoint_policy.get("credential_transport", "header-or-query")
    owner_required = endpoint_policy.get("owner_required", False) is True
    return {
        "modes": {
            "none": {
                "credential_transport": "none",
                "middleware": "bypassed",
                "principal_resolution": "open-scope-resolver",
            },
            "password": {
                "credential_transport": "none",
                "middleware": "falls-through-without-validation",
                "principal_resolution": "unsupported",
            },
            "token": {
                "credential_transport": token_transport,
                "middleware": "token-required",
                "principal_resolution": "token-scope-resolver",
            },
            "trusted-proxy": {
                "credential_transport": "x-forwarded-for",
                "middleware": "configured-proxy-substring-check-or-pass-when-unset",
                "principal_resolution": "unsupported",
            },
        },
        "owner_required": owner_required,
        "scheme": "gateway-auth-mode-matrix",
    }


def _route_surface(path: str, transport: str, control_base_path: str) -> str:
    if path == control_base_path or path.startswith(f"{control_base_path}/"):
        return "control-ui"
    if transport == "websocket":
        return "gateway-rpc"
    if path.startswith("/api/"):
        return "client-api"
    return "operations"


def _origin_contract(policy: str) -> dict[str, Any]:
    if policy == "not-required":
        return {"policy": "not-required"}
    if policy == "not-checked":
        return {"policy": "not-checked"}
    if policy != SAME_CONFIGURED_OR_NO_ORIGIN:
        raise ClientContractError(f"Unsupported origin policy: {policy!r}")
    return {
        "allowed": [
            "origin-header-absent",
            "same-scheme-host-effective-port",
            "exact-configured-origin",
        ],
        "configured_wildcard_accepted": False,
        "policy": "browser-origin-guard",
    }


def collect_http_routes(
    app: Starlette,
    *,
    control_base_path: str = "/control",
) -> tuple[dict[str, Any], ...]:
    """Collect the final ASGI route tree, including opaque mounts.

    Mutating HTTP handlers must carry explicit same-origin metadata. Failing
    closed here prevents the exported contract from claiming protection for a
    newly added route that forgot to install the guard.
    """

    records: list[dict[str, Any]] = []
    order = 0

    def visit(routes: list[Any], prefix: str = "") -> None:
        nonlocal order
        for route in routes:
            route_order = order
            order += 1
            if isinstance(route, Mount):
                raw_path = str(getattr(route, "path_format", route.path))
                path = f"{prefix}{raw_path}"
                records.append(
                    {
                        "auth": _http_auth_policy(path, "mount", control_base_path),
                        "methods": ["GET", "HEAD"],
                        "order": route_order,
                        "origin": _origin_contract("not-required"),
                        "path": path,
                        "surface": _route_surface(path, "mount", control_base_path),
                        "transport": "mount",
                    }
                )
                children = list(getattr(route, "routes", ()) or ())
                if children:
                    visit(children, prefix=f"{prefix}{route.path}")
                continue

            if isinstance(route, WebSocketRoute):
                path = f"{prefix}{route.path}"
                records.append(
                    {
                        "auth": _http_auth_policy(path, "websocket", control_base_path),
                        "methods": [],
                        "order": route_order,
                        "origin": _origin_contract("not-checked"),
                        "path": path,
                        "surface": _route_surface(path, "websocket", control_base_path),
                        "transport": "websocket",
                    }
                )
                continue

            if not isinstance(route, Route):
                raise ClientContractError(f"Unsupported ASGI route type: {type(route).__name__}")

            path = f"{prefix}{route.path}"
            methods = sorted(route.methods or ())
            unsafe = bool(set(methods) & _UNSAFE_HTTP_METHODS)
            origin = client_origin_policy(route.endpoint)
            if unsafe and origin != SAME_CONFIGURED_OR_NO_ORIGIN:
                raise ClientContractError(
                    f"Mutating route {path!r} lacks explicit same-origin contract metadata"
                )
            origin_contract = _origin_contract(
                SAME_CONFIGURED_OR_NO_ORIGIN if unsafe else "not-required"
            )
            records.append(
                {
                    "auth": _http_auth_policy(
                        path,
                        "http",
                        control_base_path,
                        route.endpoint,
                    ),
                    "methods": methods,
                    "order": route_order,
                    "origin": origin_contract,
                    "path": path,
                    "surface": _route_surface(path, "http", control_base_path),
                    "transport": "http",
                }
            )

    visit(list(app.routes))
    return tuple(
        sorted(
            records,
            key=lambda row: (
                str(row["path"]).encode("utf-8"),
                str(row["transport"]),
                tuple(row["methods"]),
            ),
        )
    )


def _build_synthetic_app_routes() -> tuple[dict[str, Any], ...]:
    """Build the default app without touching the operator's state directory."""

    from opensquilla.gateway.app import create_gateway_app
    from opensquilla.gateway.config import (
        AttachmentsConfig,
        AuthConfig,
        ChannelsConfig,
        ControlUiConfig,
        CorsConfig,
        GatewayConfig,
    )
    from opensquilla.gateway.diagnostics import DiagnosticsState
    from opensquilla.gateway.uploads import UploadStore

    with TemporaryDirectory(prefix="opensquilla-client-contract-") as temp_dir:
        root = Path(temp_dir)
        synthetic_store = UploadStore(root / "media" / "uploads")
        config = GatewayConfig(
            auth=AuthConfig(mode="none", password=None, token=None),
            attachments=AttachmentsConfig(media_root=str(root / "media")),
            channels=ChannelsConfig(),
            config_path=str(root / "config.toml"),
            control_ui=ControlUiConfig(
                base_path="/control",
                default_locale="en",
                enabled=True,
                frontend="vue",
            ),
            cors=CorsConfig(allowed_origins=[]),
            state_dir=str(root / "state"),
            workspace_dir=str(root / "workspace"),
        )
        app = create_gateway_app(
            config,
            diagnostics_state=DiagnosticsState(configured_enabled=False),
            upload_store=synthetic_store,
        )
        return collect_http_routes(app, control_base_path="/control")


def _http_routes() -> dict[str, Any]:
    routes = _build_synthetic_app_routes()
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "coverage": {
            "control_ui": "enabled-at-/control",
            "extra_runtime_routes": "excluded; channel webhooks are configuration-dependent",
            "profile": "default-create_gateway_app",
            "source": "final-starlette-route-tree-after-dynamic-registration",
        },
        "policies": {
            "auth": {
                "default_profile_mode": "none",
                "http": "per-route auth-mode matrix records current middleware behavior",
                "password_mode": "HTTP falls through; principal resolver unsupported",
                "trusted_proxy_mode": (
                    "HTTP checks X-Forwarded-For only when a proxy is configured; "
                    "principal resolver unsupported"
                ),
                "websocket": "none/token supported; password/trusted-proxy unsupported",
            },
            "cors_default": "no cross-origin response headers",
            "origin": {
                "guarded_routes_allow_missing_origin": True,
                "present_origin_must_be": "same-origin-or-exactly-configured",
                "wildcard_does_not_bypass_guard": True,
            },
        },
        "route_count": len(routes),
        "routes": list(routes),
    }


def _golden_connect() -> dict[str, Any]:
    return {
        "id": "contract-connect-1",
        "method": "connect",
        "params": {
            "auth": {"token": "<synthetic>"},
            "client": {"name": "opensquilla-contract-client"},
            "maxProtocol": CONTRACT_PROTOCOL_MAX,
            "minProtocol": CONTRACT_PROTOCOL_MIN,
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
        },
        "type": "req",
    }


def _golden_hello(entries: tuple[RpcContractEntry, ...]) -> dict[str, Any]:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write"}),
        is_owner=True,
        authenticated=True,
    )
    hello = HelloOk(
        protocol=PROTOCOL_VERSION,
        server=ServerInfo(version="0.0.0-contract", conn_id=_SYNTHETIC_CONN_ID),
        features=FeaturesInfo(
            methods=[entry.name for entry in entries],
            events=list(DECLARED_EVENTS),
        ),
        snapshot=SnapshotInfo(
            presence=[],
            health=None,
            state_version=StateVersion(),
            uptime_ms=1_700_000_000_000,
            config_path="synthetic-config.toml",
            state_dir="synthetic-state",
            auth_mode="token",
        ),
        policy=PolicyInfo(),
        auth=hello_auth_payload(principal),
    )
    return hello.model_dump(mode="json")


def _golden_error() -> dict[str, Any]:
    frame = make_error_res(
        "contract-error-1",
        ERROR_INVALID_REQUEST,
        "Synthetic invalid request",
        details={"field": "method"},
    )
    return frame.model_dump(mode="json")


def build_client_contract_snapshot() -> ClientContractSnapshot:
    """Build the complete v3 snapshot from locked runtime registries."""

    entries = _rpc_contract_entries()
    return ClientContractSnapshot(
        protocol_schema=_protocol_schema(),
        rpc_methods=_rpc_methods(entries),
        events=_events(),
        http_routes=_http_routes(),
        golden_connect=_golden_connect(),
        golden_hello=_golden_hello(entries),
        golden_error=_golden_error(),
    )


def render_contract_artifacts(
    snapshot: ClientContractSnapshot | None = None,
) -> dict[PurePosixPath, bytes]:
    """Return the exact committed artifact inventory and byte content."""

    current = snapshot or build_client_contract_snapshot()
    artifacts = {
        PurePosixPath("protocol.schema.json"): render_json(current.protocol_schema),
        PurePosixPath("rpc-methods.json"): render_json(current.rpc_methods),
        PurePosixPath("events.json"): render_json(current.events),
        PurePosixPath("http-routes.json"): render_json(current.http_routes),
        PurePosixPath("golden/connect.json"): render_json(current.golden_connect),
        PurePosixPath("golden/hello-ok.json"): render_json(current.golden_hello),
        PurePosixPath("golden/error.json"): render_json(current.golden_error),
    }
    if tuple(artifacts) != CONTRACT_ARTIFACT_PATHS:
        raise ClientContractError("Rendered artifact inventory differs from the declared inventory")
    return artifacts
