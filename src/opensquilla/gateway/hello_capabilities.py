"""Server-side builders for product-neutral Gateway Hello metadata."""

from __future__ import annotations

import platform

from opensquilla import __version__
from opensquilla._build_info import BUILD_COMMIT
from opensquilla.gateway.contract_identity import (
    CLIENT_CONTRACT_DIGEST,
    CONTRACT_GENERATED_FROM,
    CONTRACT_SCHEMA_VERSION,
)
from opensquilla.gateway.protocol import (
    PROTOCOL_VERSION,
    ContractInfo,
    ProtocolRangeInfo,
    RuntimeInfo,
)
from opensquilla.gateway_hello import (
    CAPABILITY_ARTIFACTS,
    CAPABILITY_RPC,
    CAPABILITY_SESSIONS,
    HelloValidationError,
    ParsedHelloCapabilities,
    ParsedRuntimeInfo,
    capabilities_for_methods,
    normalize_build_commit,
    parse_hello_frame,
)


def build_contract_info() -> ContractInfo:
    """Return the generated public-contract identity advertised by the server."""

    return ContractInfo(
        schemaVersion=CONTRACT_SCHEMA_VERSION,
        digest=CLIENT_CONTRACT_DIGEST,
        generatedFrom=CONTRACT_GENERATED_FROM,
    )


def build_runtime_info(
    *,
    build_commit: object = BUILD_COMMIT,
) -> RuntimeInfo:
    """Build runtime identity without consulting a source checkout or ``.git``."""

    platform_name = platform.system().strip().lower() or "unknown"
    architecture = platform.machine().strip().lower() or "unknown"
    return RuntimeInfo(
        coreVersion=__version__,
        buildCommit=normalize_build_commit(build_commit),
        platform=platform_name,
        arch=architecture,
    )


def build_protocol_range() -> ProtocolRangeInfo:
    """Return the full server-supported range, separate from negotiation."""

    return ProtocolRangeInfo(min=1, max=PROTOCOL_VERSION)


__all__ = [
    "CAPABILITY_ARTIFACTS",
    "CAPABILITY_RPC",
    "CAPABILITY_SESSIONS",
    "HelloValidationError",
    "ParsedHelloCapabilities",
    "ParsedRuntimeInfo",
    "build_contract_info",
    "build_protocol_range",
    "build_runtime_info",
    "capabilities_for_methods",
    "parse_hello_frame",
]
