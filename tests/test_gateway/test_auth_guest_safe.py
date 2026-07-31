from __future__ import annotations

from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.config import AuthConfig, GatewayConfig


def _token_config(tmp_path, *, token: str = "correct") -> GatewayConfig:
    return GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(mode="token", token=token),
    )


def test_missing_and_invalid_token_have_same_guest_execution_authority(tmp_path) -> None:
    config = _token_config(tmp_path)

    missing = resolve_auth(
        config,
        auth_params={},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )
    invalid = resolve_auth(
        config,
        auth_params={"token": "wrong"},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert missing is not None
    assert invalid is not None
    assert missing.capabilities == invalid.capabilities == frozenset({"guest.safe"})
    assert missing.scopes == invalid.scopes
    assert missing.auth_state == "guest"
    assert invalid.auth_state == "invalid"
    assert missing.authenticated is invalid.authenticated is False


def test_valid_legacy_operator_token_receives_host_execute(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    assert principal.auth_state == "authenticated"
    assert principal.authenticated is True
    assert "host.execute" in principal.capabilities
    assert "guest.safe" not in principal.capabilities


def test_missing_token_from_public_peer_is_rejected(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={},
        role_claim="operator",
        peer_ip="203.0.113.7",
    )

    assert principal is None


def test_allowed_client_cidrs_can_narrow_lan_access(tmp_path) -> None:
    config = GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(
            mode="token",
            token="correct",
            allowed_client_cidrs=["192.168.50.0/24"],
        ),
    )

    accepted = resolve_auth(
        config,
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.50.7",
    )
    rejected = resolve_auth(
        config,
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.51.7",
    )

    assert accepted is not None
    assert rejected is None
