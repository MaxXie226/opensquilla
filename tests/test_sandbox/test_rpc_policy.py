from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sandbox import (
    _handle_sandbox_policy_get,
    _handle_sandbox_policy_update,
)


def _ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="policy-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read", "operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
        config=SimpleNamespace(state_dir=str(tmp_path)),
    )


async def test_rpc_policy_get_and_update(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    baseline = await _handle_sandbox_policy_get({}, ctx)
    baseline["network"]["denyDomains"] = ["telemetry.example"]

    saved = await _handle_sandbox_policy_update(
        {
            "basePolicyVersion": baseline["policyVersion"],
            "policy": baseline,
        },
        ctx,
    )

    assert saved["policyVersion"] == 1
    assert saved["network"]["denyDomains"] == ["telemetry.example"]


async def test_rpc_policy_update_reports_version_conflict(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    baseline = await _handle_sandbox_policy_get({}, ctx)
    await _handle_sandbox_policy_update(
        {"basePolicyVersion": 0, "policy": baseline},
        ctx,
    )

    with pytest.raises(RpcHandlerError) as exc_info:
        await _handle_sandbox_policy_update(
            {"basePolicyVersion": 0, "policy": baseline},
            ctx,
        )

    assert exc_info.value.code == "POLICY_VERSION_CONFLICT"
    assert exc_info.value.details["currentPolicy"]["policyVersion"] == 1
