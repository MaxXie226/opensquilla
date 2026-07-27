"""Client-safe parsing for product-neutral Gateway Hello metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

CAPABILITY_RPC = "gateway.rpc"
CAPABILITY_SESSIONS = "gateway.sessions"
CAPABILITY_ARTIFACTS = "gateway.artifacts"

_CAPABILITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_BUILD_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Method-derived capabilities are limited to surfaces proven by the old
# Gateway's Hello inventory. Route-backed capabilities must be declared by a
# new Gateway and are never guessed by a legacy client.
_CAPABILITY_METHOD_REQUIREMENTS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        CAPABILITY_SESSIONS,
        frozenset({"chat.history", "chat.send", "sessions.list", "sessions.resolve"}),
    ),
)


@dataclass(frozen=True, slots=True)
class ParsedRuntimeInfo:
    """Runtime identity normalized for both new and legacy Gateways."""

    core_version: str
    build_commit: str | None
    platform: str | None
    arch: str | None


@dataclass(frozen=True, slots=True)
class ParsedHelloCapabilities:
    """Client-side, fail-closed view of a successful Hello frame."""

    protocol: int
    protocol_min: int
    protocol_max: int
    contract_status: Literal["advertised", "legacy-contract"]
    contract_schema_version: int | None
    contract_digest: str | None
    contract_generated_from: str | None
    runtime: ParsedRuntimeInfo
    capabilities: frozenset[str]
    capability_source: Literal["hello", "features.methods", "none"]
    extensions: frozenset[str]
    response_id_status: Literal["matched", "legacy-missing"]

    def supports(self, capability: str) -> bool:
        """Return whether the Gateway explicitly or compatibly exposes a capability."""

        return capability in self.capabilities


class HelloValidationError(ValueError):
    """Raised when a Hello frame cannot safely complete the handshake."""


def normalize_build_commit(value: Any) -> str | None:
    """Normalize an injected source revision without reading repository state."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate.lower() if _BUILD_COMMIT_PATTERN.fullmatch(candidate) else None


def capabilities_for_methods(
    methods: Sequence[str],
    *,
    loaded_capabilities: Sequence[str] = (),
) -> tuple[str, ...]:
    """Map a live public method inventory to stable capability identifiers."""

    available = frozenset(method for method in methods if isinstance(method, str))
    capabilities = [CAPABILITY_RPC]
    capabilities.extend(
        capability
        for capability, requirements in _CAPABILITY_METHOD_REQUIREMENTS
        if requirements <= available
    )
    capabilities.extend(
        capability
        for capability in loaded_capabilities
        if _CAPABILITY_ID_PATTERN.fullmatch(capability) and capability not in capabilities
    )
    return tuple(capabilities)


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _stable_ids(value: Any) -> frozenset[str]:
    return frozenset(item for item in _string_list(value) if _CAPABILITY_ID_PATTERN.fullmatch(item))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _wire_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _runtime_from_hello(frame: Mapping[str, Any]) -> ParsedRuntimeInfo:
    runtime = _mapping(frame.get("runtime"))
    server = _mapping(frame.get("server"))
    core_version = runtime.get("coreVersion")
    if not isinstance(core_version, str) or not core_version:
        server_version = server.get("version")
        core_version = (
            server_version
            if isinstance(server_version, str) and server_version
            else "unknown"
        )
    platform_name = runtime.get("platform")
    architecture = runtime.get("arch")
    return ParsedRuntimeInfo(
        core_version=core_version,
        build_commit=normalize_build_commit(runtime.get("buildCommit")),
        platform=platform_name if isinstance(platform_name, str) and platform_name else None,
        arch=architecture if isinstance(architecture, str) and architecture else None,
    )


def parse_hello_frame(
    frame: Mapping[str, Any],
    *,
    request_id: str,
    client_min_protocol: int,
    client_max_protocol: int,
) -> ParsedHelloCapabilities:
    """Validate a Hello response and normalize optional capability metadata.

    A missing response id is tolerated only for a legacy Hello that contains
    none of the O-02 fields. This preserves old-Gateway compatibility while
    requiring correlation from all new-format servers.
    """

    if frame.get("type") != "hello-ok":
        raise HelloValidationError("expected hello-ok frame")

    protocol = _wire_int(frame.get("protocol"))
    if (
        protocol is None
        or protocol < client_min_protocol
        or protocol > client_max_protocol
    ):
        raise HelloValidationError("negotiated protocol is outside the requested range")

    response_id = frame.get("id")
    new_fields = {"contract", "runtime", "protocolRange", "capabilities", "extensions"}
    has_new_metadata = any(field in frame for field in new_fields)
    if response_id is None:
        if has_new_metadata:
            raise HelloValidationError("new-format hello-ok frame is missing response id")
        response_id_status: Literal["matched", "legacy-missing"] = "legacy-missing"
    elif not isinstance(response_id, str) or response_id != request_id:
        raise HelloValidationError("hello-ok response id does not match connect request")
    else:
        response_id_status = "matched"

    protocol_range = _mapping(frame.get("protocolRange"))
    range_min = _wire_int(protocol_range.get("min"))
    range_max = _wire_int(protocol_range.get("max"))
    if range_min is None or range_max is None or range_min > range_max:
        range_min = protocol
        range_max = protocol
    if range_max < client_min_protocol or range_min > client_max_protocol:
        raise HelloValidationError("Gateway protocol range does not overlap client range")

    contract = _mapping(frame.get("contract"))
    schema_version = _wire_int(contract.get("schemaVersion"))
    digest = contract.get("digest")
    generated_from = contract.get("generatedFrom")
    contract_valid = (
        schema_version is not None
        and isinstance(digest, str)
        and _DIGEST_PATTERN.fullmatch(digest) is not None
        and isinstance(generated_from, str)
        and bool(generated_from)
    )
    if not contract_valid:
        schema_version = None
        digest = None
        generated_from = None

    if "capabilities" in frame:
        capabilities = _stable_ids(frame.get("capabilities"))
        capability_source: Literal["hello", "features.methods", "none"] = (
            "hello" if isinstance(frame.get("capabilities"), list) else "none"
        )
    else:
        features = _mapping(frame.get("features"))
        methods = _string_list(features.get("methods"))
        capabilities = frozenset(capabilities_for_methods(methods)) if methods else frozenset()
        capability_source = "features.methods" if methods else "none"

    extensions = _stable_ids(frame.get("extensions")) if "extensions" in frame else frozenset()
    return ParsedHelloCapabilities(
        protocol=protocol,
        protocol_min=range_min,
        protocol_max=range_max,
        contract_status="advertised" if contract_valid else "legacy-contract",
        contract_schema_version=schema_version,
        contract_digest=digest if isinstance(digest, str) else None,
        contract_generated_from=generated_from if isinstance(generated_from, str) else None,
        runtime=_runtime_from_hello(frame),
        capabilities=capabilities,
        capability_source=capability_source,
        extensions=extensions,
        response_id_status=response_id_status,
    )


__all__ = [
    "CAPABILITY_ARTIFACTS",
    "CAPABILITY_RPC",
    "CAPABILITY_SESSIONS",
    "HelloValidationError",
    "ParsedHelloCapabilities",
    "ParsedRuntimeInfo",
    "capabilities_for_methods",
    "normalize_build_commit",
    "parse_hello_frame",
]
