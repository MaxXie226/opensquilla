from __future__ import annotations

from opensquilla.gateway.auth import Principal
from opensquilla.sandbox.run_mode_policy import hello_auth_payload


def test_owner_hello_auth_payload_allows_full_by_default() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write"}),
        is_owner=True,
        authenticated=True,
    )

    assert hello_auth_payload(principal) == {
        "principal": {
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "capabilities": ["host.execute", "host.read", "task.read", "task.submit"],
            "isOwner": True,
            "authenticated": True,
            "authState": "authenticated",
            "tokenPublicId": None,
        },
        "runModePolicy": {
            "allowedRunModes": ["safe", "full"],
            "defaultRunMode": "safe",
            "fullHostAccessDisabledReason": None,
        },
    }


def test_unauthenticated_non_owner_hello_auth_payload_disables_full() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        is_owner=False,
        authenticated=False,
    )

    assert hello_auth_payload(principal) == {
        "principal": {
            "role": "operator",
            "scopes": ["operator.read"],
            "capabilities": ["guest.safe"],
            "isOwner": False,
            "authenticated": False,
            "authState": "guest",
            "tokenPublicId": None,
        },
        "runModePolicy": {
            "allowedRunModes": ["safe"],
            "defaultRunMode": "safe",
            "fullHostAccessDisabledReason": "host_capability_required",
        },
    }
