"""Gateway behavior when product UI assets are absent or rejected."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from opensquilla.gateway import control_ui
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui_assets import (
    CONTROL_UI_MANIFEST_NAME,
    ControlUiAssetResolver,
)


def _resolve(config: GatewayConfig, tmp_path: Path):
    static_root = tmp_path / "static"
    static_root.mkdir(exist_ok=True)
    return ControlUiAssetResolver(
        config,
        embedded_static_root=static_root,
        embedded_dist_root=static_root / "dist",
        template_root=control_ui._TEMPLATE_DIR,
    ).resolve()


def test_corrupt_embedded_manifest_disables_ui_not_gateway(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    dist = static_root / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="./app.js"></script>',
        encoding="utf-8",
    )
    (dist / "app.js").write_text("export {};\n", encoding="utf-8")
    (dist / CONTROL_UI_MANIFEST_NAME).write_text("{not-json", encoding="utf-8")
    config = GatewayConfig(control_ui={"assets_mode": "embedded"})
    assets = ControlUiAssetResolver(
        config,
        embedded_static_root=static_root,
        embedded_dist_root=dist,
        template_root=control_ui._TEMPLATE_DIR,
    ).resolve()
    app = create_gateway_app(config, control_ui_assets=assets)
    app.state.gateway_ready = True
    client = TestClient(app)

    assert assets.mode == "none"
    assert assets.reason == "embedded:manifest_invalid"
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    response = client.get("/control/")
    assert response.status_code == 200
    assert "Control UI assets are unavailable" in response.text


def test_rejected_external_bundle_leaves_rpc_route_tree_mounted(tmp_path: Path) -> None:
    config = GatewayConfig(
        control_ui={
            "assets_mode": "external",
            "assets_path": str(tmp_path / "missing"),
        }
    )
    assets = _resolve(config, tmp_path)
    app = create_gateway_app(config, control_ui_assets=assets)
    app.state.gateway_ready = True
    client = TestClient(app)

    assert assets.mode == "none"
    assert client.get("/healthz").status_code == 200
    assert client.get("/ready").json()["ready"] is True
    assert client.get("/control/").status_code == 200
    assert client.get("/control/static/dist/unknown.js").status_code == 404


def test_disabled_control_ui_does_not_register_control_routes(tmp_path: Path) -> None:
    config = GatewayConfig(control_ui={"enabled": False})
    assets = _resolve(config, tmp_path)
    app = create_gateway_app(config, control_ui_assets=assets)
    client = TestClient(app)

    response = client.get("/control/", follow_redirects=False)

    assert assets.reason == "disabled"
    assert response.status_code == 404
    assert client.get("/health").status_code == 200
