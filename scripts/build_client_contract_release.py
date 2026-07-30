#!/usr/bin/env python3
"""Build deterministic public client-contract release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from scripts.check_client_contract_compat import (
    CONTRACT_FILES,
    ContractCompatibilityError,
    compare_contracts,
    load_contract_directory,
    load_contract_git_ref,
    write_report,
)
from scripts.gateway_runtime.release_assets import expected_runtime_asset_names

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{7,64}$")
_ZIP_TIMESTAMP: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)


class ClientContractReleaseError(ValueError):
    """Raised when release metadata cannot be produced safely."""


def expected_client_contract_asset_names(version: str) -> tuple[str, str, str]:
    if not _VERSION_RE.fullmatch(version):
        raise ClientContractReleaseError(f"invalid client contract version: {version!r}")
    return (
        f"opensquilla-client-contract-v3-{version}.zip",
        f"opensquilla-client-contract-compatibility-{version}.json",
        f"opensquilla-client-release-notice-{version}.json",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_contract_zip(contract_dir: Path, output: Path, *, version: str) -> None:
    prefix = f"opensquilla-client-contract-v3-{version}"
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative in CONTRACT_FILES:
            source = contract_dir / relative
            if not source.is_file():
                raise ClientContractReleaseError(f"missing contract artifact: {source}")
            info = zipfile.ZipInfo(f"{prefix}/{relative}", _ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, source.read_bytes())


def build_release_assets(
    *,
    contract_dir: Path,
    output_dir: Path,
    version: str,
    source_ref: str,
    source_commit: str,
    baseline_ref: str | None = None,
    baseline_dir: Path | None = None,
    allow_bootstrap: bool = False,
    repository: Path = Path("."),
    runtime_assets_dir: Path | None = None,
) -> tuple[Path, Path, Path]:
    if not _VERSION_RE.fullmatch(version):
        raise ClientContractReleaseError(f"invalid client contract version: {version!r}")
    if not source_ref.strip() or any(ord(character) < 32 for character in source_ref):
        raise ClientContractReleaseError("source ref must be a non-empty safe string")
    commit = source_commit.strip().lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise ClientContractReleaseError(
            "source commit must be 7-64 lowercase hexadecimal characters"
        )
    if baseline_ref and baseline_dir:
        raise ClientContractReleaseError("baseline-ref and baseline-dir are mutually exclusive")
    if not baseline_ref and baseline_dir is None and not allow_bootstrap:
        raise ClientContractReleaseError(
            "a compatibility baseline is required unless --allow-bootstrap is explicit"
        )

    candidate = load_contract_directory(contract_dir)
    baseline = (
        load_contract_git_ref(baseline_ref, repository=repository)
        if baseline_ref
        else load_contract_directory(baseline_dir)
        if baseline_dir is not None
        else None
    )
    report = compare_contracts(baseline, candidate)
    if report["blocking"]:
        raise ClientContractReleaseError(
            f"client contract release is blocked: {report['status']}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    zip_name, report_name, notice_name = expected_client_contract_asset_names(version)
    zip_path = output_dir / zip_name
    report_path = output_dir / report_name
    notice_path = output_dir / notice_name

    _build_contract_zip(contract_dir, zip_path, version=version)
    write_report(report, report_path)

    runtime_root = runtime_assets_dir or output_dir
    runtime_assets = []
    encoded_ref = quote(source_ref, safe="")
    release_root = (
        f"https://github.com/opensquilla/opensquilla/releases/download/{encoded_ref}"
    )
    for name in expected_runtime_asset_names(version):
        path = runtime_root / name
        if not path.is_file():
            raise ClientContractReleaseError(f"missing Gateway Runtime release asset: {path}")
        runtime_assets.append(
            {
                "name": name,
                "sha256": _sha256(path),
                "url": f"{release_root}/{name}",
            }
        )

    hello = candidate.files["golden/hello-ok.json"]
    protocol_range = hello["protocolRange"]
    contract = candidate.files["contract.json"]
    notice = {
        "schemaVersion": 1,
        "product": "opensquilla-gateway-runtime",
        "version": version,
        "source": {
            "repository": "opensquilla/opensquilla",
            "ref": source_ref,
            "commit": commit,
        },
        "release": {
            "pageUrl": (
                f"https://github.com/opensquilla/opensquilla/releases/tag/{encoded_ref}"
            ),
            "sha256sums": {
                "name": "SHA256SUMS",
                "url": f"{release_root}/SHA256SUMS",
            },
        },
        "protocol": {
            "current": hello["protocol"],
            "min": protocol_range["min"],
            "max": protocol_range["max"],
        },
        "contract": {
            "schemaVersion": contract["schemaVersion"],
            "digest": contract["digest"],
            "artifact": {
                "name": zip_name,
                "sha256": _sha256(zip_path),
            },
        },
        "compatibility": {
            "status": report["status"],
            "baseline": report["baseline"],
            "report": {
                "name": report_name,
                "sha256": _sha256(report_path),
            },
        },
        "runtimeAssets": runtime_assets,
    }
    _write_json(notice_path, notice)
    return zip_path, report_path, notice_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--list-assets", action="store_true")
    parser.add_argument("--contract-dir", type=Path, default=Path("contracts/client/v3"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-ref")
    parser.add_argument("--source-commit")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-ref")
    baseline.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--allow-bootstrap", action="store_true")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--runtime-assets-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        names = expected_client_contract_asset_names(args.version)
        if args.list_assets:
            print("\n".join(names))
            return 0
        if args.output_dir is None or not args.source_ref or not args.source_commit:
            raise ClientContractReleaseError(
                "--output-dir, --source-ref, and --source-commit are required to build assets"
            )
        paths = build_release_assets(
            contract_dir=args.contract_dir,
            output_dir=args.output_dir,
            version=args.version,
            source_ref=args.source_ref,
            source_commit=args.source_commit,
            baseline_ref=args.baseline_ref,
            baseline_dir=args.baseline_dir,
            allow_bootstrap=args.allow_bootstrap,
            repository=args.repository,
            runtime_assets_dir=args.runtime_assets_dir,
        )
    except (
        ClientContractReleaseError,
        ContractCompatibilityError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"client contract release build failed: {error}", file=sys.stderr)
        return 2
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
