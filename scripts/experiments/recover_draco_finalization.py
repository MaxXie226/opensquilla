#!/usr/bin/env python3
"""Offline-only recovery and status reporting for a settled DRACO campaign.

This command never resumes generation or Judge work.  It only replays the
offline finalizer against immutable wave/account evidence, then publishes the
audited artifacts with ``manifest.json`` as the final commit marker.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

STATUS_SCHEMA = "opensquilla.draco.finalization-recovery-status/v1"
MANIFEST_SCHEMA = "opensquilla.draco.campaign-final-manifest/v1"
FORMAL_STAGING_NAME = ".finalizer-recovery"
STATUS_NAME = "finalization-status.json"
ARTIFACT_NAMES = (
    "results.jsonl",
    "trace.jsonl",
    "actual-spend-ledger.jsonl",
    "openrouter-non-byok-campaign-proof.json",
    "audit.json",
    "EXPERIMENT_RESULTS.md",
)
FORMAL_NAMES = frozenset((*ARTIFACT_NAMES, "manifest.json"))
PARTIAL_ROOT_NAMES = FORMAL_NAMES - {"manifest.json"}
WAVE_NAME = re.compile(r"wave-(\d+)\Z")
GATE_SUMMARY_NAME = re.compile(r"wave-(\d+)-summary\.json\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SECRET_ENV_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "token",
    "secret",
    "password",
    "credential",
)
FORBIDDEN_AUDIT_EVENTS = frozenset(
    {
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.__new__",
        "subprocess.Popen",
    }
)


class RecoveryError(RuntimeError):
    """A machine-classifiable fail-closed recovery error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OfflinePolicyError(RecoveryError):
    """The finalizer attempted an operation forbidden during offline replay."""

    def __init__(self, event: str) -> None:
        super().__init__(
            "offline_policy_violation",
            f"offline finalization blocked forbidden audit event: {event}",
        )


