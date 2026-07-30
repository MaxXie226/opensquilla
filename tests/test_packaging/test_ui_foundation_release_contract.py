from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _json(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_ui_release_manifest_owns_four_public_packages() -> None:
    manifest = _json("packages/ui-package-manifest.json")
    matrix = _json("packages/ui-compatibility-matrix.json")
    packages = manifest["packages"]

    assert manifest["schemaVersion"] == 2
    assert manifest["versionPolicy"] == "release-groups"
    assert manifest["compatibilityPolicy"] == "current-and-previous-minor"
    assert {entry["name"] for entry in packages} == {
        "@opensquilla/client-sdk",
        "@opensquilla/ui-tokens",
        "@opensquilla/ui-primitives",
        "@opensquilla/ui-foundation",
    }
    assert matrix["current"]["packages"] == {
        entry["name"]: entry["version"] for entry in packages
    }
    foundation = next(
        group for group in manifest["releaseGroups"] if group["id"] == "ui-foundation"
    )
    assert foundation["versionPolicy"] == "fixed"
    assert {
        entry["version"] for entry in packages if entry["name"] in foundation["packages"]
    } == {matrix["current"]["releaseVersion"]}
    foundation_package = _json("packages/ui-foundation/package.json")
    assert foundation_package["dependencies"]["@opensquilla/client-sdk"] == (
        matrix["current"]["clientSdkRange"]
    )


def test_ui_release_matrix_matches_gateway_and_composition_contracts() -> None:
    matrix = _json("packages/ui-compatibility-matrix.json")
    contract = _json("contracts/client/v3/contract.json")
    foundation_source = (
        ROOT / "packages/ui-foundation/src/composition/types.ts"
    ).read_text(encoding="utf-8")

    assert matrix["current"]["gateway"] == {
        "protocolMin": 3,
        "protocolMax": 3,
        "contractDigest": contract["digest"],
    }
    assert "UI_COMPOSITION_API_VERSION = 1" in foundation_source
    assert "NATIVE_CAPABILITY_API_VERSION = 1" in foundation_source
    assert matrix["current"]["featureApi"] == {"min": 1, "max": 1}
    assert matrix["current"]["nativeCapabilityApi"] == {"min": 1, "max": 1}


def test_ui_api_report_versions_match_release_manifest() -> None:
    manifest = _json("packages/ui-package-manifest.json")
    report = _json("contracts/ui-foundation/v1/api-report.json")

    assert report["schemaVersion"] == 1
    assert report["compatibilityPolicy"] == manifest["compatibilityPolicy"]
    assert {
        entry["name"]: entry["version"] for entry in report["packages"]
    } == {
        entry["name"]: entry["version"] for entry in manifest["packages"]
    }
    for entry in report["packages"]:
        assert entry["apiExports"]
        assert entry["runtimeExports"]
        assert entry["declarationsDigest"].startswith("sha256:")
        assert entry["declarations"]
