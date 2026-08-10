"""Versioned, loss-aware lockfile management for installed Community skills."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.tree import compute_tree_sha256

LOCKFILE_SCHEMA_VERSION = 2

_STRING_ENTRY_FIELDS = (
    "source",
    "identifier",
    "version",
    "installed_at",
    "path",
    "sha256",
    "license",
    "upstream_url",
    "source_trust",
    "scan_verdict",
    "scan_strategy",
    "install_id",
    "manifest_name",
    "directory_name",
    "relative_path",
    "requested_identifier",
    "resolved_identifier",
    "resolved_version",
    "resolved_revision",
    "artifact_sha256",
    "tree_sha256",
    "parser_version",
    "dialect",
    "source_package_id",
)
_INTEGER_ENTRY_FIELDS = ("file_count", "total_bytes")
_KNOWN_ENTRY_FIELDS = frozenset(
    (*_STRING_ENTRY_FIELDS, *_INTEGER_ENTRY_FIELDS, "accepted_risk_override", "scan_findings")
)


class LockfileMutationBlockedError(RuntimeError):
    """Raised when mutating a missing-trust or malformed lockfile would lose state."""

    def __init__(self, path: Path | str, diagnostics: list[SkillDiagnostic]) -> None:
        self.path = str(path)
        self.diagnostics = tuple(diagnostics)
        detail = diagnostics[0].message if diagnostics else "lockfile is not mutable"
        super().__init__(f"Skill lockfile mutation blocked for {self.path}: {detail}")


@dataclass
class LockEntry:
    """A single installed Skill entry in the name-keyed v2 lockfile.

    The original v1 fields remain first-class.  New source-resolution and
    validation fields are additive and default safely so existing installers can
    continue constructing ``LockEntry(source=..., identifier=...)``.
    """

    source: str = ""
    identifier: str = ""
    version: str = ""
    installed_at: str = ""
    path: str = ""
    sha256: str = ""
    license: str = ""
    upstream_url: str = ""
    source_trust: str = ""
    scan_verdict: str = ""
    scan_strategy: str = ""
    scan_findings: list[dict[str, Any]] = field(default_factory=list)

    # v2 identity and reproducibility fields. ``path`` remains for legacy
    # readers; ``relative_path`` is the portable path beneath the managed root.
    install_id: str = ""
    manifest_name: str = ""
    directory_name: str = ""
    relative_path: str = ""
    requested_identifier: str = ""
    resolved_identifier: str = ""
    resolved_version: str = ""
    resolved_revision: str = ""
    artifact_sha256: str = ""
    tree_sha256: str = ""
    file_count: int = 0
    total_bytes: int = 0
    parser_version: str = ""
    dialect: str = ""
    source_package_id: str = ""
    accepted_risk_override: bool = False

    # Unknown entry fields survive load/save so a newer writer is not silently
    # destroyed by this version when its core schema is still understood.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra)
        for field_name in _STRING_ENTRY_FIELDS:
            payload[field_name] = getattr(self, field_name)
        payload["file_count"] = self.file_count
        payload["total_bytes"] = self.total_bytes
        payload["accepted_risk_override"] = self.accepted_risk_override
        payload["scan_findings"] = [dict(item) for item in self.scan_findings]
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass
class Lockfile:
    """Name-keyed Skill lockfile with explicit load diagnostics.

    A malformed or unsupported file is readable as a diagnostic object but all
    mutation methods fail closed.  This prevents callers from silently replacing
    a damaged installation record with an empty lockfile.
    """

    version: int = LOCKFILE_SCHEMA_VERSION
    installed: dict[str, LockEntry] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
    source_index_extensions: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    loaded_version: int = LOCKFILE_SCHEMA_VERSION
    diagnostics: list[SkillDiagnostic] = field(default_factory=list, repr=False)
    mutation_blocked: bool = False
    source_path: str = field(default="", repr=False)

    @staticmethod
    def load(path: Path) -> Lockfile:
        """Read v2, v1, or the historical top-level ``skills`` shape.

        Missing files are valid empty v2 lockfiles. Parse, shape, I/O, and future
        schema errors produce a fail-closed object with wire-safe diagnostics.
        """

        try:
            exists = path.exists()
        except OSError as exc:
            return _blocked_lockfile(path, "LOCKFILE_IO_ERROR", str(exc))
        if not exists:
            return Lockfile(source_path=str(path))

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _blocked_lockfile(
                path,
                "LOCKFILE_CORRUPT",
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}",
            )
        except (OSError, UnicodeError) as exc:
            return _blocked_lockfile(path, "LOCKFILE_IO_ERROR", str(exc))

        if not isinstance(data, dict):
            return _blocked_lockfile(
                path,
                "LOCKFILE_INVALID_SHAPE",
                "Lockfile root must be a JSON object",
            )

        diagnostics: list[SkillDiagnostic] = []
        loaded_version = _parse_version(data.get("version", 1), path, diagnostics)
        mutation_blocked = any(item.blocking for item in diagnostics)
        if loaded_version > LOCKFILE_SCHEMA_VERSION:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_VERSION_UNSUPPORTED",
                    (
                        f"Lockfile schema {loaded_version} is newer than supported "
                        f"schema {LOCKFILE_SCHEMA_VERSION}"
                    ),
                    path,
                    blocking=True,
                    hint="Upgrade OpenSquilla before changing installed skills.",
                )
            )
            mutation_blocked = True

        raw_entries: object
        historical = False
        if "installed" in data:
            raw_entries = data["installed"]
        elif "skills" in data:
            raw_entries = data["skills"]
            historical = True
        else:
            raw_entries = {}

        normalized = _normalize_entry_container(
            raw_entries,
            path=path,
            diagnostics=diagnostics,
            historical=historical,
        )
        entries: dict[str, LockEntry] = {}
        for name, raw_entry in normalized.items():
            entry = _parse_entry(name, raw_entry, path=path, diagnostics=diagnostics)
            if entry is not None:
                entries[name] = entry

        mutation_blocked = mutation_blocked or any(item.blocking for item in diagnostics)
        extra = {
            key: value
            for key, value in data.items()
            if key not in {"version", "installed", "skills", "source_index"}
        }
        return Lockfile(
            version=LOCKFILE_SCHEMA_VERSION,
            installed=entries,
            extra=extra,
            source_index_extensions=_source_index_extensions(data.get("source_index")),
            loaded_version=loaded_version,
            diagnostics=diagnostics,
            mutation_blocked=mutation_blocked,
            source_path=str(path),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra)
        payload["version"] = LOCKFILE_SCHEMA_VERSION
        payload["installed"] = {
            name: self.installed[name].to_dict() for name in sorted(self.installed)
        }
        payload["source_index"] = self.source_index
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def source_index(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return the secondary source identity index rebuilt from ``installed``.

        Persisted index targets are never authoritative. Unknown fields attached
        to a still-valid source/identifier pair survive as additive extensions.
        """

        return _build_source_index(self.installed, self.source_index_extensions)

    def save(self, path: Path) -> None:
        """Atomically save v2 and retain the previous valid file as ``.bak``."""

        self._ensure_mutable(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        previous: bytes | None = None
        try:
            if path.exists():
                current = Lockfile.load(path)
                current._ensure_mutable(path)
                previous = path.read_bytes()
        except LockfileMutationBlockedError:
            raise
        except OSError as exc:
            raise LockfileMutationBlockedError(
                path,
                [_diagnostic("LOCKFILE_IO_ERROR", str(exc), path, blocking=True)],
            ) from exc

        if previous is not None:
            _atomic_write(lockfile_backup_path(path), previous)

        encoded = (
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        _atomic_write(path, encoded)
        self.version = LOCKFILE_SCHEMA_VERSION
        self.loaded_version = LOCKFILE_SCHEMA_VERSION
        self.source_path = str(path)

    def add(self, name: str, entry: LockEntry) -> None:
        self._ensure_mutable(self.source_path or "<memory>")
        self.installed[name] = entry

    def remove(self, name: str) -> bool:
        self._ensure_mutable(self.source_path or "<memory>")
        if name in self.installed:
            del self.installed[name]
            return True
        return False

    def get(self, name: str) -> LockEntry | None:
        return self.installed.get(name)

    def _ensure_mutable(self, path: Path | str) -> None:
        if self.mutation_blocked:
            raise LockfileMutationBlockedError(path, self.diagnostics)


def lockfile_backup_path(path: Path) -> Path:
    """Return the single-generation backup path for ``path``."""

    return path.with_name(f"{path.name}.bak")


def _source_index_extensions(raw: object) -> dict[tuple[str, str], dict[str, Any]]:
    """Retain only unknown fields associated with structurally usable index rows."""

    if not isinstance(raw, dict):
        return {}
    extensions: dict[tuple[str, str], dict[str, Any]] = {}
    for source_id, raw_identifiers in raw.items():
        if not isinstance(source_id, str) or not isinstance(raw_identifiers, dict):
            continue
        for identifier, raw_target in raw_identifiers.items():
            if not isinstance(identifier, str) or not isinstance(raw_target, dict):
                continue
            extra = {
                key: value
                for key, value in raw_target.items()
                if key not in {"name", "install_id"}
            }
            if extra:
                extensions[(source_id, identifier)] = extra
    return extensions


def _build_source_index(
    installed: dict[str, LockEntry],
    extensions: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for name in sorted(installed):
        entry = installed[name]
        source_id = entry.source
        identifier = (
            entry.resolved_identifier or entry.identifier or entry.requested_identifier
        )
        if not source_id or not identifier:
            continue
        source_entries = index.setdefault(source_id, {})
        if identifier in source_entries:
            # The name-keyed record remains authoritative. Management services
            # prevent new collisions; old collisions resolve deterministically.
            continue
        target = dict(extensions.get((source_id, identifier), {}))
        target["name"] = name
        target["install_id"] = entry.install_id
        source_entries[identifier] = target
    return index


def _parse_version(
    raw: object,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> int:
    if isinstance(raw, bool):
        value = 0
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        value = 0
    if value >= 1:
        return value
    diagnostics.append(
        _diagnostic(
            "LOCKFILE_INVALID_VERSION",
            "Lockfile version must be a positive integer",
            path,
            blocking=True,
            field_name="version",
        )
    )
    return 1


def _normalize_entry_container(
    raw: object,
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
    historical: bool,
) -> dict[str, object]:
    if isinstance(raw, dict):
        return {str(name): entry for name, entry in raw.items()}
    if historical and isinstance(raw, list):
        entries: dict[str, object] = {}
        for index, item in enumerate(raw):
            if isinstance(item, str) and item:
                name = item
                entry: object = {"identifier": item}
            elif isinstance(item, dict):
                name = str(
                    item.get("name")
                    or item.get("skill_name")
                    or item.get("identifier")
                    or ""
                )
                entry = item
            else:
                name = ""
                entry = item
            if not name:
                diagnostics.append(
                    _diagnostic(
                        "LOCKFILE_INVALID_ENTRY",
                        f"Historical skills entry {index} has no usable name",
                        path,
                        blocking=True,
                        field_name=f"skills[{index}]",
                    )
                )
                continue
            if name in entries:
                diagnostics.append(
                    _diagnostic(
                        "LOCKFILE_DUPLICATE_NAME",
                        f"Historical lockfile contains duplicate Skill name {name!r}",
                        path,
                        blocking=True,
                        field_name=f"skills[{index}]",
                    )
                )
                continue
            entries[name] = entry
        return entries

    diagnostics.append(
        _diagnostic(
            "LOCKFILE_INVALID_SHAPE",
            (
                "Historical skills must be an array or object"
                if historical
                else "installed must be an object"
            ),
            path,
            blocking=True,
            field_name="skills" if historical else "installed",
        )
    )
    return {}


def _parse_entry(
    name: str,
    raw: object,
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> LockEntry | None:
    if not name:
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY",
                "Installed Skill name must not be empty",
                path,
                blocking=True,
                field_name="installed",
            )
        )
        return None
    if isinstance(raw, str):
        raw = {"identifier": raw}
    if not isinstance(raw, dict):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY",
                f"Installed Skill {name!r} must be an object",
                path,
                blocking=True,
                field_name=f"installed.{name}",
            )
        )
        return None

    values: dict[str, Any] = {}
    for field_name in _STRING_ENTRY_FIELDS:
        value = raw.get(field_name, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_INVALID_ENTRY_FIELD",
                    f"{name}.{field_name} must be a string",
                    path,
                    blocking=True,
                    field_name=f"installed.{name}.{field_name}",
                )
            )
            value = ""
        values[field_name] = value

    for field_name in _INTEGER_ENTRY_FIELDS:
        value = raw.get(field_name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_INVALID_ENTRY_FIELD",
                    f"{name}.{field_name} must be a non-negative integer",
                    path,
                    blocking=True,
                    field_name=f"installed.{name}.{field_name}",
                )
            )
            value = 0
        values[field_name] = value

    accepted_risk = raw.get("accepted_risk_override", False)
    if not isinstance(accepted_risk, bool):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY_FIELD",
                f"{name}.accepted_risk_override must be a boolean",
                path,
                blocking=True,
                field_name=f"installed.{name}.accepted_risk_override",
            )
        )
        accepted_risk = False
    values["accepted_risk_override"] = accepted_risk

    raw_findings = raw.get("scan_findings", [])
    if not isinstance(raw_findings, list) or not all(
        isinstance(item, dict) for item in raw_findings
    ):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY_FIELD",
                f"{name}.scan_findings must be an array of objects",
                path,
                blocking=True,
                field_name=f"installed.{name}.scan_findings",
            )
        )
        raw_findings = []
    values["scan_findings"] = [dict(item) for item in raw_findings]
    values["extra"] = {
        key: value
        for key, value in raw.items()
        if key not in _KNOWN_ENTRY_FIELDS
    }
    return LockEntry(**values)


def _blocked_lockfile(path: Path, code: str, message: str) -> Lockfile:
    diagnostic = _diagnostic(code, message, path, blocking=True)
    return Lockfile(
        diagnostics=[diagnostic],
        mutation_blocked=True,
        source_path=str(path),
    )


def _diagnostic(
    code: str,
    message: str,
    path: Path,
    *,
    blocking: bool,
    field_name: str = "",
    hint: str = "Repair or restore the lockfile before changing installed skills.",
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.LOCK,
        message=message,
        blocking=blocking,
        path=str(path),
        field_name=field_name,
        hint=hint,
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            # Cleanup is best-effort and must not mask the atomic-write outcome.
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory durability; directory handles are unavailable on Windows."""

    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def compute_sha256(directory: Path) -> str:
    """Compute the legacy SHA-256 digest of all non-dotfiles in a directory."""

    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            hasher.update(str(relative).encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


__all__ = [
    "LOCKFILE_SCHEMA_VERSION",
    "LockEntry",
    "Lockfile",
    "LockfileMutationBlockedError",
    "compute_sha256",
    "compute_tree_sha256",
    "lockfile_backup_path",
]
