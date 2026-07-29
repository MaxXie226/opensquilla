"""Build the public, cross-platform OpenSquilla Gateway Runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from opensquilla import __version__
from opensquilla.gateway.control_ui_assets import (
    ControlUiAssetError,
    validate_control_ui_artifact,
)
from scripts.gateway_runtime.manifest import (  # noqa: E402
    RuntimeArtifactError,
    normalize_arch,
    normalize_build_commit,
    normalize_platform,
    package_runtime_artifact,
    source_date_epoch,
    validate_target,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY_PATH = REPO_ROOT / "scripts/gateway_runtime/entry.py"
CA_RUNTIME_HOOK_PATH = REPO_ROOT / "scripts/gateway_runtime/ensure_ca_trust.py"
MIGRATIONS_DIR = REPO_ROOT / "migrations"
ROUTER_BUNDLE_DIR = (
    REPO_ROOT
    / "src/opensquilla/squilla_router/models/v4.2_phase3_inference"
)
GIT_LFS_POINTER_HEADER = b"version https://git-lfs.github.com/spec/v1"

_FORBIDDEN_RUNTIME_PATH_PARTS = {
    ".git",
    ".opensquilla",
    "node_modules",
}
_FORBIDDEN_RUNTIME_BASENAMES = {
    ".env",
    "credentials.json",
    "sessions.db",
}
_FORBIDDEN_RUNTIME_ROOTS = {"logs", "profile", "state", "workspace"}
_SOURCE_METADATA_FILES = {"direct_url.json", "uv_cache.json"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def resolve_build_commit(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("GITHUB_SHA") or _git_output("rev-parse", "HEAD")
    return normalize_build_commit(candidate)


def resolve_source_date_epoch(explicit: int | None) -> int:
    if explicit is not None or os.environ.get("SOURCE_DATE_EPOCH", "").strip():
        return source_date_epoch(explicit)
    return source_date_epoch(int(_git_output("show", "-s", "--format=%ct", "HEAD")))


def _python_package_file(package_name: str, relative_path: Path) -> Path:
    spec = importlib.util.find_spec(package_name)
    if spec is None or spec.origin is None:
        raise RuntimeArtifactError(f"could not locate Python package {package_name}")
    path = Path(spec.origin).parent / relative_path
    if not path.exists():
        raise RuntimeArtifactError(
            f"could not locate {package_name}/{relative_path.as_posix()}"
        )
    return path


def _platform_lightgbm_library(platform_name: str) -> tuple[Path, str]:
    if platform_name == "win32":
        relative = Path("bin/lib_lightgbm.dll")
        destination = "lightgbm/bin"
    elif platform_name == "darwin":
        relative = Path("lib/lib_lightgbm.dylib")
        destination = "lightgbm/lib"
    else:
        relative = Path("lib/lib_lightgbm.so")
        destination = "lightgbm/lib"
    return _python_package_file("lightgbm", relative), destination


def verify_router_assets() -> None:
    manifest_path = ROUTER_BUNDLE_DIR / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeArtifactError(f"Router artifact manifest not found at {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeArtifactError("Router artifact manifest is unreadable") from error
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeArtifactError("Router artifact manifest has no files")

    problems: list[str] = []
    for raw_entry in files:
        if not isinstance(raw_entry, dict):
            problems.append("<entry>: invalid manifest object")
            continue
        relative = raw_entry.get("path")
        if not isinstance(relative, str) or not relative:
            problems.append("<entry>: missing path")
            continue
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            problems.append(f"{relative}: unsafe path")
            continue
        path = ROUTER_BUNDLE_DIR / relative_path
        if not path.is_file():
            problems.append(f"{relative}: missing")
            continue
        payload = path.read_bytes()
        expected_size = raw_entry.get("size_bytes")
        if not isinstance(expected_size, int) or len(payload) != expected_size:
            problems.append(
                f"{relative}: size {len(payload)} != manifest {expected_size!r}"
            )
            continue
        if payload.startswith(GIT_LFS_POINTER_HEADER):
            problems.append(f"{relative}: Git LFS pointer file, not the real Router artifact")
            continue
        expected_sha256 = raw_entry.get("sha256")
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            not isinstance(expected_sha256, str)
            or not _SHA256_RE.fullmatch(expected_sha256)
            or actual_sha256 != expected_sha256
        ):
            problems.append(f"{relative}: SHA-256 does not match the Router manifest")

    if problems:
        detail = "\n".join(f"- {problem}" for problem in problems)
        raise RuntimeArtifactError(
            "Router assets are incomplete or unverified. Run "
            '`git lfs pull --include="src/opensquilla/squilla_router/models/'
            'v4.2_phase3_inference/**"` and rebuild.\n'
            f"{detail}"
        )


def verify_ui_artifact(ui_artifact: Path) -> Path:
    try:
        manifest = validate_control_ui_artifact(
            ui_artifact,
            manifest_required=True,
        )
    except (ControlUiAssetError, OSError) as error:
        raise RuntimeArtifactError(f"Control UI artifact verification failed: {error}") from error
    if manifest is None:
        raise RuntimeArtifactError("Control UI artifact manifest is missing")
    return ui_artifact.resolve(strict=True)


def verify_sqlite_vec_support() -> None:
    """Fail before packaging when the build interpreter cannot load sqlite-vec."""

    try:
        import sqlite_vec
    except ImportError as error:
        raise RuntimeArtifactError(
            "Gateway Runtime build environment does not contain sqlite-vec"
        ) from error

    try:
        with sqlite3.connect(":memory:") as connection:
            enable_load_extension = getattr(connection, "enable_load_extension", None)
            load_extension = getattr(connection, "load_extension", None)
            if not callable(enable_load_extension) or not callable(load_extension):
                raise RuntimeArtifactError(
                    "Gateway Runtime build interpreter does not support loadable SQLite "
                    "extensions required by sqlite-vec; use uv-managed Python 3.12"
                )
            enable_load_extension(True)
            try:
                load_extension(sqlite_vec.loadable_path())
            finally:
                enable_load_extension(False)
            version = connection.execute("SELECT vec_version()").fetchone()
            if not version or not isinstance(version[0], str):
                raise RuntimeArtifactError(
                    "Gateway Runtime build interpreter could not verify sqlite-vec"
                )
    except RuntimeArtifactError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise RuntimeArtifactError(
            "Gateway Runtime build interpreter could not load sqlite-vec; "
            "use uv-managed Python 3.12"
        ) from error


def _write_identity_runtime_hook(
    path: Path,
    *,
    build_commit: str,
    ui_mode: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                '"""Generated immutable Runtime identity; do not commit."""',
                "from opensquilla import _build_info",
                f"_build_info.BUILD_COMMIT = {build_commit!r}",
                f"_build_info.BUILD_UI_MODE = {ui_mode!r}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _add_data_arg(source: Path, destination: str) -> list[str]:
    separator = ";" if os.name == "nt" else ":"
    return ["--add-data", f"{source}{separator}{destination}"]


def _add_binary_arg(source: Path, destination: str) -> list[str]:
    separator = ";" if os.name == "nt" else ":"
    return ["--add-binary", f"{source}{separator}{destination}"]


def pyinstaller_args(
    *,
    bundle_root: Path,
    work_root: Path,
    platform_name: str,
    build_commit: str,
    ui_artifact: Path | None,
) -> list[str]:
    identity_hook = _write_identity_runtime_hook(
        work_root / "runtime_identity.py",
        build_commit=build_commit,
        ui_mode="embed-ui" if ui_artifact is not None else "headless",
    )
    lightgbm_library, lightgbm_destination = _platform_lightgbm_library(platform_name)
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "opensquilla-gateway",
        "--distpath",
        os.fspath(bundle_root),
        "--workpath",
        os.fspath(work_root / "work"),
        "--specpath",
        os.fspath(work_root / "spec"),
        "--collect-all",
        "opensquilla",
        "--collect-all",
        "sqlite_vec",
        "--collect-data",
        "certifi",
        "--hidden-import",
        "certifi",
        "--collect-binaries",
        "sklearn",
        "--copy-metadata",
        "opensquilla",
        "--copy-metadata",
        "scikit-learn",
        "--copy-metadata",
        "lightgbm",
        "--copy-metadata",
        "yoyo-migrations",
        "--hidden-import",
        "joblib",
        "--hidden-import",
        "sklearn",
        "--hidden-import",
        "sklearn.feature_extraction.text",
        "--hidden-import",
        "sklearn.decomposition._truncated_svd",
        "--hidden-import",
        "sklearn.decomposition._pca",
        "--hidden-import",
        "sklearn.preprocessing._data",
        "--hidden-import",
        "lightgbm",
        "--hidden-import",
        "tokenizers",
        "--hidden-import",
        "tiktoken",
        "--hidden-import",
        "onnxruntime",
        "--hidden-import",
        "mcp",
        "--hidden-import",
        "yoyo.backends.core.sqlite3",
        "--runtime-hook",
        os.fspath(CA_RUNTIME_HOOK_PATH),
        "--runtime-hook",
        os.fspath(identity_hook),
        *_add_data_arg(MIGRATIONS_DIR, "opensquilla/_migrations"),
        *_add_binary_arg(lightgbm_library, lightgbm_destination),
    ]
    if platform_name == "darwin":
        args.extend(
            _add_binary_arg(
                _python_package_file("sklearn", Path(".dylibs/libomp.dylib")),
                ".",
            )
        )
    if ui_artifact is not None:
        args.extend(_add_data_arg(ui_artifact, "opensquilla/gateway/static/dist"))
    args.append(os.fspath(ENTRY_PATH))
    return args


def _find_files(root: Path, name: str) -> list[Path]:
    return sorted(path for path in root.rglob(name) if path.is_file())


def _sign_adhoc(path: Path) -> None:
    _run(["codesign", "--force", "--sign", "-", os.fspath(path)])


def _patch_macos_lightgbm(bundle_dir: Path) -> None:
    lightgbm_libraries = _find_files(bundle_dir, "lib_lightgbm.dylib")
    if not lightgbm_libraries:
        raise RuntimeArtifactError(
            "LightGBM was requested but lib_lightgbm.dylib was not bundled"
        )
    libomp_candidates = _find_files(bundle_dir, "libomp.dylib")
    if not libomp_candidates:
        for candidate in (
            Path("/opt/homebrew/opt/libomp/lib/libomp.dylib"),
            Path("/usr/local/opt/libomp/lib/libomp.dylib"),
            Path("/opt/local/lib/libomp/libomp.dylib"),
        ):
            if candidate.is_file():
                libomp_candidates.append(candidate)
                break
    if not libomp_candidates:
        raise RuntimeArtifactError("macOS Runtime requires a bundled libomp.dylib")

    source_libomp = libomp_candidates[0]
    for lightgbm_library in lightgbm_libraries:
        bundled_libomp = lightgbm_library.parent / "libomp.dylib"
        if source_libomp.resolve() != bundled_libomp.resolve():
            shutil.copy2(source_libomp, bundled_libomp)
        _sign_adhoc(bundled_libomp)
        _run(
            [
                "install_name_tool",
                "-change",
                "@rpath/libomp.dylib",
                "@loader_path/libomp.dylib",
                os.fspath(lightgbm_library),
            ]
        )
        _sign_adhoc(lightgbm_library)


def _forbidden_runtime_paths(bundle_dir: Path) -> list[str]:
    problems: list[str] = []
    for path in bundle_dir.rglob("*"):
        relative = path.relative_to(bundle_dir)
        lowered_parts = [part.lower() for part in relative.parts]
        if any(part in _FORBIDDEN_RUNTIME_PATH_PARTS for part in lowered_parts):
            problems.append(relative.as_posix())
        if lowered_parts and lowered_parts[0] in _FORBIDDEN_RUNTIME_ROOTS:
            problems.append(relative.as_posix())
        if path.is_file() and path.name.lower() in _FORBIDDEN_RUNTIME_BASENAMES:
            problems.append(relative.as_posix())
        if path.is_file() and len(lowered_parts) == 1 and path.name.lower() == "config.toml":
            problems.append(relative.as_posix())
        if "static" in lowered_parts and "dist" in lowered_parts:
            problems.append(relative.as_posix())
    return sorted(set(problems))


def scrub_source_build_metadata(bundle_dir: Path) -> None:
    """Remove installer metadata that records a checkout path or local cache time."""

    for path in bundle_dir.rglob("*"):
        if path.is_file() and path.name in _SOURCE_METADATA_FILES:
            path.unlink()


def verify_built_bundle(
    bundle_dir: Path,
    *,
    platform_name: str,
    ui_artifact: Path | None,
) -> None:
    entrypoint = bundle_dir / (
        "opensquilla-gateway.exe" if platform_name == "win32" else "opensquilla-gateway"
    )
    if not entrypoint.is_file():
        raise RuntimeArtifactError(f"Runtime entrypoint is missing: {entrypoint}")
    if entrypoint.read_bytes()[:80].startswith(GIT_LFS_POINTER_HEADER):
        raise RuntimeArtifactError("Runtime entrypoint is a Git LFS pointer")

    problems = _forbidden_runtime_paths(bundle_dir)
    if ui_artifact is not None:
        problems = [
            problem
            for problem in problems
            if "/static/dist/" not in f"/{problem}" and not problem.endswith("/static/dist")
        ]
    if problems:
        raise RuntimeArtifactError(
            "Runtime bundle contains forbidden client or private-state paths:\n"
            + "\n".join(f"- {problem}" for problem in problems[:50])
        )

    for path in bundle_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".bin",
            ".joblib",
            ".onnx",
            ".pkl",
        }:
            continue
        if path.read_bytes()[:80].startswith(GIT_LFS_POINTER_HEADER):
            raise RuntimeArtifactError(
                f"Runtime bundle contains a Git LFS pointer: {path.relative_to(bundle_dir)}"
            )


def build_runtime(
    *,
    bundle_root: Path,
    work_root: Path,
    artifacts_dir: Path | None,
    expected_platform: str | None,
    expected_arch: str | None,
    build_commit: str,
    created_by: str,
    epoch: int,
    ui_artifact: Path | None,
) -> dict[str, Path]:
    platform_name = normalize_platform()
    arch = normalize_arch()
    validate_target(platform_name, arch)
    if expected_platform is not None and platform_name != normalize_platform(expected_platform):
        raise RuntimeArtifactError(
            f"build host platform mismatch: expected {expected_platform}, got {platform_name}"
        )
    if expected_arch is not None and arch != normalize_arch(expected_arch):
        raise RuntimeArtifactError(
            f"build host architecture mismatch: expected {expected_arch}, got {arch}"
        )

    verify_sqlite_vec_support()
    verify_router_assets()
    verified_ui = verify_ui_artifact(ui_artifact) if ui_artifact is not None else None

    shutil.rmtree(bundle_root, ignore_errors=True)
    shutil.rmtree(work_root, ignore_errors=True)
    bundle_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    args = pyinstaller_args(
        bundle_root=bundle_root,
        work_root=work_root,
        platform_name=platform_name,
        build_commit=build_commit,
        ui_artifact=verified_ui,
    )
    _run(args)

    bundle_dir = bundle_root / "opensquilla-gateway"
    if platform_name == "darwin":
        _patch_macos_lightgbm(bundle_dir)
    scrub_source_build_metadata(bundle_dir)
    verify_built_bundle(
        bundle_dir,
        platform_name=platform_name,
        ui_artifact=verified_ui,
    )

    result = {"bundle": bundle_dir}
    if artifacts_dir is not None:
        result.update(
            package_runtime_artifact(
                bundle_dir=bundle_dir,
                artifacts_dir=artifacts_dir,
                version=__version__,
                platform_name=platform_name,
                arch=arch,
                build_commit=build_commit,
                created_by=created_by,
                epoch=epoch,
            )
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--expected-platform", choices=("darwin", "linux", "win32"))
    parser.add_argument("--expected-arch", choices=("arm64", "x64"))
    parser.add_argument("--build-commit")
    parser.add_argument(
        "--created-by",
        default="https://github.com/opensquilla/opensquilla/actions",
    )
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument(
        "--ui-artifact",
        type=Path,
        help=(
            "Optional manifest-verified UI artifact for an explicit product bundle. "
            "Public Runtime release artifacts must omit this option."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_runtime(
            bundle_root=args.bundle_root.resolve(),
            work_root=args.work_root.resolve(),
            artifacts_dir=args.artifacts_dir.resolve() if args.artifacts_dir else None,
            expected_platform=args.expected_platform,
            expected_arch=args.expected_arch,
            build_commit=resolve_build_commit(args.build_commit),
            created_by=args.created_by,
            epoch=resolve_source_date_epoch(args.source_date_epoch),
            ui_artifact=args.ui_artifact.resolve() if args.ui_artifact else None,
        )
    except (RuntimeArtifactError, subprocess.CalledProcessError) as error:
        print(f"Gateway Runtime build failed: {error}", file=sys.stderr)
        return 1
    for label, path in sorted(result.items()):
        print(f"{label}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
