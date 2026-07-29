"""Run offline capability and lifecycle smoke tests against a built Gateway Runtime."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from websockets.sync.client import connect

from scripts.gateway_runtime.manifest import RuntimeArtifactError

_STRIPPED_ENVIRONMENT = {
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
}
_CA_MARKER = "opensquilla-runtime-ca-store-ok"
_CAPABILITY_MARKER = "opensquilla-runtime-capabilities-ok"


def _binary(runtime_dir: Path) -> Path:
    name = "opensquilla-gateway.exe" if os.name == "nt" else "opensquilla-gateway"
    candidates = (runtime_dir / name, runtime_dir / "opensquilla-gateway" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeArtifactError(
        "Gateway Runtime entrypoint is missing; checked "
        + ", ".join(os.fspath(path) for path in candidates)
    )


def _clean_env(home: Path, config: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OPENSQUILLA_") and key.upper() not in _STRIPPED_ENVIRONMENT
    }
    env.update(
        {
            "HOME": os.fspath(home),
            "USERPROFILE": os.fspath(home),
            "OPENSQUILLA_GATEWAY_CONFIG_PATH": os.fspath(config),
            "PYTHONIOENCODING": "utf-8:replace",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _run(
    binary: Path,
    args: list[str],
    *,
    env: dict[str, str],
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [os.fspath(binary), *args],
        cwd=binary.parent,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeArtifactError(
            f"Runtime command failed ({' '.join(args)}): exit {result.returncode}\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )
    return result


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get_json(url: str, *, timeout: float = 2) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeArtifactError(f"{url} did not return a JSON object")
    return value


def _get_text(url: str, *, timeout: float = 2) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _wait_ready(process: subprocess.Popen[str], port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    health_passed = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeArtifactError(
                f"Gateway exited before readiness with {process.returncode}\n"
                f"{stdout[-4000:]}\n{stderr[-4000:]}"
            )
        try:
            endpoint = "readyz" if health_passed else "healthz"
            payload = _get_json(f"http://127.0.0.1:{port}/{endpoint}")
            if endpoint == "healthz" and payload.get("ok") is True:
                health_passed = True
            elif endpoint == "readyz" and payload.get("ready") is True:
                return
            last_error = f"{endpoint}={payload!r}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(1)
    raise RuntimeArtifactError(f"Gateway did not become ready: {last_error}")


def _websocket_smoke(
    port: int,
    *,
    expected_version: str | None,
    expected_build_commit: str | None,
) -> None:
    with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5, close_timeout=3) as websocket:
        challenge = json.loads(websocket.recv(timeout=10))
        if (
            challenge.get("type") != "event"
            or challenge.get("event") != "connect.challenge"
            or not isinstance(challenge.get("payload", {}).get("nonce"), str)
        ):
            raise RuntimeArtifactError(
                f"unexpected Gateway connect challenge: {challenge!r}"
            )
        websocket.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "runtime-smoke-connect",
                    "method": "connect",
                    "params": {
                        "minProtocol": 1,
                        "maxProtocol": 3,
                        "role": "operator",
                        "auth": {},
                        "client": {"name": "gateway-runtime-smoke"},
                    },
                }
            )
        )
        hello = json.loads(websocket.recv(timeout=10))
        if hello.get("type") != "hello-ok" or hello.get("id") != "runtime-smoke-connect":
            raise RuntimeArtifactError(f"unexpected Gateway Hello frame: {hello!r}")
        runtime = hello.get("runtime")
        if not isinstance(runtime, dict):
            raise RuntimeArtifactError("Gateway Hello omitted Runtime identity")
        if expected_version is not None and runtime.get("coreVersion") != expected_version:
            raise RuntimeArtifactError("Gateway Hello Runtime version does not match the manifest")
        if (
            expected_build_commit is not None
            and runtime.get("buildCommit") != expected_build_commit
        ):
            raise RuntimeArtifactError("Gateway Hello build commit does not match the manifest")
        if hello.get("protocolRange") != {"min": 1, "max": 3}:
            raise RuntimeArtifactError("Gateway Hello protocol range is invalid")
        if not isinstance(hello.get("contract", {}).get("digest"), str):
            raise RuntimeArtifactError("Gateway Hello omitted its contract digest")

        websocket.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "runtime-smoke-sessions",
                    "method": "sessions.list",
                    "params": {},
                }
            )
        )
        response = json.loads(websocket.recv(timeout=10))
        if response.get("type") != "res" or response.get("id") != "runtime-smoke-sessions":
            raise RuntimeArtifactError(f"unexpected sessions.list response: {response!r}")
        if response.get("ok") is not True:
            raise RuntimeArtifactError(f"sessions.list smoke failed: {response!r}")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _lifecycle_smoke(
    binary: Path,
    *,
    env: dict[str, str],
    config: Path,
    timeout: int,
    expected_version: str | None,
    expected_build_commit: str | None,
    full: bool,
) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [
            os.fspath(binary),
            "gateway",
            "run",
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--config",
            os.fspath(config),
        ],
        cwd=binary.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_ready(process, port, timeout)
        if full:
            status = _run(
                binary,
                [
                    "gateway",
                    "status",
                    "--port",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                    "--json",
                    "--config",
                    os.fspath(config),
                ],
                env=env,
                timeout=30,
            )
            if not isinstance(json.loads(status.stdout), dict):
                raise RuntimeArtifactError("gateway status did not return a JSON object")
            _websocket_smoke(
                port,
                expected_version=expected_version,
                expected_build_commit=expected_build_commit,
            )
            control = _get_text(f"http://127.0.0.1:{port}/control/")
            if "Gateway is running in headless mode" not in control:
                raise RuntimeArtifactError("public Runtime unexpectedly served a client bundle")
            if "/static/dist/" in control:
                raise RuntimeArtifactError("public Runtime headless page references WebUI assets")
    finally:
        _terminate(process)


def smoke_runtime(
    runtime_dir: Path,
    *,
    manifest_path: Path | None,
    timeout: int,
) -> None:
    binary = _binary(runtime_dir)
    manifest: dict[str, Any] = {}
    if manifest_path is not None:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeArtifactError("Runtime manifest must be a JSON object")
        manifest = raw

    with tempfile.TemporaryDirectory(prefix="opensquilla-runtime-smoke-") as raw_home:
        home = Path(raw_home)
        state = home / "state"
        workspace = home / "workspace"
        state.mkdir()
        workspace.mkdir()
        (workspace / "SOUL.md").write_text(
            "synthetic public Gateway Runtime smoke\n",
            encoding="utf-8",
        )
        config = home / "config.toml"
        config.write_text(
            "\n".join(
                [
                    f"state_dir = {json.dumps(os.fspath(state))}",
                    f"workspace_dir = {json.dumps(os.fspath(workspace))}",
                    "",
                    "[auth]",
                    'mode = "none"',
                    "",
                    "[rate_limit]",
                    "enabled = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        env = _clean_env(home, config)
        version = _run(binary, ["--version"], env=env).stdout.strip()
        expected_version = manifest.get("runtimeVersion")
        if expected_version is not None and version != expected_version:
            raise RuntimeArtifactError(
                f"Runtime --version mismatch: expected {expected_version}, got {version}"
            )
        ca_probe = _run(binary, ["--_runtime-ca-probe"], env=env)
        if _CA_MARKER not in ca_probe.stdout:
            raise RuntimeArtifactError("Runtime CA trust probe did not report success")
        capability_probe = _run(binary, ["--_runtime-capability-probe"], env=env)
        if _CAPABILITY_MARKER not in capability_probe.stdout:
            raise RuntimeArtifactError(
                "Runtime database and registry capability probe did not report success"
            )
        worker = _run(
            binary,
            ["--_sandbox-filesystem-worker"],
            env=env,
            input_text=json.dumps(
                {
                    "kind": "read_file",
                    "path": os.fspath(workspace / "SOUL.md"),
                    "displayPath": "SOUL.md",
                }
            ),
        )
        if "synthetic public Gateway Runtime smoke" not in worker.stdout:
            raise RuntimeArtifactError("Runtime filesystem worker smoke failed")
        _run(binary, ["code-task", "smoke-imports"], env=env, timeout=timeout)
        _run(
            binary,
            [
                "code-task",
                "smoke-imports",
                "--module",
                "opensquilla.channels.contract",
                "--module",
                "opensquilla.provider.registry",
                "--module",
                "opensquilla.skills.loader",
                "--module",
                "opensquilla.tools.registry",
            ],
            env=env,
            timeout=timeout,
        )
        _run(binary, ["code-task", "smoke-router"], env=env, timeout=timeout)
        _lifecycle_smoke(
            binary,
            env=env,
            config=config,
            timeout=timeout,
            expected_version=expected_version,
            expected_build_commit=manifest.get("buildCommit"),
            full=True,
        )
        legacy_config = home / "legacy-config.toml"
        legacy_config.write_text(
            "\n".join(
                [
                    f"state_dir = {json.dumps(os.fspath(state))}",
                    f"workspace_dir = {json.dumps(os.fspath(workspace))}",
                    "",
                    "[auth]",
                    'mode = "none"',
                    "",
                    "[rate_limit]",
                    "enabled = false",
                    "",
                    "[squilla_router]",
                    "enabled = false",
                    "",
                    "[control_ui]",
                    'frontend = "legacy"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _lifecycle_smoke(
            binary,
            env=_clean_env(home, legacy_config),
            config=legacy_config,
            timeout=timeout,
            expected_version=expected_version,
            expected_build_commit=manifest.get("buildCommit"),
            full=False,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        smoke_runtime(
            args.runtime_dir.resolve(),
            manifest_path=args.manifest.resolve() if args.manifest else None,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeArtifactError, subprocess.SubprocessError) as error:
        print(f"Gateway Runtime smoke failed: {error}", file=sys.stderr)
        return 1
    print("OpenSquilla public Gateway Runtime smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
