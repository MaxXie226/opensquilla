"""Verify a Gateway Runtime manifest, metadata, and archive without trusting paths."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from scripts.gateway_runtime.manifest import (
    RuntimeArtifactError,
    sha256_file,
    validate_manifest,
)

GIT_LFS_POINTER_HEADER = b"version https://git-lfs.github.com/spec/v1"
_MODEL_SUFFIXES = {".bin", ".joblib", ".onnx", ".pkl"}
_FORBIDDEN_PARTS = {".git", ".opensquilla", "node_modules", "static/dist"}
_FORBIDDEN_BASENAMES = {
    ".env",
    "credentials.json",
    "sessions.db",
}
_FORBIDDEN_ROOTS = {"logs", "profile", "state", "workspace"}


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    name: str
    size: int
    is_dir: bool
    is_link: bool
    mode: int


def _safe_name(raw: str) -> str:
    normalized = raw.replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise RuntimeArtifactError(f"unsafe Runtime archive path: {raw!r}")
    return path.as_posix()


class RuntimeArchive:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.name.endswith(".tar.gz"):
            self.kind = "tar"
            self.archive: tarfile.TarFile | zipfile.ZipFile = tarfile.open(path, "r:gz")
        elif path.suffix == ".zip":
            self.kind = "zip"
            self.archive = zipfile.ZipFile(path, "r")
        else:
            raise RuntimeArtifactError(f"unsupported Runtime archive format: {path.name}")

    def __enter__(self) -> RuntimeArchive:
        return self

    def __exit__(self, *_args: object) -> None:
        self.archive.close()

    def members(self) -> Iterator[ArchiveMember]:
        if self.kind == "tar":
            assert isinstance(self.archive, tarfile.TarFile)
            for info in self.archive.getmembers():
                yield ArchiveMember(
                    name=_safe_name(info.name),
                    size=info.size,
                    is_dir=info.isdir(),
                    is_link=info.issym() or info.islnk(),
                    mode=info.mode,
                )
        else:
            assert isinstance(self.archive, zipfile.ZipFile)
            for info in self.archive.infolist():
                mode = info.external_attr >> 16
                yield ArchiveMember(
                    name=_safe_name(info.filename),
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    is_link=stat.S_ISLNK(mode),
                    mode=mode,
                )

    def open_member(self, name: str) -> BinaryIO:
        if self.kind == "tar":
            assert isinstance(self.archive, tarfile.TarFile)
            handle = self.archive.extractfile(name)
            if handle is None:
                raise RuntimeArtifactError(f"Runtime archive member is unreadable: {name}")
            return handle
        assert isinstance(self.archive, zipfile.ZipFile)
        return self.archive.open(name, "r")


def _forbidden_member(name: str) -> bool:
    lowered = name.lower()
    path = PurePosixPath(lowered)
    if path.name in _FORBIDDEN_BASENAMES:
        return True
    if path.parts and path.parts[0] in _FORBIDDEN_ROOTS:
        return True
    if len(path.parts) == 1 and path.name == "config.toml":
        return True
    if any(part in _FORBIDDEN_PARTS for part in path.parts):
        return True
    return "/static/dist/" in f"/{lowered}/" or lowered.endswith("/static/dist")


def verify_archive(
    archive_path: Path,
    *,
    entrypoint: str,
    forbidden_paths: tuple[str, ...] = (),
    extract_to: Path | None = None,
) -> None:
    if not archive_path.is_file():
        raise RuntimeArtifactError(f"Runtime archive is missing: {archive_path}")
    forbidden_bytes = tuple(
        candidate.encode("utf-8")
        for candidate in forbidden_paths
        if candidate
    )
    seen: set[str] = set()
    with RuntimeArchive(archive_path) as archive:
        members = list(archive.members())
        for member in members:
            if member.name in seen:
                raise RuntimeArtifactError(
                    f"Runtime archive contains a duplicate path: {member.name}"
                )
            seen.add(member.name)
            if member.is_link:
                raise RuntimeArtifactError(
                    f"Runtime archive links are forbidden: {member.name}"
                )
            if _forbidden_member(member.name):
                raise RuntimeArtifactError(
                    f"Runtime archive contains client or private-state data: {member.name}"
                )
            if member.is_dir:
                continue
            suffix = PurePosixPath(member.name).suffix.lower()
            must_scan = suffix in _MODEL_SUFFIXES or bool(forbidden_bytes)
            if not must_scan:
                continue
            with archive.open_member(member.name) as handle:
                payload = handle.read() if forbidden_bytes else handle.read(96)
            if suffix in _MODEL_SUFFIXES and payload.startswith(GIT_LFS_POINTER_HEADER):
                raise RuntimeArtifactError(
                    f"Runtime archive contains a Git LFS pointer: {member.name}"
                )
            for forbidden in forbidden_bytes:
                if forbidden in payload:
                    raise RuntimeArtifactError(
                        f"Runtime archive leaks a forbidden build path in {member.name}"
                    )

        if entrypoint not in seen:
            raise RuntimeArtifactError(
                f"Runtime archive is missing declared entrypoint: {entrypoint}"
            )
        entry = next(member for member in members if member.name == entrypoint)
        if entry.is_dir or entry.size <= 0:
            raise RuntimeArtifactError("Runtime archive entrypoint is not an executable file")

        if extract_to is not None:
            shutil.rmtree(extract_to, ignore_errors=True)
            extract_to.mkdir(parents=True, exist_ok=True)
            for member in members:
                destination = extract_to.joinpath(*PurePosixPath(member.name).parts)
                if member.is_dir:
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open_member(member.name) as source:
                    with destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                if os.name != "nt":
                    destination.chmod(member.mode & 0o777 or 0o644)


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeArtifactError(f"{label} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeArtifactError(f"{label} must be a JSON object")
    return value


def _verify_file_ref(root: Path, ref: Mapping[str, object], label: str) -> Path:
    filename = ref["filename"]
    digest = ref["sha256"]
    assert isinstance(filename, str)
    assert isinstance(digest, str)
    path = root / filename
    if not path.is_file():
        raise RuntimeArtifactError(f"{label} file is missing: {filename}")
    if sha256_file(path) != digest:
        raise RuntimeArtifactError(f"{label} SHA-256 mismatch: {filename}")
    return path


def verify_sbom(path: Path, *, archive_name: str, archive_digest: str) -> None:
    sbom = _read_json(path, "Runtime SBOM")
    if sbom.get("spdxVersion") != "SPDX-2.3":
        raise RuntimeArtifactError("Runtime SBOM must use SPDX 2.3")
    packages = sbom.get("packages")
    if not isinstance(packages, list) or not packages:
        raise RuntimeArtifactError("Runtime SBOM contains no packages")
    runtime_package = next(
        (
            package
            for package in packages
            if isinstance(package, dict)
            and package.get("SPDXID") == "SPDXRef-Package-GatewayRuntime"
        ),
        None,
    )
    if runtime_package is None:
        raise RuntimeArtifactError("Runtime SBOM does not describe the Runtime package")
    checksums = runtime_package.get("checksums")
    if checksums != [{"algorithm": "SHA256", "checksumValue": archive_digest}]:
        raise RuntimeArtifactError("Runtime SBOM archive digest does not match")
    archive_stem = (
        archive_name.removesuffix(".tar.gz")
        if archive_name.endswith(".tar.gz")
        else archive_name.removesuffix(".zip")
    )
    if runtime_package.get("name") != archive_stem:
        raise RuntimeArtifactError("Runtime SBOM package name does not match the archive")


def verify_provenance(path: Path, *, archive_name: str, archive_digest: str) -> None:
    provenance = _read_json(path, "Runtime provenance")
    if provenance.get("_type") != "https://in-toto.io/Statement/v1":
        raise RuntimeArtifactError("Runtime provenance must be an in-toto v1 statement")
    if provenance.get("predicateType") != "https://slsa.dev/provenance/v1":
        raise RuntimeArtifactError("Runtime provenance must use the SLSA v1 predicate")
    if provenance.get("subject") != [
        {"digest": {"sha256": archive_digest}, "name": archive_name}
    ]:
        raise RuntimeArtifactError("Runtime provenance subject does not match the archive")


def verify_runtime_artifact(
    manifest_path: Path,
    *,
    expected_platform: str | None = None,
    expected_arch: str | None = None,
    forbidden_paths: tuple[str, ...] = (),
    extract_to: Path | None = None,
) -> Mapping[str, object]:
    manifest = validate_manifest(
        _read_json(manifest_path, "Runtime manifest"),
        expected_platform=expected_platform,
        expected_arch=expected_arch,
    )
    root = manifest_path.parent
    archive_ref = manifest["archive"]
    sbom_ref = manifest["sbom"]
    provenance_ref = manifest["provenance"]
    assert isinstance(archive_ref, Mapping)
    assert isinstance(sbom_ref, Mapping)
    assert isinstance(provenance_ref, Mapping)
    archive = _verify_file_ref(root, archive_ref, "Runtime archive")
    if archive.stat().st_size != archive_ref["size"]:
        raise RuntimeArtifactError("Runtime archive size does not match the manifest")
    sbom = _verify_file_ref(root, sbom_ref, "Runtime SBOM")
    provenance = _verify_file_ref(root, provenance_ref, "Runtime provenance")
    archive_digest = str(archive_ref["sha256"])
    verify_sbom(sbom, archive_name=archive.name, archive_digest=archive_digest)
    verify_provenance(
        provenance,
        archive_name=archive.name,
        archive_digest=archive_digest,
    )
    verify_archive(
        archive,
        entrypoint=str(manifest["entrypoint"]),
        forbidden_paths=forbidden_paths,
        extract_to=extract_to,
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-platform", choices=("darwin", "linux", "win32"))
    parser.add_argument("--expected-arch", choices=("arm64", "x64"))
    parser.add_argument("--forbid-path", action="append", default=[])
    parser.add_argument("--extract-to", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = verify_runtime_artifact(
            args.manifest,
            expected_platform=args.expected_platform,
            expected_arch=args.expected_arch,
            forbidden_paths=tuple(args.forbid_path),
            extract_to=args.extract_to,
        )
    except RuntimeArtifactError as error:
        print(f"Gateway Runtime verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "Verified Gateway Runtime "
        f"{manifest['runtimeVersion']} {manifest['platform']}-{manifest['arch']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
