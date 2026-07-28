"""Manifest, path, and serving security for external Control UI assets."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.gateway import control_ui
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui import create_control_ui_routes
from opensquilla.gateway.control_ui_assets import (
    CONTROL_UI_MANIFEST_NAME,
    ControlUiAssetResolver,
    ControlUiAssets,
)


def _record(root: Path, relative: str) -> dict[str, object]:
    content = (root / relative).read_bytes()
    return {
        "path": relative,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _rewrite_manifest(
    root: Path,
    *,
    client_version: str | None = "0.2.1",
    contract_digest: str | None = "sha256:" + "1" * 64,
) -> None:
    relatives = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != CONTROL_UI_MANIFEST_NAME
    )
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "sourceFingerprint": "synthetic-external-ui",
        "files": [_record(root, relative) for relative in relatives],
    }
    if client_version is not None:
        manifest["clientVersion"] = client_version
    if contract_digest is not None:
        manifest["contractDigest"] = contract_digest
    (root / CONTROL_UI_MANIFEST_NAME).write_text(
        f"{json.dumps(manifest, indent=2)}\n",
        encoding="utf-8",
    )


def _artifact(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        '<script type="module" src="./assets/app.js"></script>'
        '<link rel="stylesheet" href="./assets/app.css">'
        '<link rel="icon" href="./opensquilla-mark.png">',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("export const external = true;\n", encoding="utf-8")
    (assets / "app.css").write_text("body{color:#123}\n", encoding="utf-8")
    (root / "opensquilla-mark.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _rewrite_manifest(root)
    return root


def _resolve(config: GatewayConfig, tmp_path: Path) -> ControlUiAssets:
    embedded_static = tmp_path / "embedded-static"
    embedded_static.mkdir(exist_ok=True)
    return ControlUiAssetResolver(
        config,
        embedded_static_root=embedded_static,
        embedded_dist_root=embedded_static / "dist",
        template_root=control_ui._TEMPLATE_DIR,
    ).resolve()


def test_external_bundle_with_spaces_and_unicode_is_served_from_existing_routes(
    tmp_path: Path,
) -> None:
    dist = _artifact(tmp_path / "客户端 UI bundle")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )
    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "external"
    assert resolved.dist_root == dist.resolve()
    assert resolved.manifest is not None
    assert resolved.manifest.client_version == "0.2.1"
    assert resolved.manifest.entry_scripts == ("assets/app.js",)
    assert resolved.manifest.entry_styles == ("assets/app.css",)

    app = Starlette(routes=create_control_ui_routes(config, resolved))
    client = TestClient(app)
    index = client.get("/control/")
    script = client.get("/control/static/dist/assets/app.js")
    raw_index = client.get("/control/static/dist/index.html")

    assert index.status_code == 200
    assert "/control/static/dist/assets/app.js" in index.text
    assert script.status_code == 200
    assert script.text == "export const external = true;\n"
    assert script.headers["content-type"].startswith("text/javascript")
    assert "max-age=2592000" in script.headers["cache-control"]
    assert raw_index.status_code == 404


def test_external_path_may_point_to_static_parent_containing_dist(tmp_path: Path) -> None:
    static_root = tmp_path / "client-static"
    dist = _artifact(static_root / "dist")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(static_root),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "external"
    assert resolved.dist_root == dist.resolve()


def test_relative_external_path_resolves_from_config_directory(tmp_path: Path) -> None:
    profile = tmp_path / "operator-config"
    profile.mkdir()
    dist = _artifact(profile / "ui bundle")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": "ui bundle",
        }
    )
    config.config_path = str(profile / "opensquilla.toml")

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "external"
    assert resolved.dist_root == dist.resolve()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("digest", "external:manifest_digest_mismatch"),
        ("extra", "external:manifest_inventory_mismatch"),
        ("remote", "external:index_remote_asset"),
        ("bad-contract", "external:manifest_contract_digest_invalid"),
    ],
)
def test_external_manifest_or_inventory_failure_rejects_entire_bundle(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    dist = _artifact(tmp_path / mutation)
    if mutation == "digest":
        (dist / "assets/app.js").write_text("tampered\n", encoding="utf-8")
    elif mutation == "extra":
        (dist / "assets/unlisted.js").write_text("unlisted\n", encoding="utf-8")
    elif mutation == "remote":
        (dist / "index.html").write_text(
            '<script type="module" src="https://example.invalid/app.js"></script>',
            encoding="utf-8",
        )
        _rewrite_manifest(dist)
    else:
        _rewrite_manifest(dist, contract_digest="sha256:not-a-digest")

    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.dist_root is None
    assert resolved.reason == reason


def test_external_manifest_path_escape_is_rejected(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "path-escape")
    manifest_path = dist / CONTROL_UI_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.js"
    manifest_path.write_text(f"{json.dumps(manifest)}\n", encoding="utf-8")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:manifest_path_escape"


def test_external_bundle_requires_manifest(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "no-manifest")
    (dist / CONTROL_UI_MANIFEST_NAME).unlink()
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:manifest_missing"


def test_external_bundle_rejects_symbolic_linked_file(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "symlink-file")
    outside = tmp_path / "outside.js"
    outside.write_text("outside\n", encoding="utf-8")
    link = dist / "assets/link.js"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on this platform: {error}")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:artifact_link_forbidden"


def test_external_bundle_rejects_symbolic_linked_root(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "real-dist")
    linked = tmp_path / "linked-dist"
    try:
        linked.symlink_to(dist, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable on this platform: {error}")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(linked),
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:external_root_link"


def test_external_bundle_cannot_live_under_runtime_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    dist = _artifact(workspace / "artifacts" / "ui")
    config = GatewayConfig(
        workspace_dir=str(workspace),
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        },
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:external_runtime_data_root"


def test_external_bundle_rejects_unc_or_network_share_path(tmp_path: Path) -> None:
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": "//server/share/control-ui",
        }
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.mode == "none"
    assert resolved.reason == "external:external_network_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX world-writable mode bits")
def test_external_bundle_rejects_world_writable_root(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "world-writable")
    original_mode = stat_mode = dist.stat().st_mode
    dist.chmod(stat_mode | 0o002)
    try:
        config = GatewayConfig(
            control_ui={
                "assets_mode": "external",
                "assets_path": str(dist),
            }
        )
        resolved = _resolve(config, tmp_path)
    finally:
        dist.chmod(original_mode)

    assert resolved.mode == "none"
    assert resolved.reason == "external:external_world_writable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX world-writable mode bits")
def test_external_bundle_rejects_world_writable_subdirectory(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "world-writable-child")
    child = dist / "assets"
    original_mode = child.stat().st_mode
    child.chmod(original_mode | 0o002)
    try:
        config = GatewayConfig(
            control_ui={
                "assets_mode": "external",
                "assets_path": str(dist),
            }
        )
        resolved = _resolve(config, tmp_path)
    finally:
        child.chmod(original_mode)

    assert resolved.mode == "none"
    assert resolved.reason == "external:artifact_world_writable"


def test_manifest_allowlist_rejects_file_added_after_resolution(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "post-resolution")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )
    resolved = _resolve(config, tmp_path)
    assert resolved.mode == "external"
    (dist / "assets/late.js").write_text("late\n", encoding="utf-8")
    client = TestClient(Starlette(routes=create_control_ui_routes(config, resolved)))

    response = client.get("/control/static/dist/assets/late.js")

    assert response.status_code == 404


def test_manifest_digest_is_rechecked_after_resolution(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "post-resolution-tamper")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )
    resolved = _resolve(config, tmp_path)
    assert resolved.mode == "external"
    client = TestClient(Starlette(routes=create_control_ui_routes(config, resolved)))
    (dist / "assets/app.js").write_text("tampered after validation\n", encoding="utf-8")

    response = client.get("/control/static/dist/assets/app.js")

    assert response.status_code == 404


def test_external_entry_urls_are_frozen_from_validated_index(tmp_path: Path) -> None:
    dist = _artifact(tmp_path / "post-resolution-index-tamper")
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(dist),
        }
    )
    resolved = _resolve(config, tmp_path)
    assert resolved.mode == "external"
    (dist / "index.html").write_text(
        '<script type="module" src="https://example.invalid/evil.js"></script>',
        encoding="utf-8",
    )
    client = TestClient(Starlette(routes=create_control_ui_routes(config, resolved)))

    response = client.get("/control/")

    assert response.status_code == 200
    assert "/control/static/dist/assets/app.js" in response.text
    assert "example.invalid" not in response.text


def test_rejected_external_error_page_does_not_echo_absolute_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "secret operator path" / "missing"
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(missing),
        }
    )
    resolved = _resolve(config, tmp_path)
    client = TestClient(Starlette(routes=create_control_ui_routes(config, resolved)))

    response = client.get("/control/")

    assert response.status_code == 200
    assert "Configured Control UI assets were rejected" in response.text
    assert str(missing) not in response.text
