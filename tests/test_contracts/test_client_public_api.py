"""Stable Python facade for the public client contract."""

from opensquilla.contracts.client import (
    DECLARED_ERROR_CODES,
    DECLARED_EVENTS,
    EVENT_PATTERNS,
    ConnectParams,
    ContractInfo,
    ErrorShape,
    EventFrame,
    HelloOk,
    PolicyInfo,
    ProtocolRangeInfo,
    ReqFrame,
    ResFrame,
    RuntimeInfo,
)
from opensquilla.gateway import protocol


def test_public_client_models_reexport_runtime_contract_owners() -> None:
    assert ReqFrame is protocol.ReqFrame
    assert ResFrame is protocol.ResFrame
    assert EventFrame is protocol.EventFrame
    assert ErrorShape is protocol.ErrorShape
    assert ConnectParams is protocol.ConnectParams
    assert HelloOk is protocol.HelloOk
    assert ContractInfo is protocol.ContractInfo
    assert RuntimeInfo is protocol.RuntimeInfo
    assert ProtocolRangeInfo is protocol.ProtocolRangeInfo
    assert PolicyInfo is protocol.PolicyInfo


def test_public_client_catalogs_match_runtime_declarations() -> None:
    assert DECLARED_EVENTS is protocol.DECLARED_EVENTS
    assert "connect.challenge" in DECLARED_EVENTS
    assert "session.event.*" in EVENT_PATTERNS
    assert "INVALID_REQUEST" in DECLARED_ERROR_CODES
