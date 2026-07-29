"""Gateway Runtime release metadata and deterministic archive helpers."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform as host_platform
import re
import stat
import tarfile
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla import __version__
from opensquilla.gateway.contract_identity import CLIENT_CONTRACT_DIGEST
from opensquilla.gateway.hello_capabilities import (
    CAPABILITY_ARTIFACTS,
    CAPABILITY_RPC,
    CAPABILITY_SESSIONS,
)
from opensquilla.gateway.protocol import PROTOCOL_VERSION

SCHEMA_VERSION = 1
SUPPORTED_TARGETS = frozenset(
    {
        ("darwin", "arm64"),
        ("linux", "x64"),
        ("win32", "x64"),
    }
)
CAPABILITIES = (
    CAPABILITY_RPC,
    CAPABILITY_SESSIONS,
    CAPABILITY_ARTIFACTS,
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_CREATED_BY_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z./:@_-]{0,255}$")


class RuntimeArtifactError(ValueError):
    """Raised when a Runtime artifact or its metadata is invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_platform(value: str | None = None) -> str:
    raw = (value or host_platform.system()).strip().lower()
    aliases = {
        "darwin": "darwin",
        "linux": "linux",
        "windows": "win32",
        "win32": "win32",
    }
    try:
        return aliases[raw]
    except KeyError as error:
        raise RuntimeArtifactError(f"unsupported Runtime platform: {raw or '<empty>'}") from error


def normalize_arch(value: str | None = None) -> str:
    raw = (value or host_platform.machine()).strip().lower()
    aliases = {
        "aarch64": "arm64",
        "amd64": "x64",
        "arm64": "arm64",
        "x64": "x64",
        "x86_64": "x64",
    }
    try:
        return aliases[raw]
    except KeyError as error:
        raise RuntimeArtifactError(
            f"unsupported Runtime architecture: {raw or '<empty>'}"
        ) from error


def validate_target(platform_name: str, arch: str) -> tuple[str, str]:
    target = (normalize_platform(platform_name), normalize_arch(arch))
    if target not in SUPPORTED_TARGETS:
        raise RuntimeArtifactError(
            f"unsupported Runtime target: {target[0]}-{target[1]}; "
            "supported targets are darwin-arm64, linux-x64, and win32-x64"
        )
    return target


def normalize_build_commit(value: str) -> str:
    commit = value.strip().lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise RuntimeArtifactError("build commit must be 7-64 lowercase hexadecimal characters")
    return commit


def source_date_epoch(value: int | None = None) -> int:
    if value is not None:
        epoch = value
    else:
        raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
        epoch = int(raw) if raw else 0
    if epoch < 0:
        raise RuntimeArtifactError("SOURCE_DATE_EPOCH cannot be negative")
    return epoch


def archive_suffix(platform_name: str) -> str:
    return ".zip" if normalize_platform(platform_name) == "win32" else ".tar.gz"


def artifact_stem(version: str, platform_name: str, arch: str) -> str:
    if not _VERSION_RE.fullmatch(version):
        raise RuntimeArtifactError(f"invalid Runtime version: {version!r}")
    normalized_platform, normalized_arch = validate_target(platform_name, arch)
    return f"gateway-runtime-{version}-{normalized_platform}-{normalized_arch}"


def archive_filename(version: str, platform_name: str, arch: str) -> str:
    return f"{artifact_stem(version, platform_name, arch)}{archive_suffix(platform_name)}"


def manifest_filename(version: str, platform_name: str, arch: str) -> str:
    return f"{artifact_stem(version, platform_name, arch)}.manifest.json"


def sbom_filename(version: str, platform_name: str, arch: str) -> str:
    return f"{artifact_stem(version, platform_name, arch)}.spdx.json"


def provenance_filename(version: str, platform_name: str, arch: str) -> str:
    return f"{artifact_stem(version, platform_name, arch)}.provenance.json"


