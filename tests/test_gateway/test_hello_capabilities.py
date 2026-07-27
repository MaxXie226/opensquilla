from __future__ import annotations

from typing import Any

import pytest

from opensquilla.gateway.contract_identity import (
    CLIENT_CONTRACT_DIGEST,
    CONTRACT_GENERATED_FROM,
    CONTRACT_SCHEMA_VERSION,
)
from opensquilla.gateway.hello_capabilities import (
    CAPABILITY_ARTIFACTS,
    CAPABILITY_RPC,
    CAPABILITY_SESSIONS,
    HelloValidationError,
    build_contract_info,
    build_protocol_range,
    build_runtime_info,
    capabilities_for_methods,
    parse_hello_frame,
)
from opensquilla.gateway.protocol import ConnectParams


def _new_hello(**overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "hello-ok",
        "id": "connect-1",
        "protocol": 3,
        "server": {"version": "0.5.0", "conn_id": "synthetic"},
        "features": {
            "methods": [
                "agents.files.get",
                "agents.files.list",
                "chat.history",
                "chat.send",
                "sessions.list",
                "sessions.resolve",
            ]
        },
        "contract": {
            "schemaVersion": CONTRACT_SCHEMA_VERSION,
            "digest": CLIENT_CONTRACT_DIGEST,
            "generatedFrom": CONTRACT_GENERATED_FROM,
        },
        "runtime": {
            "coreVersion": "0.5.0",
            "buildCommit": None,
            "platform": "linux",
            "arch": "x86_64",
        },
        "protocolRange": {"min": 1, "max": 3},
        "capabilities": [
            CAPABILITY_RPC,
            CAPABILITY_SESSIONS,
            CAPABILITY_ARTIFACTS,
        ],
        "extensions": [],
    }
    frame.update(overrides)
    return frame


def test_connect_params_parses_wire_aliases_without_changing_tolerated_extras() -> None:
    params = ConnectParams.model_validate(
        {
            "minProtocol": 1,
            "maxProtocol": 3,
            "auth": {"token": "<synthetic>"},
            "client": {"name": "test-client"},
            "futureField": {"enabled": True},
        }
    )

    assert (params.min_protocol, params.max_protocol) == (1, 3)
    assert params.model_dump(by_alias=True)["minProtocol"] == 1
    assert params.model_extra == {"futureField": {"enabled": True}}


def test_server_metadata_is_product_neutral_and_build_commit_is_injected() -> None:
    contract = build_contract_info()
    protocol_range = build_protocol_range()
    runtime = build_runtime_info(build_commit="A345C13E")

    assert contract.schema_version == CONTRACT_SCHEMA_VERSION
    assert contract.digest == CLIENT_CONTRACT_DIGEST
    assert contract.generated_from == CONTRACT_GENERATED_FROM
    assert (protocol_range.min, protocol_range.max) == (1, 3)
    assert runtime.build_commit == "a345c13e"
    assert runtime.platform
    assert runtime.arch
    assert not {"edition", "license", "entitlements"} & set(
        contract.model_dump() | runtime.model_dump()
    )


@pytest.mark.parametrize(
    "raw_commit",
    ["", "not-a-commit", "../.git/HEAD", "secret value", "abc123"],
)
def test_runtime_does_not_use_invalid_or_source_checkout_commit(raw_commit: str) -> None:
    runtime = build_runtime_info(build_commit=raw_commit)

    assert runtime.build_commit is None


def test_runtime_does_not_read_a_commit_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_BUILD_COMMIT", "a345c13e")

    assert build_runtime_info().build_commit is None


def test_capabilities_are_derived_only_from_complete_public_method_sets() -> None:
    complete = [
        "agents.files.get",
        "agents.files.list",
        "chat.history",
        "chat.send",
        "sessions.list",
        "sessions.resolve",
    ]

    assert capabilities_for_methods(
        complete,
        loaded_capabilities=(CAPABILITY_ARTIFACTS,),
    ) == (
        CAPABILITY_RPC,
        CAPABILITY_SESSIONS,
        CAPABILITY_ARTIFACTS,
    )
    assert capabilities_for_methods(["sessions.list"]) == (CAPABILITY_RPC,)


def test_new_hello_requires_matching_response_id_and_uses_explicit_capabilities() -> None:
    parsed = parse_hello_frame(
        _new_hello(),
        request_id="connect-1",
        client_min_protocol=1,
        client_max_protocol=3,
    )

    assert parsed.contract_status == "advertised"
    assert parsed.contract_digest == CLIENT_CONTRACT_DIGEST
    assert parsed.response_id_status == "matched"
    assert parsed.capability_source == "hello"
    assert parsed.capabilities == frozenset(
        {CAPABILITY_RPC, CAPABILITY_SESSIONS, CAPABILITY_ARTIFACTS}
    )
    assert parsed.runtime.core_version == "0.5.0"
    assert parsed.runtime.build_commit is None


@pytest.mark.parametrize(
    "frame",
    [
        _new_hello(id="wrong"),
        {key: value for key, value in _new_hello().items() if key != "id"},
        _new_hello(type="res"),
    ],
)
def test_new_hello_rejects_uncorrelated_or_wrong_type_frames(frame: dict[str, Any]) -> None:
    with pytest.raises(HelloValidationError):
        parse_hello_frame(
            frame,
            request_id="connect-1",
            client_min_protocol=1,
            client_max_protocol=3,
        )


def test_legacy_hello_uses_method_fallback_and_minimal_runtime_identity() -> None:
    legacy = {
        "type": "hello-ok",
        "protocol": 3,
        "server": {"version": "0.4.0"},
        "features": {
            "methods": [
                "chat.history",
                "chat.send",
                "sessions.list",
                "sessions.resolve",
            ]
        },
    }

    parsed = parse_hello_frame(
        legacy,
        request_id="connect-1",
        client_min_protocol=1,
        client_max_protocol=3,
    )

    assert parsed.contract_status == "legacy-contract"
    assert parsed.response_id_status == "legacy-missing"
    assert parsed.capability_source == "features.methods"
    assert parsed.capabilities == frozenset({CAPABILITY_RPC, CAPABILITY_SESSIONS})
    assert parsed.runtime.core_version == "0.4.0"
    assert parsed.runtime.build_commit is None
    assert (parsed.protocol_min, parsed.protocol_max) == (3, 3)


def test_explicit_empty_capabilities_do_not_fall_back_to_methods() -> None:
    parsed = parse_hello_frame(
        _new_hello(capabilities=[]),
        request_id="connect-1",
        client_min_protocol=1,
        client_max_protocol=3,
    )

    assert parsed.capability_source == "hello"
    assert parsed.capabilities == frozenset()


def test_protocol_range_without_overlap_is_rejected() -> None:
    with pytest.raises(HelloValidationError, match="does not overlap"):
        parse_hello_frame(
            _new_hello(protocolRange={"min": 1, "max": 2}),
            request_id="connect-1",
            client_min_protocol=3,
            client_max_protocol=3,
        )
