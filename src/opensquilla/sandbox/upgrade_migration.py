"""Idempotent direct-update migration for legacy sandbox state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.lossless_toml import patch_import_config
from opensquilla.sandbox.legacy_codec import (
    LegacyModeContext,
    decode_legacy_config_mode,
    decode_legacy_run_mode,
)

MIGRATION_VERSION = 2
JOURNAL_NAME = ".sandbox-upgrade-v2.json"
SNAPSHOT_NAME = ".sandbox-upgrade-snapshot"


@dataclass(frozen=True)
class UpgradeMigrationReport:
    ok: bool
    status: str
    canonical_mode: str | None
    journal_path: Path
    snapshot_path: Path | None
    stores: tuple[str, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "canonicalMode": self.canonical_mode,
            "journalPath": str(self.journal_path),
            "snapshotPath": str(self.snapshot_path) if self.snapshot_path else None,
            "stores": list(self.stores),
            "error": self.error,
        }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
    )


def _store_candidates(home: Path) -> tuple[Path, ...]:
    candidates = [
        home / "config.toml",
        home / "desktop-preferences.json",
        home / "preferences.json",
        home / "sessions.db",
        home / "state" / "sessions.db",
        home / "data" / "sessions.db",
    ]
    return tuple(path for path in candidates if path.is_file())


def inventory_sandbox_stores(home: str | Path) -> tuple[Path, ...]:
    root = Path(home).expanduser().absolute()
    return _store_candidates(root)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_mode(payload: dict[str, Any]):
    sandbox = payload.get("sandbox")
    sandbox_table = sandbox if isinstance(sandbox, dict) else {}
    permissions = payload.get("permissions")
    permissions_table = permissions if isinstance(permissions, dict) else {}
    arguments: dict[str, object] = {}
    if "run_mode" in sandbox_table:
        arguments["run_mode"] = sandbox_table["run_mode"]
    if "default_mode" in permissions_table:
        arguments["permissions_default_mode"] = permissions_table["default_mode"]
    if "sandbox" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["sandbox"]
    elif "enabled" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["enabled"]
    if "security_grading" in sandbox_table:
        arguments["grading_enabled"] = sandbox_table["security_grading"]
    return decode_legacy_config_mode(**arguments)


def lossless_patch_sandbox_fields(raw: bytes) -> tuple[bytes, str]:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = json.loads(json.dumps(original))
    mode = _config_mode(original)
    sandbox = transformed.setdefault("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox config must be a table")
    sandbox["run_mode"] = mode.value
    patched = patch_import_config(raw, original, transformed)
    return patched, mode.value


def _canonicalize_preferences(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize_preferences(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key in {"runMode", "run_mode", "sandboxMode", "sandbox_mode"} and isinstance(
            child, str
        ):
            result[key] = decode_legacy_run_mode(
                child,
                context=LegacyModeContext.STORED_EVENT,
            ).value
        else:
            result[key] = _canonicalize_preferences(child)
    return result


class SandboxUpgradeCoordinator:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().absolute()
        self.journal_path = self.home / JOURNAL_NAME
        self.snapshot_path = self.home / SNAPSHOT_NAME

    def _load_journal(self) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            return None
        payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("migrationVersion") != MIGRATION_VERSION:
            raise ValueError("unsupported sandbox upgrade journal")
        return payload

    def _snapshot(self, stores: tuple[Path, ...]) -> None:
        if self.snapshot_path.exists():
            return
        staging = self.home / f".{SNAPSHOT_NAME}.{os.getpid()}.tmp"
        staging.mkdir(parents=True)
        try:
            manifest: list[dict[str, object]] = []
            for source in stores:
                relative = source.relative_to(self.home)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                manifest.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": _digest(destination),
                        "size": destination.stat().st_size,
                    }
                )
            _write_json(staging / "manifest.json", {"stores": manifest})
            os.replace(staging, self.snapshot_path)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def run(self) -> UpgradeMigrationReport:
        self.home.mkdir(parents=True, exist_ok=True)
        stores = inventory_sandbox_stores(self.home)
        store_names = tuple(path.relative_to(self.home).as_posix() for path in stores)
        journal = self._load_journal()
        if journal is not None and journal.get("status") == "committed":
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=journal.get("canonicalMode"),
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path if self.snapshot_path.exists() else None,
                stores=store_names,
            )
        try:
            self._snapshot(stores)
            prepared = {
                "migrationVersion": MIGRATION_VERSION,
                "status": "prepared",
                "preparedAt": int(time.time()),
                "stores": store_names,
                "snapshot": str(self.snapshot_path),
            }
            _write_json(self.journal_path, prepared)
            canonical_mode: str | None = None
            config_path = self.home / "config.toml"
            if config_path.is_file():
                patched, canonical_mode = lossless_patch_sandbox_fields(
                    config_path.read_bytes()
                )
                if patched != config_path.read_bytes():
                    _atomic_write(config_path, patched)
            for name in ("desktop-preferences.json", "preferences.json"):
                preference_path = self.home / name
                if not preference_path.is_file():
                    continue
                original = json.loads(preference_path.read_text(encoding="utf-8"))
                transformed = _canonicalize_preferences(original)
                if transformed != original:
                    _write_json(preference_path, transformed)
            committed = {
                **prepared,
                "status": "committed",
                "committedAt": int(time.time()),
                "canonicalMode": canonical_mode,
            }
            _write_json(self.journal_path, committed)
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=canonical_mode,
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path,
                stores=store_names,
            )
        except Exception as exc:
            failed = {
                "migrationVersion": MIGRATION_VERSION,
                "status": "prepared",
                "stores": store_names,
                "snapshot": str(self.snapshot_path),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _write_json(self.journal_path, failed)
            return UpgradeMigrationReport(
                ok=False,
                status="manual_recovery_required",
                canonical_mode=None,
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path if self.snapshot_path.exists() else None,
                stores=store_names,
                error=failed["error"],
            )


def ensure_sandbox_upgrade_migrated(home: str | Path) -> UpgradeMigrationReport:
    return SandboxUpgradeCoordinator(home).run()


def inspect_sandbox_upgrade(home: str | Path) -> UpgradeMigrationReport:
    coordinator = SandboxUpgradeCoordinator(home)
    try:
        journal = coordinator._load_journal()
    except Exception as exc:
        return UpgradeMigrationReport(
            ok=False,
            status="manual_recovery_required",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=(
                coordinator.snapshot_path if coordinator.snapshot_path.exists() else None
            ),
            stores=(),
            error=f"{type(exc).__name__}: {exc}",
        )
    if journal is None:
        return UpgradeMigrationReport(
            ok=True,
            status="not_started",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=None,
            stores=(),
        )
    return UpgradeMigrationReport(
        ok=journal.get("status") == "committed",
        status=str(journal.get("status") or "manual_recovery_required"),
        canonical_mode=journal.get("canonicalMode"),
        journal_path=coordinator.journal_path,
        snapshot_path=(
            coordinator.snapshot_path if coordinator.snapshot_path.exists() else None
        ),
        stores=tuple(str(item) for item in journal.get("stores", ())),
        error=journal.get("error"),
    )


__all__ = [
    "SandboxUpgradeCoordinator",
    "UpgradeMigrationReport",
    "ensure_sandbox_upgrade_migrated",
    "inspect_sandbox_upgrade",
    "inventory_sandbox_stores",
    "lossless_patch_sandbox_fields",
]