@dataclass(frozen=True)
class RecoveryPlan:
    campaign_dir: Path
    archive_dir: Path
    input_path: Path
    results: tuple[Path, ...]
    manifests: tuple[Path, ...]
    groups: tuple[str, ...]
    expected_task_concurrency: int
    expected_judge_concurrency: int
    max_generation_attempts: int
    account_before: Path
    account_after: Path
    account_reconciliation: Path
    runtime_environment: Path
    lock_file: Path
    lock_inode: int
    prior_account_window_dirs: tuple[Path, ...]
    formal_dir: Path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_directory(path: Path, *, label: str) -> Path:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise RecoveryError("missing_path", f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RecoveryError("unsafe_path", f"{label} must be a non-symlink directory")
    if path_stat.st_uid != os.getuid():
        raise RecoveryError("unsafe_owner", f"{label} is not owned by the recovery user")
    return path.resolve()


def _plain_file(path: Path, *, label: str, owner_only: bool = True) -> os.stat_result:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise RecoveryError("missing_path", f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryError("unsafe_path", f"{label} must be a regular non-symlink file")
    if owner_only and path_stat.st_uid != os.getuid():
        raise RecoveryError("unsafe_owner", f"{label} is not owned by the recovery user")
    if path_stat.st_nlink != 1:
        raise RecoveryError("unsafe_link_count", f"{label} must be singly linked")
    return path_stat


def _load_json(path: Path, *, label: str, owner_only: bool = True) -> dict[str, Any]:
    _plain_file(path, label=label, owner_only=owner_only)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("invalid_json", f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RecoveryError("invalid_json", f"{label} must contain one JSON object")
    return value


def _wave_sources(archive_dir: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    waves_dir = _plain_directory(archive_dir / "waves", label="wave archive")
    numbered: list[tuple[int, Path]] = []
    for candidate in waves_dir.iterdir():
        match = WAVE_NAME.fullmatch(candidate.name)
        if match is None:
            continue
        resolved = _plain_directory(candidate, label=f"wave directory {candidate.name}")
        numbered.append((int(match.group(1)), resolved))
    numbered.sort()
    if not numbered:
        raise RecoveryError("missing_waves", "campaign has no archived execution waves")
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise RecoveryError("noncontiguous_waves", "campaign wave numbering is not contiguous")

    results: list[Path] = []
    manifests: list[Path] = []
    for _, wave_dir in numbered:
        wave_results = sorted(wave_dir.glob("draco_ensemble_*.jsonl"))
        wave_manifests = sorted(wave_dir.glob("draco_run_*.manifest.json"))
        if len(wave_results) != 1 or len(wave_manifests) != 1:
            raise RecoveryError(
                "ambiguous_wave_artifacts",
                f"{wave_dir.name} must contain exactly one result and one manifest",
            )
        _plain_file(wave_results[0], label=f"{wave_dir.name} result")
        _plain_file(wave_manifests[0], label=f"{wave_dir.name} manifest")
        results.append(wave_results[0].resolve())
        manifests.append(wave_manifests[0].resolve())
    return tuple(results), tuple(manifests)


def _manifest_execution_contract(
    payload: Mapping[str, Any],
    *,
    manifest_index: int,
) -> tuple[int, int, int]:
    command = payload.get("command")
    parsed = command.get("parsed_args") if isinstance(command, Mapping) else None
    fields = (
        "concurrency",
        "judge_concurrency",
        "generation_max_attempts",
    )
    values: list[int] = []
    for field_name in fields:
        value = parsed.get(field_name) if isinstance(parsed, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RecoveryError(
                "missing_execution_binding",
                f"wave manifest {manifest_index} lacks a positive integer "
                f"command.parsed_args.{field_name} binding",
            )
        values.append(value)
    return values[0], values[1], values[2]


def _manifest_input_and_groups(
    manifests: Sequence[Path],
) -> tuple[Path, tuple[str, ...], tuple[int, int, int]]:
    input_paths: set[Path] = set()
    group_sets: set[tuple[str, ...]] = set()
    execution_contracts: set[tuple[int, int, int]] = set()
    for index, path in enumerate(manifests):
        payload = _load_json(path, label=f"wave manifest {index + 1}")
        args = payload.get("args")
        raw_input = args.get("input") if isinstance(args, Mapping) else None
        if not isinstance(raw_input, str) or not Path(raw_input).is_absolute():
            raise RecoveryError(
                "missing_input_binding",
                f"wave manifest {index + 1} lacks an absolute args.input binding",
            )
        raw_input_path = Path(raw_input)
        _plain_file(
            raw_input_path,
            label=f"wave manifest {index + 1} frozen input",
            owner_only=False,
        )
        input_paths.add(raw_input_path.resolve())

        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise RecoveryError(
                "missing_group_binding",
                f"wave manifest {index + 1} lacks a group list",
            )
        groups = tuple(str(value).strip() for value in raw_groups)
        if any(not value for value in groups) or len(set(groups)) != len(groups):
            raise RecoveryError(
                "invalid_group_binding",
                f"wave manifest {index + 1} has invalid groups",
            )
        group_sets.add(groups)
        execution_contracts.add(
            _manifest_execution_contract(payload, manifest_index=index + 1)
        )
    if len(input_paths) != 1:
        raise RecoveryError("input_binding_changed", "wave manifests bind different inputs")
    if len(group_sets) != 1:
        raise RecoveryError("group_binding_changed", "wave manifests bind different groups")
    if len(execution_contracts) != 1:
        raise RecoveryError(
            "execution_binding_changed",
            "wave manifests bind different execution concurrency or attempt limits",
        )
    input_path = next(iter(input_paths))
    _plain_file(input_path, label="frozen DRACO input", owner_only=False)
    return input_path, next(iter(group_sets)), next(iter(execution_contracts))


def discover_plan(campaign_dir: Path) -> RecoveryPlan:
    campaign = _plain_directory(campaign_dir, label="campaign directory")
    archive = _plain_directory(campaign / "archive", label="campaign archive")
    results, manifests = _wave_sources(archive)
    input_path, groups, execution_contract = _manifest_input_and_groups(manifests)

    account_dir = _plain_directory(archive / "account", label="account archive")
    before = account_dir / "openrouter-account-before.json"
    after = account_dir / "openrouter-account-after.json"
    reconciliation_path = account_dir / "openrouter-account-reconciliation.json"
    runtime = archive / "runtime-environment.json"
    for path, label in (
        (before, "account-before evidence"),
        (after, "account-after evidence"),
        (reconciliation_path, "account reconciliation"),
        (runtime, "runtime environment"),
    ):
        _plain_file(path, label=label)

    reconciliation = _load_json(reconciliation_path, label="account reconciliation")
    if reconciliation.get("settlement_status") != "stable":
        raise RecoveryError(
            "account_not_settled",
            "offline finalization requires a stable account reconciliation",
        )
    raw_lock = reconciliation.get("lock_file")
    raw_inode = reconciliation.get("lock_inode")
    if not isinstance(raw_lock, str) or not Path(raw_lock).is_absolute():
        raise RecoveryError("invalid_lock_binding", "reconciliation lock path is invalid")
    if isinstance(raw_inode, bool) or not isinstance(raw_inode, int) or raw_inode < 1:
        raise RecoveryError("invalid_lock_binding", "reconciliation lock inode is invalid")

    prior_dirs = tuple(
        _plain_directory(path, label=f"prior account window {path.name}")
        for path in sorted(account_dir.glob("prior-aborted-window-*"))
    )
    supported_account_subdirectories = {
        "stable-polls",
        *(path.name for path in prior_dirs),
    }
    unsupported_account_subdirectories = sorted(
        path.name
        for path in account_dir.iterdir()
        if path.is_dir() and path.name not in supported_account_subdirectories
    )
    if unsupported_account_subdirectories:
        raise RecoveryError(
            "unsupported_account_layout",
            "offline recovery requires explicit support for account directories: "
            + ", ".join(unsupported_account_subdirectories),
        )
    return RecoveryPlan(
        campaign_dir=campaign,
        archive_dir=archive,
        input_path=input_path,
        results=results,
        manifests=manifests,
        groups=groups,
        expected_task_concurrency=execution_contract[0],
        expected_judge_concurrency=execution_contract[1],
        max_generation_attempts=execution_contract[2],
        account_before=before.resolve(),
        account_after=after.resolve(),
        account_reconciliation=reconciliation_path.resolve(),
        runtime_environment=runtime.resolve(),
        lock_file=Path(raw_lock),
        lock_inode=raw_inode,
        prior_account_window_dirs=prior_dirs,
        formal_dir=campaign / FORMAL_STAGING_NAME,
    )


def _terminal_gate_budget_exhaustion(archive_dir: Path) -> tuple[int, int]:
    """Return terminal generation/Judge budget exhaustion counts, if recorded.

    Gate summaries are not used to approve publication; the finalizer remains
    authoritative for that.  A terminal exhausted pair is nevertheless
    sufficient to reject an *offline-only* recovery attempt because producing
    the missing successful generation or Judge result would require a new
    model call.
    """

    gates = archive_dir / "gates"
    if not gates.exists():
        return 0, 0
    gates = _plain_directory(gates, label="campaign gate archive")
    summaries: list[tuple[int, Path]] = []
    for candidate in gates.iterdir():
        match = GATE_SUMMARY_NAME.fullmatch(candidate.name)
        if match is None:
            continue
        _plain_file(candidate, label=f"gate summary {candidate.name}")
        summaries.append((int(match.group(1)), candidate))
    if not summaries:
        return 0, 0

    _, latest_path = max(summaries)
    summary = _load_json(latest_path, label=f"terminal gate summary {latest_path.name}")

    def exhaustion_count(name: str) -> int:
        value = summary.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryError(
                "invalid_gate_summary",
                f"terminal gate summary has invalid {name}",
            )
        return value

    generation_count = exhaustion_count("generation_budget_exhausted_pair_count")
    judge_count = exhaustion_count("judge_budget_exhausted_pair_count")
    for count, pairs_name in (
        (generation_count, "generation_budget_exhausted_pairs"),
        (judge_count, "judge_budget_exhausted_pairs"),
    ):
        pairs = summary.get(pairs_name, [])
        if not isinstance(pairs, list) or len(pairs) != count:
            raise RecoveryError(
                "invalid_gate_summary",
                f"terminal gate summary has inconsistent {pairs_name}",
            )
    return generation_count, judge_count


def _require_archived_source(
    *,
    archive_dir: Path,
    raw_path: object,
    raw_digest: object,
    label: str,
) -> None:
    source_path = Path(str(raw_path or ""))
    if not source_path.is_absolute():
        raise RecoveryError("invalid_source_binding", f"{label} path is not absolute")
    try:
        source_resolved = source_path.resolve(strict=True)
        source_resolved.relative_to(archive_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise RecoveryError(
            "invalid_source_binding",
            f"{label} is missing or outside archive/",
        ) from exc
    _plain_file(source_path, label=label)
    if raw_digest != _file_sha256(source_path):
        raise RecoveryError("source_hash_mismatch", f"{label} hash differs")


def _validate_publication_state_bindings(
    *,
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    proof: Mapping[str, Any],
) -> None:
    """Keep execution publication independent from policy/cost audit status."""

    if not all(
        value is True
        for value in (
            manifest.get("execution_pass"),
            audit.get("execution_pass"),
            proof.get("execution_pass"),
        )
    ):
        raise RecoveryError(
            "execution_audit_failed",
            "formal artifacts do not agree on a successful execution",
        )

    policy_values = (
        manifest.get("policy_pass"),
        audit.get("policy_pass"),
        proof.get("policy_pass"),
    )
    if (
        any(not isinstance(value, bool) for value in policy_values)
        or len(set(policy_values)) != 1
        or proof.get("pass") is not policy_values[0]
    ):
        raise RecoveryError(
            "policy_state_mismatch",
            "formal manifest, audit, and account proof policy states differ",
        )

    reconciliations = (
        manifest.get("reconciliation"),
        audit.get("reconciliation"),
        proof.get("reconciliation"),
    )
    if (
        any(not isinstance(value, Mapping) for value in reconciliations)
        or not (reconciliations[0] == reconciliations[1] == reconciliations[2])
    ):
        raise RecoveryError(
            "reconciliation_state_mismatch",
            "formal manifest, audit, and account proof reconciliation states differ",
        )

    audit_pass = audit.get("pass")
    audit_status = audit.get("status")
    if (
        not isinstance(audit_pass, bool)
        or manifest.get("audit_pass") is not audit_pass
        or manifest.get("audit_status") != audit_status
        or audit_status
        != ("passed" if audit_pass else "complete_with_warnings")
    ):
        raise RecoveryError(
            "audit_state_mismatch",
            "formal manifest and audit publication states differ",
        )
    audit_warnings = audit.get("warnings")
    if (
        not isinstance(audit_warnings, list)
        or manifest.get("warnings") != audit_warnings
        or (not audit_pass and not audit_warnings)
    ):
        raise RecoveryError(
            "audit_warning_mismatch",
            "formal manifest and audit warnings differ",
        )

    if audit_pass:
        if policy_values[0] is not True or reconciliations[0].get("pass") is not True:
            raise RecoveryError(
                "invalid_passing_audit",
                "a passing formal audit has failed policy or reconciliation",
            )
        return

    proof_publication_eligible = proof.get("publication_eligible") is True
    reconciliation = reconciliations[0]
    reconciliation_warning_only = (
        policy_values[0] is True
        and proof.get("pass") is True
        and reconciliation.get("pass") is False
        and reconciliation.get("status") == "account_exact_per_request_incomplete"
    )
    if reconciliation_warning_only:
        try:
            gap = Decimal(str(reconciliation.get("gap_usd")))
            tolerance = Decimal(str(reconciliation.get("tolerance_usd")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RecoveryError(
                "invalid_incomplete_reconciliation",
                "warning-only reconciliation has invalid numeric evidence",
            ) from exc
        if (
            not gap.is_finite()
            or not tolerance.is_finite()
            or tolerance < 0
            or tolerance > Decimal("0.000001")
            or abs(gap) > tolerance
        ):
            raise RecoveryError(
                "unsafe_incomplete_reconciliation",
                "warning-only reconciliation is outside the exact account tolerance",
            )
        cost_scope = proof.get("cost_scope")
        windows = (
            cost_scope.get("ledger_window_reconciliation")
            if isinstance(cost_scope, Mapping)
            else None
        )
        if not isinstance(windows, list) or not windows:
            raise RecoveryError(
                "missing_window_reconciliation",
                "warning-only reconciliation lacks account-window ledger evidence",
            )
        incomplete_windows = 0
        for window in windows:
            if not isinstance(window, Mapping):
                raise RecoveryError(
                    "invalid_window_reconciliation",
                    "account-window reconciliation is not an object",
                )
            status = window.get("reconciliation_status")
            unknown = window.get("unknown_cost_request_count")
            non_exact = window.get("non_exact_cost_request_count")
            try:
                window_gap = Decimal(str(window.get("reconciliation_gap_usd")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise RecoveryError(
                    "invalid_window_reconciliation",
                    "account-window reconciliation has an invalid gap",
                ) from exc
            if (
                not window_gap.is_finite()
                or abs(window_gap) > tolerance
                or isinstance(unknown, bool)
                or not isinstance(unknown, int)
                or unknown < 0
                or isinstance(non_exact, bool)
                or not isinstance(non_exact, int)
                or non_exact < 0
            ):
                raise RecoveryError(
                    "unsafe_window_reconciliation",
                    "account-window reconciliation is outside the publication contract",
                )
            incomplete_count = unknown + non_exact
            if status == "exact":
                if incomplete_count:
                    raise RecoveryError(
                        "unsafe_window_reconciliation",
                        "an exact account window contains incomplete requests",
                    )
            elif status == "account_exact_per_request_incomplete":
                if incomplete_count <= 0:
                    raise RecoveryError(
                        "unsafe_window_reconciliation",
                        "an incomplete account window lacks incomplete requests",
                    )
                incomplete_windows += 1
            else:
                raise RecoveryError(
                    "unsafe_window_reconciliation",
                    "account-window reconciliation has a conflicting status",
                )
        if incomplete_windows <= 0:
            raise RecoveryError(
                "missing_incomplete_window",
                "warning-only reconciliation lacks an incomplete account window",
            )
    if proof_publication_eligible and proof.get("pass") is not True:
        if proof.get("status") not in {"policy_failed", "audit_conflict"}:
            raise RecoveryError(
                "invalid_publication_eligibility",
                "failed account proof lacks an allowed audit-only status",
            )
    if not (proof_publication_eligible or reconciliation_warning_only):
        raise RecoveryError(
            "audit_failed",
            "formal audit failure is not explicitly publication eligible",
        )


def _validate_manifest_and_artifacts(
    *,
    campaign_dir: Path,
    container: Path,
    staged: bool,
    plan: RecoveryPlan,
) -> dict[str, Any]:
    archive = _plain_directory(campaign_dir / "archive", label="campaign archive")
    container = _plain_directory(container, label="formal artifact container")
    expected_names = set(FORMAL_NAMES)
    if staged:
        if container.parent != campaign_dir:
            raise RecoveryError(
                "unsafe_staging_location",
                "formal recovery staging is not inside the campaign root",
            )
        if {path.name for path in container.iterdir()} != expected_names:
            raise RecoveryError(
                "invalid_artifact_inventory",
                "formal staging has missing or unexpected files",
            )
    elif {path.name for path in container.iterdir()} != expected_names | {"archive"}:
        raise RecoveryError(
            "invalid_artifact_inventory",
            "published campaign root has missing or unexpected files",
        )

    manifest_path = container / "manifest.json"
    _plain_file(manifest_path, label="formal manifest")
    manifest = _load_json(manifest_path, label="formal manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("status") != "complete":
        raise RecoveryError(
            "invalid_formal_manifest",
            "formal manifest is not a completed DRACO campaign manifest",
        )
    declared_manifest_hash = manifest.get("manifest_sha256")
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("manifest_sha256", None)
    if declared_manifest_hash != _canonical_sha256(manifest_without_hash):
        raise RecoveryError("manifest_hash_mismatch", "formal manifest self-hash differs")
    if manifest.get("groups") != list(plan.groups):
        raise RecoveryError(
            "group_binding_changed",
            "formal manifest groups differ from the archived wave set",
        )
    input_binding = manifest.get("input")
    if (
        not isinstance(input_binding, Mapping)
        or Path(str(input_binding.get("path") or "")).resolve() != plan.input_path
        or input_binding.get("sha256") != _file_sha256(plan.input_path)
    ):
        raise RecoveryError(
            "input_binding_changed",
            "formal manifest input differs from the archived wave set",
        )

    artifact_records = manifest.get("artifacts")
    if not isinstance(artifact_records, Mapping) or set(artifact_records) != set(ARTIFACT_NAMES):
        raise RecoveryError(
            "invalid_artifact_inventory",
            "formal manifest artifact inventory differs",
        )
    for name in ARTIFACT_NAMES:
        artifact_path = container / name
        artifact_stat = _plain_file(artifact_path, label=f"formal artifact {name}")
        record = artifact_records.get(name)
        if (
            not isinstance(record, Mapping)
            or record.get("path") != name
            or record.get("sha256") != _file_sha256(artifact_path)
            or record.get("size_bytes") != artifact_stat.st_size
            or record.get("mode") != oct(stat.S_IMODE(artifact_stat.st_mode))
        ):
            raise RecoveryError(
                "artifact_record_mismatch",
                f"formal artifact record differs for {name}",
            )

    source_results = manifest.get("source_results")
    source_manifests = manifest.get("source_manifests")
    if not isinstance(source_results, list) or not source_results:
        raise RecoveryError("missing_source_evidence", "manifest lacks source results")
    if not isinstance(source_manifests, list) or not source_manifests:
        raise RecoveryError("missing_source_evidence", "manifest lacks source manifests")
    expected_result_paths = [str(path) for path in plan.results]
    actual_result_paths = [
        str(source.get("path") or "") if isinstance(source, Mapping) else ""
        for source in source_results
    ]
    if actual_result_paths != expected_result_paths:
        raise RecoveryError(
            "source_set_changed",
            "formal manifest source results differ from the complete wave set",
        )
    expected_manifest_paths = [str(path) for path in plan.manifests]
    actual_manifest_paths = [
        str(source.get("path") or "") if isinstance(source, Mapping) else ""
        for source in source_manifests
    ]
    if actual_manifest_paths != expected_manifest_paths:
        raise RecoveryError(
            "source_set_changed",
            "formal manifest source manifests differ from the complete wave set",
        )
    for index, source in enumerate(source_results):
        if not isinstance(source, Mapping):
            raise RecoveryError("invalid_source_binding", "source result is not an object")
        _require_archived_source(
            archive_dir=archive,
            raw_path=source.get("path"),
            raw_digest=source.get("sha256"),
            label=f"source result {index}",
        )
    for index, source in enumerate(source_manifests):
        if not isinstance(source, Mapping):
            raise RecoveryError("invalid_source_binding", "source manifest is not an object")
        _require_archived_source(
            archive_dir=archive,
            raw_path=source.get("path"),
            raw_digest=source.get("sha256"),
            label=f"source manifest {index}",
        )
        _require_archived_source(
            archive_dir=archive,
            raw_path=source.get("result_path"),
            raw_digest=source.get("result_sha256"),
            label=f"source manifest result {index}",
        )

    cost_attribution = manifest.get("cost_attribution")
    account_windows = (
        cost_attribution.get("account_windows") if isinstance(cost_attribution, Mapping) else None
    )
    if not isinstance(account_windows, list) or not account_windows:
        raise RecoveryError("missing_account_evidence", "manifest lacks account windows")
    kinds: list[str] = []
    bound_window_sources: list[tuple[str, frozenset[Path]]] = []
    for window_index, window in enumerate(account_windows):
        if not isinstance(window, Mapping):
            raise RecoveryError("invalid_account_evidence", "account window is not an object")
        kind = window.get("kind")
        if kind not in {"current", "prior_aborted", "prior_campaign"}:
            raise RecoveryError("invalid_account_evidence", "account window kind differs")
        kinds.append(str(kind))
        sources = window.get("sources")
        if not isinstance(sources, list) or len(sources) != 4:
            raise RecoveryError(
                "invalid_account_evidence",
                "account window source inventory differs",
            )
        source_paths: set[Path] = set()
        for source_index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                raise RecoveryError(
                    "invalid_account_evidence",
                    "account window source is not an object",
                )
            _require_archived_source(
                archive_dir=archive,
                raw_path=source.get("path"),
                raw_digest=source.get("sha256"),
                label=f"account window {window_index} source {source_index}",
            )
            source_paths.add(Path(str(source.get("path"))).resolve())
        bound_window_sources.append((str(kind), frozenset(source_paths)))
    if kinds.count("current") != 1:
        raise RecoveryError(
            "invalid_account_evidence",
            "manifest must contain exactly one current account window",
        )
    expected_current_sources = frozenset(
        {
            plan.account_before,
            plan.account_after,
            plan.account_reconciliation,
            plan.runtime_environment,
        }
    )
    actual_current_sources = [
        sources for kind, sources in bound_window_sources if kind == "current"
    ]
    if actual_current_sources != [expected_current_sources]:
        raise RecoveryError(
            "account_source_set_changed",
            "formal current account window differs from the settled campaign",
        )
    expected_prior_sources = {
        frozenset(
            (directory / name).resolve()
            for name in (
                "openrouter-account-before.json",
                "openrouter-account-after.json",
                "openrouter-account-reconciliation.json",
                "runtime-environment.json",
            )
        )
        for directory in plan.prior_account_window_dirs
    }
    actual_prior_sources = {
        sources for kind, sources in bound_window_sources if kind == "prior_aborted"
    }
    if actual_prior_sources != expected_prior_sources or "prior_campaign" in kinds:
        raise RecoveryError(
            "unsupported_account_layout",
            "formal account windows differ from the supported settled layout",
        )
    try:
        has_positive_prior = any(
            window.get("kind") == "prior_aborted"
            and Decimal(str(window.get("usage_delta_usd"))) > 0
            for window in account_windows
            if isinstance(window, Mapping)
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RecoveryError(
            "invalid_account_evidence",
            "formal prior account-window delta is invalid",
        ) from exc
    if has_positive_prior and (
        cost_attribution.get("attribution_precision")
        != "multi-window-counter-exact-campaign-attribution-unproven"
        or cost_attribution.get("campaign_attributable_exact") is not False
    ):
        raise RecoveryError(
            "invalid_account_attribution",
            "positive prior-aborted spend has unsafe attribution semantics",
        )

    audit = _load_json(container / "audit.json", label="formal audit")
    proof = _load_json(
        container / "openrouter-non-byok-campaign-proof.json",
        label="formal non-BYOK proof",
    )
    _validate_publication_state_bindings(
        manifest=manifest,
        audit=audit,
        proof=proof,
    )

    raw_result_count = manifest.get("result_count")
    raw_task_count = manifest.get("task_count")
    groups = manifest.get("groups")
    if (
        isinstance(raw_result_count, bool)
        or not isinstance(raw_result_count, int)
        or raw_result_count < 1
        or isinstance(raw_task_count, bool)
        or not isinstance(raw_task_count, int)
        or raw_task_count < 1
        or not isinstance(groups, list)
        or not groups
        or raw_result_count != raw_task_count * len(groups)
    ):
        raise RecoveryError(
            "invalid_result_count",
            "formal result count does not cover every task/group pair",
        )
    result_lines = 0
    with (container / "results.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecoveryError(
                    "invalid_results_jsonl",
                    "formal results contain invalid JSON",
                ) from exc
            if not isinstance(row, dict):
                raise RecoveryError(
                    "invalid_results_jsonl",
                    "formal result row is not an object",
                )
            result_lines += 1
    if result_lines != raw_result_count:
        raise RecoveryError(
            "invalid_result_count",
            "formal results line count differs from the manifest",
        )
    return manifest


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_manifest_last(plan: RecoveryPlan) -> dict[str, Any]:
    """Validate and publish finalizer output, committing ``manifest.json`` last."""

    formal_dir = plan.formal_dir
    manifest = _validate_manifest_and_artifacts(
        campaign_dir=plan.campaign_dir,
        container=formal_dir,
        staged=True,
        plan=plan,
    )
    root_names = {path.name for path in plan.campaign_dir.iterdir()}
    if root_names != {"archive", formal_dir.name}:
        raise RecoveryError(
            "unexpected_campaign_root",
            "campaign root must contain only archive/ and formal staging",
        )
    for name in FORMAL_NAMES:
        target = plan.campaign_dir / name
        if target.exists() or target.is_symlink():
            raise RecoveryError(
                "refuse_overwrite",
                f"refusing to overwrite campaign root artifact: {name}",
            )

    pending_manifest = plan.campaign_dir / f".manifest.pending-{os.getpid()}"
    if pending_manifest.exists() or pending_manifest.is_symlink():
        raise RecoveryError(
            "unsafe_pending_manifest",
            "manifest publication staging path already exists",
        )
    moved: list[str] = []
    manifest_location = formal_dir / "manifest.json"
    try:
        for name in ARTIFACT_NAMES:
            os.replace(formal_dir / name, plan.campaign_dir / name)
            moved.append(name)
        os.replace(manifest_location, pending_manifest)
        manifest_location = pending_manifest
        formal_dir.rmdir()
        _fsync_directory(plan.campaign_dir)
        os.replace(pending_manifest, plan.campaign_dir / "manifest.json")
        manifest_location = plan.campaign_dir / "manifest.json"
        _fsync_directory(plan.campaign_dir)
    except BaseException:
        formal_dir.mkdir(mode=0o700, exist_ok=True)
        if manifest_location.exists() or manifest_location.is_symlink():
            os.replace(manifest_location, formal_dir / "manifest.json")
        for name in reversed(moved):
            target = plan.campaign_dir / name
            if target.exists() or target.is_symlink():
                os.replace(target, formal_dir / name)
        _fsync_directory(plan.campaign_dir)
        raise
    return manifest


def _status_payload(
    *,
    campaign_dir: Path,
    state: str,
    reason_code: str,
    plan: RecoveryPlan | None = None,
    manifest: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "observed_at": _utc_now(),
        "state": state,
        "reason_code": reason_code,
        "campaign_dir": str(campaign_dir.resolve(strict=False)),
        "offline_only": True,
        "model_requests_allowed": False,
        "root_manifest_present": (campaign_dir / "manifest.json").is_file(),
    }
    if plan is not None:
        payload.update(
            {
                "source_result_count": len(plan.results),
                "source_manifest_count": len(plan.manifests),
                "groups": list(plan.groups),
                "account_settlement_status": "stable",
                "formal_staging_present": plan.formal_dir.is_dir(),
            }
        )
    if manifest is not None:
        payload.update(
            {
                "result_count": manifest.get("result_count"),
                "manifest_sha256": manifest.get("manifest_sha256"),
                "finalizer_version": manifest.get("finalizer_version"),
                "execution_pass": manifest.get("execution_pass"),
                "audit_pass": manifest.get("audit_pass"),
                "audit_status": manifest.get("audit_status"),
                "policy_pass": manifest.get("policy_pass"),
                "reconciliation": manifest.get("reconciliation"),
            }
        )
    if error is not None:
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }
    return payload


def inspect_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign = campaign_dir.resolve(strict=False)
    try:
        campaign = _plain_directory(campaign, label="campaign directory")
        root_names = {path.name for path in campaign.iterdir()}
        if "manifest.json" in root_names:
            plan = discover_plan(campaign)
            manifest = _validate_manifest_and_artifacts(
                campaign_dir=campaign,
                container=campaign,
                staged=False,
                plan=plan,
            )
            return _status_payload(
                campaign_dir=campaign,
                state="complete",
                reason_code="published_manifest_valid",
                manifest=manifest,
            )
        partial = sorted(root_names & PARTIAL_ROOT_NAMES)
        if partial:
            raise RecoveryError(
                "partial_root_publication",
                "campaign root has formal artifacts without manifest.json: " + ", ".join(partial),
            )
        unexpected = root_names - {"archive", FORMAL_STAGING_NAME}
        if unexpected:
            raise RecoveryError(
                "unexpected_campaign_root",
                "campaign root contains unexpected entries: " + ", ".join(sorted(unexpected)),
            )
        plan = discover_plan(campaign)
        if plan.formal_dir.exists() or plan.formal_dir.is_symlink():
            manifest = _validate_manifest_and_artifacts(
                campaign_dir=campaign,
                container=plan.formal_dir,
                staged=True,
                plan=plan,
            )
            return _status_payload(
                campaign_dir=campaign,
                state="publish_ready",
                reason_code="valid_formal_staging_present",
                plan=plan,
                manifest=manifest,
            )
        generation_exhausted, judge_exhausted = _terminal_gate_budget_exhaustion(
            plan.archive_dir
        )
        if generation_exhausted or judge_exhausted:
            error = RecoveryError(
                "model_attempt_budget_exhausted",
                "offline finalization cannot repair "
                f"{generation_exhausted} generation and {judge_exhausted} Judge "
                "pair(s) whose model-attempt budgets are exhausted",
            )
            status = _status_payload(
                campaign_dir=campaign,
                state="blocked",
                reason_code=error.code,
                plan=plan,
                error=error,
            )
            status["generation_budget_exhausted_pair_count"] = generation_exhausted
            status["judge_budget_exhausted_pair_count"] = judge_exhausted
            return status
        return _status_payload(
            campaign_dir=campaign,
            state="ready",
            reason_code="settled_evidence_ready_for_offline_finalizer",
            plan=plan,
        )
    except RecoveryError as exc:
        return _status_payload(
            campaign_dir=campaign,
            state="blocked",
            reason_code=exc.code,
            error=exc,
        )
    except Exception as exc:
        return _status_payload(
            campaign_dir=campaign,
            state="blocked",
            reason_code="inspection_failed",
            error=exc,
        )


def _write_status(archive_dir: Path, payload: Mapping[str, Any]) -> Path:
    archive = _plain_directory(archive_dir, label="campaign archive")
    target = archive / STATUS_NAME
    if target.is_symlink():
        raise RecoveryError("unsafe_status_path", "status path must not be a symlink")
    if target.exists():
        _plain_file(target, label="existing finalization status")
    temporary = archive / f".{STATUS_NAME}.tmp-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise RecoveryError("unsafe_status_path", "temporary status path already exists")
    encoded = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    _fsync_directory(archive)
    return target


@contextmanager
def _exclusive_original_lock(plan: RecoveryPlan):
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(plan.lock_file, flags)
    except OSError as exc:
        raise RecoveryError(
            "lock_unavailable",
            "cannot open the account-window lock file",
        ) from exc
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
            or lock_stat.st_ino != plan.lock_inode
        ):
            raise RecoveryError(
                "lock_binding_changed",
                "account-window lock no longer matches the settled reconciliation",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError(
                "lock_busy",
                "another benchmark currently owns the account-window lock",
            ) from exc
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _scrub_secret_environment() -> None:
    for name in tuple(os.environ):
        lowered = name.casefold()
        if any(marker in lowered for marker in SECRET_ENV_MARKERS):
            os.environ.pop(name, None)
    os.environ["OPENSQUILLA_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["NO_PROXY"] = ""
    os.environ["no_proxy"] = ""
    for name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        os.environ.pop(name, None)


def _install_offline_audit_guard() -> None:
    def guard(event: str, _args: tuple[Any, ...]) -> None:
        if event in FORBIDDEN_AUDIT_EVENTS or event.startswith("socket.connect"):
            raise OfflinePolicyError(event)

    sys.addaudithook(guard)


def _load_finalizer(path: Path) -> ModuleType:
    _plain_file(path, label="DRACO finalizer")
    repo_root = path.resolve().parents[2]
    source_root = repo_root / "src"
    if source_root.is_dir():
        sys.path.insert(0, str(source_root))
    spec = importlib.util.spec_from_file_location("_draco_offline_recovery_finalizer", path)
    if spec is None or spec.loader is None:
        raise RecoveryError("finalizer_load_failed", "cannot load the DRACO finalizer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "build_parser", None)) or not callable(
        getattr(module, "run_finalization", None)
    ):
        raise RecoveryError(
            "finalizer_api_mismatch",
            "DRACO finalizer does not expose build_parser/run_finalization",
        )
    return module


def _run_finalizer(
    *,
    plan: RecoveryPlan,
    finalizer_path: Path,
    lock_fd: int,
) -> Mapping[str, Any]:
    _scrub_secret_environment()
    _install_offline_audit_guard()
    module = _load_finalizer(finalizer_path)
    argv = [
        "--input",
        str(plan.input_path),
        "--account-before",
        str(plan.account_before),
        "--account-after",
        str(plan.account_after),
        "--account-reconciliation",
        str(plan.account_reconciliation),
        "--runtime-environment",
        str(plan.runtime_environment),
        "--lock-file",
        str(plan.lock_file),
        "--lock-fd",
        str(lock_fd),
        "--output-dir",
        str(plan.formal_dir),
        "--groups",
        ",".join(plan.groups),
        "--max-generation-attempts",
        str(plan.max_generation_attempts),
        "--expected-task-concurrency",
        str(plan.expected_task_concurrency),
        "--expected-judge-concurrency",
        str(plan.expected_judge_concurrency),
    ]
    for prior_dir in plan.prior_account_window_dirs:
        argv.extend(("--prior-account-window-dir", str(prior_dir)))
    for result in plan.results:
        argv.extend(("--result", str(result)))
    for manifest in plan.manifests:
        argv.extend(("--manifest", str(manifest)))
    args = module.build_parser().parse_args(argv)
    result = module.run_finalization(args)
    if not isinstance(result, Mapping) or result.get("status") != "complete":
        raise RecoveryError(
            "finalizer_result_invalid",
            "offline finalizer did not return a completed manifest",
        )
    return result


_FinalizerRunner = Callable[[RecoveryPlan, int], Mapping[str, Any]]


def recover_campaign(
    campaign_dir: Path,
    *,
    finalizer_path: Path,
    _runner: _FinalizerRunner | None = None,
) -> dict[str, Any]:
    """Run or resume offline finalization and publish the manifest last."""

    initial = inspect_campaign(campaign_dir)
    if initial["state"] == "complete":
        return initial
    if initial["state"] not in {"ready", "publish_ready"}:
        raise RecoveryError(
            str(initial.get("reason_code") or "recovery_blocked"),
            str((initial.get("error") or {}).get("message") or "recovery is blocked"),
        )
    plan = discover_plan(campaign_dir)
    finalizing = _status_payload(
        campaign_dir=plan.campaign_dir,
        state="finalizing",
        reason_code=(
            "publishing_existing_formal_staging"
            if initial["state"] == "publish_ready"
            else "running_offline_finalizer"
        ),
        plan=plan,
    )
    _write_status(plan.archive_dir, finalizing)

    try:
        with _exclusive_original_lock(plan) as lock_fd:
            if initial["state"] == "ready":
                if _runner is None:
                    _run_finalizer(
                        plan=plan,
                        finalizer_path=finalizer_path,
                        lock_fd=lock_fd,
                    )
                else:
                    result = _runner(plan, lock_fd)
                    if not isinstance(result, Mapping) or result.get("status") != "complete":
                        raise RecoveryError(
                            "finalizer_result_invalid",
                            "offline finalizer did not return a completed manifest",
                        )
            manifest = publish_manifest_last(plan)
    except Exception as exc:
        reason_code = exc.code if isinstance(exc, RecoveryError) else "finalizer_failed"
        failed = _status_payload(
            campaign_dir=plan.campaign_dir,
            state="failed",
            reason_code=reason_code,
            plan=plan,
            error=exc,
        )
        try:
            _write_status(plan.archive_dir, failed)
        except Exception:
            pass
        raise
    completed = _status_payload(
        campaign_dir=plan.campaign_dir,
        state="complete",
        reason_code="published_manifest_valid",
        plan=plan,
        manifest=manifest,
    )
    try:
        _write_status(plan.archive_dir, completed)
    except Exception as exc:
        completed["status_persistence"] = {
            "persisted": False,
            "type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
    return completed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status",
        help="Inspect finalization readiness without changing the campaign.",
    )
    status_parser.add_argument("campaign_dir", type=Path)
    status_parser.add_argument(
        "--write-status",
        action="store_true",
        help=f"Atomically persist the status under archive/{STATUS_NAME}.",
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help="Replay the finalizer offline and publish manifest.json last.",
    )
    recover_parser.add_argument("campaign_dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        payload = inspect_campaign(args.campaign_dir)
        if args.write_status:
            try:
                campaign = _plain_directory(args.campaign_dir, label="campaign directory")
                _write_status(campaign / "archive", payload)
            except Exception as exc:
                payload["status_write_error"] = {
                    "type": type(exc).__name__,
                    "reason_code": (
                        exc.code if isinstance(exc, RecoveryError) else "status_write_failed"
                    ),
                    "message": str(exc)[:1000],
                }
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 2
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["state"] in {"ready", "publish_ready", "complete"} else 2

    try:
        payload = recover_campaign(
            args.campaign_dir,
            finalizer_path=Path(__file__).resolve().with_name("finalize_draco_campaign.py"),
        )
    except Exception as exc:
        code = exc.code if isinstance(exc, RecoveryError) else "finalizer_failed"
        payload = _status_payload(
            campaign_dir=args.campaign_dir,
            state="failed",
            reason_code=code,
            error=exc,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