def runtime_entrypoint(platform_name: str) -> str:
    return "opensquilla-gateway.exe" if normalize_platform(platform_name) == "win32" else (
        "opensquilla-gateway"
    )


def _tar_filter(info: tarfile.TarInfo, *, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        executable = bool(info.mode & stat.S_IXUSR)
        info.mode = 0o755 if executable else 0o644
    return info


def _archive_paths(bundle_dir: Path) -> list[Path]:
    return sorted(
        (path for path in bundle_dir.rglob("*") if path.is_file() or path.is_dir()),
        key=lambda path: path.relative_to(bundle_dir).as_posix(),
    )


def _build_tar_archive(bundle_dir: Path, output: Path, *, epoch: int) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in _archive_paths(bundle_dir):
                    relative = path.relative_to(bundle_dir).as_posix()
                    info = archive.gettarinfo(os.fspath(path), arcname=relative)
                    info = _tar_filter(info, epoch=epoch)
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def _zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    minimum = int(datetime(1980, 1, 1, tzinfo=UTC).timestamp())
    return datetime.fromtimestamp(max(epoch, minimum), tz=UTC).timetuple()[:6]


def _build_zip_archive(bundle_dir: Path, output: Path, *, epoch: int) -> None:
    timestamp = _zip_timestamp(epoch)
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in _archive_paths(bundle_dir):
            relative = path.relative_to(bundle_dir).as_posix()
            if path.is_dir():
                relative += "/"
            info = zipfile.ZipInfo(relative, timestamp)
            info.create_system = 3
            mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            file_type = stat.S_IFDIR if path.is_dir() else stat.S_IFREG
            info.external_attr = (file_type | mode) << 16
            if path.is_dir():
                archive.writestr(info, b"")
            else:
                archive.writestr(info, path.read_bytes())


def build_archive(
    bundle_dir: Path,
    output: Path,
    *,
    platform_name: str,
    epoch: int | None = None,
) -> Path:
    if not bundle_dir.is_dir():
        raise RuntimeArtifactError(f"Runtime bundle directory does not exist: {bundle_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    normalized_platform = normalize_platform(platform_name)
    normalized_epoch = source_date_epoch(epoch)
    if normalized_platform == "win32":
        _build_zip_archive(bundle_dir, output, epoch=normalized_epoch)
    else:
        _build_tar_archive(bundle_dir, output, epoch=normalized_epoch)
    return output


def _created_at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _installed_packages() -> list[dict[str, Any]]:
    packages: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        version = str(distribution.version or "").strip()
        if name and version:
            packages[name] = version
    return [
        {
            "SPDXID": f"SPDXRef-Package-{index}",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "name": name,
            "versionInfo": packages[name],
        }
        for index, name in enumerate(sorted(packages, key=str.casefold), 1)
    ]


def build_sbom(
    output: Path,
    *,
    archive: Path,
    version: str,
    platform_name: str,
    arch: str,
    build_commit: str,
    created_by: str,
    epoch: int,
) -> Path:
    stem = artifact_stem(version, platform_name, arch)
    packages = _installed_packages()
    runtime_package = {
        "SPDXID": "SPDXRef-Package-GatewayRuntime",
        "checksums": [{"algorithm": "SHA256", "checksumValue": sha256_file(archive)}],
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "Apache-2.0",
        "licenseDeclared": "Apache-2.0",
        "name": stem,
        "supplier": "Organization: OpenSquilla",
        "versionInfo": version,
    }
    payload = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": _created_at(epoch),
            "creators": [f"Tool: {created_by}"],
        },
        "dataLicense": "CC0-1.0",
        "documentDescribes": ["SPDXRef-Package-GatewayRuntime"],
        "documentNamespace": (
            f"https://opensquilla.ai/spdx/gateway-runtime/{version}/"
            f"{platform_name}-{arch}/{build_commit}"
        ),
        "name": f"{stem}-sbom",
        "packages": [runtime_package, *packages],
        "spdxVersion": "SPDX-2.3",
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def build_provenance(
    output: Path,
    *,
    archive: Path,
    version: str,
    platform_name: str,
    arch: str,
    build_commit: str,
    created_by: str,
    epoch: int,
) -> Path:
    payload = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "digest": {"sha256": sha256_file(archive)},
                "name": archive.name,
            }
        ],
        "predicate": {
            "buildDefinition": {
                "buildType": "https://opensquilla.ai/build-types/gateway-runtime/v1",
                "externalParameters": {
                    "arch": arch,
                    "platform": platform_name,
                    "runtimeVersion": version,
                },
                "internalParameters": {"buildCommit": build_commit},
                "resolvedDependencies": [],
            },
            "runDetails": {
                "builder": {"id": created_by},
                "metadata": {
                    "finishedOn": _created_at(epoch),
                    "invocationId": os.environ.get("GITHUB_RUN_ID", "") or "local",
                    "startedOn": _created_at(epoch),
                },
            },
        },
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def build_manifest(
    output: Path,
    *,
    archive: Path,
    sbom: Path,
    provenance: Path,
    version: str,
    platform_name: str,
    arch: str,
    build_commit: str,
    created_by: str,
    epoch: int,
) -> Path:
    normalized_platform, normalized_arch = validate_target(platform_name, arch)
    if not _CREATED_BY_RE.fullmatch(created_by):
        raise RuntimeArtifactError(f"invalid Runtime builder identity: {created_by!r}")
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "runtimeVersion": version,
        "coreVersion": version,
        "buildCommit": normalize_build_commit(build_commit),
        "protocol": {"min": 1, "max": PROTOCOL_VERSION},
        "contractDigest": CLIENT_CONTRACT_DIGEST,
        "platform": normalized_platform,
        "arch": normalized_arch,
        "entrypoint": runtime_entrypoint(normalized_platform),
        "archive": {
            "filename": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
        "sbom": {
            "filename": sbom.name,
            "sha256": sha256_file(sbom),
        },
        "provenance": {
            "filename": provenance.name,
            "sha256": sha256_file(provenance),
        },
        "capabilities": list(CAPABILITIES),
        "createdAt": _created_at(epoch),
        "createdBy": created_by,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeArtifactError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeArtifactError(f"{label} must be a non-empty string")
    return value


def _validate_file_ref(value: Any, label: str, *, size_required: bool) -> Mapping[str, Any]:
    ref = _require_mapping(value, label)
    filename = _require_string(ref.get("filename"), f"{label}.filename")
    path = PurePosixPath(filename)
    if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
        raise RuntimeArtifactError(f"{label}.filename must be a safe basename")
    digest = _require_string(ref.get("sha256"), f"{label}.sha256")
    if not _DIGEST_RE.fullmatch(digest):
        raise RuntimeArtifactError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    if size_required:
        size = ref.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeArtifactError(f"{label}.size must be a positive integer")
    return ref


def validate_manifest(
    value: Mapping[str, Any],
    *,
    expected_platform: str | None = None,
    expected_arch: str | None = None,
) -> Mapping[str, Any]:
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise RuntimeArtifactError("unsupported Runtime manifest schemaVersion")
    version = _require_string(value.get("runtimeVersion"), "runtimeVersion")
    if not _VERSION_RE.fullmatch(version) or value.get("coreVersion") != version:
        raise RuntimeArtifactError("runtimeVersion/coreVersion must be equal valid versions")
    normalize_build_commit(_require_string(value.get("buildCommit"), "buildCommit"))
    platform_name, arch = validate_target(
        _require_string(value.get("platform"), "platform"),
        _require_string(value.get("arch"), "arch"),
    )
    if expected_platform is not None and platform_name != normalize_platform(expected_platform):
        raise RuntimeArtifactError(
            f"Runtime platform mismatch: expected {normalize_platform(expected_platform)}, "
            f"got {platform_name}"
        )
    if expected_arch is not None and arch != normalize_arch(expected_arch):
        raise RuntimeArtifactError(
            f"Runtime architecture mismatch: expected {normalize_arch(expected_arch)}, got {arch}"
        )
    protocol = _require_mapping(value.get("protocol"), "protocol")
    if protocol.get("min") != 1 or protocol.get("max") != PROTOCOL_VERSION:
        raise RuntimeArtifactError("Runtime protocol range does not match the public Gateway")
    digest = _require_string(value.get("contractDigest"), "contractDigest")
    if not _CONTRACT_DIGEST_RE.fullmatch(digest) or digest != CLIENT_CONTRACT_DIGEST:
        raise RuntimeArtifactError("Runtime contract digest does not match the public Gateway")
    if value.get("entrypoint") != runtime_entrypoint(platform_name):
        raise RuntimeArtifactError("Runtime entrypoint does not match the target platform")
    _validate_file_ref(value.get("archive"), "archive", size_required=True)
    _validate_file_ref(value.get("sbom"), "sbom", size_required=False)
    _validate_file_ref(value.get("provenance"), "provenance", size_required=False)
    capabilities = value.get("capabilities")
    if capabilities != list(CAPABILITIES):
        raise RuntimeArtifactError("Runtime capabilities do not match the stable public set")
    _require_string(value.get("createdAt"), "createdAt")
    created_by = _require_string(value.get("createdBy"), "createdBy")
    if not _CREATED_BY_RE.fullmatch(created_by):
        raise RuntimeArtifactError("createdBy is not a stable builder identity")
    return value


def write_sha256s(paths: Iterable[Path], output: Path) -> Path:
    ordered = sorted(paths, key=lambda path: path.name)
    names = [path.name for path in ordered]
    if not names or len(names) != len(set(names)):
        raise RuntimeArtifactError("checksum inputs must be non-empty and have unique basenames")
    output.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in ordered),
        encoding="utf-8",
    )
    return output


