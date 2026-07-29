from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.gateway_runtime import build
from scripts.gateway_runtime.entry import main as runtime_main
from scripts.gateway_runtime.manifest import RuntimeArtifactError


def _router_bundle(tmp_path: Path, *, lfs_pointer: bool = False) -> Path:
    root = tmp_path / "router"
    root.mkdir(parents=True)
    payload = (
        b"version https://git-lfs.github.com/spec/v1\n"
        if lfs_pointer
        else b"synthetic-router"
    )
    (root / "model.onnx").write_bytes(payload)
    import hashlib

    (root / "artifact_manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "model.onnx",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def test_router_verification_checks_size_digest_and_lfs_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = _router_bundle(tmp_path / "ready")
    monkeypatch.setattr(build, "ROUTER_BUNDLE_DIR", ready)
    build.verify_router_assets()

    (ready / "model.onnx").write_bytes(b"changed")
    with pytest.raises(RuntimeArtifactError, match="size"):
        build.verify_router_assets()

    pointer = _router_bundle(tmp_path / "pointer", lfs_pointer=True)
    monkeypatch.setattr(build, "ROUTER_BUNDLE_DIR", pointer)
    with pytest.raises(RuntimeArtifactError, match="Git LFS pointer"):
        build.verify_router_assets()


def test_pyinstaller_contract_is_public_headless_and_has_explicit_ui_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_file = tmp_path / "package/lib.bin"
    package_file.parent.mkdir()
    package_file.write_bytes(b"native")
    monkeypatch.setattr(build, "_python_package_file", lambda *_args: package_file)

    headless = build.pyinstaller_args(
        bundle_root=tmp_path / "bundle",
        work_root=tmp_path / "work",
        platform_name="linux",
        build_commit="a" * 40,
        ui_artifact=None,
    )
    headless_text = "\n".join(map(os.fspath, headless))
    assert "scripts/gateway_runtime/entry.py" in headless_text
    assert "scripts/gateway_runtime/ensure_ca_trust.py" in headless_text
    assert "static/dist" not in headless_text
    assert "--collect-all\nopensquilla" in headless_text
    assert "--collect-all\nsqlite_vec" in headless_text
    assert "--collect-binaries\nsklearn" in headless_text
    for module in (
        "joblib",
        "lightgbm",
        "mcp",
        "onnxruntime",
        "sklearn",
        "tiktoken",
        "tokenizers",
    ):
        assert f"--hidden-import\n{module}" in headless_text
    identity = (tmp_path / "work/runtime_identity.py").read_text(encoding="utf-8")
    assert "_build_info.BUILD_COMMIT = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'" in identity
    assert "_build_info.BUILD_UI_MODE = 'headless'" in identity

    ui_artifact = tmp_path / "ui-dist"
    ui_artifact.mkdir()
    embedded = build.pyinstaller_args(
        bundle_root=tmp_path / "bundle-ui",
        work_root=tmp_path / "work-ui",
        platform_name="linux",
        build_commit="b" * 40,
        ui_artifact=ui_artifact,
    )
    assert "opensquilla/gateway/static/dist" in "\n".join(map(os.fspath, embedded))
    assert "_build_info.BUILD_UI_MODE = 'embed-ui'" in (
        tmp_path / "work-ui/runtime_identity.py"
    ).read_text(encoding="utf-8")


def test_public_bundle_verifier_rejects_client_and_private_state(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    entrypoint = bundle / ("opensquilla-gateway.exe" if os.name == "nt" else "opensquilla-gateway")
    entrypoint.write_bytes(b"runtime")
    build.verify_built_bundle(
        bundle,
        platform_name="win32" if os.name == "nt" else "linux",
        ui_artifact=None,
    )

    ui = bundle / "opensquilla/gateway/static/dist/index.html"
    ui.parent.mkdir(parents=True)
    ui.write_text("client", encoding="utf-8")
    with pytest.raises(RuntimeArtifactError, match="forbidden client"):
        build.verify_built_bundle(
            bundle,
            platform_name="win32" if os.name == "nt" else "linux",
            ui_artifact=None,
        )


def test_source_checkout_metadata_is_removed_from_runtime_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    dist_info = bundle / "_internal/opensquilla-9.9.9.dist-info"
    dist_info.mkdir(parents=True)
    direct_url = dist_info / "direct_url.json"
    uv_cache = dist_info / "uv_cache.json"
    metadata = dist_info / "METADATA"
    direct_url.write_text('{"url": "file:///private/source"}', encoding="utf-8")
    uv_cache.write_text('{"timestamp": 1}', encoding="utf-8")
    metadata.write_text("Name: opensquilla\nVersion: 9.9.9\n", encoding="utf-8")

    build.scrub_source_build_metadata(bundle)

    assert not direct_url.exists()
    assert not uv_cache.exists()
    assert metadata.is_file()


def test_build_rejects_host_target_mismatch_before_running_pyinstaller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build,
        "normalize_platform",
        lambda value=None: "linux" if value is None else value,
    )
    monkeypatch.setattr(
        build,
        "normalize_arch",
        lambda value=None: "x64" if value is None else value,
    )

    with pytest.raises(RuntimeArtifactError, match="platform mismatch"):
        build.build_runtime(
            bundle_root=tmp_path / "bundle",
            work_root=tmp_path / "work",
            artifacts_dir=None,
            expected_platform="darwin",
            expected_arch="arm64",
            build_commit="a" * 40,
            created_by="unit-test",
            epoch=0,
            ui_artifact=None,
        )


def test_builder_rejects_python_without_loadable_sqlite_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConnectionWithoutExtensions:
        def __enter__(self) -> _ConnectionWithoutExtensions:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        build.sqlite3,
        "connect",
        lambda *_args, **_kwargs: _ConnectionWithoutExtensions(),
    )

    with pytest.raises(RuntimeArtifactError, match="loadable SQLite extensions"):
        build.verify_sqlite_vec_support()


def test_runtime_entrypoint_supports_stable_version_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from opensquilla import __version__

    assert runtime_main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_runtime_entrypoint_probes_database_and_registry_capabilities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runtime_main(["--_runtime-capability-probe"]) == 0
    output = capsys.readouterr().out
    assert "opensquilla-runtime-capabilities-ok" in output
    assert "migrations=" in output
    assert "sqlite_vec=" in output


def test_desktop_gateway_builder_is_only_a_public_builder_adapter() -> None:
    script = Path("desktop/electron/scripts/build-gateway.mjs").read_text(encoding="utf-8")

    assert "scripts.gateway_runtime.build" in script
    assert "'--ui-artifact'" in script
    assert "'--no-dev'" in script
    assert "'recommended'" in script
    assert "'mcp'" in script
    assert "PyInstaller" not in script
    assert "lib_lightgbm" not in script
    assert "install_name_tool" not in script
    assert "git lfs pull" not in script


def test_legacy_desktop_entrypoint_delegates_filesystem_worker(tmp_path: Path) -> None:
    target = tmp_path / "worker.txt"
    target.write_text("public runtime worker\n", encoding="utf-8")
    payload = json.dumps(
        {
            "kind": "read_file",
            "path": os.fspath(target),
            "displayPath": "worker.txt",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "desktop/electron/scripts/gateway-entry.py",
            "--_sandbox-filesystem-worker",
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "public runtime worker" in result.stdout
