from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scripts.gateway_runtime import smoke


class _RunningProcess:
    def poll(self) -> None:
        return None


class _ExitedProcess:
    returncode = 7

    def poll(self) -> int:
        return self.returncode


class _WebSocket:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self._incoming = iter(incoming)
        self.sent: list[dict[str, Any]] = []

    def __enter__(self) -> _WebSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def recv(self, *, timeout: int) -> str:
        del timeout
        return json.dumps(next(self._incoming))

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def test_wait_ready_uses_health_and_readiness_response_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: Iterator[dict[str, Any]] = iter(
        (
            {"ok": True, "status": "live"},
            {"ready": True, "status": "ready", "uptime_ms": 123},
        )
    )
    requested: list[str] = []

    def get_json(url: str, *, timeout: float = 2) -> dict[str, Any]:
        del timeout
        requested.append(url)
        return next(responses)

    monkeypatch.setattr(smoke, "_get_json", get_json)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    smoke._wait_ready(_RunningProcess(), 18791, 5)  # type: ignore[arg-type]

    assert requested == [
        "http://127.0.0.1:18791/healthz",
        "http://127.0.0.1:18791/readyz",
    ]


def test_lifecycle_smoke_uses_file_backed_logs_and_reports_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def popen(*_args: object, **kwargs: Any) -> _ExitedProcess:
        captured.update(kwargs)
        kwargs["stdout"].write("synthetic gateway stdout\n")
        kwargs["stderr"].write("synthetic gateway stderr\n")
        return _ExitedProcess()

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)

    with pytest.raises(smoke.RuntimeArtifactError) as raised:
        smoke._lifecycle_smoke(
            tmp_path / "opensquilla-gateway",
            env={},
            config=tmp_path / "config.toml",
            timeout=1,
            expected_version=None,
            expected_build_commit=None,
            full=False,
        )

    assert captured["stdout"] is not smoke.subprocess.PIPE
    assert captured["stderr"] is not smoke.subprocess.PIPE
    assert "synthetic gateway stdout" in str(raised.value)
    assert "synthetic gateway stderr" in str(raised.value)


def test_websocket_smoke_completes_challenge_before_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket(
        [
            {
                "type": "event",
                "event": "connect.challenge",
                "payload": {"nonce": "synthetic-nonce"},
            },
            {
                "type": "hello-ok",
                "id": "runtime-smoke-connect",
                "runtime": {"coreVersion": "1.2.3", "buildCommit": "a" * 40},
                "protocolRange": {"min": 1, "max": 3},
                "contract": {"digest": "sha256:" + "b" * 64},
            },
            {
                "type": "res",
                "id": "runtime-smoke-sessions",
                "ok": True,
                "payload": {"sessions": []},
            },
        ]
    )
    monkeypatch.setattr(smoke, "connect", lambda *_args, **_kwargs: websocket)

    smoke._websocket_smoke(
        18791,
        expected_version="1.2.3",
        expected_build_commit="a" * 40,
    )

    assert [frame["method"] for frame in websocket.sent] == ["connect", "sessions.list"]