def package_runtime_artifact(
    *,
    bundle_dir: Path,
    artifacts_dir: Path,
    version: str = __version__,
    platform_name: str,
    arch: str,
    build_commit: str,
    created_by: str,
    epoch: int | None = None,
) -> dict[str, Path]:
    normalized_platform, normalized_arch = validate_target(platform_name, arch)
    normalized_epoch = source_date_epoch(epoch)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    archive = artifacts_dir / archive_filename(version, normalized_platform, normalized_arch)
    build_archive(
        bundle_dir,
        archive,
        platform_name=normalized_platform,
        epoch=normalized_epoch,
    )
    sbom = artifacts_dir / sbom_filename(version, normalized_platform, normalized_arch)
    provenance = artifacts_dir / provenance_filename(
        version, normalized_platform, normalized_arch
    )
    manifest = artifacts_dir / manifest_filename(version, normalized_platform, normalized_arch)
    build_sbom(
        sbom,
        archive=archive,
        version=version,
        platform_name=normalized_platform,
        arch=normalized_arch,
        build_commit=build_commit,
        created_by=created_by,
        epoch=normalized_epoch,
    )
    build_provenance(
        provenance,
        archive=archive,
        version=version,
        platform_name=normalized_platform,
        arch=normalized_arch,
        build_commit=build_commit,
        created_by=created_by,
        epoch=normalized_epoch,
    )
    build_manifest(
        manifest,
        archive=archive,
        sbom=sbom,
        provenance=provenance,
        version=version,
        platform_name=normalized_platform,
        arch=normalized_arch,
        build_commit=build_commit,
        created_by=created_by,
        epoch=normalized_epoch,
    )
    return {
        "archive": archive,
        "manifest": manifest,
        "provenance": provenance,
        "sbom": sbom,
    }
