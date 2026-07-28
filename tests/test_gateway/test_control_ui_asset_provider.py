"""Control UI asset-mode resolution and Core/headless behavior."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.gateway import control_ui
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import ControlUiConfig, GatewayConfig
from opensquilla.gateway.control_ui import create_control_ui_routes
from opensquilla.gateway.control_ui_assets import (
    ControlUiAssetResolver,
    ControlUiAssets,
)
from opensquilla.onboarding.config_store import persist_config


def _embedded_tree(root: Path) -> tuple[Path, Path]:
    static_root = root / "static"
    dist_root = static_root / "dist"
    assets = dist_root / "assets"
    assets.mkdir(parents=True)
    (dist_root / "index.html").write_text(
        '<script type="module" src="./assets/app.js"></script>'
        '<link rel="stylesheet" href="./assets/app.css">',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("export {};\n", encoding="utf-8")
    (assets / "app.css").write_text("body{}\n", encoding="utf-8")
    return static_root, dist_root


def _resolve(
    config: GatewayConfig,
    *,
    static_root: Path,
    dist_root: Path,
    template_root: Path | None = None,
) -> ControlUiAssets:
    return ControlUiAssetResolver(
        config,
        embedded_static_root=static_root,
        embedded_dist_root=dist_root,
        template_root=template_root or control_ui._TEMPLATE_DIR,
    ).resolve()


def test_auto_uses_existing_embedded_bundle_without_requiring_new_manifest(
    tmp_path: Path,
) -> None:
    static_root, dist_root = _embedded_tree(tmp_path)

    assets = _resolve(
        GatewayConfig(),
        static_root=static_root,
        dist_root=dist_root,
    )

    assert assets.mode == "embedded"
    assert assets.static_root == static_root
    assert assets.dist_root == dist_root
    assert assets.manifest is None
    assert assets.reason is None


@pytest.mark.parametrize("mode", ["auto", "embedded"])
def test_missing_embedded_bundle_degrades_to_none(
    tmp_path: Path,
    mode: str,
) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()

    assets = _resolve(
        GatewayConfig(control_ui={"assets_mode": mode}),
        static_root=static_root,
        dist_root=static_root / "dist",
    )

    assert assets.mode == "none"
    assert assets.dist_root is None
    assert assets.reason == "embedded:asset_directory_missing"


def test_explicit_none_does_not_probe_configured_asset_path(tmp_path: Path) -> None:
    assets = _resolve(
        GatewayConfig(
            control_ui={
                "assets_mode": "none",
                "assets_path": str(tmp_path / "does-not-exist"),
            }
        ),
        static_root=tmp_path / "static",
        dist_root=tmp_path / "static" / "dist",
    )

    assert assets.mode == "none"
    assert assets.reason == "explicit_none"


def test_explicit_none_does_not_expose_an_existing_embedded_dist(tmp_path: Path) -> None:
    static_root, dist_root = _embedded_tree(tmp_path)
    config = GatewayConfig(control_ui={"assets_mode": "none"})
    assets = _resolve(
        config,
        static_root=static_root,
        dist_root=dist_root,
    )
    client = TestClient(Starlette(routes=create_control_ui_routes(config, assets)))

    response = client.get("/control/static/dist/assets/app.js")

    assert response.status_code == 404


def test_disabled_control_ui_registers_no_routes(tmp_path: Path) -> None:
    config = GatewayConfig(control_ui={"enabled": False, "assets_mode": "none"})
    assets = _resolve(
        config,
        static_root=tmp_path / "static",
        dist_root=tmp_path / "static" / "dist",
    )

    assert assets.reason == "disabled"
    assert create_control_ui_routes(config, assets) == []


def test_none_mode_keeps_core_health_and_readiness_available(tmp_path: Path) -> None:
    config = GatewayConfig(control_ui={"assets_mode": "none"})
    assets = _resolve(
        config,
        static_root=tmp_path / "static",
        dist_root=tmp_path / "static" / "dist",
    )
    app = create_gateway_app(config, control_ui_assets=assets)
    app.state.gateway_ready = True
    client = TestClient(app)

    assert client.get("/health").json() == {"ok": True, "status": "live"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True

    response = client.get("/control/")
    assert response.status_code == 200
    assert "Gateway is running without a Control UI" in response.text
    assert "npm ci" not in response.text
    assert 'data-control-ui-assets-mode="none"' in response.text


def test_auto_missing_bundle_keeps_source_checkout_recovery_guidance(
    tmp_path: Path,
) -> None:
    config = GatewayConfig(control_ui={"assets_mode": "auto"})
    assets = _resolve(
        config,
        static_root=tmp_path / "static",
        dist_root=tmp_path / "static" / "dist",
    )
    app = Starlette(routes=create_control_ui_routes(config, assets))

    response = TestClient(app).get("/control/")

    assert response.status_code == 200
    assert "Control UI assets are unavailable" in response.text
    assert "npm ci &amp;&amp; npm run build" in response.text


def test_missing_neutral_template_falls_back_without_breaking_route(
    tmp_path: Path,
) -> None:
    config = GatewayConfig(control_ui={"assets_mode": "none"})
    assets = ControlUiAssets(
        mode="none",
        static_root=None,
        dist_root=None,
        template_root=tmp_path / "missing-templates",
        manifest=None,
        reason="explicit_none",
    )
    app = Starlette(routes=create_control_ui_routes(config, assets))

    response = TestClient(app).get("/control/")

    assert response.status_code == 200
    assert "Gateway is running without a Control UI" in response.text


@pytest.mark.parametrize("value", ["AUTO", " Embedded ", "external", "none"])
def test_assets_mode_normalizes_supported_values(value: str) -> None:
    assert ControlUiConfig(assets_mode=value).assets_mode == value.strip().lower()


def test_assets_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ControlUiConfig(assets_mode="discover")


def test_control_ui_assets_read_direct_environment_spelling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_CONTROL_UI_ASSETS_MODE", "external")
    monkeypatch.setenv("OPENSQUILLA_CONTROL_UI_ASSETS_PATH", f"  {tmp_path}  ")

    config = GatewayConfig()

    assert config.control_ui.assets_mode == "external"
    assert config.control_ui.assets_path == str(tmp_path)


def test_control_ui_assets_read_gateway_nested_environment_spelling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONTROL_UI__ASSETS_MODE", "external")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONTROL_UI__ASSETS_PATH", str(tmp_path))

    config = GatewayConfig()

    assert config.control_ui.assets_mode == "external"
    assert config.control_ui.assets_path == str(tmp_path)


def test_new_asset_fields_coexist_with_legacy_frontend_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "opensquilla.toml"
    config_path.write_text(
        "\n".join(
            [
                "[control_ui]",
                'frontend = "legacy"',
                'assets_mode = "none"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.warns(DeprecationWarning, match="Vue is always served"):
        config = GatewayConfig.load_from_toml(config_path)

    assert config.control_ui.frontend == "vue"
    assert config.control_ui.assets_mode == "none"


def test_asset_environment_override_wins_over_toml_without_being_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "opensquilla.toml"
    config_path.write_text(
        "\n".join(
            [
                "[control_ui]",
                'assets_mode = "auto"',
                'assets_path = "stored-ui"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / "runtime-only-ui"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONTROL_UI__ASSETS_MODE", "external")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONTROL_UI__ASSETS_PATH", str(env_path))

    config = GatewayConfig.load(config_path)

    assert config.control_ui.assets_mode == "external"
    assert config.control_ui.assets_path == str(env_path)

    persist_config(config, path=config_path, backup=False)
    with open(config_path, "rb") as handle:
        persisted = tomllib.load(handle)

    assert persisted["control_ui"]["assets_mode"] == "auto"
    assert persisted["control_ui"]["assets_path"] == "stored-ui"
