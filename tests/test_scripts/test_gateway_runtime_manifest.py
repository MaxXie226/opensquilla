from __future__ import annotations

import copy
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opensquilla.gateway.contract_identity import CLIENT_CONTRACT_DIGEST
from scripts.gateway_runtime.manifest import (
    RuntimeArtifactError,
    archive_filename,
    artifact_stem,
    manifest_filename,
    package_runtime_artifact,
    provenance_filename,
    sbom_filename,
    sha256_file,
    validate_manifest,
    write_sha256s,
)
from scripts.gateway_runtime.release_assets import expected_runtime_asset_names
from scripts.gateway_runtime.verify import verify_runtime_artifact

BUILD_COMMIT = "a" * 40
VERSION = "9.8.7rc1"


def _bundle(tmp_path: Path, *, platform_name: str) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    entrypoint = (
        bundle / "opensquilla-gateway.exe"
        if platform_name == "win32"
        else bundle / "opensquilla-gateway"
    )
    entrypoint.write_bytes(b"synthetic-runtime")
    entrypoint.chmod(0o755)
    (bundle / "_internal").mkdir()
    (bundle / "_internal/runtime-data.json").write_text(
        '{"synthetic": true}\n',
        encoding="utf-8",
    )
    return bundle


def _artifacts(
    tmp_path: Path,
    *,
    platform_name: str = "linux",
    arch: str = "x64",
) -> dict[str, Path]:
    return package_runtime_artifact(
        bundle_dir=_bundle(tmp_path, platform_name=platform_name),
        artifacts_dir=tmp_path / "artifacts",
        version=VERSION,
        platform_name=platform_name,
        arch=arch,
        build_commit=BUILD_COMMIT,
        created_by="unit-test",
        epoch=1_700_000_000,
    )


@pytest.mark.parametrize(
    ("platform_name", "arch", "suffix"),
    [
        ("darwin", "arm64", ".tar.gz"),
        ("linux", "x64", ".tar.gz"),
        ("win32", "x64", ".zip"),
    ],
)
def test_runtime_artifact_names_are_versioned_and_target_specific(
    platform_name: str,
    arch: str,
    suffix: str,
) -> None:
    stem = f"gateway-runtime-{VERSION}-{platform_name}-{arch}"

    assert artifact_stem(VERSION, platform_name, arch) == stem
    assert archive_filename(VERSION, platform_name, arch) == f"{stem}{suffix}"
    assert manifest_filename(VERSION, platform_name, arch) == f"{stem}.manifest.json"
    assert sbom_filename(VERSION, platform_name, arch) == f"{stem}.spdx.json"
    assert provenance_filename(VERSION, platform_name, arch) == f"{stem}.provenance.json"


def test_release_asset_inventory_contains_all_target_metadata() -> None:
    names = expected_runtime_asset_names(VERSION)

    assert len(names) == 12
    assert len(set(names)) == len(names)
    for platform_name, arch, suffix in (
        ("darwin", "arm64", ".tar.gz"),
        ("linux", "x64", ".tar.gz"),
        ("win32", "x64", ".zip"),
    ):
        stem = f"gateway-runtime-{VERSION}-{platform_name}-{arch}"
        assert f"{stem}{suffix}" in names
        assert f"{stem}.manifest.json" in names
        assert f"{stem}.spdx.json" in names
        assert f"{stem}.provenance.json" in names


@pytest.mark.parametrize(
    ("platform_name", "arch"),
    [
        ("darwin", "x64"),
        ("linux", "arm64"),
        ("win32", "arm64"),
    ],
)
def test_runtime_artifact_rejects_unpublished_platform_matrix(
    platform_name: str,
    arch: str,
) -> None:
    with pytest.raises(RuntimeArtifactError, match="unsupported Runtime target"):
        artifact_stem(VERSION, platform_name, arch)


def test_runtime_manifest_matches_public_schema_and_verified_files(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    manifest = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))
    schema = json.loads(
        Path("contracts/gateway-runtime-manifest.schema.json").read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest)
    verified = verify_runtime_artifact(
        artifacts["manifest"],
        expected_platform="linux",
        expected_arch="x64",
        extract_to=tmp_path / "extracted",
    )

    assert verified["contractDigest"] == CLIENT_CONTRACT_DIGEST
    assert verified["protocol"] == {"min": 1, "max": 3}
    assert verified["buildCommit"] == BUILD_COMMIT
    assert verified["capabilities"] == [
        "gateway.rpc",
        "gateway.sessions",
        "gateway.artifacts",
    ]
    assert (tmp_path / "extracted/opensquilla-gateway").read_bytes() == b"synthetic-runtime"
    assert "/Users/" not in artifacts["manifest"].read_text(encoding="utf-8")
    assert "C:\\Users\\" not in artifacts["manifest"].read_text(encoding="utf-8")


def test_runtime_archive_is_reproducible_for_equal_inputs(tmp_path: Path) -> None:
    first = _artifacts(tmp_path / "first")
    second = _artifacts(tmp_path / "second")

    for key in ("archive", "manifest", "provenance", "sbom"):
        assert first[key].read_bytes() == second[key].read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="Windows CI cannot create unprivileged symlinks")
