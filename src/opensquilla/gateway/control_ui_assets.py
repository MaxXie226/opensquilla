"""Resolve and validate Control UI assets without coupling Gateway boot to a bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from opensquilla.gateway.config import GatewayConfig
from opensquilla.paths import default_opensquilla_home, native_io_path

CONTROL_UI_MANIFEST_NAME = "webui-artifact-manifest.json"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_FILES = 50_000
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_ARTIFACT_FILE_BYTES = 128 * 1024 * 1024
_MAX_INDEX_BYTES = 5 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FORBIDDEN_FILE_NAMES = frozenset({".ds_store", ".env", ".npmrc"})
_FORBIDDEN_FILE_SUFFIXES = frozenset({".key", ".pem"})

ControlUiAssetsMode = Literal["embedded", "external", "none"]
ControlUiAssetsRequestMode = Literal["auto", "embedded", "external", "none"]


class ControlUiAssetError(ValueError):
    """A configured UI bundle is missing, unsafe, or internally inconsistent."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ControlUiArtifactFile:
    """One immutable file declared by the UI artifact manifest."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ControlUiArtifactManifest:
    """Validated metadata and serving allowlist for a Control UI bundle."""

    schema_version: int
    source_fingerprint: str
    files: tuple[ControlUiArtifactFile, ...]
    client_version: str | None = None
    contract_digest: str | None = None
    entry_scripts: tuple[str, ...] = ()
    entry_styles: tuple[str, ...] = ()

    @property
    def allowed_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.files)


@dataclass(frozen=True, slots=True)
class ControlUiAssets:
    """Read-only description consumed by the Control UI route factory."""

    mode: ControlUiAssetsMode
    static_root: Path | None
    dist_root: Path | None
    template_root: Path
    manifest: ControlUiArtifactManifest | None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.dist_root is not None


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or _is_junction(path)
    except OSError:
        return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_manifest_path(raw: object) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        raise ControlUiAssetError(
            "manifest_path_invalid",
            "The Control UI manifest contains an invalid file path.",
        )
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != raw
    ):
        raise ControlUiAssetError(
            "manifest_path_escape",
            "The Control UI manifest contains a path outside the asset root.",
        )
    return raw


def _forbidden_artifact_path(relative: str) -> bool:
    name = PurePosixPath(relative).name.lower()
    return (
        name in _FORBIDDEN_FILE_NAMES
        or name.startswith(".env.")
        or PurePosixPath(name).suffix in _FORBIDDEN_FILE_SUFFIXES
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(native_io_path(path), "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(
        native_io_path(root).rglob("*"),
        key=lambda value: value.as_posix().encode("utf-8", errors="surrogateescape"),
    ):
        if _is_link(path):
            raise ControlUiAssetError(
                "artifact_link_forbidden",
                "The Control UI asset directory contains a symbolic link or junction.",
            )
        if os.name != "nt" and path.stat().st_mode & stat.S_IWOTH:
            raise ControlUiAssetError(
                "artifact_world_writable",
                "The Control UI asset tree is writable by other users.",
            )
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(native_io_path(root)).as_posix()
        except ValueError as error:
            raise ControlUiAssetError(
                "artifact_path_escape",
                "A Control UI asset resolves outside the configured directory.",
            ) from error
        relative = _safe_manifest_path(relative)
        if _forbidden_artifact_path(relative):
            raise ControlUiAssetError(
                "artifact_sensitive_file",
                "The Control UI asset directory contains forbidden metadata or key material.",
            )
        files[relative] = path
        if len(files) > _MAX_ARTIFACT_FILES:
            raise ControlUiAssetError(
                "artifact_file_limit",
                "The Control UI asset directory contains too many files.",
            )
    return files


def _entry_references(index: bytes) -> tuple[str, ...]:
    try:
        html = index.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ControlUiAssetError(
            "index_encoding_invalid",
            "The Control UI index is not valid UTF-8.",
        ) from error

    references: list[str] = []
    for raw in re.findall(r'\b(?:src|href)="([^"]+)"', html):
        if raw.startswith(("data:", "#")):
            continue
        if raw.startswith(("http://", "https://", "//")):
            raise ControlUiAssetError(
                "index_remote_asset",
                "The Control UI index references a network asset.",
            )
        relative = raw.split("?", 1)[0].split("#", 1)[0]
        marker = "/static/dist/"
        if relative.startswith("/") and marker in relative:
            relative = relative.split(marker, 1)[1]
        else:
            relative = relative.removeprefix("./")
        if not relative:
            continue
        references.append(_safe_manifest_path(relative))
    return tuple(references)


def _parse_manifest(raw: object) -> ControlUiArtifactManifest:
    if (
        not isinstance(raw, dict)
        or raw.get("schemaVersion") != 1
        or not isinstance(raw.get("sourceFingerprint"), str)
        or not isinstance(raw.get("files"), list)
    ):
        raise ControlUiAssetError(
            "manifest_schema_unsupported",
            "The Control UI manifest uses an unsupported schema.",
        )

    entries = raw["files"]
    if len(entries) > _MAX_ARTIFACT_FILES:
        raise ControlUiAssetError(
            "manifest_file_limit",
            "The Control UI manifest declares too many files.",
        )

    files: list[ControlUiArtifactFile] = []
    seen: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ControlUiAssetError(
                "manifest_record_invalid",
                "The Control UI manifest contains an invalid file record.",
            )
        path = _safe_manifest_path(entry.get("path"))
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            path in seen
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > _MAX_ARTIFACT_FILE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ControlUiAssetError(
                "manifest_record_invalid",
                "The Control UI manifest contains an invalid file record.",
            )
        if _forbidden_artifact_path(path):
            raise ControlUiAssetError(
                "manifest_sensitive_file",
                "The Control UI manifest declares forbidden metadata or key material.",
            )
        seen.add(path)
        total_size += size
        if total_size > _MAX_ARTIFACT_BYTES:
            raise ControlUiAssetError(
                "manifest_size_limit",
                "The Control UI manifest declares an oversized asset bundle.",
            )
        files.append(
            ControlUiArtifactFile(
                path=path,
                size=size,
                sha256=digest.lower(),
            )
        )

    client_version = raw.get("clientVersion")
    if client_version is not None and not isinstance(client_version, str):
        raise ControlUiAssetError(
            "manifest_client_version_invalid",
            "The Control UI manifest client version is invalid.",
        )
    contract_digest = raw.get("contractDigest")
    if contract_digest is not None and (
        not isinstance(contract_digest, str)
        or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", contract_digest) is None
    ):
        raise ControlUiAssetError(
            "manifest_contract_digest_invalid",
            "The Control UI manifest contract digest is invalid.",
        )

    return ControlUiArtifactManifest(
        schema_version=1,
        source_fingerprint=raw["sourceFingerprint"],
        files=tuple(files),
        client_version=client_version,
        contract_digest=contract_digest.lower() if contract_digest else None,
    )


def validate_control_ui_artifact(
    dist_root: Path,
    *,
    manifest_required: bool,
) -> ControlUiArtifactManifest | None:
    """Validate a bundle tree and return its manifest when one is present."""

    dist_io = native_io_path(dist_root)
    if _is_link(dist_io) or not dist_io.is_dir():
        raise ControlUiAssetError(
            "asset_directory_missing",
            "The configured Control UI asset directory is unavailable.",
        )

    manifest_path = dist_io / CONTROL_UI_MANIFEST_NAME
    if not manifest_path.is_file():
        if manifest_required:
            raise ControlUiAssetError(
                "manifest_missing",
                "The external Control UI manifest is missing.",
            )
        embedded_index_path = dist_io / "index.html"
        if (
            not embedded_index_path.is_file()
            or embedded_index_path.stat().st_size == 0
        ):
            raise ControlUiAssetError(
                "index_missing",
                "The Control UI entrypoint is missing.",
            )
        return None

    if _is_link(manifest_path) or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ControlUiAssetError(
            "manifest_invalid",
            "The Control UI manifest is not a safe regular file.",
        )
    try:
        manifest_raw = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlUiAssetError(
            "manifest_invalid",
            "The Control UI manifest is not valid JSON.",
        ) from error
    manifest = _parse_manifest(manifest_raw)

    actual = _directory_files(dist_root)
    actual.pop(CONTROL_UI_MANIFEST_NAME, None)
    declared = {entry.path: entry for entry in manifest.files}
    if set(actual) != set(declared):
        raise ControlUiAssetError(
            "manifest_inventory_mismatch",
            "The Control UI files do not match the manifest inventory.",
        )

    for relative, entry in declared.items():
        path = actual[relative]
        file_stat = path.stat()
        if file_stat.st_size != entry.size or _file_sha256(path) != entry.sha256:
            raise ControlUiAssetError(
                "manifest_digest_mismatch",
                "A Control UI file does not match its manifest digest.",
            )
        if os.name != "nt" and file_stat.st_mode & stat.S_IWOTH:
            raise ControlUiAssetError(
                "artifact_world_writable",
                "A Control UI file is writable by other users.",
            )

    validated_index_path = actual.get("index.html")
    if (
        validated_index_path is None
        or declared["index.html"].size == 0
        or declared["index.html"].size > _MAX_INDEX_BYTES
    ):
        raise ControlUiAssetError(
            "index_missing",
            "The Control UI entrypoint is missing.",
        )
    references = _entry_references(validated_index_path.read_bytes())
    entry_scripts = tuple(
        path for path in references if path.lower().endswith((".js", ".mjs"))
    )
    entry_styles = tuple(path for path in references if path.lower().endswith(".css"))
    if not entry_scripts:
        raise ControlUiAssetError(
            "index_script_missing",
            "The Control UI entrypoint has no JavaScript module.",
        )
    if not entry_styles:
        raise ControlUiAssetError(
            "index_stylesheet_missing",
            "The Control UI entrypoint has no stylesheet.",
        )
    missing = sorted(set(references) - set(declared))
    if missing:
        raise ControlUiAssetError(
            "index_reference_missing",
            "The Control UI entrypoint references files outside its manifest.",
        )
    return replace(
        manifest,
        entry_scripts=entry_scripts,
        entry_styles=entry_styles,
    )


class ControlUiAssetResolver:
    """Resolve embedded, external, or intentionally absent Control UI assets."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        embedded_static_root: Path,
        embedded_dist_root: Path,
        template_root: Path,
    ) -> None:
        self._config = config
        self._embedded_static_root = embedded_static_root
        self._embedded_dist_root = embedded_dist_root
        self._template_root = template_root

    def _static_root(self) -> Path | None:
        root = self._embedded_static_root
        return root if native_io_path(root).is_dir() else None

    def _none(self, reason: str) -> ControlUiAssets:
        return ControlUiAssets(
            mode="none",
            static_root=self._static_root(),
            dist_root=None,
            template_root=self._template_root,
            manifest=None,
            reason=reason,
        )

    def _embedded(self) -> ControlUiAssets:
        manifest = validate_control_ui_artifact(
            self._embedded_dist_root,
            manifest_required=False,
        )
        return ControlUiAssets(
            mode="embedded",
            static_root=self._static_root(),
            dist_root=self._embedded_dist_root,
            template_root=self._template_root,
            manifest=manifest,
        )

    def _external_path(self) -> Path:
        raw = self._config.control_ui.assets_path.strip()
        if not raw:
            raise ControlUiAssetError(
                "external_path_missing",
                "External Control UI mode requires an asset directory.",
            )
        if raw.startswith(("\\\\", "//")):
            raise ControlUiAssetError(
                "external_network_path",
                "External Control UI assets must come from a local directory.",
            )

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            config_path = str(self._config.config_path or "").strip()
            base = Path(config_path).expanduser().parent if config_path else Path.cwd()
            candidate = base / candidate
        candidate = Path(os.path.abspath(os.fspath(candidate)))
        if _is_link(native_io_path(candidate)):
            raise ControlUiAssetError(
                "external_root_link",
                "The external Control UI root cannot be a symbolic link or junction.",
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ControlUiAssetError(
                "external_path_missing",
                "The external Control UI asset directory is unavailable.",
            ) from error
        if not native_io_path(resolved).is_dir():
            raise ControlUiAssetError(
                "external_path_missing",
                "The external Control UI asset path is not a directory.",
            )

        if (native_io_path(resolved) / "index.html").is_file():
            dist_root = resolved
        elif (native_io_path(resolved) / "dist" / "index.html").is_file():
            dist_root = resolved / "dist"
        else:
            raise ControlUiAssetError(
                "external_index_missing",
                "The external Control UI entrypoint is missing.",
            )
        if _is_link(native_io_path(dist_root)):
            raise ControlUiAssetError(
                "external_dist_link",
                "The external Control UI bundle cannot be a symbolic link or junction.",
            )
        return dist_root.resolve(strict=True)

    def _reject_runtime_data_root(self, dist_root: Path) -> None:
        roots: list[Path] = [default_opensquilla_home()]
        for raw in (self._config.state_dir, self._config.workspace_dir):
            if isinstance(raw, str) and raw.strip():
                roots.append(Path(raw).expanduser())
        for root in roots:
            try:
                resolved_root = root.resolve(strict=False)
            except OSError:
                continue
            if dist_root == resolved_root or _is_within(dist_root, resolved_root):
                raise ControlUiAssetError(
                    "external_runtime_data_root",
                    "External Control UI assets cannot be served from runtime data directories.",
                )

    def _external(self) -> ControlUiAssets:
        dist_root = self._external_path()
        self._reject_runtime_data_root(dist_root)
        if os.name != "nt" and dist_root.stat().st_mode & stat.S_IWOTH:
            raise ControlUiAssetError(
                "external_world_writable",
                "The external Control UI directory is writable by other users.",
            )
        manifest = validate_control_ui_artifact(dist_root, manifest_required=True)
        assert manifest is not None
        return ControlUiAssets(
            mode="external",
            static_root=self._static_root(),
            dist_root=dist_root,
            template_root=self._template_root,
            manifest=manifest,
        )

    def resolve(self) -> ControlUiAssets:
        if not self._config.control_ui.enabled:
            return self._none("disabled")

        requested: ControlUiAssetsRequestMode = self._config.control_ui.assets_mode
        if requested == "none":
            return self._none("explicit_none")

        try:
            if requested == "external":
                return self._external()
            return self._embedded()
        except ControlUiAssetError as error:
            prefix = "external" if requested == "external" else "embedded"
            return self._none(f"{prefix}:{error.code}")
        except OSError:
            prefix = "external" if requested == "external" else "embedded"
            return self._none(f"{prefix}:asset_io_error")


__all__ = [
    "CONTROL_UI_MANIFEST_NAME",
    "ControlUiArtifactFile",
    "ControlUiArtifactManifest",
    "ControlUiAssetError",
    "ControlUiAssetResolver",
    "ControlUiAssets",
    "ControlUiAssetsMode",
    "ControlUiAssetsRequestMode",
    "validate_control_ui_artifact",
]
