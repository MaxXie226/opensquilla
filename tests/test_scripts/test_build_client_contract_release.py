from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_client_contract_release import (
    ClientContractReleaseError,
    build_release_assets,
    expected_client_contract_asset_names,
)
from scripts.gateway_runtime.release_assets import expected_runtime_asset_names

CONTRACT_DIR = Path("contracts/client/v3")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_runtime_assets(root: Path, *, version: str = "0.5.2") -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in expected_runtime_asset_names(version):
        (root / name).write_bytes(f"synthetic:{name}\n".encode())


def test_expected_client_contract_asset_names_are_versioned() -> None:
    assert expected_client_contract_asset_names("0.5.2") == (
        "opensquilla-client-contract-v3-0.5.2.zip",
        "opensquilla-client-contract-compatibility-0.5.2.json",
        "opensquilla-client-release-notice-0.5.2.json",
    )
    with pytest.raises(ClientContractReleaseError):
        expected_client_contract_asset_names("../unsafe")


def test_release_assets_are_deterministic_and_self_describing(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_runtime_assets(first)
    _write_runtime_assets(second)
    kwargs = {
        "contract_dir": CONTRACT_DIR,
        "version": "0.5.2",
        "source_ref": "v0.5.2",
        "source_commit": "a" * 40,
        "baseline_dir": CONTRACT_DIR,
    }

    first_paths = build_release_assets(output_dir=first, **kwargs)
    second_paths = build_release_assets(output_dir=second, **kwargs)

    assert [path.name for path in first_paths] == list(
        expected_client_contract_asset_names("0.5.2")
    )
    assert [_sha256(path) for path in first_paths] == [
        _sha256(path) for path in second_paths
    ]

    contract_zip, report_path, notice_path = first_paths
    with ZipFile(contract_zip) as archive:
        names = archive.namelist()
        assert "opensquilla-client-contract-v3-0.5.2/contract.json" in names
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(info.external_attr >> 16 == 0o100644 for info in archive.infolist())

    report = json.loads(report_path.read_text(encoding="utf-8"))
    notice = json.loads(notice_path.read_text(encoding="utf-8"))
    assert report["status"] == "compatible"
    assert notice["contract"]["digest"] == report["candidate"]["digest"]
    assert notice["contract"]["artifact"] == {
        "name": contract_zip.name,
        "sha256": _sha256(contract_zip),
    }
    assert notice["compatibility"]["report"] == {
        "name": report_path.name,
        "sha256": _sha256(report_path),
    }
    assert len(notice["runtimeAssets"]) == 12
    assert notice["runtimeAssets"][0]["sha256"] == _sha256(
        first / notice["runtimeAssets"][0]["name"]
    )
    assert notice["runtimeAssets"][0]["url"].startswith(
        "https://github.com/opensquilla/opensquilla/releases/download/v0.5.2/"
    )


def test_bootstrap_requires_an_explicit_release_decision(tmp_path: Path) -> None:
    _write_runtime_assets(tmp_path)
    common = {
        "contract_dir": CONTRACT_DIR,
        "output_dir": tmp_path,
        "version": "0.5.2",
        "source_ref": "v0.5.2",
        "source_commit": "b" * 40,
    }
    with pytest.raises(ClientContractReleaseError, match="baseline is required"):
        build_release_assets(**common)

    paths = build_release_assets(**common, allow_bootstrap=True)
    report = json.loads(paths[1].read_text(encoding="utf-8"))
    assert report["status"] == "bootstrap"
    assert report["blocking"] is False


def test_review_required_contract_cannot_be_published(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    for source in CONTRACT_DIR.rglob("*.json"):
        destination = candidate / source.relative_to(CONTRACT_DIR)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    error_path = candidate / "golden/error.json"
    error = json.loads(error_path.read_text(encoding="utf-8"))
    error["error"]["retryable"] = not error["error"]["retryable"]
    error_path.write_text(json.dumps(error), encoding="utf-8")
    runtime_assets = tmp_path / "runtime"
    _write_runtime_assets(runtime_assets)

    with pytest.raises(ClientContractReleaseError, match="review-required"):
        build_release_assets(
            contract_dir=candidate,
            output_dir=tmp_path / "out",
            version="0.5.2",
            source_ref="v0.5.2",
            source_commit="c" * 40,
            baseline_dir=CONTRACT_DIR,
            runtime_assets_dir=runtime_assets,
        )
