"""Reliable Community Skill management across Gateway, CLI, and agent tools."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import ExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
import yaml

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.eligibility import (
    EligibilityContext,
    diagnose_eligibility,
    is_skill_available_live,
)
from opensquilla.skills.hub.archive import normalize_relative_path, validate_portable_file_paths
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillCompatibilityState,
    SkillDiagnostic,
    SkillInstallState,
    SkillInvocationCapabilities,
    SkillLifecycle,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.lockfile import (
    LockEntry,
    Lockfile,
    LockfileMutationBlockedError,
    compute_sha256,
    compute_tree_sha256,
)
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.scanner import ScanResult, scan_skill_bundle
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillSource,
    SkillSourceFetchError,
    SourceResolution,
)
from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    cleanup_empty_transaction_directories,
    cleanup_staging_transaction_reservation,
    default_journal_path,
    ensure_safe_transaction_roots,
    fsync_directory,
    fsync_staging_tree,
    guard_retained_recovery_journal,
    path_is_occupied,
    recover_pending_skill_transaction,
    remove_transaction_journal,
    rollback_root,
    staging_root,
    validate_transaction_journal_paths,
)
from opensquilla.skills.manifest import (
    _parse_skill_frontmatter_strict,
    validate_hub_candidate,
)
from opensquilla.skills.paths import default_managed_skills_dir
from opensquilla.skills.types import SkillLayer, SkillSpec

log = structlog.get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SAFE_TRACKED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)$", re.DOTALL)
_MAX_MANAGED_SKILLS = 200
_MAX_BUNDLE_ENTRIES = 2_048
_MAX_BUNDLE_BYTES = 50 * 1024 * 1024
_MAX_BUNDLE_DEPTH = 32
_DEGRADED_CAPABILITIES_KEY = "degraded_capabilities"
_SCOPED_TOOL_PERMISSIONS_CAPABILITY = "scoped_tool_permissions"
_DYNAMIC_CONTEXT_CAPABILITY = "dynamic_context"
_RESERVED_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".opensquilla",
        ".opensquilla-staging",
        ".opensquilla-rollback",
        ".quarantine",
        ".staging",
        "__macosx",
    }
)


class _CandidateManifestError(ValueError):
    """Preparation error that retains its public manifest diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

_mutation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def mutation_lock_for(managed_dir: Path) -> asyncio.Lock:
    """Return the process-wide writer lock for one managed root."""

    key = os.path.normcase(str(managed_dir.expanduser().resolve(strict=False)))
    lock = _mutation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _mutation_locks[key] = lock
    return lock


@asynccontextmanager
async def committed_store_read_guard(managed_dir: Path) -> AsyncIterator[None]:
    """Serialize a disk/lockfile observation with managed-store publishers."""

    async with mutation_lock_for(managed_dir):
        yield


