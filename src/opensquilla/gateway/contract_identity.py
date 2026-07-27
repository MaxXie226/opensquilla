"""Stable identity for the generated public Gateway client contract."""

from __future__ import annotations

CONTRACT_SCHEMA_VERSION = 1
CONTRACT_GENERATED_FROM = "gateway"

# Updated only when the deterministic client-contract snapshot changes.
# ``tests/test_contracts/test_gateway_client_contract_v3.py`` verifies that this
# value matches the generated snapshot, so runtime handshakes never need to
# scan the repository or regenerate artifacts.
CLIENT_CONTRACT_DIGEST = (
    "sha256:0d06af1e696bfd22bb23aa341252a7cff14f1b980678be312bb84204b75d3239"
)

__all__ = [
    "CLIENT_CONTRACT_DIGEST",
    "CONTRACT_GENERATED_FROM",
    "CONTRACT_SCHEMA_VERSION",
]
