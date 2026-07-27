"""Protocol-v3 client baseline generated from the live Gateway surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensquilla.gateway.client_contract import (
    CONTRACT_ARTIFACT_PATHS,
    ClientContractError,
    ClientContractSnapshot,
    build_client_contract_snapshot,
    collect_http_routes,
    render_contract_artifacts,
)
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.contract_identity import (
    CLIENT_CONTRACT_DIGEST,
    CONTRACT_GENERATED_FROM,
    CONTRACT_SCHEMA_VERSION,
)
from opensquilla.gateway.hello_capabilities import (
    CAPABILITY_ARTIFACTS,
    CAPABILITY_RPC,
    CAPABILITY_SESSIONS,
)
from opensquilla.gateway.protocol import (
    DECLARED_EVENTS,
    PROTOCOL_VERSION,
    HelloOk,
    ResFrame,
)
from opensquilla.gateway.rpc import get_dispatcher, get_registry
from opensquilla.gateway.scopes import (
    METHOD_SCOPES,
    NODE_ROLE_METHODS,
    resolve_required_scope,
)
from opensquilla.gateway.uploads import get_upload_store
from opensquilla.gateway.websocket import _build_features, handle_ws_connection

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "client" / "v3"


@pytest.fixture(scope="module")
def snapshot() -> ClientContractSnapshot:
    return build_client_contract_snapshot()


def _read_json(relative_path: str) -> dict[str, Any]:
    return json.loads((CONTRACT_ROOT / relative_path).read_text(encoding="utf-8"))


def test_committed_inventory_and_bytes_match_runtime_snapshot(
    snapshot: ClientContractSnapshot,
) -> None:
    expected = render_contract_artifacts(snapshot)
    existing = {
        path.relative_to(CONTRACT_ROOT).as_posix(): path
        for path in CONTRACT_ROOT.rglob("*")
        if path.is_file()
    }

    assert tuple(expected) == CONTRACT_ARTIFACT_PATHS
    assert set(existing) == {path.as_posix() for path in CONTRACT_ARTIFACT_PATHS}
    for relative_path, content in expected.items():
        assert existing[relative_path.as_posix()].read_bytes() == content


def test_protocol_v3_and_live_connect_model_are_explicit() -> None:
    protocol = _read_json("protocol.schema.json")
    metadata = protocol["x-opensquilla-contract"]

    assert PROTOCOL_VERSION == 3
    assert metadata["protocol"] == {
        "current": 3,
        "maximum_supported": 3,
        "minimum_supported": 1,
    }
    connect = metadata["connect"]
    assert connect["canonical_client_frame"]["properties"]["params"]["properties"][
        "minProtocol"
    ]["type"] == "integer"
    assert connect["live_parser_model"]["status"] == "used-by-websocket-parser"
    assert connect["live_parser_model"]["wire_aliases"] == (
        "min_protocol/max_protocol serialize as minProtocol/maxProtocol"
    )
    assert connect["accepted_currently"]["params"] == (
        "non-object values are treated as an empty object"
    )


def test_contract_manifest_matches_runtime_identity(
    snapshot: ClientContractSnapshot,
) -> None:
    manifest = _read_json("contract.json")

    assert snapshot.contract_digest == CLIENT_CONTRACT_DIGEST
    assert manifest == snapshot.contract_manifest
    assert manifest["digest"] == CLIENT_CONTRACT_DIGEST
    assert manifest["schemaVersion"] == CONTRACT_SCHEMA_VERSION
    assert manifest["generatedFrom"] == CONTRACT_GENERATED_FROM
    assert "golden/hello-ok.json" not in manifest["digestPaths"]


def test_declared_payload_limits_are_not_misrepresented_as_enforced() -> None:
    limits = _read_json("protocol.schema.json")["x-opensquilla-contract"]["limits"]

    assert limits["max_payload_bytes"] == {
        "advertised_in_hello": True,
        "application_enforced": False,
        "value": 26_214_400,
    }
    assert limits["max_buffered_bytes"]["application_enforced"] is False
    assert limits["max_preauth_payload_bytes"]["status"] == "defined-only"
    assert limits["transport_frame_limit"]["value"] is None


def test_protocol_schema_validates_golden_frames_and_rejects_malformed_frames() -> None:
    protocol = _read_json("protocol.schema.json")
    Draft202012Validator.check_schema(protocol)
    validator = Draft202012Validator(protocol)

    for relative_path in (
        "golden/connect.json",
        "golden/hello-ok.json",
        "golden/error.json",
    ):
        validator.validate(_read_json(relative_path))

    for malformed in ({}, {"type": "pingg"}, {"id": "1", "method": "connect"}):
        with pytest.raises(ValidationError):
            validator.validate(malformed)

    refs: set[str] = set()

    def collect_refs(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                refs.add(ref)
            for nested in value.values():
                collect_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_refs(nested)

    collect_refs(protocol)
    definition_names = set(protocol["$defs"])
    assert all(
        ref.removeprefix("#/$defs/") in definition_names
        for ref in refs
        if ref.startswith("#/$defs/")
    )


def test_rpc_snapshot_comes_from_complete_locked_registry() -> None:
    registry = get_registry()
    contract = _read_json("rpc-methods.json")
    rows = contract["methods"]

    assert registry.registration_locked is True
    assert contract["source"] == "locked-rpc-registry"
    assert contract["method_count"] == len(rows) == 207
    assert [row["name"] for row in rows] == registry.methods()
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    assert [
        (entry.name, entry.required_scope) for entry in registry.contract_entries()
    ] == [(row["name"], row["required_scope"]) for row in rows]

    # Scope classification must be complete in both directions: no live method
    # without policy, and no explicit method policy advertising a missing RPC.
    registered = set(registry.methods())
    assert set(METHOD_SCOPES) <= registered
    assert set(NODE_ROLE_METHODS) <= registered
    for row in rows:
        expected_scope = (
            "node"
            if row["name"] in NODE_ROLE_METHODS
            else resolve_required_scope(row["name"])
        )
        assert row["required_scope"] == expected_scope


def test_unlocked_registry_snapshot_fails_closed() -> None:
    from opensquilla.gateway.rpc.registry import RpcRegistry, ScopeDriftError

    registry = RpcRegistry()
    with pytest.raises(ScopeDriftError, match="must be locked"):
        registry.contract_entries()


def test_hello_declared_features_and_event_baseline_share_sources() -> None:
    features = _build_features(get_dispatcher())
    events = _read_json("events.json")

    assert features.methods == get_registry().methods()
    assert features.events == list(DECLARED_EVENTS)
    assert events["declared_events"] == list(DECLARED_EVENTS)
    assert events["completeness"] == "open-event-set"
    assert events["event_patterns"] == ["session.event.*", "task.*"]
    assert "session.event.text_delta" in events["observed_only"]
    assert "models.routing.changed" in events["observed_only"]


def test_observed_event_inventory_matches_webui_literal_subscriptions() -> None:
    source_root = REPOSITORY_ROOT / "opensquilla-webui" / "src"
    subscription = re.compile(
        r"""\b(?:rpc|options\.rpc)\.on(?:\?\.)?\(\s*['"]([^'"]+)['"]"""
    )
    internal_events = {"*", "_gap", "_hello", "_state"}
    observed: set[str] = set()

    for path in source_root.rglob("*"):
        if path.suffix not in {".ts", ".vue"} or ".test." in path.name:
            continue
        observed.update(subscription.findall(path.read_text(encoding="utf-8")))

    assert observed - internal_events == set(_read_json("events.json")["observed_client_events"])


def test_final_route_tree_freezes_dynamic_routes_and_security_policy() -> None:
    contract = _read_json("http-routes.json")
    routes = contract["routes"]
    by_path = {row["path"]: row for row in routes}

    assert contract["route_count"] == len(routes) == 36
    assert by_path["/ws"]["transport"] == "websocket"
    assert by_path["/ws"]["auth"]["scheme"] == "websocket-connect-handshake"
    assert by_path["/ws"]["auth"]["modes"]["trusted-proxy"]["status"] == (
        "unsupported-by-principal-resolver"
    )
    assert by_path["/control/static/{path}"]["transport"] == "mount"
    assert by_path["/control/static/{path}"]["methods"] == ["GET", "HEAD"]

    late_registered = {
        "/api/v1/files/upload",
        "/api/v1/attachments/{sha256}",
        "/api/v1/artifacts/{artifact_id}/open",
        "/api/v1/artifacts/{artifact_id}",
        "/api/audio/transcribe",
        "/api/v1/diagnostics/bundle",
    }
    assert late_registered <= set(by_path)

    for row in routes:
        if set(row["methods"]) & {"DELETE", "PATCH", "POST", "PUT"}:
            assert row["origin"] == {
                "allowed": [
                    "origin-header-absent",
                    "same-scheme-host-effective-port",
                    "exact-configured-origin",
                ],
                "configured_wildcard_accepted": False,
                "policy": "browser-origin-guard",
            }
        if row["path"] in {"/health", "/healthz", "/ready", "/readyz"}:
            assert row["auth"]["scheme"] == "public"

    assert by_path["/api/desktop/identity"]["auth"] == {
        "caller_credential_required": False,
        "desktop_instance_required": True,
        "middleware": "bypassed",
        "network_constraints": ["loopback-bind", "loopback-peer"],
        "request_fields": ["challenge"],
        "scheme": "desktop-server-identity-challenge",
    }
    assert by_path["/api/desktop/shutdown"]["auth"]["scheme"] == "desktop-ownership-proof"
    assert by_path["/api/config"]["auth"]["scheme"] == "gateway-auth-mode-matrix"
    assert by_path["/api/config"]["auth"]["modes"]["none"]["credential_transport"] == "none"
    assert by_path["/api/config"]["auth"]["modes"]["password"]["middleware"] == (
        "falls-through-without-validation"
    )
    assert by_path["/api/v1/files/upload"]["auth"]["modes"]["token"][
        "credential_transport"
    ] == (
        "header-only-in-token-mode"
    )
    assert by_path["/api/audio/transcribe"]["auth"]["modes"]["token"][
        "credential_transport"
    ] == (
        "header-only-in-token-mode"
    )
    assert by_path["/api/v1/artifacts/{artifact_id}/open"]["auth"]["owner_required"] is True
    assert by_path["/api/v1/diagnostics/bundle"]["auth"]["owner_required"] is True
    assert by_path["/api/system/shutdown"]["auth"]["owner_required"] is True
    assert by_path["/api/elevated-mode"]["auth"]["owner_required"] is True


def test_route_collector_includes_configured_extra_route() -> None:
    async def extra_handler(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/synthetic-webhook", extra_handler, methods=["GET"])])

    assert collect_http_routes(app) == (
        {
            "auth": {
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
                        "credential_transport": "header-or-query",
                        "middleware": "token-required",
                        "principal_resolution": "token-scope-resolver",
                    },
                    "trusted-proxy": {
                        "credential_transport": "x-forwarded-for",
                        "middleware": "configured-proxy-substring-check-or-pass-when-unset",
                        "principal_resolution": "unsupported",
                    },
                },
                "owner_required": False,
                "scheme": "gateway-auth-mode-matrix",
            },
            "methods": ["GET", "HEAD"],
            "order": 0,
            "origin": {"policy": "not-required"},
            "path": "/synthetic-webhook",
            "surface": "operations",
            "transport": "http",
        },
    )


def test_route_collector_rejects_unmarked_mutating_route() -> None:
    async def unsafe_handler(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/unsafe", unsafe_handler, methods=["POST"])])

    with pytest.raises(ClientContractError, match="lacks explicit same-origin"):
        collect_http_routes(app)


def test_golden_frames_validate_and_contain_only_synthetic_identity() -> None:
    connect = _read_json("golden/connect.json")
    hello = HelloOk.model_validate(_read_json("golden/hello-ok.json"))
    error = ResFrame.model_validate(_read_json("golden/error.json"))

    assert connect["type"] == "req"
    assert connect["method"] == "connect"
    assert connect["params"]["minProtocol"] == 1
    assert connect["params"]["maxProtocol"] == 3
    assert connect["params"]["auth"]["token"] == "<synthetic>"
    assert hello.id == connect["id"]
    assert hello.protocol == 3
    assert hello.server.conn_id == "00000000-0000-0000-0000-000000000001"
    assert hello.features.methods == get_registry().methods()
    assert hello.features.events == list(DECLARED_EVENTS)
    assert hello.contract is not None
    assert hello.contract.schema_version == CONTRACT_SCHEMA_VERSION
    assert hello.contract.digest == CLIENT_CONTRACT_DIGEST
    assert hello.contract.generated_from == CONTRACT_GENERATED_FROM
    assert hello.runtime is not None
    assert hello.runtime.core_version == "0.0.0-contract"
    assert hello.runtime.build_commit is None
    assert hello.protocol_range is not None
    assert (hello.protocol_range.min, hello.protocol_range.max) == (1, 3)
    assert hello.capabilities == [
        CAPABILITY_RPC,
        CAPABILITY_SESSIONS,
        CAPABILITY_ARTIFACTS,
    ]
    assert hello.extensions == []
    assert error.ok is False
    assert error.error is not None
    assert error.error.code == "INVALID_REQUEST"
    assert hello.snapshot.auth_mode == "token"

    assert hello.snapshot.config_path == "synthetic-config.toml"
    assert hello.snapshot.state_dir == "synthetic-state"
    for value in (hello.snapshot.config_path, hello.snapshot.state_dir):
        assert value is not None
        assert not Path(value).is_absolute()
        assert not PureWindowsPath(value).is_absolute()


async def test_golden_hello_matches_real_handshake_after_dynamic_fields_are_scrubbed() -> None:
    class HandshakeWebSocket:
        client_state = WebSocketState.CONNECTED
        client = SimpleNamespace(host="127.0.0.1", port=18791)

        def __init__(self) -> None:
            self._frames = [json.dumps(_read_json("golden/connect.json"))]
            self.sent: list[str] = []

        async def accept(self) -> None:
            return None

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

        async def receive_text(self) -> str:
            if self._frames:
                return self._frames.pop(0)
            raise WebSocketDisconnect(code=1000)

        async def close(self, code: int = 1000, reason: str = "") -> None:
            return None

    websocket = HandshakeWebSocket()
    config = GatewayConfig(
        auth=AuthConfig(
            mode="token",
            token="<synthetic>",
            token_scopes=["operator.read", "operator.write"],
        ),
        config_path="synthetic-config.toml",
        state_dir="synthetic-state",
        ws_writer_queue_enabled=False,
    )

    await handle_ws_connection(
        websocket,
        config,
        get_dispatcher(),
        loaded_capabilities=(CAPABILITY_ARTIFACTS,),
    )

    actual = next(
        frame
        for frame in (json.loads(payload) for payload in websocket.sent)
        if frame.get("type") == "hello-ok"
    )
    actual["server"] = {
        "conn_id": "00000000-0000-0000-0000-000000000001",
        "version": "0.0.0-contract",
    }
    actual["snapshot"]["uptime_ms"] = 1_700_000_000_000
    actual["runtime"] = {
        "arch": "synthetic",
        "buildCommit": None,
        "coreVersion": "0.0.0-contract",
        "platform": "synthetic",
    }

    assert actual == _read_json("golden/hello-ok.json")


def test_rendered_contract_has_no_local_paths_or_real_credentials(
    snapshot: ClientContractSnapshot,
) -> None:
    blob = b"".join(render_contract_artifacts(snapshot).values())

    assert str(Path.home()).encode() not in blob
    assert str(REPOSITORY_ROOT).encode() not in blob
    assert b"sk-" not in blob
    assert b"Bearer " not in blob
    assert b"<synthetic>" in blob


def test_synthetic_route_build_restores_process_upload_store() -> None:
    original = get_upload_store()

    build_client_contract_snapshot()

    assert get_upload_store() is original