async def _run_postflight_worker(
    function: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Keep transaction fences held until a postflight thread settles.

    Cancelling ``asyncio.to_thread`` cannot stop its underlying thread. Defer
    caller cancellation, including repeated requests, until the worker exits so
    rollback cannot race a catalog reload that is still reading managed files.
    """

    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is None:
                raise
            break
    if cancellation is not None:
        try:
            operation.result()
        except BaseException:
            pass
        raise cancellation
    return operation.result()


def _diagnostic(
    code: str,
    message: str,
    *,
    phase: DiagnosticPhase,
    blocking: bool = False,
    severity: DiagnosticSeverity | None = None,
    hint: str = "",
    details: dict[str, Any] | None = None,
    path: str = "",
    field_name: str = "",
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=severity
        or (DiagnosticSeverity.ERROR if blocking else DiagnosticSeverity.WARNING),
        phase=phase,
        message=message,
        blocking=blocking,
        hint=hint,
        details=details or {},
        path=path,
        field_name=field_name,
    )


def _invocation_capabilities(
    spec: SkillSpec | None,
    *,
    selected: bool,
    enabled: bool,
    readiness: SkillReadinessState,
) -> SkillInvocationCapabilities:
    reachable = spec is not None and selected and enabled
    return SkillInvocationCapabilities(
        model_catalog=bool(
            reachable
            and spec
            and not spec.disable_model_invocation
            and readiness is not SkillReadinessState.NEEDS_SETUP
        ),
        # The current skill_view tool resolves any enabled winner by exact name;
        # user-invocable only governs completion/discovery, not exact lookup.
        skill_view=bool(reachable),
        user_completion=bool(reachable and spec and spec.user_invocable),
        direct_command=False,
        argument_substitution=False,
        scoped_tool_permissions=False,
        sandbox_execution="unknown",
    )


def _readiness_for_spec(
    spec: SkillSpec | None,
    diagnostics: list[SkillDiagnostic],
) -> SkillReadinessState:
    if spec is None:
        return SkillReadinessState.UNKNOWN
    requires = spec.metadata.requires if spec.metadata else None
    if requires and requires.config:
        diagnostics.append(
            _diagnostic(
                "REQUIREMENT_UNSUPPORTED",
                "requires.config is declared but is not interpreted by OpenSquilla",
                phase=DiagnosticPhase.READINESS,
                blocking=True,
                hint=(
                    "Use documented environment or binary requirements until "
                    "config mappings are supported."
                ),
                details={"config": list(requires.config)},
            )
        )
        return SkillReadinessState.UNKNOWN
    report = diagnose_eligibility(spec, EligibilityContext.auto())
    return (
        SkillReadinessState.READY
        if report.eligible
        else SkillReadinessState.NEEDS_SETUP
    )


def _selection_for_spec(
    spec: SkillSpec | None,
    *,
    selected: bool,
) -> SkillSelectionState:
    if spec is None or not selected:
        return SkillSelectionState.SHADOWED
    if not is_skill_available_live(spec.name):
        return SkillSelectionState.DISABLED
    if spec.disable_model_invocation:
        return SkillSelectionState.HIDDEN
    return SkillSelectionState.ACTIVE


def lifecycle_for_candidate(
    *,
    spec: SkillSpec | None,
    selected: bool,
    tracked: bool,
    present: bool | None = None,
    drifted: bool = False,
    offline: bool = False,
    rejected: bool = False,
    serving_previous: bool = False,
    compatibility: SkillCompatibilityState = SkillCompatibilityState.INSTRUCTION_ONLY,
    diagnostics: list[SkillDiagnostic] | None = None,
) -> SkillLifecycle:
    """Build lifecycle axes from current store/catalog facts."""

    lifecycle_diagnostics = diagnostics if diagnostics is not None else []
    if (
        compatibility is SkillCompatibilityState.INSTRUCTION_ONLY
        and spec is not None
        and spec.metadata is not None
        and spec.metadata.requires is not None
        and spec.metadata.requires.config
    ):
        compatibility = SkillCompatibilityState.DEGRADED
    selection = (
        SkillSelectionState.HIDDEN
        if offline and spec is not None
        else _selection_for_spec(spec, selected=selected)
    )
    enabled = selection not in {
        SkillSelectionState.SHADOWED,
        SkillSelectionState.DISABLED,
    }
    readiness = _readiness_for_spec(spec, lifecycle_diagnostics)
    physically_present = spec is not None if present is None else present
    if offline:
        load_state = SkillLoadState.VALIDATED_OFFLINE
    elif serving_previous:
        load_state = SkillLoadState.SERVING_PREVIOUS
    elif rejected:
        load_state = SkillLoadState.REJECTED
    elif spec is not None:
        load_state = SkillLoadState.LOADED
    else:
        load_state = SkillLoadState.NOT_DISCOVERED
    return SkillLifecycle(
        install_state=(
            SkillInstallState.DRIFTED
            if drifted
            else SkillInstallState.TRACKED
            if tracked and physically_present
            else SkillInstallState.UNTRACKED
            if physically_present
            else SkillInstallState.MISSING
        ),
        load_state=load_state,
        selection_state=selection,
        compatibility_state=compatibility,
        readiness_state=readiness,
        invocation=_invocation_capabilities(
            spec,
            selected=selected,
            enabled=enabled,
            readiness=readiness,
        ),
    )


def _committed_install_message(
    name: str,
    lifecycle: SkillLifecycle,
    *,
    online: bool,
    updated: bool,
) -> str:
    action = "Updated" if updated else "Installed"
    if not online:
        compatibility_note = (
            "; tool preapproval will not apply"
            if lifecycle.compatibility_state is SkillCompatibilityState.DEGRADED
            else ""
        )
        return (
            f"Validated and {action.lower()} {name!r}; catalog activation and "
            f"readiness will be evaluated on next start{compatibility_note}"
        )
    if lifecycle.usable is True:
        if lifecycle.compatibility_state is SkillCompatibilityState.DEGRADED:
            return (
                f"{action} {name!r}; it can be used with limited compatibility "
                "from the next turn"
            )
        return f"{action} {name!r}; it can be used from the next turn"
    if lifecycle.selection_state is SkillSelectionState.SHADOWED:
        return (
            f"{action} {name!r}; its catalog state is visible from the next turn, "
            "but a higher-precedence Skill remains active"
        )
    if lifecycle.selection_state is SkillSelectionState.DISABLED:
        return (
            f"{action} {name!r}; its catalog state is visible from the next turn, "
            "but the Skill is disabled"
        )
    if lifecycle.readiness_state is SkillReadinessState.NEEDS_SETUP:
        return (
            f"{action} {name!r}; its catalog state is visible from the next turn, "
            "but dependency setup is required before use"
        )
    return (
        f"{action} {name!r}; its catalog state is visible from the next turn, "
        "but usability has not been established"
    )


@dataclass
class InstallResult:
    """Additive install/update/uninstall result preserving legacy fields."""

    success: bool
    name: str = ""
    message: str = ""
    scan: ScanResult | None = None
    path: str = ""
    unchanged: bool = False
    installed: bool = False
    active: bool = False
    instruction_usable: bool = False
    install_id: str = ""
    lifecycle: SkillLifecycle | None = None
    resolution: SourceResolution | None = None
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)
    reload: dict[str, Any] = field(default_factory=dict)
    rollback_performed: bool = False
    catalog_generation: int = 0
    effective_from: str = ""

    def to_dict(self) -> dict[str, Any]:
        scan_payload: dict[str, Any] | None = None
        if self.scan is not None:
            scan_payload = {
                "verdict": self.scan.verdict,
                "strategy": self.scan.strategy,
                "findings": [vars(item) for item in self.scan.findings],
            }
        resolution_payload: dict[str, Any] | None = None
        if self.resolution is not None:
            serializer = getattr(self.resolution, "to_dict", None)
            if callable(serializer):
                resolution_payload = serializer()
            else:
                resolution_payload = {
                    "source": self.resolution.source_id,
                    "requestedIdentifier": self.resolution.requested_identifier,
                    "canonicalIdentifier": self.resolution.canonical_identifier,
                    "immutableRevision": self.resolution.revision,
                    "artifactDigest": self.resolution.expected_digest,
                    "trustState": self.resolution.trust_state,
                    "immutable": self.resolution.immutable,
                }
        return {
            "success": self.success,
            "unchanged": self.unchanged,
            "name": self.name,
            "message": self.message,
            "path": self.path,
            "scan": scan_payload,
            "installed": self.installed,
            "active": self.active,
            "instruction_usable": self.instruction_usable,
            "installId": self.install_id,
            "lifecycle": self.lifecycle.to_dict() if self.lifecycle else None,
            "resolution": resolution_payload,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "reload": dict(self.reload),
            "rollbackPerformed": self.rollback_performed,
            "catalogGeneration": self.catalog_generation,
            "effectiveFrom": self.effective_from,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


def _canonical_bundle_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str) or "\x00" in raw_path:
        raise ValueError("bundle contains an invalid path")
    path = normalize_relative_path(raw_path)
    if len(path.parts) > _MAX_BUNDLE_DEPTH:
        raise ValueError(f"bundle path exceeds {_MAX_BUNDLE_DEPTH} directory levels")
    if any(part.casefold() in _RESERVED_COMPONENTS for part in path.parts):
        raise ValueError(f"bundle path uses a reserved directory: {raw_path!r}")
    return path


def _bundle_digest(files: dict[str, str | bytes]) -> str:
    hasher = hashlib.sha256()
    for raw_name, value in sorted(files.items()):
        content = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        hasher.update(raw_name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
    return hasher.hexdigest()


def _installed_file_modes(bundle: SkillBundle) -> dict[str, int]:
    modes: dict[str, int] = {}
    for raw_path, value in bundle.file_modes.items():
        path = _canonical_bundle_path(raw_path)
        if len(path.parts) == 1 and path.name.casefold() in {"skill.md", "skills.md"}:
            key = "SKILL.md"
        else:
            key = path.as_posix()
        modes[key] = int(value) & 0o777
    return modes


def _write_bundle(
    files: dict[str, str | bytes],
    candidate_dir: Path,
    file_modes: dict[str, int] | None = None,
) -> None:
    if len(files) > _MAX_BUNDLE_ENTRIES:
        raise ValueError(f"bundle contains more than {_MAX_BUNDLE_ENTRIES} entries")
    total_bytes = 0
    canonical_paths = {
        raw_name: _canonical_bundle_path(raw_name) for raw_name in files
    }
    try:
        validate_portable_file_paths(canonical_paths.values())
    except ValueError as exc:
        raise ValueError(f"bundle contains a portable path collision: {exc}") from None
    candidate_dir.mkdir(parents=True, exist_ok=False)
    for raw_name, value in files.items():
        relative = canonical_paths[raw_name]
        content = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        total_bytes += len(content)
        if total_bytes > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle exceeds the 50 MiB expanded-size limit")
        destination = candidate_dir.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(content)
        if os.name != "nt" and file_modes and raw_name in file_modes:
            destination.chmod(int(file_modes[raw_name]) & 0o777)


def _derive_slug(bundle: SkillBundle, resolution: SourceResolution) -> str:
    skill_path = str(getattr(resolution, "skill_path", "") or "").strip("/")
    candidate = skill_path.rsplit("/", 1)[-1] if skill_path else ""
    if not candidate:
        identifier = str(
            getattr(resolution, "canonical_identifier", "")
            or getattr(resolution, "requested_identifier", "")
            or bundle.name
        )
        identifier = identifier.split(":", 1)[-1].rstrip("/")
        candidate = identifier.rsplit("/", 1)[-1]
        candidate = candidate.split("@", 1)[0]
        if candidate.lower() in {"skill.md", "skills.md"}:
            candidate = bundle.name
    return candidate.strip()


def _first_body_paragraph(body: str) -> str:
    for paragraph in re.split(r"\r?\n\s*\r?\n", body):
        text = " ".join(line.strip() for line in paragraph.splitlines()).strip()
        text = re.sub(r"^(?:#{1,6}|[-*+])\s+", "", text).strip()
        if text:
            return text[:1_024]
    return ""


def _normalize_legacy_manifest(
    candidate_dir: Path,
    *,
    bundle: SkillBundle,
    resolution: SourceResolution,
    source_id: str,
) -> tuple[Path, bool]:
    allow_clawhub_legacy = source_id == "clawhub"
    manifests = [
        child
        for child in candidate_dir.iterdir()
        if child.is_file()
        and (
            child.name.casefold() in {"skill.md", "skills.md"}
            if allow_clawhub_legacy
            else child.name == "SKILL.md"
        )
    ]
    if len(manifests) != 1:
        expected = "SKILL.md/skill.md/skills.md" if allow_clawhub_legacy else "SKILL.md"
        raise ValueError(f"bundle must contain exactly one root {expected}")
    manifest = manifests[0]
    raw = manifest.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _CandidateManifestError(
            "FRONTMATTER_INVALID",
            "SKILL.md is not valid UTF-8",
        ) from exc

    match = _FRONTMATTER_RE.match(text)
    changed = raw.startswith(b"\xef\xbb\xbf") or (
        allow_clawhub_legacy and manifest.name != "SKILL.md"
    )
    if match is None:
        first_line = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            "",
        )
        if first_line == "---":
            raise _CandidateManifestError(
                "FRONTMATTER_INVALID",
                "SKILL.md frontmatter is missing its closing delimiter",
            )
        if not allow_clawhub_legacy:
            raise _CandidateManifestError(
                "FRONTMATTER_INVALID",
                "SKILL.md must contain YAML frontmatter",
            )
        frontmatter: dict[str, Any] = {}
        body = text.strip()
        changed = True
    else:
        try:
            loaded, body = _parse_skill_frontmatter_strict(text)
        except ValueError as exc:
            raise _CandidateManifestError("FRONTMATTER_INVALID", str(exc)) from exc
        frontmatter = dict(loaded)

    if "name" not in frontmatter and allow_clawhub_legacy:
        frontmatter["name"] = _derive_slug(bundle, resolution)
        changed = True
    if "description" not in frontmatter and allow_clawhub_legacy:
        meta = bundle.meta or getattr(resolution, "meta", None)
        registry_description = str(getattr(meta, "description", "") or "").strip()
        frontmatter["description"] = registry_description or _first_body_paragraph(body)
        changed = True

    name = frontmatter.get("name")
    if name is not None and (
        not isinstance(name, str) or not _SAFE_NAME_RE.fullmatch(name)
    ):
        raise _CandidateManifestError("NAME_INVALID", f"invalid skill name: {name!r}")
    final_dir = candidate_dir.with_name(name) if isinstance(name, str) else candidate_dir
    if final_dir != candidate_dir:
        if path_is_occupied(final_dir):
            raise ValueError(f"staging collision for Skill {name!r}")
        os.replace(candidate_dir, final_dir)
        manifest = final_dir / manifest.name
    else:
        final_dir = candidate_dir

    canonical_manifest = final_dir / "SKILL.md"
    if allow_clawhub_legacy and manifest.name != canonical_manifest.name:
        if manifest.name.casefold() == canonical_manifest.name.casefold():
            # A case-only replace is a no-op on Windows and can leave the
            # directory entry spelled ``skill.md``. Rename through a unique
            # sibling so the production loader sees the canonical filename.
            intermediate_manifest = final_dir / (
                f".opensquilla-manifest-{uuid.uuid4().hex}.tmp"
            )
            os.replace(manifest, intermediate_manifest)
            os.replace(intermediate_manifest, canonical_manifest)
        else:
            os.replace(manifest, canonical_manifest)
        manifest = canonical_manifest
    if changed:
        rendered = yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            sort_keys=False,
            width=1_000,
        ).rstrip()
        manifest.write_text(f"---\n{rendered}\n---\n{body.strip()}\n", encoding="utf-8")
    return final_dir, changed


def _candidate_files(candidate_dir: Path) -> dict[str, str | bytes]:
    """Read the normalized staging tree for scanning, without executing it."""

    files: dict[str, str | bytes] = {}
    for path in sorted(candidate_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        try:
            content: str | bytes = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw
        files[path.relative_to(candidate_dir).as_posix()] = content
    return files


def _manifest_diagnostics(
    items: tuple[dict[str, str], ...],
    *,
    blocking: bool = True,
    path_override: str | None = None,
) -> list[SkillDiagnostic]:
    diagnostics: list[SkillDiagnostic] = []
    for item in items:
        code = str(item.get("code") or "MANIFEST_INVALID")
        compatibility_advisory = code in {
            "DYNAMIC_CONTEXT_UNSUPPORTED",
            "TOOL_PREAPPROVAL_IGNORED",
        }
        if code == "TOOL_PREAPPROVAL_IGNORED":
            hint = (
                "The Skill remains usable, but matching tools still follow normal approval."
            )
        elif code == "DYNAMIC_CONTEXT_UNSUPPORTED":
            hint = (
                "Run the command through the normal tool flow when its output is needed."
            )
        else:
            hint = (
                "Remove unsupported execution features or publish an instruction-first Skill."
            )
        diagnostics.append(
            _diagnostic(
                code,
                str(item.get("message") or "Skill manifest is invalid"),
                phase=(
                    DiagnosticPhase.COMPATIBILITY
                    if code
                    in {
                        "DIALECT_FIELD_UNSUPPORTED",
                        "DYNAMIC_CONTEXT_UNSUPPORTED",
                        "TOOL_PREAPPROVAL_IGNORED",
                    }
                    else DiagnosticPhase.MANIFEST
                ),
                blocking=blocking and not compatibility_advisory,
                severity=(
                    DiagnosticSeverity.WARNING if compatibility_advisory else None
                ),
                hint=hint,
                path=(
                    path_override
                    if path_override is not None
                    else str(item.get("path") or "")
                ),
                field_name=str(item.get("field") or ""),
            )
        )
    return diagnostics


def _degraded_capabilities_from_diagnostics(
    diagnostics: Iterable[SkillDiagnostic],
) -> list[str]:
    capabilities: set[str] = set()
    if any(item.code == "TOOL_PREAPPROVAL_IGNORED" for item in diagnostics):
        capabilities.add(_SCOPED_TOOL_PERMISSIONS_CAPABILITY)
    if any(item.code == "DYNAMIC_CONTEXT_UNSUPPORTED" for item in diagnostics):
        capabilities.add(_DYNAMIC_CONTEXT_CAPABILITY)
    return sorted(capabilities)


def _entry_compatibility(entry: LockEntry | None) -> SkillCompatibilityState:
    if entry is None:
        return SkillCompatibilityState.INSTRUCTION_ONLY
    raw = entry.extra.get(_DEGRADED_CAPABILITIES_KEY, [])
    if isinstance(raw, list) and _SCOPED_TOOL_PERMISSIONS_CAPABILITY in {
        str(item) for item in raw
    }:
        return SkillCompatibilityState.DEGRADED
    return SkillCompatibilityState.INSTRUCTION_ONLY


def _resolution_dict(resolution: SourceResolution) -> dict[str, Any]:
    serializer = getattr(resolution, "to_dict", None)
    if callable(serializer):
        payload = serializer()
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _installed_digest(target: Path, entry: LockEntry) -> str:
    """Use the v2 complete-tree digest while preserving v1 comparisons."""

    return (
        compute_tree_sha256(target)
        if entry.tree_sha256
        else compute_sha256(target)
    )


def _github_package_identifier(repository: str, skill_path: str) -> str:
    normalized_path = skill_path.strip("/")
    if normalized_path.casefold().endswith("/skill.md"):
        normalized_path = normalized_path.rsplit("/", 1)[0]
    elif normalized_path.casefold() in {"skill.md", "skills.md"}:
        normalized_path = ""
    normalized_repository = repository.casefold()
    return (
        f"{normalized_repository}:{normalized_path}"
        if normalized_path
        else normalized_repository
    )


def _resolution_package_identity(
    source_id: str,
    resolution: SourceResolution,
    requested_identifier: str,
) -> str:
    package = str(getattr(resolution, "package_identifier", "") or "").strip()
    if source_id == "github":
        from opensquilla.skills.hub.github import package_identifier_for

        package = package_identifier_for(package or requested_identifier)
        if not package and resolution.repository:
            package = _github_package_identifier(
                resolution.repository,
                resolution.skill_path,
            )
    if not package and source_id == "clawhub":
        package = resolution.canonical_identifier
        revision = str(resolution.revision or resolution.version).strip()
        if revision and package.endswith(f"@{revision}"):
            package = package[: -(len(revision) + 1)]
    if not package:
        package = requested_identifier.strip()
    return f"{source_id}:{package}" if package else ""


def _entry_package_identity(entry: LockEntry) -> str:
    if entry.source_package_id:
        if entry.source == "github":
            from opensquilla.skills.hub.github import package_identifier_for

            raw_package = entry.source_package_id.removeprefix("github:")
            normalized = package_identifier_for(raw_package)
            return f"github:{normalized}" if normalized else entry.source_package_id
        return entry.source_package_id
    if entry.source == "github":
        from opensquilla.skills.hub.github import package_identifier_for

        for identifier in (
            entry.resolved_identifier,
            entry.requested_identifier,
            entry.identifier,
        ):
            package = package_identifier_for(identifier)
            if package:
                return f"github:{package}"
        # A v1 identifier may use an extension-specific spelling. Preserve
        # one-cycle same-adapter update compatibility when it cannot be
        # normalized instead of misclassifying it as another package.
        if entry.source:
            return ""
    if entry.source == "clawhub" and entry.resolved_identifier:
        package = entry.resolved_identifier
        revision = entry.resolved_revision or entry.resolved_version
        if revision and package.endswith(f"@{revision}"):
            package = package[: -(len(revision) + 1)]
        return f"clawhub:{package}"
    requested = entry.requested_identifier or entry.identifier
    return f"{entry.source}:{requested}" if entry.source and requested else ""


def _is_legacy_clawhub_package_upgrade(
    entry: LockEntry,
    new_package_identity: str,
) -> bool:
    """Recognize the bounded v1 bare-slug → owner-qualified migration.

    Old lockfiles could only record a ClawHub slug.  A modern immutable
    resolution additionally binds that slug to its publisher.  Treat this as
    the same package only during an update and only when the canonical package
    keeps the exact legacy slug; every other identity change still requires an
    explicit source replacement.
    """

    if entry.source != "clawhub" or entry.source_package_id:
        return False
    legacy = (entry.requested_identifier or entry.identifier).strip()
    if not legacy or "/" in legacy or "@" in legacy or ":" in legacy:
        return False
    prefix = "clawhub:@"
    if not new_package_identity.startswith(prefix):
        return False
    qualified = new_package_identity[len(prefix) :]
    owner, separator, slug = qualified.partition("/")
    return bool(owner and separator and slug == legacy)


def _update_precondition(entry: LockEntry) -> tuple[str, str, str, str, str]:
    return (
        entry.install_id,
        entry.source,
        entry.resolved_revision,
        entry.requested_identifier or entry.identifier,
        entry.tree_sha256 or entry.sha256,
    )


class SkillManagementService:
    """Dependency-injected, transactional Community Skill manager."""

    def __init__(
        self,
        *,
        router: SourceRouter,
        managed_dir: Path,
        lockfile_path: Path,
        loader: Any | None = None,
        journal_path: Path | None = None,
        mutation_lock: asyncio.Lock | None = None,
        offline: bool = False,
        startup_recovery_diagnostics: Iterable[SkillDiagnostic] = (),
    ) -> None:
        self._router = router
        self._managed_dir = managed_dir
        self._lockfile_path = lockfile_path
        self._loader = loader
        self._journal_path = journal_path or default_journal_path(managed_dir)
        self._mutation_lock = mutation_lock or mutation_lock_for(managed_dir)
        self._offline = offline or loader is None
        self._recovery_required_diagnostics: tuple[SkillDiagnostic, ...] = ()
        self._observe_recovery(list(startup_recovery_diagnostics))

    @property
    def managed_dir(self) -> Path:
        return self._managed_dir

    @property
    def router(self) -> SourceRouter:
        return self._router

    @property
    def lockfile_path(self) -> Path:
        return self._lockfile_path

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    @property
    def recovery_diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return self._recovery_required_diagnostics

    @asynccontextmanager
    async def committed_store_read(self) -> AsyncIterator[None]:
        """Pin catalog, lockfile, and managed-tree reads to one committed state."""

        async with self._mutation_lock:
            yield

    def recover_offline_store(self) -> list[SkillDiagnostic]:
        """Recover crash leftovers while the offline composition root holds its lease."""

        if not self._offline:
            raise RuntimeError("offline Skill recovery requires an offline management service")
        diagnostics = recover_pending_skill_transaction(
            managed_dir=self._managed_dir,
            lockfile_path=self._lockfile_path,
            journal_path=self._journal_path,
            sweep_orphan_staging=True,
        )
        diagnostics = guard_retained_recovery_journal(
            diagnostics,
            journal_path=self._journal_path,
        )
        self._observe_recovery(diagnostics)
        return diagnostics

    def _observe_recovery(self, diagnostics: list[SkillDiagnostic]) -> None:
        blocking = tuple(item for item in diagnostics if item.blocking)
        if blocking:
            self._recovery_required_diagnostics = blocking
            freeze = getattr(self._loader, "freeze_catalog_for_recovery", None)
            if callable(freeze):
                freeze(reason="skill.management.recovery-required")
        elif not self._journal_path.exists():
            self._recovery_required_diagnostics = ()
            clear = getattr(self._loader, "clear_catalog_recovery_freeze", None)
            if callable(clear):
                clear()

    def _recovery_required_result(self, name: str = "") -> InstallResult:
        diagnostics = list(self._recovery_required_diagnostics)
        message = "Managed Skill store requires recovery before mutation"
        if name:
            lockfile = Lockfile.load(self._lockfile_path)
            if not lockfile.mutation_blocked and lockfile.get(name) is not None:
                return self._failure_for_current_install(
                    name=name,
                    message=message,
                    diagnostics=diagnostics,
                )
        return self._failure(
            name=name,
            message=message,
            diagnostics=diagnostics,
        )

    async def _resolve_and_fetch(
        self,
        identifier: str,
        source_id: str,
    ) -> tuple[SourceResolution | None, SkillBundle | None, list[SkillDiagnostic]]:
        diagnostics: list[SkillDiagnostic] = []
        try:
            source_getter = getattr(self._router, "get_source", None)
            source = source_getter(source_id) if callable(source_getter) else None
            resolver = getattr(self._router, "resolve", None)
            source_resolver = getattr(type(source), "resolve", None) if source else None
            modern_resolution = bool(
                callable(resolver)
                and (
                    source is None
                    or (
                        callable(getattr(source, "resolve", None))
                        and source_resolver is not SkillSource.resolve
                    )
                )
            )
            resolution = (
                await resolver(identifier, source_id)
                if callable(resolver)
                else SourceResolution(
                    source_id=source_id,
                    requested_identifier=identifier,
                    canonical_identifier=identifier,
                )
            )
        except SkillSourceFetchError as exc:
            diagnostics.extend(exc.diagnostics)
            return None, None, diagnostics
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    "SOURCE_RESOLUTION_FAILED",
                    str(exc) or type(exc).__name__,
                    phase=DiagnosticPhase.SOURCE,
                    blocking=True,
                )
            )
            return None, None, diagnostics
        if resolution is None:
            code = "SOURCE_UNKNOWN" if source is None else "SOURCE_IDENTIFIER_INVALID"
            diagnostics.append(
                _diagnostic(
                    code,
                    (
                        f"Unknown Skill source {source_id!r}"
                        if source is None
                        else f"Invalid {source_id} Skill identifier {identifier!r}"
                    ),
                    phase=DiagnosticPhase.SOURCE,
                    blocking=True,
                )
            )
            return None, None, diagnostics
        for source_item in getattr(resolution, "diagnostics", ()):
            if isinstance(source_item, SkillDiagnostic):
                diagnostics.append(source_item)
            else:
                diagnostics.append(
                    _diagnostic(
                        str(getattr(source_item, "code", "SOURCE_UNSUPPORTED")),
                        str(
                            getattr(
                                source_item,
                                "message",
                                "Source resolution is unsupported",
                            )
                        ),
                        phase=DiagnosticPhase.SOURCE,
                        blocking=bool(getattr(source_item, "blocking", True)),
                        details=dict(getattr(source_item, "details", {}) or {}),
                    )
                )
        if any(item.blocking for item in diagnostics):
            return resolution, None, diagnostics
        if (
            modern_resolution
            and source_id in {"clawhub", "github"}
            and not resolution.immutable
        ):
            diagnostics.append(
                _diagnostic(
                    "SOURCE_NOT_IMMUTABLE",
                    "The source did not resolve to an immutable artifact revision",
                    phase=DiagnosticPhase.SOURCE,
                    blocking=True,
                    hint=(
                        "Retry using an exact version, tag, commit, or registry "
                        "install reference."
                    ),
                )
            )
            return resolution, None, diagnostics
        try:
            fetch_resolved = getattr(source, "fetch_resolved", None) if source else None
            if callable(fetch_resolved):
                bundle = await fetch_resolved(resolution)
            else:
                bundle = await self._router.fetch(identifier, source_id)
        except SkillSourceFetchError as exc:
            diagnostics.extend(exc.diagnostics)
            return resolution, None, diagnostics
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    "FETCH_FAILED",
                    str(exc) or type(exc).__name__,
                    phase=DiagnosticPhase.FETCH,
                    blocking=True,
                )
            )
            return resolution, None, diagnostics
        if bundle is None:
            diagnostics.append(
                _diagnostic(
                    "FETCH_FAILED",
                    f"Failed to fetch {identifier!r} from {source_id}",
                    phase=DiagnosticPhase.FETCH,
                    blocking=True,
                    hint=(
                        "Verify the exact install reference and retry after any "
                        "rate limit expires."
                    ),
                )
            )
            return resolution, None, diagnostics
        if bundle.resolution is not None:
            resolution = bundle.resolution
            for source_item in resolution.diagnostics:
                if source_item not in diagnostics:
                    diagnostics.append(source_item)
        if any(item.blocking for item in diagnostics):
            return resolution, None, diagnostics
        if (
            modern_resolution
            and source_id in {"clawhub", "github"}
            and not resolution.immutable
        ):
            diagnostics.append(
                _diagnostic(
                    "SOURCE_NOT_IMMUTABLE",
                    "Fetched artifact resolution is no longer immutable",
                    phase=DiagnosticPhase.FETCH,
                    blocking=True,
                )
            )
            return resolution, None, diagnostics
        return resolution, bundle, diagnostics

    def _snapshot_state(
        self,
        name: str,
        target: Path,
        diagnostics: list[SkillDiagnostic],
    ) -> tuple[SkillSpec | None, bool, int, dict[str, Any]]:
        if self._loader is None:
            return None, False, 0, {}
        return self._snapshot_state_from(
            self._loader.snapshot(),
            name,
            target,
            diagnostics,
        )

    @staticmethod
    def _snapshot_state_from(
        snapshot: Any,
        name: str,
        target: Path,
        diagnostics: list[SkillDiagnostic],
    ) -> tuple[SkillSpec | None, bool, int, dict[str, Any]]:
        target_file = (target / "SKILL.md").resolve(strict=False)
        candidates = tuple(getattr(snapshot, "candidates", snapshot.skills))
        candidate = next(
            (
                spec
                for spec in candidates
                if Path(spec.file_path).resolve(strict=False) == target_file
            ),
            None,
        )
        winner = next((spec for spec in snapshot.skills if spec.name == name), None)
        selected = bool(
            candidate is not None
            and winner is not None
            and candidate.instance_id == winner.instance_id
        )
        for error in getattr(snapshot, "diagnostics", snapshot.errors):
            if Path(str(error.path)).resolve(strict=False) == target_file:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_REJECTED",
                        str(error.message),
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        path=str(error.path),
                        details={"kept_previous": bool(error.kept_previous)},
                    )
                )
        return (
            candidate,
            selected,
            int(snapshot.generation),
            dict(getattr(snapshot, "source_digests", {}) or {}),
        )

    async def _reload_and_verify(
        self,
        *,
        name: str,
        target: Path,
        expected_tree: str,
        expected_manifest_digest: str,
        expected_previous_generation: int,
        diagnostics: list[SkillDiagnostic],
    ) -> tuple[SkillSpec | None, bool, int, dict[str, Any]]:
        if self._loader is None:
            return None, False, 0, {}
        verified_state: dict[str, Any] = {}

        def verify(snapshot: Any) -> None:
            diagnostic_start = len(diagnostics)
            candidate, selected, generation, source_digests = self._snapshot_state_from(
                snapshot,
                name,
                target,
                diagnostics,
            )
            verified_state.update(
                candidate=candidate,
                selected=selected,
                generation=generation,
                source_digests=source_digests,
            )
            if candidate is None:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_NOT_DISCOVERED",
                        "The production Skill loader did not discover the installed candidate",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        path=str(target),
                    )
                )
            elif candidate.name != name:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_NAME_MISMATCH",
                        f"Loader reported {candidate.name!r} for installed Skill {name!r}",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                    )
                )
            if candidate is not None and candidate.tree_digest != expected_tree:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_TREE_DIGEST_MISMATCH",
                        "The production Skill catalog built a different resource tree",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "expected": expected_tree,
                            "observed": candidate.tree_digest,
                        },
                    )
                )
            if generation <= expected_previous_generation:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_GENERATION_NOT_ADVANCED",
                        "The production Skill catalog did not build a new generation",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "previous": expected_previous_generation,
                            "observed": generation,
                        },
                    )
                )
            target_file = str((target / "SKILL.md").resolve(strict=False))
            observed_manifest_digest = str(source_digests.get(target_file) or "")
            if observed_manifest_digest != expected_manifest_digest:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_SOURCE_DIGEST_MISMATCH",
                        "The production Skill catalog did not use the installed manifest bytes",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "expected": expected_manifest_digest,
                            "observed": observed_manifest_digest,
                        },
                    )
                )
            actual_tree = compute_tree_sha256(target) if target.exists() else ""
            if actual_tree != expected_tree:
                diagnostics.append(
                    _diagnostic(
                        "POSTFLIGHT_TREE_DRIFT",
                        "Installed files changed while the catalog candidate was built",
                        phase=DiagnosticPhase.STORE,
                        blocking=True,
                        details={"expected": expected_tree, "actual": actual_tree},
                    )
                )
            blocking = [
                item for item in diagnostics[diagnostic_start:] if item.blocking
            ]
            if blocking:
                raise RuntimeError(blocking[-1].message)

        verified_reload = getattr(self._loader, "reload_verified", None)
        if callable(verified_reload):
            reload_result = await _run_postflight_worker(
                verified_reload,
                verify,
                reason="skill.management.postflight",
            )
        else:
            reload_result = await _run_postflight_worker(
                self._loader.reload,
                force=True,
                reason="skill.management.postflight",
            )
            if reload_result.success:
                try:
                    verify(self._loader.snapshot())
                except RuntimeError:
                    pass
        reload_payload = reload_result.to_dict()
        if not reload_result.success:
            diagnostics.append(
                _diagnostic(
                    "CATALOG_RELOAD_FAILED",
                    "The production Skill loader rejected the catalog reload",
                    phase=DiagnosticPhase.CATALOG,
                    blocking=True,
                    details=reload_payload,
                )
            )
        elif not verified_state:
            if reload_result.generation <= expected_previous_generation:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_GENERATION_NOT_ADVANCED",
                        "The production Skill catalog did not build a new generation",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "previous": expected_previous_generation,
                            "observed": reload_result.generation,
                        },
                    )
                )
            else:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_VERIFICATION_NOT_PERFORMED",
                        "The production Skill loader did not expose its candidate for verification",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                    )
                )

        candidate = verified_state.get("candidate")
        selected = bool(verified_state.get("selected", False))
        generation = int(verified_state.get("generation", reload_result.generation) or 0)
        actual_tree = compute_tree_sha256(target) if target.exists() else ""
        if actual_tree != expected_tree:
            if not any(item.code == "POSTFLIGHT_TREE_DRIFT" for item in diagnostics):
                diagnostics.append(
                    _diagnostic(
                        "POSTFLIGHT_TREE_DRIFT",
                        "Installed files changed before the transaction committed",
                        phase=DiagnosticPhase.STORE,
                        blocking=True,
                        details={"expected": expected_tree, "actual": actual_tree},
                    )
                )
        return candidate, selected, generation, reload_payload

    async def _reload_uninstall_and_verify(
        self,
        *,
        target: Path,
        expected_previous_generation: int,
        diagnostics: list[SkillDiagnostic],
    ) -> tuple[int, dict[str, Any]]:
        """Build and verify the catalog without exposing an uncommitted removal."""

        if self._loader is None:
            return 0, {}
        verified_generation = 0
        verification_performed = False
        target_file = (target / "SKILL.md").resolve(strict=False)

        def verify(snapshot: Any) -> None:
            nonlocal verification_performed, verified_generation
            verification_performed = True
            diagnostic_start = len(diagnostics)
            verified_generation = int(getattr(snapshot, "generation", 0) or 0)
            if verified_generation <= expected_previous_generation:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_GENERATION_NOT_ADVANCED",
                        "The production Skill catalog did not build a new generation",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "previous": expected_previous_generation,
                            "observed": verified_generation,
                        },
                    )
                )
            candidates = tuple(getattr(snapshot, "candidates", snapshot.skills))
            if any(
                Path(spec.file_path).resolve(strict=False) == target_file
                for spec in candidates
            ):
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_UNINSTALL_STILL_DISCOVERED",
                        "Uninstalled Skill remains in the production catalog candidate",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        path=str(target),
                    )
                )
            if target.exists():
                diagnostics.append(
                    _diagnostic(
                        "POSTFLIGHT_UNINSTALL_TARGET_PRESENT",
                        "The managed Skill path reappeared before uninstall committed",
                        phase=DiagnosticPhase.STORE,
                        blocking=True,
                        path=str(target),
                    )
                )
            blocking = [
                item for item in diagnostics[diagnostic_start:] if item.blocking
            ]
            if blocking:
                raise RuntimeError(blocking[-1].message)

        verified_reload = getattr(self._loader, "reload_verified", None)
        if callable(verified_reload):
            reload_result = await _run_postflight_worker(
                verified_reload,
                verify,
                reason="skill.management.uninstall.postflight",
            )
        else:
            reload_result = await _run_postflight_worker(
                self._loader.reload,
                force=True,
                reason="skill.management.uninstall.postflight",
            )
            if reload_result.success:
                try:
                    verify(self._loader.snapshot())
                except RuntimeError:
                    pass
        reload_payload = reload_result.to_dict()
        if not reload_result.success:
            diagnostics.append(
                _diagnostic(
                    "CATALOG_RELOAD_FAILED",
                    "The production Skill loader rejected the uninstall catalog reload",
                    phase=DiagnosticPhase.CATALOG,
                    blocking=True,
                    details=reload_payload,
                )
            )
        elif not verification_performed:
            if reload_result.generation <= expected_previous_generation:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_GENERATION_NOT_ADVANCED",
                        "The production Skill catalog did not build a new generation",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                        details={
                            "previous": expected_previous_generation,
                            "observed": reload_result.generation,
                        },
                    )
                )
            else:
                diagnostics.append(
                    _diagnostic(
                        "CATALOG_VERIFICATION_NOT_PERFORMED",
                        "The production Skill loader did not expose its candidate for verification",
                        phase=DiagnosticPhase.CATALOG,
                        blocking=True,
                    )
                )
        if target.exists() and not any(
            item.code == "POSTFLIGHT_UNINSTALL_TARGET_PRESENT" for item in diagnostics
        ):
            diagnostics.append(
                _diagnostic(
                    "POSTFLIGHT_UNINSTALL_TARGET_PRESENT",
                    "The managed Skill path reappeared before uninstall committed",
                    phase=DiagnosticPhase.STORE,
                    blocking=True,
                    path=str(target),
                )
            )
        return verified_generation or int(reload_result.generation), reload_payload

    def _failure(
        self,
        *,
        name: str = "",
        message: str,
        diagnostics: list[SkillDiagnostic],
        resolution: SourceResolution | None = None,
        scan: ScanResult | None = None,
        installed: bool = False,
        tracked: bool | None = None,
        present: bool | None = None,
        drifted: bool = False,
        rollback_performed: bool = False,
        previous_spec: SkillSpec | None = None,
        selected: bool = False,
        generation: int = 0,
        reload_payload: dict[str, Any] | None = None,
        path: str = "",
        install_id: str = "",
        compatibility: SkillCompatibilityState | None = None,
    ) -> InstallResult:
        tracked_state = installed if tracked is None else tracked
        present_state = installed if present is None else present
        if compatibility is None:
            compatibility = (
                SkillCompatibilityState.INSTRUCTION_ONLY
                if tracked_state and previous_spec is not None
                else SkillCompatibilityState.UNSUPPORTED
                if any(item.code == "DIALECT_FIELD_UNSUPPORTED" for item in diagnostics)
                else SkillCompatibilityState.INSTRUCTION_ONLY
            )
        rejected = bool(
            previous_spec is None
            and not tracked_state
            and any(
                item.blocking
                and item.phase
                in {
                    DiagnosticPhase.MANIFEST,
                    DiagnosticPhase.COMPATIBILITY,
                    DiagnosticPhase.CATALOG,
                }
                for item in diagnostics
            )
        )
        lifecycle = lifecycle_for_candidate(
            spec=previous_spec,
            selected=selected,
            tracked=tracked_state,
            present=present_state,
            drifted=drifted,
            rejected=rejected,
            serving_previous=rollback_performed and previous_spec is not None,
            compatibility=compatibility,
            diagnostics=diagnostics,
        )
        return InstallResult(
            success=False,
            name=name,
            message=message,
            scan=scan,
            path=path,
            installed=installed,
            active=(
                lifecycle.selection_state is SkillSelectionState.ACTIVE
                and lifecycle.load_state
                in {SkillLoadState.LOADED, SkillLoadState.SERVING_PREVIOUS}
            ),
            instruction_usable=bool(lifecycle.usable is True),
            install_id=install_id,
            lifecycle=lifecycle,
            resolution=resolution,
            diagnostics=diagnostics,
            reload=reload_payload or {},
            rollback_performed=rollback_performed,
            catalog_generation=generation,
            effective_from="next_start" if self._offline else "next_turn",
        )

    def _failure_for_current_install(
        self,
        *,
        name: str,
        message: str,
        diagnostics: list[SkillDiagnostic],
        resolution: SourceResolution | None = None,
        scan: ScanResult | None = None,
    ) -> InstallResult:
        """Report a failed operation without erasing the existing install truth."""

        target = self._managed_dir / name
        present = target.is_dir() and not target.is_symlink()
        lockfile = Lockfile.load(self._lockfile_path)
        entry = None if lockfile.mutation_blocked else lockfile.get(name)
        tracked = entry is not None
        drifted = False
        if tracked and present and entry is not None:
            expected = entry.tree_sha256 or entry.sha256
            try:
                drifted = bool(expected and _installed_digest(target, entry) != expected)
            except OSError:
                drifted = True

        previous_spec: SkillSpec | None = None
        selected = False
        generation = 0
        if self._loader is not None:
            previous_spec, selected, generation, _ = self._snapshot_state(
                name,
                target,
                diagnostics,
            )
        return self._failure(
            name=name,
            message=message,
            diagnostics=diagnostics,
            resolution=resolution,
            scan=scan,
            installed=present,
            tracked=tracked,
            present=present,
            drifted=drifted,
            previous_spec=previous_spec,
            selected=selected,
            generation=generation,
            path=str(target) if tracked or present else "",
            install_id=entry.install_id if entry is not None else "",
            compatibility=_entry_compatibility(entry),
        )

    async def install(
        self,
        identifier: str,
        source_id: str,
        force: bool = False,
        *,
        replace_source: bool = False,
    ) -> InstallResult:
        """Resolve, validate, commit, live-reload, and postflight one Skill."""

        return await self._install_or_update(
            identifier=identifier,
            source_id=source_id,
            force=force,
            replace_source=replace_source,
            update_name=None,
        )

    async def _install_or_update(
        self,
        *,
        identifier: str,
        source_id: str,
        force: bool,
        replace_source: bool,
        update_name: str | None,
        expected_update: tuple[str, str, str, str, str] | None = None,
    ) -> InstallResult:
        if self._recovery_required_diagnostics:
            recovery_name = update_name or (
                identifier if _SAFE_TRACKED_NAME_RE.fullmatch(identifier) else ""
            )
            return self._recovery_required_result(recovery_name)

        resolution, bundle, diagnostics = await self._resolve_and_fetch(identifier, source_id)
        candidate_compatibility = SkillCompatibilityState.INSTRUCTION_ONLY

        def fail_before_mutation(
            *,
            fallback_name: str,
            message: str,
            scan: ScanResult | None = None,
        ) -> InstallResult:
            if update_name is not None:
                return self._failure_for_current_install(
                    name=update_name,
                    message=message,
                    diagnostics=diagnostics,
                    resolution=resolution,
                    scan=scan,
                )
            if fallback_name:
                current_lock = Lockfile.load(self._lockfile_path)
                if (
                    not current_lock.mutation_blocked
                    and current_lock.get(fallback_name) is not None
                ):
                    return self._failure_for_current_install(
                        name=fallback_name,
                        message=message,
                        diagnostics=diagnostics,
                        resolution=resolution,
                        scan=scan,
                    )
            return self._failure(
                name=fallback_name,
                message=message,
                diagnostics=diagnostics,
                resolution=resolution,
                scan=scan,
            )

        if resolution is None or bundle is None:
            return fail_before_mutation(
                fallback_name="",
                message=diagnostics[-1].message if diagnostics else "Source fetch failed",
            )
        artifact_digest = str(
            getattr(resolution, "artifact_digest", "")
            or getattr(resolution, "expected_digest", "")
            or _bundle_digest(bundle.files)
        )
        transaction_id = uuid.uuid4().hex
        transaction_root = staging_root(self._managed_dir) / transaction_id
        raw_candidate = transaction_root / "_candidate"

        def cleanup_pre_journal_reservation() -> None:
            diagnostics.extend(
                cleanup_staging_transaction_reservation(
                    managed_dir=self._managed_dir,
                    transaction_id=transaction_id,
                )
            )

        try:
            ensure_safe_transaction_roots(self._managed_dir)
            transaction_root.mkdir(parents=True, exist_ok=False)
            _write_bundle(bundle.files, raw_candidate, bundle.file_modes)
            candidate_dir, normalized = _normalize_legacy_manifest(
                raw_candidate,
                bundle=bundle,
                resolution=resolution,
                source_id=source_id,
            )
            expected_name = update_name or _derive_slug(bundle, resolution)
            validation = validate_hub_candidate(
                candidate_dir,
                layer=SkillLayer.MANAGED,
                expected_name=expected_name or None,
            )
            compatibility_diagnostics = _manifest_diagnostics(
                validation.compatibility_diagnostics,
                blocking=False,
                path_override=str(
                    self._managed_dir / candidate_dir.name / "SKILL.md"
                ),
            )
            diagnostics.extend(compatibility_diagnostics)
            if compatibility_diagnostics:
                candidate_compatibility = SkillCompatibilityState.DEGRADED
            if not validation.ok or validation.spec is None:
                diagnostics.extend(_manifest_diagnostics(validation.diagnostics))
                result = fail_before_mutation(
                    fallback_name=candidate_dir.name,
                    message=diagnostics[-1].message,
                )
                cleanup_pre_journal_reservation()
                return result
            spec = validation.spec
            name = spec.name
            if update_name is not None and name != update_name:
                diagnostics.append(
                    _diagnostic(
                        "UPDATE_NAME_CHANGED",
                        f"Update resolved Skill name {name!r}, expected {update_name!r}",
                        phase=DiagnosticPhase.MANIFEST,
                        blocking=True,
                    )
                )
                result = fail_before_mutation(
                    fallback_name=update_name,
                    message=diagnostics[-1].message,
                )
                cleanup_pre_journal_reservation()
                return result
            installed_tree = compute_tree_sha256(candidate_dir)
            legacy_tree = compute_sha256(candidate_dir)
            manifest_digest = hashlib.sha256(
                (candidate_dir / "SKILL.md").read_bytes()
            ).hexdigest()
            source_package_id = _resolution_package_identity(
                source_id,
                resolution,
                identifier,
            )
            scan_result = scan_skill_bundle(_candidate_files(candidate_dir))
            if scan_result.verdict == "dangerous" and not force:
                diagnostics.append(
                    _diagnostic(
                        "SECURITY_SCAN_BLOCKED",
                        f"Security scan found {len(scan_result.findings)} blocking finding(s)",
                        phase=DiagnosticPhase.SECURITY,
                        blocking=True,
                        hint="Review the findings; use force only to accept this scanner verdict.",
                    )
                )
                result = fail_before_mutation(
                    fallback_name=name,
                    message=diagnostics[-1].message,
                    scan=scan_result,
                )
                cleanup_pre_journal_reservation()
                return result
            if normalized:
                diagnostics.append(
                    _diagnostic(
                        "LEGACY_MANIFEST_NORMALIZED",
                        "Legacy manifest spelling or missing portable metadata was normalized",
                        phase=DiagnosticPhase.MANIFEST,
                        severity=DiagnosticSeverity.INFO,
                        details={
                            "artifactDigest": artifact_digest,
                            "installedTreeDigest": installed_tree,
                        },
                    )
                )
        except _CandidateManifestError as exc:
            diagnostics.append(
                _diagnostic(
                    exc.code,
                    str(exc) or type(exc).__name__,
                    phase=DiagnosticPhase.MANIFEST,
                    blocking=True,
                )
            )
            result = fail_before_mutation(
                fallback_name=bundle.name,
                message=diagnostics[-1].message,
            )
            cleanup_pre_journal_reservation()
            return result
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    "CANDIDATE_PREPARATION_FAILED",
                    str(exc) or type(exc).__name__,
                    phase=DiagnosticPhase.ARCHIVE,
                    blocking=True,
                )
            )
            result = fail_before_mutation(
                fallback_name=bundle.name,
                message=diagnostics[-1].message,
            )
            shutil.rmtree(transaction_root, ignore_errors=True)
            return result

        target = self._managed_dir / name
        rollback = rollback_root(self._managed_dir) / transaction_id / name
        journal: SkillTransactionJournal | None = None
        old_snapshot: Any | None = None
        old_entry: LockEntry | None = None
        reload_payload: dict[str, Any] = {}
        generation = 0
        publication_stack = ExitStack()
        publication_barrier: Any | None = None
        durably_committed = False
        success_result: InstallResult | None = None
        try:
            async with self._mutation_lock:
                recovery = recover_pending_skill_transaction(
                    managed_dir=self._managed_dir,
                    lockfile_path=self._lockfile_path,
                    journal_path=self._journal_path,
                )
                recovery = guard_retained_recovery_journal(
                    recovery,
                    journal_path=self._journal_path,
                )
                self._observe_recovery(recovery)
                diagnostics.extend(recovery)
                if any(item.blocking for item in recovery):
                    raise RuntimeError(recovery[-1].message)

                lockfile = Lockfile.load(self._lockfile_path)
                if lockfile.mutation_blocked:
                    raise LockfileMutationBlockedError(
                        self._lockfile_path,
                        lockfile.diagnostics,
                    )
                old_entry = lockfile.get(name)
                if update_name is not None and (
                    old_entry is None
                    or expected_update is None
                    or _update_precondition(old_entry) != expected_update
                ):
                    message = (
                        f"Tracked state for {update_name!r} changed while its update "
                        "artifact was being fetched"
                    )
                    diagnostics.append(
                        _diagnostic(
                            "UPDATE_PRECONDITION_CHANGED",
                            message,
                            phase=DiagnosticPhase.LOCK,
                            blocking=True,
                            hint="Retry the update against the current lock entry.",
                        )
                    )
                    raise RuntimeError(message)
                if path_is_occupied(target) and old_entry is None:
                    raise RuntimeError(
                        f"Refusing to overwrite occupied untracked managed path {target}"
                    )
                if old_entry is not None:
                    if not target.is_dir() or target.is_symlink():
                        raise RuntimeError(f"Tracked Skill path is missing or unsafe: {target}")
                    current_digest = _installed_digest(target, old_entry)
                    expected_digest = old_entry.tree_sha256 or old_entry.sha256
                    if expected_digest and current_digest != expected_digest:
                        raise RuntimeError(
                            f"Local drift detected for {name!r}; update/install was not applied"
                        )
                    old_package_id = _entry_package_identity(old_entry)
                    legacy_clawhub_upgrade = bool(
                        update_name is not None
                        and _is_legacy_clawhub_package_upgrade(
                            old_entry,
                            source_package_id,
                        )
                    )
                    replacing_package = bool(
                        (old_entry.source and old_entry.source != source_id)
                        or (
                            old_package_id
                            and source_package_id
                            and old_package_id != source_package_id
                            and not legacy_clawhub_upgrade
                        )
                    )
                    if replacing_package and not replace_source:
                        raise RuntimeError(
                            "Replacing a same-name installation from a different package "
                            "requires replaceSource=true"
                        )
                    resolved_revision = str(getattr(resolution, "revision", "") or "")
                    prior_revision = old_entry.resolved_revision
                    prior_tree = old_entry.tree_sha256 or old_entry.sha256
                    same_immutable_package_revision = bool(
                        resolution.immutable
                        and old_entry.source == source_id
                        and old_package_id
                        and source_package_id
                        and old_package_id == source_package_id
                        and resolved_revision
                        and resolved_revision == prior_revision
                    )
                    artifact_changed = bool(
                        old_entry.artifact_sha256
                        and artifact_digest
                        and old_entry.artifact_sha256 != artifact_digest
                    )
                    tree_changed = bool(prior_tree and prior_tree != installed_tree)
                    if same_immutable_package_revision and (
                        tree_changed or artifact_changed
                    ):
                        message = (
                            f"Source returned different content for immutable revision "
                            f"{resolved_revision!r} of Skill {name!r}"
                        )
                        diagnostics.append(
                            _diagnostic(
                                "SOURCE_IMMUTABILITY_VIOLATION",
                                message,
                                phase=DiagnosticPhase.SECURITY,
                                blocking=True,
                                hint=(
                                    "Do not trust or publish this revision; retry only after "
                                    "the source exposes a new immutable revision."
                                ),
                                details={
                                    "revision": resolved_revision,
                                    "treeChanged": tree_changed,
                                    "artifactChanged": artifact_changed,
                                },
                            )
                        )
                        raise RuntimeError(message)
                    tree_unchanged = bool(prior_tree and prior_tree == installed_tree)
                    artifact_unchanged = bool(
                        old_entry.artifact_sha256
                        and artifact_digest
                        and old_entry.artifact_sha256 == artifact_digest
                    )
                    if (
                        same_immutable_package_revision
                        and tree_unchanged
                        and artifact_unchanged
                    ):
                        diagnostics.append(
                            _diagnostic(
                                "ALREADY_CURRENT",
                                f"Skill {name!r} already uses the resolved revision",
                                phase=DiagnosticPhase.SOURCE,
                                severity=DiagnosticSeverity.INFO,
                                details={"revision": resolved_revision},
                            )
                        )
                        current_spec, current_selected, generation, _ = (
                            self._snapshot_state(name, target, diagnostics)
                        )
                        if self._loader is None:
                            current_spec = spec
                            current_selected = False
                        lifecycle = lifecycle_for_candidate(
                            spec=current_spec,
                            selected=current_selected,
                            tracked=True,
                            offline=self._loader is None,
                            compatibility=candidate_compatibility,
                            diagnostics=diagnostics,
                        )
                        cleanup_pre_journal_reservation()
                        return InstallResult(
                            success=True,
                            unchanged=True,
                            name=name,
                            message=f"Skill {name!r} is already current",
                            scan=scan_result,
                            path=str(target),
                            installed=True,
                            active=(
                                lifecycle.selection_state is SkillSelectionState.ACTIVE
                                and lifecycle.load_state
                                in {
                                    SkillLoadState.LOADED,
                                    SkillLoadState.SERVING_PREVIOUS,
                                }
                            ),
                            instruction_usable=bool(lifecycle.usable is True),
                            install_id=old_entry.install_id,
                            lifecycle=lifecycle,
                            resolution=resolution,
                            diagnostics=diagnostics,
                            catalog_generation=generation,
                            effective_from=(
                                "next_turn"
                                if self._loader is not None
                                else "next_start"
                            ),
                        )
                else:
                    installed_count = sum(
                        1
                        for child in self._managed_dir.iterdir()
                        if child.is_dir() and not child.name.startswith(".")
                    ) if self._managed_dir.exists() else 0
                    if installed_count >= _MAX_MANAGED_SKILLS:
                        raise RuntimeError(
                            f"Managed Skill layer already contains {_MAX_MANAGED_SKILLS} entries"
                        )

                # Snapshot only after this writer owns the per-managed-root
                # lock.  Capturing it before waiting would let a later failed
                # writer roll the live catalog back past an earlier writer
                # that committed while it was queued.
                if self._loader is not None:
                    old_snapshot = self._loader.snapshot()
                    generation = int(getattr(old_snapshot, "generation", 0) or 0)

                ensure_safe_transaction_roots(self._managed_dir)
                rollback.parent.mkdir(parents=True, exist_ok=True)
                journal = SkillTransactionJournal.prepare(
                    operation="update" if old_entry else "install",
                    managed_dir=self._managed_dir,
                    name=name,
                    target=target,
                    staging=candidate_dir,
                    rollback=rollback,
                    lockfile_path=self._lockfile_path,
                )
                fsync_staging_tree(candidate_dir)
                fsync_directory(rollback.parent)
                fsync_directory(rollback.parent.parent)
                journal.write(self._journal_path)
                if self._loader is not None:
                    barrier_factory = getattr(
                        self._loader,
                        "catalog_publication_barrier",
                        None,
                    )
                    if callable(barrier_factory):
                        publication_barrier = publication_stack.enter_context(
                            barrier_factory(reason="skill.management.publish")
                        )
                guard = (
                    self._loader.mutation_guard(reason="skill.management.publish")
                    if self._loader is not None
                    else nullcontext()
                )
                with guard:
                    validate_transaction_journal_paths(
                        journal,
                        managed_dir=self._managed_dir,
                        lockfile_path=self._lockfile_path,
                    )
                    if path_is_occupied(target):
                        os.replace(target, rollback)
                        fsync_directory(target.parent)
                        fsync_directory(rollback.parent)
                        fsync_directory(rollback.parent.parent)
                        journal.advance("old_moved", self._journal_path)
                    os.replace(candidate_dir, target)
                    fsync_directory(candidate_dir.parent)
                    fsync_directory(target.parent)
                    journal.advance("new_moved", self._journal_path)
                    install_id = (
                        old_entry.install_id
                        if old_entry and old_entry.install_id
                        else uuid.uuid4().hex
                    )
                    meta = bundle.meta or getattr(resolution, "meta", None)
                    entry_extra: dict[str, Any] = {
                        "files": [
                            path.relative_to(target).as_posix()
                            for path in sorted(target.rglob("*"))
                            if path.is_file()
                        ],
                        "file_modes": _installed_file_modes(bundle),
                    }
                    degraded_capabilities = _degraded_capabilities_from_diagnostics(
                        diagnostics
                    )
                    if degraded_capabilities:
                        entry_extra[_DEGRADED_CAPABILITIES_KEY] = degraded_capabilities
                    lockfile.add(
                        name,
                        LockEntry(
                            source=source_id,
                            identifier=identifier,
                            version=str(getattr(resolution, "version", "") or ""),
                            installed_at=time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                            path=str(target),
                            sha256=legacy_tree,
                            license=str(getattr(meta, "license", "") or ""),
                            upstream_url=str(
                                getattr(resolution, "upstream_url", "")
                                or getattr(meta, "homepage", "")
                                or ""
                            ),
                            source_trust=str(
                                getattr(resolution, "trust_state", "")
                                or getattr(meta, "trust_level", "")
                                or "community"
                            ),
                            scan_verdict=scan_result.verdict,
                            scan_strategy=scan_result.strategy,
                            scan_findings=[vars(item) for item in scan_result.findings],
                            install_id=install_id,
                            manifest_name=name,
                            directory_name=name,
                            relative_path=name,
                            requested_identifier=identifier,
                            resolved_identifier=str(
                                getattr(resolution, "canonical_identifier", "") or identifier
                            ),
                            resolved_version=str(
                                getattr(resolution, "version", "") or ""
                            ),
                            resolved_revision=str(
                                getattr(resolution, "revision", "") or ""
                            ),
                            artifact_sha256=artifact_digest,
                            tree_sha256=installed_tree,
                            file_count=sum(1 for path in target.rglob("*") if path.is_file()),
                            total_bytes=sum(
                                path.stat().st_size
                                for path in target.rglob("*")
                                if path.is_file()
                            ),
                            parser_version="community-strict-v1",
                            dialect="instruction-first",
                            source_package_id=source_package_id,
                            accepted_risk_override=bool(
                                force and scan_result.verdict == "dangerous"
                            ),
                            extra=entry_extra,
                        ),
                    )
                    lockfile.save(self._lockfile_path)
                    fsync_directory(self._lockfile_path.parent)
                    journal.advance("lock_written", self._journal_path)

                candidate, selected, generation, reload_payload = (
                    await self._reload_and_verify(
                        name=name,
                        target=target,
                        expected_tree=installed_tree,
                        expected_manifest_digest=manifest_digest,
                        expected_previous_generation=generation,
                        diagnostics=diagnostics,
                    )
                )
                if any(item.blocking for item in diagnostics):
                    raise RuntimeError(diagnostics[-1].message)

                if self._loader is None:
                    candidate = spec
                    selected = False
                lifecycle = lifecycle_for_candidate(
                    spec=candidate,
                    selected=selected,
                    tracked=True,
                    offline=self._loader is None,
                    compatibility=candidate_compatibility,
                    diagnostics=diagnostics,
                )
                active = lifecycle.selection_state is SkillSelectionState.ACTIVE
                instruction_usable = bool(lifecycle.usable is True)
                success_result = InstallResult(
                    success=True,
                    name=name,
                    message=_committed_install_message(
                        name,
                        lifecycle,
                        online=self._loader is not None,
                        updated=old_entry is not None,
                    ),
                    scan=scan_result,
                    path=str(target),
                    installed=True,
                    active=active,
                    instruction_usable=instruction_usable,
                    install_id=install_id,
                    lifecycle=lifecycle,
                    resolution=resolution,
                    diagnostics=diagnostics,
                    reload=reload_payload,
                    catalog_generation=generation,
                    effective_from="next_turn" if self._loader is not None else "next_start",
                )
                if journal is not None:
                    journal.advance("committed", self._journal_path)
                durably_committed = True
                if publication_barrier is not None:
                    publication_barrier.commit()
                try:
                    if journal is not None:
                        validate_transaction_journal_paths(
                            journal,
                            managed_dir=self._managed_dir,
                            lockfile_path=self._lockfile_path,
                        )
                    if rollback.exists():
                        shutil.rmtree(rollback)
                        fsync_directory(rollback.parent)
                    if journal is not None:
                        retained = cleanup_empty_transaction_directories(
                            journal,
                            managed_dir=self._managed_dir,
                            lockfile_path=self._lockfile_path,
                        )
                        if retained:
                            raise OSError(
                                "transaction reservation is not empty: "
                                + ", ".join(str(path) for path in retained)
                            )
                    remove_transaction_journal(self._journal_path)
                except Exception as cleanup_error:
                    diagnostics.append(
                        _diagnostic(
                            "TRANSACTION_CLEANUP_PENDING",
                            f"Committed install cleanup is pending: {cleanup_error}",
                            phase=DiagnosticPhase.STORE,
                            severity=DiagnosticSeverity.WARNING,
                            details={"journal": str(self._journal_path)},
                        )
                    )
                try:
                    log.info(
                        "skill.management_committed",
                        name=name,
                        source=source_id,
                        active=active,
                        generation=generation,
                    )
                except Exception as log_error:
                    diagnostics.append(
                        _diagnostic(
                            "POST_COMMIT_REPORTING_FAILED",
                            f"Skill committed, but commit logging failed: {log_error}",
                            phase=DiagnosticPhase.STORE,
                            severity=DiagnosticSeverity.WARNING,
                        )
                    )
                return success_result
        except BaseException as exc:
            if durably_committed and success_result is not None:
                if publication_barrier is not None:
                    publication_barrier.commit()
                if not isinstance(exc, Exception):
                    raise
                diagnostics.append(
                    _diagnostic(
                        "POST_COMMIT_FINALIZATION_FAILED",
                        f"Skill committed, but finalization failed: {exc}",
                        phase=DiagnosticPhase.STORE,
                        severity=DiagnosticSeverity.WARNING,
                    )
                )
                return success_result
            if isinstance(exc, LockfileMutationBlockedError):
                diagnostics.extend(exc.diagnostics)
            elif not any(item.blocking and item.message == str(exc) for item in diagnostics):
                diagnostics.append(
                    _diagnostic(
                        "STORE_TRANSACTION_FAILED",
                        str(exc) or type(exc).__name__,
                        phase=DiagnosticPhase.STORE,
                        blocking=True,
                    )
                )
            rollback_performed = False
            if journal is not None and path_is_occupied(self._journal_path):
                recovery = recover_pending_skill_transaction(
                    managed_dir=self._managed_dir,
                    lockfile_path=self._lockfile_path,
                    journal_path=self._journal_path,
                )
                recovery = guard_retained_recovery_journal(
                    recovery,
                    journal_path=self._journal_path,
                )
                self._observe_recovery(recovery)
                diagnostics.extend(recovery)
                rollback_performed = any(item.code == "TRANSACTION_RECOVERED" for item in recovery)
            previous_spec: SkillSpec | None = None
            selected = False
            if self._loader is not None:
                restore = getattr(self._loader, "restore_snapshot", None)
                if journal is not None:
                    if callable(restore) and old_snapshot is not None:
                        restore(old_snapshot, reason="skill.management.rollback")
                    elif old_snapshot is not None:
                        try:
                            await asyncio.to_thread(
                                self._loader.reload,
                                force=True,
                                reason="skill.management.rollback",
                            )
                        except Exception:
                            diagnostics.append(
                                _diagnostic(
                                    "CATALOG_ROLLBACK_FAILED",
                                    (
                                        "Previous files were restored but the live catalog "
                                        "could not reload"
                                    ),
                                    phase=DiagnosticPhase.CATALOG,
                                    blocking=True,
                                )
                            )
                previous_spec, selected, generation, _ = self._snapshot_state(
                    name,
                    target,
                    diagnostics,
                )
            present = target.is_dir() and not target.is_symlink()
            tracked = old_entry is not None
            drifted = False
            if tracked and present and old_entry is not None:
                expected = old_entry.tree_sha256 or old_entry.sha256
                try:
                    drifted = bool(
                        expected and _installed_digest(target, old_entry) != expected
                    )
                except OSError:
                    drifted = True
            if journal is None or not path_is_occupied(self._journal_path):
                cleanup_pre_journal_reservation()
            if not isinstance(exc, Exception):
                raise
            return self._failure(
                name=name,
                message=str(exc) or type(exc).__name__,
                diagnostics=diagnostics,
                resolution=resolution,
                scan=scan_result,
                installed=present,
                tracked=tracked,
                present=present,
                drifted=drifted,
                rollback_performed=rollback_performed,
                previous_spec=previous_spec,
                selected=selected,
                generation=generation,
                reload_payload=reload_payload,
                path=str(target) if tracked or present else "",
                install_id=old_entry.install_id if old_entry is not None else "",
                compatibility=_entry_compatibility(old_entry),
            )
        finally:
            publication_stack.close()

    async def update(
        self,
        name: str | None = None,
        *,
        force: bool = False,
    ) -> list[InstallResult]:
        """Update one or all installs from their original immutable source."""

        if self._recovery_required_diagnostics:
            return [self._recovery_required_result(name or "")]

        lockfile = Lockfile.load(self._lockfile_path)
        if lockfile.mutation_blocked:
            diagnostic = list(lockfile.diagnostics)
            return [
                self._failure(
                    name=name or "",
                    message=diagnostic[0].message if diagnostic else "Lockfile is not mutable",
                    diagnostics=diagnostic,
                )
            ]
        skill_names: list[str] = (
            [name] if name is not None else list(lockfile.installed)
        )
        results: list[InstallResult] = []
        for skill_name in skill_names:
            current_lockfile = Lockfile.load(self._lockfile_path)
            if current_lockfile.mutation_blocked:
                diagnostics = list(current_lockfile.diagnostics)
                results.append(
                    self._failure(
                        name=skill_name or "",
                        message=(
                            diagnostics[0].message
                            if diagnostics
                            else "Lockfile is not mutable"
                        ),
                        diagnostics=diagnostics,
                    )
                )
                continue
            entry = current_lockfile.get(skill_name)
            if entry is None:
                results.append(
                    self._failure_for_current_install(
                        name=skill_name,
                        message="Not in lockfile",
                        diagnostics=[
                            _diagnostic(
                                "INSTALL_NOT_TRACKED",
                                f"Skill {skill_name!r} is not tracked",
                                phase=DiagnosticPhase.LOCK,
                                blocking=True,
                            )
                        ],
                    )
                )
                continue
            target = self._managed_dir / skill_name
            expected = entry.tree_sha256 or entry.sha256
            if not target.is_dir() or target.is_symlink():
                results.append(
                    self._failure_for_current_install(
                        name=skill_name,
                        message="Tracked Skill directory is missing",
                        diagnostics=[
                            _diagnostic(
                                "INSTALL_MISSING",
                                f"Tracked path is missing: {target}",
                                phase=DiagnosticPhase.STORE,
                                blocking=True,
                            )
                        ],
                    )
                )
                continue
            if expected and _installed_digest(target, entry) != expected:
                results.append(
                    self._failure_for_current_install(
                        name=skill_name,
                        message="Local drift must be resolved before update",
                        diagnostics=[
                            _diagnostic(
                                "LOCAL_DRIFT",
                                f"Tracked files for {skill_name!r} differ from the lockfile",
                                phase=DiagnosticPhase.STORE,
                                blocking=True,
                            )
                        ],
                    )
                )
                continue
            results.append(
                await self._install_or_update(
                    identifier=(
                        entry.requested_identifier
                        or entry.identifier
                        or entry.resolved_identifier
                    ),
                    source_id=entry.source,
                    force=force,
                    replace_source=False,
                    update_name=skill_name,
                    expected_update=_update_precondition(entry),
                )
            )
        return results

    async def uninstall(
        self,
        name: str,
        *,
        allow_drift: bool = False,
    ) -> InstallResult:
        """Transactionally remove a tracked Skill and reload the live catalog."""

        if self._recovery_required_diagnostics:
            return self._recovery_required_result(name)

        if (
            not isinstance(name, str)
            or not _SAFE_TRACKED_NAME_RE.fullmatch(name)
            or name.endswith(".")
        ):
            return self._failure(
                name=name,
                message=f"Invalid skill name: {name}",
                diagnostics=[
                    _diagnostic(
                        "INVALID_NAME",
                        f"Invalid skill name: {name}",
                        phase=DiagnosticPhase.MANIFEST,
                        blocking=True,
                    )
                ],
            )
        target = self._managed_dir / name
        transaction_id = uuid.uuid4().hex
        rollback = rollback_root(self._managed_dir) / transaction_id / name
        staging = staging_root(self._managed_dir) / transaction_id / name
        diagnostics: list[SkillDiagnostic] = []
        old_snapshot: Any | None = None
        journal: SkillTransactionJournal | None = None
        entry: LockEntry | None = None
        generation = 0
        reload_payload: dict[str, Any] = {}
        publication_stack = ExitStack()
        publication_barrier: Any | None = None
        durably_committed = False
        success_result: InstallResult | None = None
        try:
            async with self._mutation_lock:
                recovery = recover_pending_skill_transaction(
                    managed_dir=self._managed_dir,
                    lockfile_path=self._lockfile_path,
                    journal_path=self._journal_path,
                )
                recovery = guard_retained_recovery_journal(
                    recovery,
                    journal_path=self._journal_path,
                )
                self._observe_recovery(recovery)
                diagnostics.extend(recovery)
                if any(item.blocking for item in recovery):
                    raise RuntimeError(recovery[-1].message)
                ensure_safe_transaction_roots(self._managed_dir)
                lockfile = Lockfile.load(self._lockfile_path)
                if lockfile.mutation_blocked:
                    raise LockfileMutationBlockedError(
                        self._lockfile_path,
                        lockfile.diagnostics,
                    )
                entry = lockfile.get(name)
                if entry is None:
                    raise RuntimeError(f"Skill {name!r} is not tracked")
                if not target.is_dir() or target.is_symlink():
                    raise RuntimeError(f"Tracked Skill directory is missing or unsafe: {target}")
                expected = entry.tree_sha256 or entry.sha256
                actual = _installed_digest(target, entry)
                if expected and expected != actual and not allow_drift:
                    raise RuntimeError(
                        "Local drift detected; pass an explicit drift confirmation to uninstall"
                    )
                # See install/update: the rollback baseline must include every
                # writer that committed before this mutation acquired the
                # shared managed-root lock.
                if self._loader is not None:
                    old_snapshot = self._loader.snapshot()
                    generation = int(getattr(old_snapshot, "generation", 0) or 0)
                rollback.parent.mkdir(parents=True, exist_ok=True)
                journal = SkillTransactionJournal.prepare(
                    operation="uninstall",
                    managed_dir=self._managed_dir,
                    name=name,
                    target=target,
                    staging=staging,
                    rollback=rollback,
                    lockfile_path=self._lockfile_path,
                )
                fsync_directory(rollback.parent)
                fsync_directory(rollback.parent.parent)
                fsync_directory(rollback.parent.parent.parent)
                journal.write(self._journal_path)
                if self._loader is not None:
                    barrier_factory = getattr(
                        self._loader,
                        "catalog_publication_barrier",
                        None,
                    )
                    if callable(barrier_factory):
                        publication_barrier = publication_stack.enter_context(
                            barrier_factory(reason="skill.management.uninstall")
                        )
                guard = (
                    self._loader.mutation_guard(reason="skill.management.uninstall")
                    if self._loader is not None
                    else nullcontext()
                )
                with guard:
                    validate_transaction_journal_paths(
                        journal,
                        managed_dir=self._managed_dir,
                        lockfile_path=self._lockfile_path,
                    )
                    os.replace(target, rollback)
                    fsync_directory(target.parent)
                    fsync_directory(rollback.parent)
                    fsync_directory(rollback.parent.parent)
                    journal.advance("old_moved", self._journal_path)
                    lockfile.remove(name)
                    lockfile.save(self._lockfile_path)
                    fsync_directory(self._lockfile_path.parent)
                    journal.advance("lock_written", self._journal_path)
                if self._loader is not None:
                    generation, reload_payload = await self._reload_uninstall_and_verify(
                        target=target,
                        expected_previous_generation=generation,
                        diagnostics=diagnostics,
                    )
                    if any(item.blocking for item in diagnostics):
                        raise RuntimeError(diagnostics[-1].message)
                lifecycle = SkillLifecycle(
                    install_state=SkillInstallState.MISSING,
                    load_state=(
                        SkillLoadState.VALIDATED_OFFLINE
                        if self._loader is None
                        else SkillLoadState.NOT_DISCOVERED
                    ),
                    selection_state=SkillSelectionState.SHADOWED,
                    compatibility_state=SkillCompatibilityState.INSTRUCTION_ONLY,
                    readiness_state=SkillReadinessState.UNKNOWN,
                    invocation=SkillInvocationCapabilities(sandbox_execution="unknown"),
                )
                success_result = InstallResult(
                    success=True,
                    name=name,
                    message=f"Uninstalled {name!r}",
                    installed=False,
                    active=False,
                    instruction_usable=False,
                    install_id=entry.install_id,
                    lifecycle=lifecycle,
                    diagnostics=diagnostics,
                    reload=reload_payload,
                    catalog_generation=generation,
                    effective_from="next_turn" if self._loader is not None else "next_start",
                )
                journal.advance("committed", self._journal_path)
                durably_committed = True
                if publication_barrier is not None:
                    publication_barrier.commit()
                try:
                    validate_transaction_journal_paths(
                        journal,
                        managed_dir=self._managed_dir,
                        lockfile_path=self._lockfile_path,
                    )
                    shutil.rmtree(rollback)
                    fsync_directory(rollback.parent)
                    retained = cleanup_empty_transaction_directories(
                        journal,
                        managed_dir=self._managed_dir,
                        lockfile_path=self._lockfile_path,
                    )
                    if retained:
                        raise OSError(
                            "transaction reservation is not empty: "
                            + ", ".join(str(path) for path in retained)
                        )
                    remove_transaction_journal(self._journal_path)
                except Exception as cleanup_error:
                    diagnostics.append(
                        _diagnostic(
                            "TRANSACTION_CLEANUP_PENDING",
                            f"Committed uninstall cleanup is pending: {cleanup_error}",
                            phase=DiagnosticPhase.STORE,
                            severity=DiagnosticSeverity.WARNING,
                            details={"journal": str(self._journal_path)},
                        )
                    )
                return success_result
        except BaseException as exc:
            if durably_committed and success_result is not None:
                if publication_barrier is not None:
                    publication_barrier.commit()
                if not isinstance(exc, Exception):
                    raise
                diagnostics.append(
                    _diagnostic(
                        "POST_COMMIT_FINALIZATION_FAILED",
                        f"Skill uninstall committed, but finalization failed: {exc}",
                        phase=DiagnosticPhase.STORE,
                        severity=DiagnosticSeverity.WARNING,
                    )
                )
                return success_result
            if isinstance(exc, LockfileMutationBlockedError):
                diagnostics.extend(exc.diagnostics)
            else:
                diagnostics.append(
                    _diagnostic(
                        "UNINSTALL_FAILED",
                        str(exc) or type(exc).__name__,
                        phase=DiagnosticPhase.STORE,
                        blocking=True,
                    )
                )
            rollback_performed = False
            if journal is not None and path_is_occupied(self._journal_path):
                recovery = recover_pending_skill_transaction(
                    managed_dir=self._managed_dir,
                    lockfile_path=self._lockfile_path,
                    journal_path=self._journal_path,
                )
                recovery = guard_retained_recovery_journal(
                    recovery,
                    journal_path=self._journal_path,
                )
                self._observe_recovery(recovery)
                diagnostics.extend(recovery)
                rollback_performed = any(item.code == "TRANSACTION_RECOVERED" for item in recovery)
            if self._loader is not None:
                restore = getattr(self._loader, "restore_snapshot", None)
                if callable(restore) and old_snapshot is not None:
                    restore(old_snapshot, reason="skill.management.uninstall.rollback")
                elif old_snapshot is not None:
                    try:
                        rollback_reload = await asyncio.to_thread(
                            self._loader.reload,
                            force=True,
                            reason="skill.management.uninstall.rollback",
                        )
                        reload_payload = rollback_reload.to_dict()
                    except Exception:
                        diagnostics.append(
                            _diagnostic(
                                "CATALOG_ROLLBACK_FAILED",
                                (
                                    "Previous files were restored but the live catalog "
                                    "could not reload"
                                ),
                                phase=DiagnosticPhase.CATALOG,
                                blocking=True,
                            )
                        )
                previous_spec, selected, generation, _ = self._snapshot_state(
                    name,
                    target,
                    diagnostics,
                )
            else:
                previous_spec = None
                selected = False
            present = target.is_dir() and not target.is_symlink()
            tracked = entry is not None
            drifted = False
            if tracked and present and entry is not None:
                expected = entry.tree_sha256 or entry.sha256
                try:
                    drifted = bool(expected and _installed_digest(target, entry) != expected)
                except OSError:
                    drifted = True
            if journal is not None and not path_is_occupied(self._journal_path):
                try:
                    retained = cleanup_empty_transaction_directories(
                        journal,
                        managed_dir=self._managed_dir,
                        lockfile_path=self._lockfile_path,
                    )
                    if retained:
                        raise OSError(
                            "transaction reservation is not empty: "
                            + ", ".join(str(path) for path in retained)
                        )
                except Exception as cleanup_error:
                    diagnostics.append(
                        _diagnostic(
                            "TRANSACTION_CLEANUP_PENDING",
                            f"Failed uninstall cleanup is pending: {cleanup_error}",
                            phase=DiagnosticPhase.STORE,
                            severity=DiagnosticSeverity.WARNING,
                        )
                    )
            if not isinstance(exc, Exception):
                raise
            return self._failure(
                name=name,
                message=str(exc) or type(exc).__name__,
                diagnostics=diagnostics,
                installed=present,
                tracked=tracked,
                present=present,
                drifted=drifted,
                rollback_performed=rollback_performed,
                previous_spec=previous_spec,
                selected=selected,
                generation=generation,
                reload_payload=reload_payload,
                path=str(target) if tracked or present else "",
                install_id=entry.install_id if entry is not None else "",
                compatibility=_entry_compatibility(entry),
            )
        finally:
            publication_stack.close()


def build_management_service(
    *,
    router: SourceRouter,
    managed_dir: Path | None = None,
    lockfile_path: Path | None = None,
    loader: Any | None = None,
    journal_path: Path | None = None,
    offline: bool = False,
) -> SkillManagementService:
    """Build the shared service without loading GatewayConfig inside core code."""

    selected_managed = managed_dir or default_managed_skills_dir()
    selected_lock = lockfile_path or default_opensquilla_home() / "skills-lock.json"
    return SkillManagementService(
        router=router,
        managed_dir=selected_managed,
        lockfile_path=selected_lock,
        loader=loader,
        journal_path=journal_path,
        offline=offline,
    )


__all__ = [
    "InstallResult",
    "SkillManagementService",
    "build_management_service",
    "committed_store_read_guard",
    "lifecycle_for_candidate",
    "mutation_lock_for",
]
