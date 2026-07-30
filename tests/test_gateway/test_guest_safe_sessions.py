from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.routing import (
    build_web_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _guest_profile_for_principal,
    _trusted_run_mode_hint,
)
from opensquilla.sandbox.guest_profile import GuestProfileFactory
from opensquilla.tools.builtin.shell import _base_shell_environment
from opensquilla.tools.types import ToolContext, current_tool_context


def _guest_principal(*, invalid: bool = False) -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset(),
        is_owner=False,
        authenticated=False,
        capabilities=frozenset({"guest.safe"}),
        auth_state="invalid" if invalid else "guest",
    )


@pytest.mark.parametrize("invalid", [False, True])
def test_guest_and_invalid_token_reject_explicit_full_before_materialization(
    invalid: bool,
) -> None:
    ctx = RpcContext(conn_id="lan", principal=_guest_principal(invalid=invalid))

    with pytest.raises(RpcHandlerError) as raised:
        _trusted_run_mode_hint(ctx, {"runMode": "full"})

    assert raised.value.code == "HOST_CAPABILITY_REQUIRED"


def test_guest_route_uses_ephemeral_workspace_and_scrubbed_environment(
    tmp_path: Path,
) -> None:
    profile = GuestProfileFactory.create("turn", temp_parent=tmp_path)
    envelope = build_web_route_envelope(session_key="agent:main:web:guest")
    envelope.metadata["guest_safe"] = True
    envelope.metadata["guest_environment"] = dict(profile.environment)
    envelope.metadata["run_mode"] = "safe"
    envelope.metadata["sandbox_run_context"] = profile.run_context().to_origin_payload()

    context = tool_context_from_envelope(
        envelope,
        is_owner=False,
        workspace_dir=str(tmp_path / "host-project"),
    )

    assert context.guest_safe is True
    assert context.run_mode == "safe"
    assert context.workspace_dir == str(profile.workspace)
    assert context.environment == profile.environment
    profile.cleanup()


def test_guest_shell_environment_never_inherits_host_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-secret")
    profile = GuestProfileFactory.create("turn", temp_parent=tmp_path)
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            workspace_dir=str(profile.workspace),
            environment=profile.environment,
        )
    )
    try:
        environment = _base_shell_environment()
    finally:
        current_tool_context.reset(token)
        profile.cleanup()

    assert "AWS_SECRET_ACCESS_KEY" not in environment


@pytest.mark.parametrize("invalid", [False, True])
def test_missing_and_invalid_token_materialize_the_same_guest_boundary(
    invalid: bool,
) -> None:
    profile = _guest_profile_for_principal(
        _guest_principal(invalid=invalid),
        "turn",
    )

    assert profile is not None
    assert profile.run_context().run_mode.value == "safe"
    assert profile.host_home_mounted is False
    profile.cleanup()