def test_runtime_packaging_dereferences_only_internal_bundle_links(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "internal", platform_name="linux")
    target = bundle / "_internal/libpython.so"
    target.write_bytes(b"synthetic-python-runtime")
    (bundle / "_internal/Python").symlink_to("libpython.so")

    artifacts = package_runtime_artifact(
        bundle_dir=bundle,
        artifacts_dir=tmp_path / "internal/artifacts",
        version=VERSION,
        platform_name="linux",
        arch="x64",
        build_commit=BUILD_COMMIT,
        created_by="unit-test",
        epoch=1_700_000_000,
    )
    verify_runtime_artifact(artifacts["manifest"])
    with tarfile.open(artifacts["archive"], "r:gz") as archive:
        link = archive.getmember("_internal/Python")
        assert link.isfile()
        assert archive.extractfile(link).read() == b"synthetic-python-runtime"

    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    escaped = _bundle(tmp_path / "escaped", platform_name="linux")
    (escaped / "_internal/Python").symlink_to(outside)
    with pytest.raises(RuntimeArtifactError, match="link escapes or is broken"):
        package_runtime_artifact(
            bundle_dir=escaped,
            artifacts_dir=tmp_path / "escaped/artifacts",
            version=VERSION,
            platform_name="linux",
            arch="x64",
            build_commit=BUILD_COMMIT,
            created_by="unit-test",
            epoch=1_700_000_000,
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("platform", "win32", "platform mismatch"),
        ("arch", "arm64", "unsupported Runtime target"),
        ("buildCommit", "not-a-commit", "build commit"),
        ("contractDigest", f"sha256:{'0' * 64}", "contract digest"),
        ("entrypoint", "gateway", "entrypoint"),
    ],
)
def test_runtime_manifest_rejects_identity_mismatches(
    tmp_path: Path,
    field: str,
    replacement: object,
    message: str,
) -> None:
    artifacts = _artifacts(tmp_path)
    payload = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))
    payload[field] = replacement

    with pytest.raises(RuntimeArtifactError, match=message):
        validate_manifest(
            payload,
            expected_platform="linux",
            expected_arch="x64",
        )


def test_runtime_verifier_rejects_archive_metadata_and_subject_tampering(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    artifacts["archive"].write_bytes(artifacts["archive"].read_bytes() + b"tampered")

    with pytest.raises(RuntimeArtifactError, match="archive SHA-256 mismatch"):
        verify_runtime_artifact(artifacts["manifest"])


def test_runtime_verifier_rejects_sbom_and_provenance_tampering(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    original_manifest = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))

    artifacts["sbom"].write_text('{"spdxVersion": "SPDX-2.2"}\n', encoding="utf-8")
    manifest = copy.deepcopy(original_manifest)
    manifest["sbom"]["sha256"] = sha256_file(artifacts["sbom"])
    artifacts["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeArtifactError, match="SPDX 2.3"):
        verify_runtime_artifact(artifacts["manifest"])

    artifacts = _artifacts(tmp_path / "provenance")
    provenance = json.loads(artifacts["provenance"].read_text(encoding="utf-8"))
    provenance["subject"][0]["name"] = "other.tar.gz"
    artifacts["provenance"].write_text(json.dumps(provenance), encoding="utf-8")
    manifest = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))
    manifest["provenance"]["sha256"] = sha256_file(artifacts["provenance"])
    artifacts["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeArtifactError, match="provenance subject"):
        verify_runtime_artifact(artifacts["manifest"])


def test_runtime_verifier_rejects_ui_private_state_lfs_and_build_paths(
    tmp_path: Path,
) -> None:
    cases = (
        ("opensquilla/gateway/static/dist/index.html", b"<html>client</html>", ()),
        ("state/sessions.db", b"private", ()),
        (
            "opensquilla/router/model.onnx",
            b"version https://git-lfs.github.com/spec/v1\n",
            (),
        ),
        (
            "_internal/build.txt",
            b"/private/build/opensquilla/source.py",
            ("/private/build/opensquilla",),
        ),
    )
    for index, (relative, content, forbidden) in enumerate(cases):
        root = tmp_path / str(index)
        bundle = _bundle(root, platform_name="linux")
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        artifacts = package_runtime_artifact(
            bundle_dir=bundle,
            artifacts_dir=root / "artifacts",
            version=VERSION,
            platform_name="linux",
            arch="x64",
            build_commit=BUILD_COMMIT,
            created_by="unit-test",
            epoch=1_700_000_000,
        )

        with pytest.raises(RuntimeArtifactError):
            verify_runtime_artifact(
                artifacts["manifest"],
                forbidden_paths=forbidden,
            )


def test_runtime_verifier_rejects_archive_traversal_and_links(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        handle.addfile(traversal, io.BytesIO(b"x"))

    from scripts.gateway_runtime.verify import verify_archive

    with pytest.raises(RuntimeArtifactError, match="unsafe Runtime archive path"):
        verify_archive(archive, entrypoint="opensquilla-gateway")

    zip_archive = tmp_path / "link.zip"
    with zipfile.ZipFile(zip_archive, "w") as handle:
        link = zipfile.ZipInfo("opensquilla-gateway")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        handle.writestr(link, "target")
    with pytest.raises(RuntimeArtifactError, match="links are forbidden"):
        verify_archive(zip_archive, entrypoint="opensquilla-gateway")


def test_sha256s_is_sorted_and_rejects_duplicate_basenames(tmp_path: Path) -> None:
    first = tmp_path / "b.txt"
    second = tmp_path / "a.txt"
    first.write_text("b", encoding="utf-8")
    second.write_text("a", encoding="utf-8")
    output = write_sha256s([first, second], tmp_path / "SHA256SUMS")

    assert [line.split(None, 1)[1] for line in output.read_text().splitlines()] == [
        "a.txt",
        "b.txt",
    ]
    duplicate = tmp_path / "nested/b.txt"
    duplicate.parent.mkdir()
    duplicate.write_text("duplicate", encoding="utf-8")
    with pytest.raises(RuntimeArtifactError, match="unique basenames"):
        write_sha256s([first, duplicate], output)
