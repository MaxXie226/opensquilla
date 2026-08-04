#!/usr/bin/env python3
"""Serial, restart-safe controller for the P0/P0.5 DRACO-mini matrix.

This controller deliberately contains no credential handling and never calls a model
unless the explicit ``run`` command is used.  The formal launcher remains the
sole owner of preflight, the OpenRouter account window, generation/Judge calls,
settlement, and finalization.

The campaign is intentionally split into two freezes:

* campaign-plan.json freezes the matrix and every derivation/gating algorithm;
* derived-plan.json is written after the first complete E0 and freezes the
  Analyzer replay artifact, the type-7 p99-derived P0.5-06 values, and the
  authenticated offline-effect decisions before any dependent arm is launched.

The controller uses the production main runner for dry replay, the production
request-budget and compatibility helpers for request-visible projections, and
a separately hash-frozen terminal reporter.  Template ``TODO_FREEZE_*`` values
must be replaced before the live ``run`` command is accepted.
"""

from __future__ import annotations

import argparse
import ast
import copy
import fcntl
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLAN_SCHEMA = "opensquilla.draco-p0-p05-campaign-plan/v1"
STATUS_SCHEMA = "opensquilla.draco-p0-p05-controller-status/v1"
ANALYZER_ARTIFACT_SCHEMA = "opensquilla.draco-frozen-task-analysis-source/v1"
DERIVED_SCHEMA = "opensquilla.draco-p0-p05-derived-plan/v1"
RECEIPT_SCHEMA = "opensquilla.draco-offline-effect-receipt/v1"
AGGREGATOR_PROMPT_SCHEMA = "opensquilla.router-dynamic-aggregator-prompt/v1"
AGGREGATOR_PROMPT_VERSIONS = frozenset(
    {
        "aggregator-v1-current",
        "aggregator-v2-verify-first",
        "aggregator-v3-preserve-best",
    }
)
EXPECTED_TASK_COUNT = 10
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLACEHOLDER_PREFIX = "TODO_"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PHYSICAL_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PRODUCTION_BUDGET_GATE_EXPERIMENTS = frozenset({"P0.5-10", "P0.5-38", "P0.5-39"})
REQUIRED_REPLICATE_ARMS = frozenset(
    {
        "common-E0-R2",
        "common-E0-R3",
        "P0.5-11-E1-R1",
        "P0.5-11-E1-R2",
        "P0.5-11-E1-R3",
        "P0.5-36-E1-R1",
        "P0.5-36-E1-R2",
        "P0.5-36-E1-R3",
    }
)
RUN_DECISIONS = frozenset(
    {
        "run",
        "run_required_replicate",
        "run_conservative_projection_uncertain",
        "run_conservative_unproven_budget_binding",
    }
)


class ControllerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_regular_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ControllerError(f"expected regular non-symlink file: {path}")


def load_json(path: Path) -> Any:
    require_regular_file(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot load JSON {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def run_text(command: Sequence[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ControllerError(f"command failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def git_identity(snapshot: Path) -> dict[str, str]:
    return {
        "commit": run_text(["git", "rev-parse", "HEAD"], cwd=snapshot),
        "tree": run_text(["git", "rev-parse", "HEAD^{tree}"], cwd=snapshot),
        "status": run_text(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=snapshot,
        ),
    }


def require_frozen_sha256(
    value: Any,
    *,
    label: str,
    allow_placeholders: bool,
) -> str:
    normalized = str(value or "").strip()
    if allow_placeholders and normalized.startswith(PLACEHOLDER_PREFIX):
        return normalized
    if SHA256_RE.fullmatch(normalized) is None:
        raise ControllerError(f"{label} must be one frozen lowercase SHA-256")
    return normalized


def require_frozen_text(
    value: Any,
    *,
    label: str,
    allow_placeholders: bool,
) -> str:
    normalized = str(value or "").strip()
    if allow_placeholders and normalized.startswith(PLACEHOLDER_PREFIX):
        return normalized
    if not normalized:
        raise ControllerError(f"{label} must be frozen")
    return normalized


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def set_path(target: dict[str, Any], path: Sequence[str], value: Any) -> None:
    if not path or any(not isinstance(item, str) or not item for item in path):
        raise ControllerError(f"invalid JSON path: {path!r}")
    cursor = target
    for key in path[:-1]:
        existing = cursor.get(key)
        if existing is None:
            existing = {}
            cursor[key] = existing
        if not isinstance(existing, dict):
            raise ControllerError(f"JSON path crosses a scalar at {key!r}")
        cursor = existing
    cursor[path[-1]] = copy.deepcopy(value)


@dataclass(frozen=True)
class Arm:
    arm_id: str
    experiment_id: str
    directory_name: str
    variant: str
    replicate: int
    analyzer_mode: str
    override: dict[str, Any]
    dynamic: dict[str, Any] | None
    wire_gate: str | None
    output_name: str
    control_arm_id: str | None


def expand_arms(plan: Mapping[str, Any]) -> list[Arm]:
    run_id = str(plan["run_id"])
    arms: list[Arm] = []
    for row in plan["common_e0"]:
        arm_id = str(row["arm_id"])
        arms.append(
            Arm(
                arm_id=arm_id,
                experiment_id="common-E0",
                directory_name="common",
                variant=str(row["variant"]),
                replicate=int(row["replicate"]),
                analyzer_mode=str(row["analyzer_mode"]),
                override=copy.deepcopy(row.get("override") or {}),
                dynamic=None,
                wire_gate=None,
                output_name=f"{arm_id}-{run_id}",
                control_arm_id=None,
            )
        )
    for experiment in plan["experiments"]:
        experiment_id = str(experiment["id"])
        directory_name = str(experiment["directory_name"])
        for variant in experiment.get("variants", []):
            repetitions = int(variant.get("replicates", 1))
            for replicate in range(1, repetitions + 1):
                suffix = f"-R{replicate}" if repetitions > 1 else ""
                arm_id = f"{experiment_id}-{variant['id']}{suffix}"
                control_ids = experiment.get("control_arm_ids")
                if isinstance(control_ids, list) and repetitions > 1:
                    if len(control_ids) != repetitions:
                        raise ControllerError(
                            f"{experiment_id} control_arm_ids must match repetitions"
                        )
                    default_control = control_ids[replicate - 1]
                else:
                    default_control = experiment.get("control_arm_id", "common-E0-R1")
                control = variant.get("control_arm_id") or default_control
                arms.append(
                    Arm(
                        arm_id=arm_id,
                        experiment_id=experiment_id,
                        directory_name=directory_name,
                        variant=str(variant["id"]),
                        replicate=replicate,
                        analyzer_mode=str(variant.get("analyzer_mode", "frozen_replay")),
                        override=copy.deepcopy(variant.get("override") or {}),
                        dynamic=copy.deepcopy(variant.get("dynamic")),
                        wire_gate=(
                            str(variant["wire_gate"])
                            if variant.get("wire_gate") is not None
                            else None
                        ),
                        output_name=f"{arm_id}-{run_id}",
                        control_arm_id=str(control) if control is not None else None,
                    )
                )
    return arms


def all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from all_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from all_strings(item)


def validate_plan(plan: Mapping[str, Any], *, allow_placeholders: bool) -> list[Arm]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ControllerError("campaign plan schema differs")
    if plan.get("benchmark", {}).get("task_count") != EXPECTED_TASK_COUNT:
        raise ControllerError("campaign must freeze exactly 10 DRACO-mini tasks")
    task_ids = plan.get("benchmark", {}).get("task_ids")
    if (
        not isinstance(task_ids, list)
        or len(task_ids) != EXPECTED_TASK_COUNT
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != EXPECTED_TASK_COUNT
    ):
        raise ControllerError("benchmark must freeze ten unique non-empty task ids")
    if plan.get("benchmark", {}).get("groups") != ["G1"]:
        raise ControllerError("campaign must run G1 only")
    execution = plan.get("execution")
    if not isinstance(execution, Mapping):
        raise ControllerError("execution contract is missing")
    if execution.get("serial_arms") is not True:
        raise ControllerError("arms must be serialized")
    if execution.get("task_concurrency") != 6:
        raise ControllerError("task concurrency must be exactly 6")
    if execution.get("judge_concurrency") != 6:
        raise ControllerError("Judge concurrency must be exactly 6")
    if execution.get("generation_max_attempts") != 3:
        raise ControllerError("generation max attempts must be exactly 3")

    freeze = plan.get("freeze")
    if not isinstance(freeze, Mapping):
        raise ControllerError("freeze contract is missing")
    require_frozen_text(
        freeze.get("snapshot_commit"),
        label="freeze.snapshot_commit",
        allow_placeholders=allow_placeholders,
    )
    require_frozen_text(
        freeze.get("snapshot_tree"),
        label="freeze.snapshot_tree",
        allow_placeholders=allow_placeholders,
    )
    inputs = freeze.get("inputs")
    sources = freeze.get("sources")
    registry = freeze.get("model_registry")
    ranking = freeze.get("ranking_config")
    for label, section in (
        ("freeze.inputs", inputs),
        ("freeze.sources", sources),
        ("freeze.model_registry", registry),
        ("freeze.ranking_config", ranking),
    ):
        if not isinstance(section, Mapping):
            raise ControllerError(f"{label} is missing")
    assert isinstance(inputs, Mapping)
    assert isinstance(sources, Mapping)
    assert isinstance(registry, Mapping)
    assert isinstance(ranking, Mapping)
    for key in ("benchmark_input_raw_sha256", "reference_config_raw_sha256"):
        require_frozen_sha256(
            inputs.get(key),
            label=f"freeze.inputs.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "launcher_raw_sha256",
        "controller_raw_sha256",
        "reporter_raw_sha256",
        "main_runner_raw_sha256",
        "resume_runner_raw_sha256",
    ):
        require_frozen_sha256(
            sources.get(key),
            label=f"freeze.sources.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "raw_sha256",
        "full_canonical_sha256",
        "formal_canonical_sha256",
        "full_identities_sha256",
        "formal_identities_sha256",
    ):
        require_frozen_sha256(
            registry.get(key),
            label=f"freeze.model_registry.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "full_snapshot_version",
        "formal_snapshot_version",
    ):
        require_frozen_text(
            registry.get(key),
            label=f"freeze.model_registry.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in ("model_count", "full_model_count", "formal_model_count"):
        if registry.get(key) != 79:
            raise ControllerError(f"freeze.model_registry.{key} must contain exactly 79 models")
    for key in (
        "raw_sha256",
        "packaged_canonical_sha256",
        "formal_canonical_sha256",
    ):
        require_frozen_sha256(
            ranking.get(key),
            label=f"freeze.ranking_config.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "packaged_schema_version",
        "packaged_config_version",
        "formal_schema_version",
        "formal_config_version",
    ):
        require_frozen_text(
            ranking.get(key),
            label=f"freeze.ranking_config.{key}",
            allow_placeholders=allow_placeholders,
        )
    if freeze.get("ranking_thinking_assignment_enabled") is not False:
        raise ControllerError("this campaign freezes ranking thinking assignment OFF")

    reporting = plan.get("reporting")
    if not isinstance(reporting, Mapping):
        raise ControllerError("reporting contract is missing")
    terminal_hook = reporting.get("terminal_hook")
    if (
        not isinstance(terminal_hook, Mapping)
        or terminal_hook.get("enabled") is not True
        or terminal_hook.get("strict") is not True
        or terminal_hook.get("mode") != "frozen_python_reporter"
        or reporting.get("mini_is_diagnostic_only") is not True
        or reporting.get("automatic_winner_promotion") is not False
        or reporting.get("independent_safety_gate_available") is not False
    ):
        raise ControllerError("terminal reporting/promotion contract differs")
    require_frozen_text(
        plan.get("paths", {}).get("reporter"),
        label="paths.reporter",
        allow_placeholders=allow_placeholders,
    )

    if (
        inputs.get("benchmark_input_raw_sha256") != plan.get("benchmark", {}).get("input_sha256")
        and not allow_placeholders
    ):
        raise ControllerError("benchmark hash differs between plan and freeze contract")
    expected_exclusions = {"P0-01", "P0-02", "P0.5-31", "P0-15"}
    exclusions = {str(row.get("id")) for row in plan.get("excluded", [])}
    if exclusions != expected_exclusions:
        raise ControllerError(f"excluded experiment set differs: {sorted(exclusions)}")

    arms = expand_arms(plan)
    arm_ids = [arm.arm_id for arm in arms]
    if len(arm_ids) != len(set(arm_ids)):
        raise ControllerError("expanded arm ids are not unique")
    if len(plan.get("common_e0", [])) != 3:
        raise ControllerError("plan must contain exactly three common E0 repetitions")
    if len(arms) != 65:
        raise ControllerError(f"expected 65 candidate live arms before gates, got {len(arms)}")
    experiment_ids = {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
    if len(experiment_ids) != 31:
        raise ControllerError(f"expected 31 live experiment groups, got {len(experiment_ids)}")
    noop_rows = plan.get("no_op_experiments", [])
    if not isinstance(noop_rows, list):
        raise ControllerError("no_op_experiments must be a list")
    noop_ids = [str(row.get("id")) for row in noop_rows if isinstance(row, Mapping)]
    if len(noop_ids) != len(noop_rows) or len(noop_ids) != len(set(noop_ids)):
        raise ControllerError("predeclared no-op experiment ids must be unique")
    if "P0.5-07" not in noop_ids:
        raise ControllerError("P0.5-07 must remain a predeclared no-op experiment")
    temperature_noop = next(
        row for row in noop_rows if isinstance(row, Mapping) and row.get("id") == "P0.5-07"
    )
    if (
        temperature_noop.get("provider_kind") != "openrouter"
        or temperature_noop.get("model") != "anthropic/claude-opus-4.8"
        or temperature_noop.get("requested_values") != [0.0, 0.2]
    ):
        raise ControllerError("P0.5-07 official-host wire gate contract differs")
    gated = {arm.experiment_id for arm in arms if arm.wire_gate}
    if gated != PRODUCTION_BUDGET_GATE_EXPERIMENTS:
        raise ControllerError(f"wire-effect gate set differs: {sorted(gated)}")
    offline_contract = plan.get("runtime_contract", {}).get("offline_effect")
    if not isinstance(offline_contract, Mapping):
        raise ControllerError("runtime_contract.offline_effect is missing")
    if offline_contract.get("runner_mode") != "main_runner_dry_run_frozen_replay":
        raise ControllerError("offline effect gate must use the production main runner")
    if set(offline_contract.get("production_budget_gate_experiments") or []) != set(
        PRODUCTION_BUDGET_GATE_EXPERIMENTS
    ):
        raise ControllerError("offline production-budget gate set differs")
    if set(offline_contract.get("required_replicate_arm_ids") or []) != set(
        REQUIRED_REPLICATE_ARMS
    ):
        raise ControllerError("offline required-replicate arm set differs")
    for arm in arms:
        if not SAFE_COMPONENT_RE.fullmatch(arm.arm_id):
            raise ControllerError(f"unsafe arm id: {arm.arm_id}")
        if not SAFE_COMPONENT_RE.fullmatch(arm.directory_name):
            raise ControllerError(f"unsafe directory name: {arm.directory_name}")
        if not SAFE_COMPONENT_RE.fullmatch(arm.output_name):
            raise ControllerError(f"unsafe output name: {arm.output_name}")
        if arm.analyzer_mode not in {"live", "frozen_replay"}:
            raise ControllerError(f"unknown analyzer mode for {arm.arm_id}")
    if not allow_placeholders:
        placeholders = sorted(
            {item for item in all_strings(plan) if item.startswith(PLACEHOLDER_PREFIX)}
        )
        if placeholders:
            raise ControllerError(
                "unresolved plan placeholders prevent live execution: " + ", ".join(placeholders)
            )
    return arms


def output_dir(plan: Mapping[str, Any], arm: Arm) -> Path:
    return Path(str(plan["paths"]["report_root"])) / arm.directory_name / arm.output_name


def verify_document_self_hash(
    document: Mapping[str, Any],
    *,
    field: str,
    label: str,
) -> None:
    claimed = str(document.get(field) or "")
    detached = copy.deepcopy(dict(document))
    detached.pop(field, None)
    expected = "sha256:" + canonical_sha256(detached)
    if claimed != expected:
        raise ControllerError(f"{label} self-hash differs")


def verify_bare_document_self_hash(
    document: Mapping[str, Any],
    *,
    field: str,
    label: str,
) -> None:
    claimed = str(document.get(field) or "")
    detached = copy.deepcopy(dict(document))
    detached.pop(field, None)
    if SHA256_RE.fullmatch(claimed) is None or claimed != canonical_sha256(detached):
        raise ControllerError(f"{label} self-hash differs")


def verify_result_row_evidence(row: Mapping[str, Any]) -> None:
    schema = row.get("result_evidence_schema")
    claimed = str(row.get("result_evidence_sha256") or "")
    if (
        not isinstance(schema, str)
        or not schema
        or re.fullmatch(r"sha256:[0-9a-f]{64}", claimed) is None
    ):
        raise ControllerError("result row lacks authenticated evidence schema/hash")
    detached = {key: value for key, value in row.items() if key != "result_evidence_sha256"}
    expected = "sha256:" + canonical_sha256({"schema": schema, "result": detached})
    if claimed != expected:
        raise ControllerError("result row evidence hash differs")


def verify_published_artifact_bindings(
    directory: Path,
    manifest: Mapping[str, Any],
    *,
    names: Sequence[str],
) -> dict[str, Path]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ControllerError("manifest lacks artifact bindings")
    resolved: dict[str, Path] = {}
    for name in names:
        record = artifacts.get(name)
        if not isinstance(record, Mapping) or record.get("path") != name:
            raise ControllerError(f"manifest lacks exact binding for {name}")
        path = directory / name
        require_regular_file(path)
        size = path.stat().st_size
        if record.get("size_bytes") != size or record.get("sha256") != file_sha256(path):
            raise ControllerError(f"manifest artifact size/hash differs for {name}")
        resolved[name] = path
    return resolved


def authenticate_published_arm_artifacts(
    directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Path]]:
    """Authenticate the formal publication envelope before consuming any row."""

    paths = {
        "manifest.json": directory / "manifest.json",
        "results.jsonl": directory / "results.jsonl",
        "trace.jsonl": directory / "trace.jsonl",
        "audit.json": directory / "audit.json",
        "openrouter-non-byok-campaign-proof.json": (
            directory / "openrouter-non-byok-campaign-proof.json"
        ),
    }
    for path in paths.values():
        require_regular_file(path)
    manifest = load_json(paths["manifest.json"])
    audit = load_json(paths["audit.json"])
    proof = load_json(paths["openrouter-non-byok-campaign-proof.json"])
    if not all(isinstance(value, dict) for value in (manifest, audit, proof)):
        raise ControllerError("formal publication documents must be JSON objects")
    verify_document_self_hash(manifest, field="manifest_sha256", label="manifest")
    verify_document_self_hash(audit, field="audit_sha256", label="audit")
    verify_document_self_hash(
        proof,
        field="proof_sha256",
        label="non-BYOK proof",
    )
    bound = verify_published_artifact_bindings(
        directory,
        manifest,
        names=(
            "results.jsonl",
            "trace.jsonl",
            "audit.json",
            "openrouter-non-byok-campaign-proof.json",
        ),
    )
    if manifest.get("audit_sha256") != audit.get("audit_sha256"):
        raise ControllerError("manifest/audit semantic hash binding differs")
    if manifest.get("openrouter_non_byok_campaign_proof_sha256") != proof.get("proof_sha256"):
        raise ControllerError("manifest/non-BYOK proof semantic binding differs")
    return (
        manifest,
        audit,
        proof,
        {
            "manifest.json": paths["manifest.json"],
            **bound,
        },
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def verify_arm_publication_identity(
    directory: Path,
    manifest: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a formal package to this exact arm, snapshot, and merged config."""

    if directory.resolve() != Path(str(expected["output_dir"])).resolve():
        raise ControllerError("formal output directory differs from expected arm")
    source_bindings = manifest.get("source_manifests")
    if not isinstance(source_bindings, list) or not source_bindings:
        raise ControllerError("manifest has no source-manifest identity bindings")
    verified_sources: list[dict[str, Any]] = []
    for index, binding in enumerate(source_bindings):
        if not isinstance(binding, Mapping):
            raise ControllerError("manifest has malformed source-manifest binding")
        source_path = Path(str(binding.get("path") or ""))
        result_path = Path(str(binding.get("result_path") or ""))
        if not _is_within(source_path, directory) or not _is_within(result_path, directory):
            raise ControllerError("source wave does not belong to expected arm directory")
        require_regular_file(source_path)
        require_regular_file(result_path)
        if binding.get("sha256") != file_sha256(source_path) or binding.get(
            "result_sha256"
        ) != file_sha256(result_path):
            raise ControllerError("source wave raw hash binding differs")
        source = load_json(source_path)
        if not isinstance(source, Mapping):
            raise ControllerError("source wave manifest is not an object")
        args = source.get("args")
        command = source.get("command")
        provenance = source.get("source_provenance")
        input_validation = source.get("benchmark_input_validation")
        artifacts = source.get("artifacts")
        if not all(
            isinstance(value, Mapping)
            for value in (args, command, provenance, input_validation, artifacts)
        ):
            raise ControllerError("source wave lacks execution identity evidence")
        assert isinstance(args, Mapping)
        assert isinstance(command, Mapping)
        assert isinstance(provenance, Mapping)
        assert isinstance(input_validation, Mapping)
        assert isinstance(artifacts, Mapping)
        runner_path = Path(str(provenance.get("runner_path") or "")).resolve()
        runner_identities = expected.get("runner_identities")
        if not isinstance(runner_identities, Mapping):
            raise ControllerError("expected runner identity set is missing")
        expected_runner_sha = runner_identities.get(str(runner_path))
        if (
            Path(str(command.get("cwd") or "")).resolve()
            != Path(str(expected["snapshot"])).resolve()
            or provenance.get("git_head") != expected["snapshot_commit"]
            or provenance.get("git_dirty") is not False
            or provenance.get("git_tracked_dirty") is not False
            or expected_runner_sha is None
            or provenance.get("runner_sha256") != expected_runner_sha
        ):
            raise ControllerError("source wave snapshot/runner identity differs")
        is_resume = runner_path.name == "run_draco_routing_experiment_resume.py"
        if (index == 0 and is_resume) or (index > 0 and not is_resume):
            raise ControllerError("formal source wave runner order differs")
        scheduled_pairs = binding.get("resume_scheduled_pairs")
        schedule_verified = binding.get("resume_schedule_contract_verified")
        if is_resume:
            if (
                schedule_verified is not True
                or not isinstance(scheduled_pairs, list)
                or not scheduled_pairs
            ):
                raise ControllerError("resume wave lacks a verified non-empty schedule")
            seen_schedule: set[tuple[str, str]] = set()
            for scheduled in scheduled_pairs:
                if not isinstance(scheduled, Mapping):
                    raise ControllerError("resume wave schedule row is malformed")
                pair = (
                    str(scheduled.get("group") or ""),
                    str(scheduled.get("task_id") or ""),
                )
                if (
                    pair[0] != "G1"
                    or pair[1] not in expected["task_ids"]
                    or scheduled.get("action")
                    not in {
                        "regenerate",
                        "model_regenerate",
                        "judge_only",
                        "metadata_only",
                        "audit_only",
                    }
                    or pair in seen_schedule
                ):
                    raise ControllerError("resume wave schedule is outside the arm contract")
                seen_schedule.add(pair)
        elif schedule_verified is not False or scheduled_pairs not in ([], None):
            raise ControllerError("main wave unexpectedly carries a resume schedule")
        if (
            Path(str(args.get("input") or "")).resolve()
            != Path(str(expected["benchmark_path"])).resolve()
            or Path(str(args.get("config") or "")).resolve()
            != Path(str(expected["reference_config_path"])).resolve()
            or args.get("groups") != "G1"
            or args.get("max_tasks") != EXPECTED_TASK_COUNT
            or args.get("concurrency") != expected["task_concurrency"]
            or args.get("judge_concurrency") != expected["judge_concurrency"]
            or args.get("generation_max_attempts") != expected["generation_max_attempts"]
            or args.get("dry_run") is not False
            or args.get("require_openrouter_non_byok") is not True
            or args.get("require_clean_source") is not True
            or not _is_within(Path(str(args.get("output_dir") or "")), directory)
        ):
            raise ControllerError("source wave invocation differs from arm contract")
        if (
            input_validation.get("actual_sha256") != expected["benchmark_sha256"]
            or input_validation.get("actual_task_count") != EXPECTED_TASK_COUNT
            or input_validation.get("task_ids_match") is not True
            or input_validation.get("status") != "matched"
        ):
            raise ControllerError("source wave benchmark identity differs")
        effective_path = Path(str(artifacts.get("experiment_config_effective_json") or ""))
        if not _is_within(effective_path, directory):
            raise ControllerError("source wave effective config is outside arm directory")
        effective = load_json(effective_path)
        effective_judge = effective.get("judge") if isinstance(effective, Mapping) else None
        effective_generation = (
            effective.get("generation") if isinstance(effective, Mapping) else None
        )
        if (
            not isinstance(effective_judge, Mapping)
            or effective_judge.get("concurrency") != expected["judge_concurrency"]
            or not isinstance(effective_generation, Mapping)
            or effective_generation.get("max_attempts") != expected["generation_max_attempts"]
        ):
            raise ControllerError(
                "source wave effective Judge/retry policy differs from arm contract"
            )
        if canonical_sha256(effective) != expected["effective_config_sha256"]:
            raise ControllerError("source wave effective config differs from arm override")
        verified_sources.append(
            {
                "source_index": index,
                "runner_kind": "resume" if is_resume else "main",
                "manifest_sha256": file_sha256(source_path),
                "result_sha256": file_sha256(result_path),
                "effective_config_sha256": canonical_sha256(effective),
            }
        )
    return {
        "arm_id": expected["arm_id"],
        "output_name": expected["output_name"],
        "run_id": expected["run_id"],
        "override_sha256": expected["override_sha256"],
        "effective_config_sha256": expected["effective_config_sha256"],
        "source_manifests": verified_sources,
    }


def inspect_complete_arm(
    directory: Path,
    *,
    expected_task_ids: set[str],
    expected_task_concurrency: int,
    expected_identity: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    required = {
        "manifest": directory / "manifest.json",
        "results": directory / "results.jsonl",
        "trace": directory / "trace.jsonl",
        "audit": directory / "audit.json",
        "proof": directory / "openrouter-non-byok-campaign-proof.json",
    }
    if not directory.exists():
        return False, {"reason": "output_absent"}
    if directory.is_symlink() or not directory.is_dir():
        return False, {"reason": "unsafe_output_directory"}
    try:
        manifest, audit, proof, _ = authenticate_published_arm_artifacts(directory)
        if expected_identity is None:
            raise ControllerError("expected arm publication identity is unavailable")
        arm_identity = verify_arm_publication_identity(
            directory,
            manifest,
            expected=expected_identity,
        )
        rows: list[dict[str, Any]] = []
        with required["results"].open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ControllerError(f"invalid result line {line_number}") from exc
                if not isinstance(row, dict):
                    raise ControllerError(f"non-object result line {line_number}")
                verify_result_row_evidence(row)
                rows.append(row)
        trace_rows: list[dict[str, Any]] = []
        with required["trace"].open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    trace_row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ControllerError(f"invalid trace line {line_number}") from exc
                if not isinstance(trace_row, dict):
                    raise ControllerError(f"non-object trace line {line_number}")
                trace_schema = trace_row.get("trace_schema")
                if trace_schema is not None and trace_schema != trace_row.get(
                    "result_evidence_schema",
                    "opensquilla.draco.result-evidence/v1",
                ):
                    raise ControllerError(
                        f"trace line {line_number} has an unknown evidence schema"
                    )
                trace_result_hash = str(trace_row.get("result_evidence_sha256") or "")
                if re.fullmatch(r"sha256:[0-9a-f]{64}", trace_result_hash) is None:
                    raise ControllerError(
                        f"trace line {line_number} lacks a result evidence binding"
                    )
                trace_rows.append(trace_row)
    except (ControllerError, OSError) as exc:
        return False, {"reason": "artifact_validation_failed", "detail": str(exc)}
    actual_task_ids = {str(row.get("task_id") or "") for row in rows}
    results_by_task = {str(row.get("task_id") or ""): row for row in rows}
    trace_by_task = {str(row.get("task_id") or ""): row for row in trace_rows}
    trace_binding_ok = (
        len(trace_rows) == len(trace_by_task) == 10
        and set(trace_by_task) == actual_task_ids
        and all(
            trace_by_task[task_id].get("result_evidence_sha256")
            == results_by_task[task_id].get("result_evidence_sha256")
            for task_id in actual_task_ids
        )
    )
    groups = {str(row.get("group") or "") for row in rows}
    source_manifests = manifest.get("source_manifests")
    scheduling_ok = bool(source_manifests) and all(
        isinstance(source, Mapping)
        and isinstance(source.get("execution_scheduling"), Mapping)
        and source["execution_scheduling"].get("task_concurrency") == expected_task_concurrency
        for source in source_manifests or []
    )
    checks = {
        "manifest_status_complete": manifest.get("status") == "complete",
        "manifest_result_count": manifest.get("result_count") == 10,
        "manifest_task_count": manifest.get("task_count") == 10,
        "manifest_groups": manifest.get("groups") == ["G1"],
        "manifest_execution_pass": manifest.get("execution_pass") is True,
        "results_row_count": len(rows) == 10,
        "results_group": groups == {"G1"},
        "results_unique_tasks": actual_task_ids == expected_task_ids,
        "trace_result_evidence_bindings": trace_binding_ok,
        "task_concurrency": scheduling_ok,
        "audit_execution_pass": audit.get("execution_pass") is True,
        "proof_execution_pass": proof.get("execution_pass") is True,
    }
    evidence = {
        "reason": "complete" if all(checks.values()) else "completion_contract_failed",
        "checks": checks,
        "manifest_status": manifest.get("status"),
        "execution_pass": manifest.get("execution_pass"),
        "policy_pass": manifest.get("policy_pass"),
        "audit_pass": manifest.get("audit_pass"),
        "proof_pass": proof.get("pass"),
        "audit_warnings": copy.deepcopy(audit.get("warnings") or []),
        "proof_warnings": copy.deepcopy(proof.get("warnings") or []),
        "manifest_policy_pass": manifest.get("policy_pass"),
        "manifest_audit_pass": manifest.get("audit_pass"),
        "artifact_sha256": {key: file_sha256(path) for key, path in required.items()},
        "arm_identity": arm_identity,
    }
    # Audit/policy findings are retained, but do not erase a complete answer or
    # cause a costly duplicate rerun.  That separation is intentional.
    return all(checks.values()), evidence


def _selection_plans(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    plans: list[Mapping[str, Any]] = []
    routing_trace = row.get("routing_trace")
    if isinstance(routing_trace, Mapping) and isinstance(
        routing_trace.get("selection_plan"), Mapping
    ):
        plans.append(routing_trace["selection_plan"])
    trace = row.get("ensemble_trace")
    if isinstance(trace, Mapping) and isinstance(trace.get("selection_plan"), Mapping):
        plans.append(trace["selection_plan"])
    calls = trace.get("calls") if isinstance(trace, Mapping) else None
    if isinstance(calls, list):
        plans.extend(
            call["selection_plan"]
            for call in calls
            if isinstance(call, Mapping) and isinstance(call.get("selection_plan"), Mapping)
        )
    if not plans:
        raise ControllerError("trace row has no selection plan")
    return plans


def _first_selection_plan(row: Mapping[str, Any]) -> Mapping[str, Any]:
    plans = _selection_plans(row)
    profile_hashes = {canonical_sha256(plan.get("task_profile_pre_escalation")) for plan in plans}
    if len(profile_hashes) != 1:
        raise ControllerError("task has multiple pre-escalation Analyzer profiles")
    # routing_trace.selection_plan is the original provider-build decision and
    # therefore precedes any execution retry route. _selection_plans orders it
    # first when present.
    return plans[0]


def _analyzer_metadata_without_usage(analyzer: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(analyzer))
    result.pop("usage", None)
    return result


def _validated_live_analyzer_evidence(
    *,
    task_id: str,
    analyzer: Mapping[str, Any],
    expected_config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Return detached usage, the final successful attempt, and its tokens."""

    expected_provider = str(expected_config.get("provider") or "").strip().lower()
    expected_model = str(expected_config.get("model") or "").strip().lower()
    if (
        analyzer.get("source") != "llm_provider"
        or analyzer.get("schema_valid") is not True
        or str(analyzer.get("fallback_reason") or "")
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer is not a valid live result")
    if (
        str(analyzer.get("provider") or "").strip().lower() != expected_provider
        or str(analyzer.get("model") or "").strip().lower() != expected_model
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer identity differs from config")
    warnings = analyzer.get("normalization_warnings")
    if (
        not isinstance(warnings, list)
        or any(not isinstance(item, str) or not item.strip() for item in warnings)
        or len(warnings) != len(set(warnings))
    ):
        raise ControllerError(f"E0 task {task_id} has invalid Analyzer warnings")
    usage = analyzer.get("usage")
    if not isinstance(usage, Mapping):
        raise ControllerError(f"E0 task {task_id} lacks Analyzer usage")
    usage_copy = copy.deepcopy(dict(usage))
    attempts = usage_copy.get("physical_attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ControllerError(f"E0 task {task_id} lacks physical Analyzer attempts")
    normalized_attempts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ordinal, raw_attempt in enumerate(attempts, start=1):
        if not isinstance(raw_attempt, Mapping):
            raise ControllerError(f"E0 task {task_id} has malformed Analyzer attempt")
        attempt = copy.deepcopy(dict(raw_attempt))
        if attempt.get("attempt") != ordinal:
            raise ControllerError(f"E0 task {task_id} Analyzer attempt order is invalid")
        attempt_id = str(attempt.get("physical_attempt_id") or "").strip().lower()
        if PHYSICAL_ATTEMPT_ID_RE.fullmatch(attempt_id) is None or attempt_id in seen_ids:
            raise ControllerError(f"E0 task {task_id} Analyzer attempt identity is invalid")
        seen_ids.add(attempt_id)
        if (
            str(attempt.get("requested_provider") or "").strip().lower() != expected_provider
            or str(attempt.get("requested_model") or "").strip().lower() != expected_model
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer physical request identity differs")
        usage_unknown = attempt.get("usage_unknown") is True
        provider_usage = attempt.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            raise ControllerError(
                f"E0 task {task_id} Analyzer attempt lacks provider usage evidence"
            )
        mirrored_id = str(provider_usage.get("physical_attempt_id") or "").strip().lower()
        if mirrored_id != attempt_id:
            raise ControllerError(
                f"E0 task {task_id} Analyzer physical attempt identity mirror differs"
            )
        output_tokens = attempt.get("output_tokens")
        if (
            isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer attempt output usage is invalid")
        if usage_unknown:
            if output_tokens != 0 or provider_usage.get("usage_unknown") is not True:
                raise ControllerError(
                    f"E0 task {task_id} Analyzer unknown attempt is contradictory"
                )
        elif (
            str(attempt.get("provider") or "").strip().lower() != expected_provider
            or str(attempt.get("model") or "").strip().lower() != expected_model
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer physical response identity differs")
        normalized_attempts.append(attempt)
    if usage_copy.get("attempt_count") != len(normalized_attempts):
        raise ControllerError(f"E0 task {task_id} Analyzer attempt count differs")
    # A schema-valid live profile is produced by the terminal successful Done
    # request. Earlier Done/error attempts may contribute to aggregate usage;
    # p99 must use only this final physical attempt.
    final_attempt = normalized_attempts[-1]
    if final_attempt.get("usage_unknown") is True:
        raise ControllerError(f"E0 task {task_id} final Analyzer attempt has unknown usage")
    final_output_tokens = final_attempt.get("output_tokens")
    if (
        isinstance(final_output_tokens, bool)
        or not isinstance(final_output_tokens, int)
        or final_output_tokens <= 0
    ):
        raise ControllerError(
            f"E0 task {task_id} final Analyzer attempt has no positive output tokens"
        )
    # The enclosing schema-valid llm_provider result proves that the terminal
    # known-usage Done attempt is the one whose JSON profile was accepted.
    # Every preceding attempt remains authenticated above, but contributes no
    # observation to the p99 derivation.
    return usage_copy, final_attempt, final_output_tokens


def extract_analyzer_artifact(
    *,
    source_arm: Arm,
    source_dir: Path,
    destination: Path,
    expected_task_ids: set[str],
    snapshot_identity: Mapping[str, str],
    plan_sha256: str,
) -> dict[str, Any]:
    manifest, _, _, bound_paths = authenticate_published_arm_artifacts(source_dir)
    manifest_path = bound_paths["manifest.json"]
    trace_path = bound_paths["trace.jsonl"]
    results_path = bound_paths["results.jsonl"]
    result_evidence_by_task: dict[str, str] = {}
    with results_path.open(encoding="utf-8") as results_handle:
        for line_number, line in enumerate(results_handle, start=1):
            try:
                result_row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid source result row {line_number}") from exc
            if not isinstance(result_row, Mapping):
                raise ControllerError(f"source result row {line_number} is not an object")
            verify_result_row_evidence(result_row)
            result_task_id = str(result_row.get("task_id") or "")
            if result_task_id in result_evidence_by_task:
                raise ControllerError(f"duplicate source result task {result_task_id}")
            result_evidence_by_task[result_task_id] = str(
                result_row.get("result_evidence_sha256") or ""
            )
    if set(result_evidence_by_task) != expected_task_ids:
        raise ControllerError("source result evidence coverage differs")
    profiles: dict[str, Any] = {}
    source_task_analyzer_config: dict[str, Any] | None = None
    with trace_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid E0 trace row {line_number}") from exc
            if not isinstance(row, Mapping):
                raise ControllerError(f"E0 trace row {line_number} is not an object")
            task_id = str(row.get("task_id") or "")
            if row.get("group") != "G1" or task_id not in expected_task_ids:
                raise ControllerError(f"E0 trace row {line_number} is outside frozen G1 tasks")
            if task_id in profiles:
                raise ControllerError(f"duplicate E0 trace task: {task_id}")
            if row.get("error") not in (None, "", {}):
                raise ControllerError(f"E0 task {task_id} is not execution-complete")
            task_input_sha256 = str(row.get("task_input_sha256") or "").strip()
            task_prompt_sha256 = str(row.get("prompt_sha256") or "").strip()
            result_evidence_sha256 = str(row.get("result_evidence_sha256") or "").strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", task_input_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid task input hash")
            if SHA256_RE.fullmatch(task_prompt_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid prompt hash")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", result_evidence_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid result evidence hash")
            if result_evidence_by_task.get(task_id) != result_evidence_sha256:
                raise ControllerError(f"E0 task {task_id} trace/result evidence binding differs")
            selection = _first_selection_plan(row)
            profile = selection.get("task_profile_pre_escalation")
            analyzer = selection.get("task_analyzer")
            if not isinstance(profile, Mapping) or not isinstance(analyzer, Mapping):
                raise ControllerError(f"E0 task {task_id} lacks Analyzer evidence")
            profile_copy = copy.deepcopy(dict(profile))
            metadata = _analyzer_metadata_without_usage(analyzer)
            ranking_parameters = selection.get("ranking_parameters")
            current_analyzer_config = (
                ranking_parameters.get("task_analyzer")
                if isinstance(ranking_parameters, Mapping)
                else None
            )
            if not isinstance(current_analyzer_config, Mapping):
                raise ControllerError(f"E0 task {task_id} lacks effective Analyzer config")
            current_config_copy = copy.deepcopy(dict(current_analyzer_config))
            if source_task_analyzer_config is None:
                source_task_analyzer_config = current_config_copy
            elif source_task_analyzer_config != current_config_copy:
                raise ControllerError("E0 rows do not share one effective Analyzer config")
            usage, final_attempt, output_tokens = _validated_live_analyzer_evidence(
                task_id=task_id,
                analyzer=analyzer,
                expected_config=current_config_copy,
            )
            profile_sha256 = canonical_sha256(profile_copy)
            if (
                str(selection.get("task_profile_hash") or "")
                not in {
                    "",
                    profile_sha256,
                }
                and selection.get("task_profile_post_escalation") == profile
            ):
                raise ControllerError(f"E0 task {task_id} profile hash is contradictory")
            # Every call/retry must carry the exact same frozen Analyzer
            # profile and provenance. Route changes are allowed, Analyzer
            # drift is not.
            expected_analyzer_hash = canonical_sha256(analyzer)
            for repeated_plan in _selection_plans(row):
                if (
                    canonical_sha256(repeated_plan.get("task_profile_pre_escalation"))
                    != profile_sha256
                ):
                    raise ControllerError(f"E0 task {task_id} Analyzer profile drifted")
                repeated_analyzer = repeated_plan.get("task_analyzer")
                if (
                    not isinstance(repeated_analyzer, Mapping)
                    or canonical_sha256(repeated_analyzer) != expected_analyzer_hash
                ):
                    raise ControllerError(f"E0 task {task_id} Analyzer provenance drifted")
            profiles[task_id] = {
                "task_id": task_id,
                "task_input_sha256": task_input_sha256,
                "task_prompt_sha256": task_prompt_sha256,
                "task_profile_pre_escalation": profile_copy,
                "task_profile_pre_escalation_sha256": profile_sha256,
                "original_analyzer": metadata,
                "original_analyzer_sha256": canonical_sha256(metadata),
                "original_analyzer_usage_sha256": canonical_sha256(usage),
                "original_analyzer_physical_attempt_count": len(usage["physical_attempts"]),
                "final_successful_physical_attempt_sha256": canonical_sha256(final_attempt),
                "final_successful_physical_attempt_id": final_attempt["physical_attempt_id"],
                "final_successful_physical_attempt_output_tokens": output_tokens,
                # Compatibility alias for the derivation reader; its meaning
                # is explicitly the terminal successful physical attempt.
                "original_analyzer_output_tokens": output_tokens,
                "source_trace_row_sha256": canonical_sha256(row),
                "source_result_evidence_sha256": result_evidence_sha256,
            }
    if set(profiles) != expected_task_ids:
        raise ControllerError(
            "E0 Analyzer profile coverage differs: "
            f"missing={sorted(expected_task_ids - set(profiles))}, "
            f"extra={sorted(set(profiles) - expected_task_ids)}"
        )
    assert source_task_analyzer_config is not None
    replay_entries = {
        task_id: {
            "task_input_sha256": row["task_input_sha256"],
            "prompt_sha256": row["task_prompt_sha256"],
            "task_profile_pre_escalation": row["task_profile_pre_escalation"],
            "task_profile_pre_escalation_sha256": row["task_profile_pre_escalation_sha256"],
            "task_analyzer": {
                "source": "frozen_replay",
                "schema_valid": row["original_analyzer"].get("schema_valid") is True,
                "confidence": row["original_analyzer"].get("confidence"),
                "analyzer_version": row["original_analyzer"].get("analyzer_version"),
                "provider": row["original_analyzer"].get("provider"),
                "model": row["original_analyzer"].get("model"),
                "fallback_reason": row["original_analyzer"].get("fallback_reason", ""),
                "usage": {},
                "normalization_warnings": copy.deepcopy(
                    row["original_analyzer"].get("normalization_warnings") or []
                ),
            },
        }
        for task_id, row in sorted(profiles.items())
    }
    replay_payload = {
        "schema": "opensquilla.draco.frozen-task-analysis/v1",
        "mode": "frozen_replay",
        "source_experiment": source_arm.arm_id,
        "source_manifest_sha256": file_sha256(manifest_path),
        "source_results_sha256": file_sha256(results_path),
        "source_task_analyzer_config": source_task_analyzer_config,
        "source_task_analyzer_config_sha256": canonical_sha256(source_task_analyzer_config),
        "entries": replay_entries,
        "entries_sha256": canonical_sha256(replay_entries),
    }
    artifact: dict[str, Any] = {
        "schema": ANALYZER_ARTIFACT_SCHEMA,
        "created_at": utc_now(),
        "source": {
            "arm_id": source_arm.arm_id,
            "output_dir": str(source_dir),
            "manifest_sha256": file_sha256(manifest_path),
            "trace_sha256": file_sha256(trace_path),
            "snapshot_commit": snapshot_identity["commit"],
            "snapshot_tree": snapshot_identity["tree"],
            "campaign_plan_sha256": plan_sha256,
        },
        "task_count": len(profiles),
        "task_ids": sorted(profiles),
        "profiles": profiles,
        # This is the only portion embedded into the experiment overlay.  Its
        # exact wrapper/path is controlled by runtime_contract.frozen_replay.
        "replay_payload": replay_payload,
    }
    artifact["profiles_sha256"] = canonical_sha256(replay_entries)
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    atomic_write_json(destination, artifact)
    return artifact


def linear_type7_quantile(values: Sequence[int], q: float) -> float:
    if not values:
        raise ControllerError("cannot calculate a quantile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ControllerError("quantile must be between zero and one")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    h = (len(ordered) - 1) * q
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def derive_analyzer_p99_receipt(
    artifact: Mapping[str, Any],
    *,
    destination: Path,
    plan_sha256: str,
) -> dict[str, Any]:
    values = [
        int(row["final_successful_physical_attempt_output_tokens"])
        for row in artifact["profiles"].values()
    ]
    p99 = linear_type7_quantile(values, 0.99)
    derived = {
        "P0.5-06-E1": math.floor(0.8 * p99),
        "P0.5-06-E2": math.ceil(1.1 * p99),
    }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "analyzer-output-token-p99-derivation",
        "created_at": utc_now(),
        "campaign_plan_sha256": plan_sha256,
        "source_artifact_sha256": artifact["artifact_sha256"],
        "method": "Hyndman-Fan linear type 7; h=(n-1)*q; q=0.99",
        "observation_unit": "final successful physical Analyzer attempt per task",
        "ordered_output_tokens": sorted(values),
        "p99": p99,
        "formulae": {
            "P0.5-06-E1": "floor(0.8 * p99)",
            "P0.5-06-E2": "ceil(1.1 * p99)",
        },
        "derived_max_output_tokens": derived,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_write_json(destination, receipt)
    return receipt


def make_replay_overlay(plan: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    contract = plan["runtime_contract"]["frozen_replay"]
    mode_path = contract["mode_path"]
    payload_path = contract["payload_path"]
    payload_key = str(contract.get("artifact_projection_key", "replay_payload"))
    if payload_key not in artifact:
        raise ControllerError(f"frozen artifact lacks projection {payload_key!r}")
    overlay: dict[str, Any] = {}
    set_path(overlay, payload_path, artifact[payload_key])
    set_path(overlay, mode_path, contract["mode_value"])
    return overlay


def make_noop_temperature_receipt(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    destination: Path,
    plan_sha256: str,
) -> dict[str, Any]:
    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.compat_policy import compat_policy_for_kind  # type: ignore
        from opensquilla.provider.ensemble import (  # type: ignore
            openrouter_static_capabilities,
        )
        from opensquilla.provider.openai import _should_send_temperature  # type: ignore
        from opensquilla.provider.ranking_router import ranking_config_resolution  # type: ignore
        from opensquilla.provider.registry import get_provider_spec  # type: ignore
        from opensquilla.provider.types import ChatConfig  # type: ignore
    finally:
        sys.path.pop(0)
    noop = next(row for row in plan["no_op_experiments"] if row["id"] == "P0.5-07")
    model = str(noop["model"])
    provider_kind = str(noop["provider_kind"])
    policy = compat_policy_for_kind(provider_kind)
    ranking_config_path = snapshot / "src/opensquilla/provider/router_dynamic_ranking_config.json"
    provider_spec = get_provider_spec(provider_kind)
    base_url = str(provider_spec.default_base_url or "").strip()
    official_host = str(policy.official_host or "").strip().lower()
    resolved_host = str(urlsplit(base_url).hostname or "").strip().lower()
    if (
        provider_kind != "openrouter"
        or official_host != "openrouter.ai"
        or resolved_host != official_host
    ):
        raise ControllerError("P0.5-07 must remain bound to OpenRouter official host")
    projected: list[dict[str, Any]] = []
    analyzer_configs: list[dict[str, Any]] = []
    for requested_temperature in noop["requested_values"]:
        experiment = load_effective_experiment_config(
            snapshot,
            base_config,
            {
                "router_dynamic_ranking_override": {
                    "task_analyzer": {"temperature": requested_temperature}
                }
            },
        )
        resolution = ranking_config_resolution(
            thinking_assignment_enabled=False,
            override=(experiment.router_dynamic_ranking_override or None),
        )
        effective = resolution.get("effective_config")
        analyzer_config = effective.get("task_analyzer") if isinstance(effective, Mapping) else None
        if not isinstance(analyzer_config, Mapping):
            raise ControllerError("effective ranking config has no task_analyzer")
        analyzer_copy = copy.deepcopy(dict(analyzer_config))
        if (
            analyzer_copy.get("provider") != provider_kind
            or analyzer_copy.get("model") != model
            or float(analyzer_copy.get("temperature")) != float(requested_temperature)
        ):
            raise ControllerError("P0.5-07 overlay differs from effective Analyzer")
        chat_config = ChatConfig(
            temperature=float(requested_temperature),
            thinking=bool(analyzer_copy.get("thinking")),
        )
        sends_temperature = _should_send_temperature(
            policy,
            base_url,
            model,
            chat_config,
            openrouter_static_capabilities(model),
        )
        projected.append(
            {
                "requested_temperature": requested_temperature,
                "production_should_send_temperature": sends_temperature,
                "wire_temperature": requested_temperature if sends_temperature else None,
            }
        )
        analyzer_configs.append(analyzer_copy)
    if any(row["production_should_send_temperature"] for row in projected):
        raise ControllerError("P0.5-07 is no longer a production wire no-op")
    policy_path = snapshot / "src/opensquilla/provider/compat_policy.py"
    openai_path = snapshot / "src/opensquilla/provider/openai.py"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "wire-no-op",
        "experiment_id": "P0.5-07",
        "created_at": utc_now(),
        "decision": "deleted_no_live_run",
        "reason_code": "openrouter_compat_omits_temperature",
        "provider_kind": provider_kind,
        "model": model,
        "official_base_url": base_url,
        "official_host": official_host,
        "resolved_base_url_host": resolved_host,
        "requested_values": noop["requested_values"],
        "production_projection": projected,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "compat_policy_source_sha256": file_sha256(policy_path),
        "openai_payload_source_sha256": file_sha256(openai_path),
        "ranking_config_source_sha256": file_sha256(ranking_config_path),
        "effective_task_analyzer_configs_sha256": canonical_sha256(analyzer_configs),
        "campaign_plan_sha256": plan_sha256,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_write_json(destination, receipt)
    return receipt


def load_effective_experiment_config(
    snapshot: Path,
    base_config: Path,
    overlay: Mapping[str, Any],
) -> Any:
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.eval.draco_experiment_config import (  # type: ignore
            load_draco_experiment_config,
        )

        return load_draco_experiment_config(
            base_config,
            inline_overlay_json=json.dumps(overlay, separators=(",", ":"), sort_keys=True),
        ).config
    finally:
        sys.path.pop(0)


_RUNTIME_HELPERS: dict[str, dict[str, Any]] = {}


def _runtime_helpers(snapshot: Path) -> dict[str, Any]:
    cache_key = str(snapshot.resolve())
    if cache_key in _RUNTIME_HELPERS:
        return _RUNTIME_HELPERS[cache_key]
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.aggregator_prompt import (  # type: ignore
            aggregator_prompt_version_evidence,
        )
        from opensquilla.provider.compat_policy import compat_policy_for_kind  # type: ignore
        from opensquilla.provider.ensemble import (  # type: ignore
            EnsembleMemberConfig,
            _aggregator_chat_config,
            _member_max_tokens,
            _member_model_capabilities,
            _proposer_chat_config,
            build_ensemble_provider_from_config,
        )
        from opensquilla.provider.model_catalog import shared_catalog  # type: ignore
        from opensquilla.provider.openai import _should_send_temperature  # type: ignore
        from opensquilla.provider.registry import get_provider_spec  # type: ignore
        from opensquilla.provider.selector import ProviderConfig  # type: ignore
        from opensquilla.provider.types import ChatConfig  # type: ignore

        module_name = "_p0_p05_draco_runner_" + hashlib.sha256(cache_key.encode()).hexdigest()[:12]
        runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
        spec = importlib.util.spec_from_file_location(module_name, runner_path)
        if spec is None or spec.loader is None:
            raise ControllerError("cannot load production DRACO runner helpers")
        runner = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = runner
        spec.loader.exec_module(runner)
    finally:
        sys.path.pop(0)
    required_signatures = {
        "_aggregator_chat_config": (
            _aggregator_chat_config,
            {
                "base",
                "member",
                "max_tokens_cap",
                "visible_answer_reserve_tokens",
                "recovery",
                "request_budget_binding",
            },
        ),
        "_proposer_chat_config": (
            _proposer_chat_config,
            {
                "base",
                "member",
                "max_tokens_cap",
                "visible_answer_reserve_tokens",
                "max_tokens_cap_explicit",
                "request_budget_binding",
            },
        ),
        "_should_send_temperature": (
            _should_send_temperature,
            {"policy", "base_url", "model", "cfg", "caps"},
        ),
    }
    for label, (function, expected_parameters) in required_signatures.items():
        actual_parameters = set(inspect.signature(function).parameters)
        if not expected_parameters.issubset(actual_parameters):
            raise ControllerError(
                f"production helper {label} signature drifted: {sorted(actual_parameters)}"
            )
    build_signature = inspect.signature(build_ensemble_provider_from_config)
    rebinding_parameter = build_signature.parameters.get("_enable_member_request_budget_rebinding")
    if rebinding_parameter is None or rebinding_parameter.default is not False:
        raise ControllerError("production member request-budget binding default drifted")
    build_source = inspect.getsource(runner.build_experiment_provider)
    build_tree = ast.parse(textwrap.dedent(build_source))
    builder_calls = [
        node
        for node in ast.walk(build_tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "build_ensemble_provider_from_config"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_ensemble_provider_from_config"
        )
    ]
    if not builder_calls:
        raise ControllerError("production DRACO runner no longer invokes the ensemble builder")
    draco_binding_disabled = True
    for call in builder_calls:
        keyword = next(
            (
                item
                for item in call.keywords
                if item.arg == "_enable_member_request_budget_rebinding"
            ),
            None,
        )
        if keyword is not None and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        ):
            draco_binding_disabled = False
    helpers = {
        "EnsembleMemberConfig": EnsembleMemberConfig,
        "ProviderConfig": ProviderConfig,
        "ChatConfig": ChatConfig,
        "aggregator_chat_config": _aggregator_chat_config,
        "proposer_chat_config": _proposer_chat_config,
        "member_max_tokens": _member_max_tokens,
        "member_model_capabilities": _member_model_capabilities,
        "shared_catalog": shared_catalog,
        "aggregator_prompt_version_evidence": (aggregator_prompt_version_evidence),
        "should_send_temperature": _should_send_temperature,
        "compat_policy_for_kind": compat_policy_for_kind,
        "get_provider_spec": get_provider_spec,
        "runner": runner,
        "draco_request_budget_rebinding_disabled": draco_binding_disabled,
        "ensemble_source_sha256": file_sha256(snapshot / "src/opensquilla/provider/ensemble.py"),
        "openai_source_sha256": file_sha256(snapshot / "src/opensquilla/provider/openai.py"),
        "runner_source_sha256": file_sha256(snapshot / "scripts/run_draco_routing_experiment.py"),
        "aggregator_prompt_source_sha256": file_sha256(
            snapshot / "src/opensquilla/provider/aggregator_prompt.py"
        ),
    }
    _RUNTIME_HELPERS[cache_key] = helpers
    return helpers


def _source_selection_plans(
    trace_path: Path,
    *,
    expected_task_ids: set[str],
    require_dry_replay: bool,
) -> dict[str, Mapping[str, Any]]:
    require_regular_file(trace_path)
    plans: dict[str, Mapping[str, Any]] = {}
    with trace_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid routing trace row {line_number}") from exc
            if not isinstance(row, Mapping) or row.get("group") != "G1":
                raise ControllerError(f"routing trace row {line_number} is not G1")
            task_id = str(row.get("task_id") or "")
            if task_id not in expected_task_ids or task_id in plans:
                raise ControllerError(f"routing trace has invalid/duplicate task {task_id!r}")
            routing = row.get("routing_trace")
            if not isinstance(routing, Mapping):
                raise ControllerError(f"routing trace task {task_id} lacks routing evidence")
            if require_dry_replay and routing.get("dry_run") is not True:
                raise ControllerError(f"routing trace task {task_id} is not a dry run")
            selection = routing.get("selection_plan")
            if not isinstance(selection, Mapping):
                raise ControllerError(f"routing trace task {task_id} lacks selection plan")
            if require_dry_replay:
                analyzer = selection.get("task_analyzer")
                replay = analyzer.get("replay") if isinstance(analyzer, Mapping) else None
                if (
                    not isinstance(analyzer, Mapping)
                    or analyzer.get("source") != "frozen_replay"
                    or analyzer.get("usage") != {}
                    or not isinstance(replay, Mapping)
                    or replay.get("physical_request_count") != 0
                ):
                    raise ControllerError(
                        f"dry trace task {task_id} did not materialize zero-call replay"
                    )
            plans[task_id] = copy.deepcopy(dict(selection))
    if set(plans) != expected_task_ids:
        raise ControllerError("routing trace task coverage differs")
    return plans


def _dry_run_output_artifacts(directory: Path) -> tuple[Path, Path]:
    traces = sorted(directory.glob("draco_run_*.trace.jsonl"))
    manifests = sorted(directory.glob("draco_run_*.manifest.json"))
    if len(traces) != 1 or len(manifests) != 1:
        raise ControllerError("offline dry run did not publish one trace and manifest")
    require_regular_file(traces[0])
    require_regular_file(manifests[0])
    manifest = load_json(manifests[0])
    if manifest.get("status") != "complete" or manifest.get("dry_run") is not True:
        # Older manifests carry dry_run only under run_compatibility. The
        # trace-level proof remains mandatory; accept status complete here.
        if manifest.get("status") != "complete":
            raise ControllerError("offline dry run manifest is not complete")
    return traces[0], manifests[0]


def run_main_dry_replay(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    overlay: Mapping[str, Any],
    expected_task_ids: set[str],
    label: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    """Run the production main runner in its zero-network dry-run mode."""

    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    overlay_sha = canonical_sha256(overlay)
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "overlay"
    output_dir = (
        Path(str(plan["paths"]["run_root"]))
        / "offline-dry-runs"
        / f"{safe_label}-{overlay_sha[:16]}-{os.getpid()}-{datetime.now().strftime('%H%M%S%f')}"
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    command = [
        str(plan["paths"]["python"]),
        str(snapshot / "scripts/run_draco_routing_experiment.py"),
        "--input",
        str(reference_repo / "data/draco/mini.jsonl"),
        "--config",
        str(reference_repo / ".local-state/config.toml"),
        "--experiment-config",
        str(snapshot / str(plan["paths"]["experiment_config_relative"])),
        "--experiment-config-override-json",
        json.dumps(overlay, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--experiment-config-set",
        "runner.concurrency=6",
        "--groups",
        "G1",
        "--max-tasks",
        "10",
        "--output-dir",
        str(output_dir),
        "--dry-run",
        "--require-openrouter-non-byok",
        "--require-clean-source",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(snapshot / "src"),
            "OPENSQUILLA_REPO": str(snapshot),
            "OPENSQUILLA_REFERENCE_REPO": str(reference_repo),
            "OPENSQUILLA_TRUST_ENV": "0",
            # A deliberately non-secret offline sentinel makes deployment
            # availability match the paid OpenRouter route without ever being
            # sent: --dry-run constructs only DryProvider instances.
            "OPENROUTER_API_KEY": "sk-or-v1-" + ("0" * 64),
            "BRAVE_API_KEY": "",
            "BRAVE_SEARCH_API_KEY": "",
            "FIRECRAWL_API_KEY": "",
        }
    )
    completed = subprocess.run(
        command,
        cwd=snapshot,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise ControllerError(
            f"offline main-runner dry replay failed ({completed.returncode}): {detail}"
        )
    trace_path, manifest_path = _dry_run_output_artifacts(output_dir)
    plans = _source_selection_plans(
        trace_path,
        expected_task_ids=expected_task_ids,
        require_dry_replay=True,
    )
    evidence = {
        "status": "complete",
        "attempted": True,
        "output_dir": str(output_dir),
        "overlay_sha256": overlay_sha,
        "trace_path": str(trace_path),
        "trace_raw_sha256": file_sha256(trace_path),
        "manifest_path": str(manifest_path),
        "manifest_raw_sha256": file_sha256(manifest_path),
        "command_projection_sha256": canonical_sha256(
            [item for item in command if not item.startswith("sk-or-v1-")]
        ),
        "network_contract": "main runner --dry-run; zero Analyzer/provider/Judge calls",
        "task_count": len(plans),
    }
    return plans, evidence


def _parse_identity(identity: Any) -> tuple[str, str]:
    provider, separator, model = str(identity or "").strip().partition(":")
    if separator != ":" or not provider or not model:
        raise ControllerError(f"invalid selected model identity: {identity!r}")
    return provider, model


def _ordered_member_refs(selection: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    raw_selected_p = selection.get("selected_P")
    raw_backup_p = selection.get("backup_P")
    raw_aggregator_candidates = selection.get("aggregator_candidates")
    if not isinstance(raw_selected_p, list) or not all(
        isinstance(value, str) and value for value in raw_selected_p
    ):
        raise ControllerError("selection plan lacks ordered selected_P")
    if not isinstance(raw_backup_p, list) or not all(
        isinstance(value, str) and value for value in raw_backup_p
    ):
        raise ControllerError("selection plan lacks ordered backup_P")
    if not isinstance(raw_aggregator_candidates, list) or not all(
        isinstance(value, str) and value for value in raw_aggregator_candidates
    ):
        raise ControllerError("selection plan lacks ordered aggregator_candidates")
    selected_p = list(raw_selected_p)
    selected_a = str(selection.get("selected_A") or "")
    backup_p = list(raw_backup_p)
    aggregator_candidates = list(raw_aggregator_candidates)
    if not selected_p or not selected_a:
        raise ControllerError("selection plan lacks selected P/A")
    if not aggregator_candidates or aggregator_candidates[0] != selected_a:
        raise ControllerError("aggregator recovery order does not start with selected_A")
    rows: list[tuple[str, str, str]] = []
    for index, identity in enumerate(selected_p, start=1):
        provider, model = _parse_identity(identity)
        rows.append(("proposer", provider, model))
    provider, model = _parse_identity(selected_a)
    rows.append(("aggregator", provider, model))
    for identity in backup_p:
        provider, model = _parse_identity(identity)
        rows.append(("proposer_backup", provider, model))
    for identity in aggregator_candidates[1:]:
        provider, model = _parse_identity(identity)
        rows.append(("aggregator_fallback", provider, model))
    return rows


def _validated_aggregator_prompt(
    selection: Mapping[str, Any],
    *,
    helpers: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = selection.get("aggregator_prompt")
    ranking_parameters = selection.get("ranking_parameters")
    aggregator_policy = (
        ranking_parameters.get("aggregator") if isinstance(ranking_parameters, Mapping) else None
    )
    version = (
        str(aggregator_policy.get("prompt_version") or "")
        if isinstance(aggregator_policy, Mapping)
        else ""
    )
    if version not in AGGREGATOR_PROMPT_VERSIONS or not isinstance(evidence, Mapping):
        raise ControllerError("selection plan lacks a versioned Aggregator prompt")
    expected = helpers["aggregator_prompt_version_evidence"](version)
    if (
        set(evidence) != {"schema", "version", "description", "additional_instructions", "sha256"}
        or dict(evidence) != expected
        or evidence.get("schema") != AGGREGATOR_PROMPT_SCHEMA
        or SHA256_RE.fullmatch(str(evidence.get("sha256") or "")) is None
    ):
        raise ControllerError("Aggregator prompt evidence differs from production code")
    detached = {key: value for key, value in evidence.items() if key != "sha256"}
    if evidence.get("sha256") != canonical_sha256(detached):
        raise ControllerError("Aggregator prompt evidence hash differs")
    return copy.deepcopy(dict(evidence))


def _selection_int(
    selection: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = selection.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ControllerError(f"selection plan has invalid {key}")
    return value


def _selected_proposer_cardinality(
    selection: Mapping[str, Any],
) -> tuple[list[str], int, int, int]:
    raw_selected = selection.get("selected_P")
    if not isinstance(raw_selected, list) or not all(
        isinstance(identity, str) and identity for identity in raw_selected
    ):
        raise ControllerError("selection plan lacks ordered selected_P")
    selected = list(raw_selected)
    normalized = [
        (provider.lower(), model.lower())
        for provider, model in (_parse_identity(identity) for identity in selected)
    ]
    if len(normalized) != len(set(normalized)):
        raise ControllerError("selection plan selected_P identities must be unique")
    n_min = _selection_int(selection, "N_min", minimum=1)
    n_max = _selection_int(selection, "N_max", minimum=n_min)
    proposer_count = _selection_int(selection, "proposer_count", minimum=1)
    if len(selected) != proposer_count:
        raise ControllerError("selection plan selected_P/proposer_count differs")
    if not n_min <= proposer_count <= n_max:
        raise ControllerError("selection plan proposer_count is outside N_min/N_max")
    return selected, n_min, n_max, proposer_count


def _normalized_declared_member_generation(
    selection: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    """Normalize optional live-plan member declarations for cross-checking.

    The production dry provider does not instantiate generation members, so
    its trace may omit these rows.  A live plan that does publish them is
    checked against the helper-derived request projection below.
    """

    primary = selection.get("member_generation")
    recovery = selection.get("recovery_member_generation")
    if primary is None and recovery is None:
        return None
    if not isinstance(primary, list) or not isinstance(recovery, list):
        raise ControllerError("selection plan member generation roster is incomplete")
    expanded: list[dict[str, Any]] = []
    for raw in [*primary, *recovery]:
        if not isinstance(raw, Mapping):
            raise ControllerError("selection plan has malformed member generation row")
        role = str(raw.get("role") or "")
        provider = str(raw.get("provider") or "")
        model = str(raw.get("model") or "")
        k = raw.get("k", 1)
        if (
            role not in {"proposer", "aggregator", "proposer_backup", "aggregator_fallback"}
            or not provider
            or not model
            or isinstance(k, bool)
            or not isinstance(k, int)
            or k <= 0
        ):
            raise ControllerError("selection plan member generation identity is invalid")
        for _ in range(k):
            expanded.append(
                {
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "temperature": raw.get("temperature"),
                    "max_tokens": raw.get("max_tokens"),
                    "thinking": raw.get("thinking"),
                    "thinking_budget_tokens": raw.get("thinking_budget_tokens"),
                }
            )
    return expanded


def _generation_policy(config: Any, runner: Any) -> dict[str, Any]:
    generation = config.generation
    return {
        "generation_thinking": runner.DEFAULT_GENERATION_THINKING,
        "temperature": generation.temperature,
        "thinking_enabled": bool(generation.thinking_enabled),
        "thinking_level": "model-specific",
        "default_thinking_level": str(generation.default_thinking_level),
        "thinking_budget_tokens": int(generation.thinking_budget_tokens),
        "max_thinking_budget_tokens": int(generation.thinking_budget_tokens),
        "max_tokens": int(generation.max_tokens),
        "max_tokens_overridden": True,
        "model_thinking_levels": dict(generation.model_thinking_levels),
        "require_highest_thinking": bool(generation.require_highest_thinking),
        "applies_to": "single baselines and ensemble members",
    }


def _model_capabilities_projection(value: Any) -> dict[str, Any]:
    return {
        "supports_reasoning": bool(getattr(value, "supports_reasoning", False)),
        "supports_tools": bool(getattr(value, "supports_tools", False)),
        "supports_streaming": bool(getattr(value, "supports_streaming", False)),
        "supports_vision": bool(getattr(value, "supports_vision", False)),
        "reasoning_format": str(getattr(value, "reasoning_format", "none") or "none"),
    }


def _aggregator_budget_projection(
    *,
    helpers: Mapping[str, Any],
    base: Any,
    member: Any,
    resolved: Any,
    configured_cap: int,
    configured_reserve: int,
    recovery: bool,
) -> dict[str, Any]:
    """Expose the inputs and result of production `_aggregator_chat_config`."""

    member_max = max(0, int(member.max_tokens or 0))
    default_request_max = max(1, int(helpers["ChatConfig"]().max_tokens))
    base_max = max(0, int(getattr(base, "max_tokens", 0) or 0))
    explicit_request_max = base_max if member_max <= 0 and base_max > default_request_max else 0
    configured_max = max(
        1,
        int(helpers["member_max_tokens"](member)),
        explicit_request_max,
    )
    capability_max = configured_max
    capability_source = "configured"
    try:
        catalog_max, catalog_source = helpers["shared_catalog"]().resolve_max_tokens_with_source(
            member.provider_config.model,
            user_override=0,
            provider=member.provider_config.provider,
        )
        if catalog_source in {"catalog", "override"} and int(catalog_max or 0) > 0:
            capability_max = int(catalog_max)
            capability_source = str(catalog_source)
    except Exception:  # noqa: BLE001 - mirrors the production conservative branch
        pass
    expansion_cap = max(1, min(max(1, configured_cap), capability_max))
    normalized_reserve = min(
        max(1, configured_reserve),
        max(1, expansion_cap - 1),
    )
    effective_reserve = min(
        normalized_reserve,
        max(1, int(resolved.max_tokens) // 2),
    )
    return {
        "configured_member_max_tokens": member_max,
        "configured_generation_max_tokens": base_max,
        "configured_max_tokens": configured_max,
        "configured_max_tokens_cap": configured_cap,
        "capability_max_tokens": capability_max,
        "capability_source": capability_source,
        "trusted_catalog_ceiling": capability_source in {"catalog", "override"},
        "configured_visible_answer_reserve_tokens": configured_reserve,
        "normalized_visible_answer_reserve_tokens": normalized_reserve,
        "effective_visible_answer_reserve_tokens": effective_reserve,
        "effective_max_tokens": int(resolved.max_tokens),
        "effective_thinking_budget_tokens": (
            int(resolved.thinking_budget_tokens or 0) if resolved.thinking else 0
        ),
        "recovery": recovery,
    }


def request_visible_selection_projection(
    *,
    snapshot: Path,
    config: Any,
    selections: Mapping[str, Mapping[str, Any]],
    max_tokens_cap_explicit: bool,
) -> dict[str, Any]:
    cardinalities: dict[str, tuple[list[str], int, int, int]] = {}
    for task_id, selection in sorted(selections.items()):
        if not isinstance(selection, Mapping):
            raise ControllerError(f"task {task_id} has no selection mapping")
        cardinalities[task_id] = _selected_proposer_cardinality(selection)
    helpers = _runtime_helpers(snapshot)
    runner = helpers["runner"]
    policy = _generation_policy(config, runner)
    projected: dict[str, Any] = {}
    for task_id, selection in sorted(selections.items()):
        if not isinstance(selection, Mapping):
            raise ControllerError(f"task {task_id} has no selection mapping")
        prompt_evidence = _validated_aggregator_prompt(selection, helpers=helpers)
        member_refs = _ordered_member_refs(selection)
        selected_p, n_min, n_max, proposer_count = cardinalities[task_id]
        effective_quorum = _selection_int(
            selection,
            "effective_min_successful_proposers",
            minimum=1,
        )
        recovery_policy = selection.get("proposer_recovery_policy")
        if not isinstance(recovery_policy, Mapping):
            raise ControllerError("selection plan lacks proposer recovery policy")
        formal_policy = runner.formal_proposer_recovery_policy_for_plan(selection)
        provider_native = formal_policy is not None and dict(recovery_policy) == formal_policy
        legal_quorum = 2 if provider_native else int(runner.legal_proposer_quorum(len(selected_p)))
        if effective_quorum != legal_quorum:
            raise ControllerError("selection plan effective/legal quorum differs")
        if selection.get("legal_min_successful_proposers") not in (None, legal_quorum):
            raise ControllerError("selection plan declared legal quorum differs")
        legal_quorum_policy = "fixed_2_provider_native" if provider_native else "ceil(2*n/3)"
        if selection.get("legal_quorum_policy") not in (None, legal_quorum_policy):
            raise ControllerError("selection plan legal quorum policy differs")
        declared_generation = _normalized_declared_member_generation(selection)
        member_rows: list[dict[str, Any]] = []
        declared_projection: list[dict[str, Any]] = []
        for index, (role, provider, model) in enumerate(member_refs, start=1):
            base = runner.generation_chat_config(policy, model=model)
            thinking = runner.generation_thinking_for_model(model, policy)
            spec = helpers["get_provider_spec"](provider)
            member = helpers["EnsembleMemberConfig"](
                provider_config=helpers["ProviderConfig"](
                    provider=provider,
                    model=model,
                    base_url=spec.default_base_url,
                ),
                label=f"{role}_{index}",
                temperature=config.generation.temperature,
                max_tokens=int(config.generation.max_tokens),
                thinking=thinking,
                k=1,
            )
            if role.startswith("aggregator"):
                configured_cap = int(config.ensemble.aggregator_max_tokens_cap)
                configured_reserve = int(config.ensemble.aggregator_visible_answer_reserve_tokens)
                resolved = helpers["aggregator_chat_config"](
                    base,
                    member,
                    max_tokens_cap=configured_cap,
                    visible_answer_reserve_tokens=configured_reserve,
                    recovery=role == "aggregator_fallback",
                    request_budget_binding=None,
                    record_budget_rebound=False,
                )
                budget = _aggregator_budget_projection(
                    helpers=helpers,
                    base=base,
                    member=member,
                    resolved=resolved,
                    configured_cap=configured_cap,
                    configured_reserve=configured_reserve,
                    recovery=role == "aggregator_fallback",
                )
            else:
                resolved, budget = helpers["proposer_chat_config"](
                    base,
                    member,
                    max_tokens_cap=int(config.ensemble.proposer_max_tokens_cap),
                    visible_answer_reserve_tokens=int(
                        config.ensemble.proposer_visible_answer_reserve_tokens
                    ),
                    max_tokens_cap_explicit=max_tokens_cap_explicit,
                    request_budget_binding=None,
                )
            compat_policy = helpers["compat_policy_for_kind"](provider)
            base_url = str(spec.default_base_url or "")
            sends_temperature = helpers["should_send_temperature"](
                compat_policy,
                base_url,
                model,
                resolved,
                helpers["member_model_capabilities"](member),
            )
            capabilities = helpers["member_model_capabilities"](member)
            declared_projection.append(
                {
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "temperature": member.temperature,
                    "max_tokens": member.max_tokens,
                    "thinking": member.thinking,
                    "thinking_budget_tokens": int(base.thinking_budget_tokens or 0),
                }
            )
            member_rows.append(
                {
                    "ordinal": index,
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "max_tokens": int(resolved.max_tokens),
                    "thinking": bool(resolved.thinking),
                    "thinking_level": (
                        str(resolved.thinking_level)
                        if resolved.thinking_level is not None
                        else None
                    ),
                    "thinking_budget_tokens": int(
                        resolved.thinking_budget_tokens if resolved.thinking else 0
                    ),
                    "thinking_budget_explicit": bool(resolved.thinking_budget_explicit),
                    "request_visible_temperature": resolved.temperature,
                    "temperature_parameter_sent": bool(sends_temperature),
                    "wire_temperature": (resolved.temperature if sends_temperature else None),
                    "tools_enabled": bool(
                        config.ensemble.proposer_tools
                        if role.startswith("proposer")
                        else config.ensemble.aggregator_tools
                    ),
                    "base_url_host": str(urlsplit(base_url).hostname or "").lower(),
                    "compat_official_host": str(compat_policy.official_host or "").lower(),
                    "model_capabilities": _model_capabilities_projection(capabilities),
                    "member_request_budget_rebinding": False,
                    "effective_context_window_tokens": None,
                    "effective_context_window_source": "unbound",
                    "provider_request_max_chars": int(
                        getattr(resolved, "provider_request_max_chars", 0) or 0
                    ),
                    "provider_request_max_chars_source": "inherited",
                    "budget": copy.deepcopy(dict(budget)),
                }
            )
        if declared_generation is not None and declared_generation != declared_projection:
            raise ControllerError(
                f"task {task_id} declared member generation differs from production policy"
            )
        projected[task_id] = {
            "N_min": n_min,
            "N_max": n_max,
            "selected_N": len(selected_p),
            "proposer_count": proposer_count,
            "proposer_sample_count": selection.get("proposer_sample_count"),
            "selected_P": selected_p,
            "selected_A": selection["selected_A"],
            "backup_P": list(selection["backup_P"]),
            "configured_proposer_backup_count": selection.get("configured_proposer_backup_count"),
            "effective_proposer_backup_count": len(selection["backup_P"]),
            "aggregator_candidates": list(selection["aggregator_candidates"]),
            "configured_aggregator_candidate_count": selection.get(
                "configured_aggregator_candidate_count"
            ),
            "effective_aggregator_candidate_count": len(selection["aggregator_candidates"]),
            "effective_min_successful_proposers": effective_quorum,
            "legal_min_successful_proposers": legal_quorum,
            "legal_quorum_policy": legal_quorum_policy,
            "proposer_recovery_policy": copy.deepcopy(dict(recovery_policy)),
            "aggregator_recovery_mode": selection.get("aggregator_recovery_mode"),
            "aggregator_recovery_top_k": selection.get("aggregator_recovery_top_k"),
            "wait_for_all_proposers": selection.get("wait_for_all_proposers"),
            "quorum_grace_seconds": selection.get("quorum_grace_seconds"),
            # This is the actual execution behavior changed by P0.5-36.
            "effective_shuffle_candidates": selection.get("effective_shuffle_candidates"),
            "effective_proposer_timeout_seconds": selection.get(
                "effective_proposer_timeout_seconds"
            ),
            "effective_aggregator_timeout_seconds": selection.get(
                "effective_aggregator_timeout_seconds"
            ),
            "aggregator_serving_chain_timeout_seconds": selection.get(
                "aggregator_serving_chain_timeout_seconds"
            ),
            "all_failed_policy": selection.get("all_failed_policy"),
            "candidate_max_chars": selection.get("candidate_max_chars"),
            "proposer_tools": selection.get("proposer_tools"),
            "aggregator_tools": selection.get("aggregator_tools"),
            "record_candidates": selection.get("record_candidates"),
            "provider_state_replay": selection.get("provider_state_replay"),
            "aggregator_prompt": prompt_evidence,
            "member_request_budget_rebinding": False,
            "proposer_max_tokens_cap_explicit": max_tokens_cap_explicit,
            "member_requests": member_rows,
        }
    return projected


def compare_behavior_projections(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    expected_task_count: int | None = None,
) -> dict[str, Any]:
    if set(baseline) != set(candidate):
        raise ControllerError("baseline/candidate projection task coverage differs")
    if expected_task_count is not None and len(baseline) != expected_task_count:
        raise ControllerError(
            f"behavior projection must contain exactly {expected_task_count} tasks"
        )
    changes = [
        {
            "task_id": task_id,
            "before_sha256": canonical_sha256(baseline[task_id]),
            "after_sha256": canonical_sha256(candidate[task_id]),
        }
        for task_id in sorted(baseline)
        if canonical_bytes(baseline[task_id]) != canonical_bytes(candidate[task_id])
    ]
    identical_count = len(baseline) - len(changes)
    return {
        "changed_task_count": len(changes),
        "byte_identical_task_count": identical_count,
        "expected_task_count": expected_task_count,
        "all_tasks_byte_identical": (
            identical_count == len(baseline)
            and (expected_task_count is None or identical_count == expected_task_count)
        ),
        "changed_tasks": changes,
        "baseline_projection_sha256": canonical_sha256(baseline),
        "candidate_projection_sha256": canonical_sha256(candidate),
    }


def offline_effect_decision(
    *,
    comparisons: Mapping[str, Mapping[str, Any]],
    arm_ids: Sequence[str],
    budget_gated: bool,
    production_budget_projection_complete: bool,
    projection_uncertain: bool,
) -> str:
    """Delete only a proven ten-task byte-identical candidate."""

    if projection_uncertain or not comparisons:
        return "run_conservative_projection_uncertain"
    byte_identical = all(
        comparison.get("all_tasks_byte_identical") is True
        and comparison.get("byte_identical_task_count") == EXPECTED_TASK_COUNT
        for comparison in comparisons.values()
    )
    if not byte_identical:
        return "run"
    if any(arm_id in REQUIRED_REPLICATE_ARMS for arm_id in arm_ids):
        return "run_required_replicate"
    if budget_gated and not production_budget_projection_complete:
        return "run_conservative_unproven_budget_binding"
    return "deleted_no_live_run"


def initialize_status(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    plan_sha256: str,
    snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "run_id": plan["run_id"],
        "campaign_plan_sha256": plan_sha256,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "phase": "prepared",
        "active_arm": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": utc_now(),
        "derived_plan": None,
        "arms": {
            arm.arm_id: {
                "experiment_id": arm.experiment_id,
                "variant": arm.variant,
                "replicate": arm.replicate,
                "analyzer_mode": arm.analyzer_mode,
                "control_arm_id": arm.control_arm_id,
                "state": "pending",
                "attempts": [],
                "output_dir": str(output_dir(plan, arm)),
            }
            for arm in arms
        },
        "no_op_experiments": {
            str(row["id"]): {"state": "pending", "receipt": None}
            for row in plan.get("no_op_experiments", [])
        },
        "offline_no_op_arms": {},
    }


def validate_snapshot(plan: Mapping[str, Any]) -> tuple[Path, dict[str, str]]:
    snapshot = Path(str(plan["paths"]["snapshot"])).resolve()
    identity = git_identity(snapshot)
    expected_commit = str(plan["freeze"]["snapshot_commit"])
    expected_tree = str(plan["freeze"]["snapshot_tree"])
    if identity["commit"] != expected_commit:
        raise ControllerError(f"snapshot commit differs: {identity['commit']} != {expected_commit}")
    if identity["tree"] != expected_tree:
        raise ControllerError(f"snapshot tree differs: {identity['tree']} != {expected_tree}")
    if identity["status"]:
        raise ControllerError("formal campaign snapshot is dirty")
    return snapshot, identity


def registry_identities(
    models: Sequence[Any],
    *,
    label: str,
) -> list[str]:
    identities: list[str] = []
    for index, raw in enumerate(models):
        if not isinstance(raw, Mapping):
            raise ControllerError(f"{label} registry model {index} is malformed")
        facts = raw.get("registry_facts")
        source = facts if isinstance(facts, Mapping) else raw
        explicit_identity = str(source.get("identity") or "").strip().lower()
        provider = str(source.get("provider") or "").strip().lower()
        model = str(source.get("model_id") or source.get("model") or "").strip().lower()
        identity = explicit_identity or (f"{provider}:{model}" if provider and model else "")
        try:
            parsed_provider, parsed_model = _parse_identity(identity)
        except ControllerError as exc:
            raise ControllerError(
                f"{label} registry model {index} has no canonical identity"
            ) from exc
        identities.append(f"{parsed_provider.lower()}:{parsed_model.lower()}")
    if len(identities) != len(set(identities)):
        raise ControllerError(f"{label} registry identities are not unique")
    return sorted(identities)


def compute_runtime_freeze_identity(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
) -> dict[str, Any]:
    """Recompute every mutable input/source identity without exposing content."""

    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    benchmark_path = reference_repo / "data/draco/mini.jsonl"
    reference_config_path = reference_repo / ".local-state/config.toml"
    launcher_path = snapshot / str(plan["paths"]["launcher_relative"])
    main_runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
    resume_runner_path = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    controller_path = Path(__file__).resolve()
    reporter_path = Path(str(plan["paths"]["reporter"])).resolve()
    registry_contract = plan["freeze"]["model_registry"]
    ranking_contract = plan["freeze"]["ranking_config"]
    registry_path = snapshot / str(registry_contract["path"])
    ranking_path = snapshot / str(ranking_contract["path"])
    for path in (
        benchmark_path,
        reference_config_path,
        launcher_path,
        main_runner_path,
        resume_runner_path,
        controller_path,
        reporter_path,
        registry_path,
        ranking_path,
    ):
        require_regular_file(path)

    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.ranking_router import (  # type: ignore
            _legacy_registry_snapshot_projection,
            load_model_registry_snapshot,
            ranking_config_resolution,
        )

        full_registry = load_model_registry_snapshot()
        formal_registry = _legacy_registry_snapshot_projection(full_registry)
        formal_ranking_resolution = ranking_config_resolution(
            thinking_assignment_enabled=False,
        )
    finally:
        sys.path.pop(0)
    packaged_ranking = load_json(ranking_path)
    full_models = full_registry.get("models")
    formal_models = formal_registry.get("models")
    if not isinstance(full_models, list) or not isinstance(formal_models, list):
        raise ControllerError("packaged model registry is malformed")
    full_identities = registry_identities(full_models, label="full")
    formal_identities = registry_identities(formal_models, label="formal")
    formal_ranking = formal_ranking_resolution.get("effective_config")
    if not isinstance(formal_ranking, Mapping):
        raise ControllerError("formal ranking projection is unavailable")
    return {
        "inputs": {
            "benchmark_input_raw_sha256": file_sha256(benchmark_path),
            "reference_config_raw_sha256": file_sha256(reference_config_path),
        },
        "sources": {
            "launcher_raw_sha256": file_sha256(launcher_path),
            "controller_raw_sha256": file_sha256(controller_path),
            "reporter_raw_sha256": file_sha256(reporter_path),
            "main_runner_raw_sha256": file_sha256(main_runner_path),
            "resume_runner_raw_sha256": file_sha256(resume_runner_path),
        },
        "model_registry": {
            "path": str(registry_contract["path"]),
            "raw_sha256": file_sha256(registry_path),
            "full_snapshot_version": str(full_registry.get("snapshot_version") or ""),
            "full_canonical_sha256": canonical_sha256(full_registry),
            "formal_snapshot_version": str(formal_registry.get("snapshot_version") or ""),
            "formal_canonical_sha256": canonical_sha256(formal_registry),
            "model_count": len(full_models),
            "full_model_count": len(full_identities),
            "formal_model_count": len(formal_identities),
            "full_identities_sha256": canonical_sha256(full_identities),
            "formal_identities_sha256": canonical_sha256(formal_identities),
        },
        "ranking_config": {
            "path": str(ranking_contract["path"]),
            "raw_sha256": file_sha256(ranking_path),
            "packaged_schema_version": str(packaged_ranking.get("schema_version") or ""),
            "packaged_config_version": str(packaged_ranking.get("config_version") or ""),
            "packaged_canonical_sha256": canonical_sha256(packaged_ranking),
            "formal_schema_version": str(formal_ranking.get("schema_version") or ""),
            "formal_config_version": str(formal_ranking.get("config_version") or ""),
            "formal_canonical_sha256": canonical_sha256(formal_ranking),
        },
    }


def validate_runtime_freeze(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    expected_snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    """Fail before every arm if the snapshot or external reference drifted."""

    current_git = git_identity(snapshot)
    if current_git != dict(expected_snapshot_identity):
        raise ControllerError("snapshot identity or cleanliness drifted during campaign")
    actual = compute_runtime_freeze_identity(plan, snapshot=snapshot)
    frozen = plan["freeze"]
    for section in ("inputs", "sources", "model_registry", "ranking_config"):
        expected_section = frozen.get(section)
        if not isinstance(expected_section, Mapping):
            raise ControllerError(f"freeze.{section} is missing")
        if actual[section] != dict(expected_section):
            differing = sorted(
                key
                for key in set(actual[section]) | set(expected_section)
                if actual[section].get(key) != expected_section.get(key)
            )
            raise ControllerError(f"freeze.{section} drifted at: {', '.join(differing)}")
    if actual["inputs"]["benchmark_input_raw_sha256"] != plan["benchmark"]["input_sha256"]:
        raise ControllerError("benchmark input differs from benchmark contract")
    return actual


def validate_static_overlays(plan: Mapping[str, Any], arms: Sequence[Arm], snapshot: Path) -> None:
    base = snapshot / str(plan["paths"]["experiment_config_relative"])
    # Frozen-replay fields and p99-derived values do not exist until E0.  Every
    # other sparse overlay is validated against the production schema now.
    for arm in arms:
        if arm.dynamic is not None or arm.analyzer_mode == "frozen_replay":
            continue
        load_effective_experiment_config(snapshot, base, arm.override)


def resolve_arm_override(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    artifact: Mapping[str, Any] | None,
    p99_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    override = copy.deepcopy(arm.override)
    if arm.dynamic is not None:
        if p99_receipt is None:
            raise ControllerError(f"{arm.arm_id} requires the p99 receipt")
        if arm.dynamic.get("kind") != "analyzer_p99_scale":
            raise ControllerError(f"unknown dynamic derivation for {arm.arm_id}")
        value = p99_receipt["derived_max_output_tokens"][arm.arm_id]
        set_path(override, arm.dynamic["path"], value)
    if arm.analyzer_mode == "frozen_replay":
        if artifact is None:
            raise ControllerError(f"{arm.arm_id} requires frozen Analyzer profiles")
        override = deep_merge(override, make_replay_overlay(plan, artifact))
    return override


def arm_completion_identity(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    config = load_effective_experiment_config(
        snapshot,
        snapshot / str(plan["paths"]["experiment_config_relative"]),
        override,
    )
    if not hasattr(config, "model_dump"):
        raise ControllerError("effective experiment config cannot be authenticated")
    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
    resume_runner_path = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    return {
        "arm_id": arm.arm_id,
        "output_name": arm.output_name,
        "run_id": str(plan["run_id"]),
        "output_dir": str(output_dir(plan, arm).resolve()),
        "snapshot": str(snapshot.resolve()),
        "snapshot_commit": snapshot_identity["commit"],
        "runner_identities": {
            str(runner_path.resolve()): file_sha256(runner_path),
            str(resume_runner_path.resolve()): file_sha256(resume_runner_path),
        },
        "benchmark_path": str((reference_repo / "data/draco/mini.jsonl").resolve()),
        "reference_config_path": str((reference_repo / ".local-state/config.toml").resolve()),
        "benchmark_sha256": str(plan["benchmark"]["input_sha256"]),
        "task_ids": sorted(plan["benchmark"]["task_ids"]),
        "task_concurrency": int(plan["execution"]["task_concurrency"]),
        "judge_concurrency": int(plan["execution"]["judge_concurrency"]),
        "generation_max_attempts": int(plan["execution"]["generation_max_attempts"]),
        "override_sha256": canonical_sha256(override),
        "effective_config_sha256": canonical_sha256(config.model_dump(mode="json")),
    }


def launch_arm(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    override: Mapping[str, Any],
) -> int:
    arm_report_root = Path(str(plan["paths"]["report_root"])) / arm.directory_name
    arm_report_root.mkdir(parents=True, exist_ok=True)
    launcher = snapshot / str(plan["paths"]["launcher_relative"])
    command = [
        str(launcher),
        "--snapshot-repo",
        str(snapshot),
        "--output-name",
        arm.output_name,
        "--groups",
        "G1",
        "--experiment-config-override-json",
        json.dumps(override, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "DRACO_CAMPAIGN_REPORT_ROOT": str(arm_report_root),
            "DRACO_CAMPAIGN_REFERENCE_REPO": str(plan["paths"]["reference_repo"]),
            "DRACO_CAMPAIGN_PYTHON": str(plan["paths"]["python"]),
            "DRACO_CAMPAIGN_TASK_CONCURRENCY": "6",
        }
    )
    print(f"[{utc_now()}] START {arm.arm_id}", flush=True)
    completed = subprocess.run(command, env=environment, check=False)
    print(f"[{utc_now()}] END {arm.arm_id} rc={completed.returncode}", flush=True)
    return int(completed.returncode)


def status_path(plan: Mapping[str, Any]) -> Path:
    return Path(str(plan["paths"]["run_root"])) / "status.json"


def load_or_initialize_status(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    plan_sha256: str,
    snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    path = status_path(plan)
    if path.exists():
        status_payload = load_json(path)
        if status_payload.get("schema") != STATUS_SCHEMA:
            raise ControllerError("status schema differs")
        if status_payload.get("campaign_plan_sha256") != plan_sha256:
            raise ControllerError("status is bound to a different campaign plan")
        if status_payload.get("snapshot_commit") != snapshot_identity["commit"]:
            raise ControllerError("status is bound to a different snapshot commit")
        return status_payload
    payload = initialize_status(
        plan,
        arms,
        plan_sha256=plan_sha256,
        snapshot_identity=snapshot_identity,
    )
    atomic_write_json(path, payload)
    return payload


def update_status(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload)


def prepare_derived(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    plan_sha256: str,
    source_arm: Arm,
    source_dir: Path,
) -> dict[str, Any]:
    run_root = Path(str(plan["paths"]["run_root"]))
    receipt_root = run_root / "receipts"
    artifact = extract_analyzer_artifact(
        source_arm=source_arm,
        source_dir=source_dir,
        destination=run_root / "frozen-analyzer-profiles.json",
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        snapshot_identity=snapshot_identity,
        plan_sha256=plan_sha256,
    )
    p99 = derive_analyzer_p99_receipt(
        artifact,
        destination=receipt_root / "P0.5-06-analyzer-p99.json",
        plan_sha256=plan_sha256,
    )
    noop = make_noop_temperature_receipt(
        plan,
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        destination=receipt_root / "P0.5-07-no-op.json",
        plan_sha256=plan_sha256,
    )

    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    expected_task_ids = set(plan["benchmark"]["task_ids"])
    baseline_overlay = make_replay_overlay(plan, artifact)
    baseline_dry_plans, baseline_dry_evidence = run_main_dry_replay(
        plan,
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        overlay=baseline_overlay,
        expected_task_ids=expected_task_ids,
        label="baseline-frozen-replay",
    )
    live_plans = _source_selection_plans(
        source_dir / "trace.jsonl",
        expected_task_ids=expected_task_ids,
        require_dry_replay=False,
    )
    baseline_config = load_effective_experiment_config(
        snapshot,
        base_config,
        baseline_overlay,
    )
    helpers = _runtime_helpers(snapshot)
    baseline_ensemble_fields = set(
        getattr(baseline_config.ensemble, "model_fields_set", set()) or set()
    )
    production_cap_explicit = "proposer_max_tokens_cap" in baseline_ensemble_fields
    production_cap_key = "explicit" if production_cap_explicit else "implicit"
    baseline_projection_by_explicit: dict[str, dict[str, Any]] = {}
    baseline_equivalence: dict[str, Any] = {}
    for explicit in (False, True):
        key = "explicit" if explicit else "implicit"
        live_projection = request_visible_selection_projection(
            snapshot=snapshot,
            config=baseline_config,
            selections=live_plans,
            max_tokens_cap_explicit=explicit,
        )
        dry_projection = request_visible_selection_projection(
            snapshot=snapshot,
            config=baseline_config,
            selections=baseline_dry_plans,
            max_tokens_cap_explicit=explicit,
        )
        comparison = compare_behavior_projections(
            live_projection,
            dry_projection,
            expected_task_count=EXPECTED_TASK_COUNT,
        )
        if comparison["changed_task_count"] != 0:
            raise ControllerError(
                "baseline frozen replay dry route differs from live E0 selection projection"
            )
        baseline_projection_by_explicit[key] = dry_projection
        baseline_equivalence[key] = comparison

    unique_overlays: dict[str, dict[str, Any]] = {}
    for arm in arms:
        if arm.analyzer_mode != "frozen_replay":
            continue
        candidate_override = resolve_arm_override(
            plan,
            arm,
            artifact=artifact,
            p99_receipt=p99,
        )
        overlay_sha = canonical_sha256(candidate_override)
        unique = unique_overlays.setdefault(
            overlay_sha,
            {"overlay": candidate_override, "arm_ids": []},
        )
        unique["arm_ids"].append(arm.arm_id)

    offline_by_arm: dict[str, Any] = {}
    unique_receipts: dict[str, Any] = {}
    baseline_overlay_sha = canonical_sha256(baseline_overlay)
    for overlay_sha, unique in sorted(unique_overlays.items()):
        candidate_overlay = unique["overlay"]
        arm_ids = sorted(unique["arm_ids"])
        experiments = {
            next(arm.experiment_id for arm in arms if arm.arm_id == arm_id) for arm_id in arm_ids
        }
        budget_gated = bool(experiments & PRODUCTION_BUDGET_GATE_EXPERIMENTS)
        comparisons: dict[str, Any] = {}
        candidate_projection_by_explicit: dict[str, dict[str, Any]] = {}
        projection_error: dict[str, Any] | None = None
        try:
            if overlay_sha == baseline_overlay_sha:
                candidate_plans = baseline_dry_plans
                dry_evidence = baseline_dry_evidence
            else:
                candidate_plans, dry_evidence = run_main_dry_replay(
                    plan,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    overlay=candidate_overlay,
                    expected_task_ids=expected_task_ids,
                    label=arm_ids[0],
                )
            candidate_config = load_effective_experiment_config(
                snapshot,
                base_config,
                candidate_overlay,
            )
            candidate_fields = set(
                getattr(candidate_config.ensemble, "model_fields_set", set()) or set()
            )
            if ("proposer_max_tokens_cap" in candidate_fields) != production_cap_explicit:
                raise ControllerError(
                    "candidate proposer-cap explicitness differs from production baseline"
                )
            for explicit in (False, True):
                key = "explicit" if explicit else "implicit"
                candidate_projection = request_visible_selection_projection(
                    snapshot=snapshot,
                    config=candidate_config,
                    selections=candidate_plans,
                    max_tokens_cap_explicit=explicit,
                )
                candidate_projection_by_explicit[key] = candidate_projection
                comparisons[key] = compare_behavior_projections(
                    baseline_projection_by_explicit[key],
                    candidate_projection,
                    expected_task_count=EXPECTED_TASK_COUNT,
                )
        except Exception as exc:  # noqa: BLE001 - uncertainty forces a paid run
            error_message = str(exc)
            projection_error = {
                "error_class": type(exc).__name__,
                "message_sha256": hashlib.sha256(
                    error_message.encode("utf-8", errors="replace")
                ).hexdigest(),
            }
            dry_evidence = {
                "status": "projection_failed",
                "attempted": overlay_sha != baseline_overlay_sha,
                "overlay_sha256": overlay_sha,
                "error": projection_error,
            }
        complete_budget_proof = bool(
            projection_error is None and helpers["draco_request_budget_rebinding_disabled"]
        )
        decision = offline_effect_decision(
            comparisons=comparisons,
            arm_ids=arm_ids,
            budget_gated=budget_gated,
            production_budget_projection_complete=complete_budget_proof,
            projection_uncertain=projection_error is not None,
        )
        temperature_analysis_scope: dict[str, Any] | None = None
        if "P0.5-11" in experiments and projection_error is None:
            temperature_tasks = []
            for task_id, task_projection in sorted(
                candidate_projection_by_explicit[production_cap_key].items()
            ):
                members = [
                    {
                        "role": row["role"],
                        "model": row["identity"].split(":", 1)[1],
                        "temperature_parameter_sent": row["temperature_parameter_sent"],
                        "wire_temperature": row["wire_temperature"],
                    }
                    for row in task_projection["member_requests"]
                ]
                temperature_tasks.append(
                    {
                        "task_id": task_id,
                        "members": members,
                        "has_wire_temperature_member": any(
                            row["temperature_parameter_sent"] for row in members
                        ),
                    }
                )
            temperature_analysis_scope = {
                "schema": "opensquilla.draco.temperature-wire-analysis-scope/v1",
                "status": "exact_production_compat_projection",
                "tasks": temperature_tasks,
                "analyzable_task_ids": [
                    row["task_id"]
                    for row in temperature_tasks
                    if row["has_wire_temperature_member"]
                ],
            }
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "kind": "offline-main-runner-behavior-effect",
            "created_at": utc_now(),
            "arm_ids": arm_ids,
            "experiment_ids": sorted(experiments),
            "campaign_plan_sha256": plan_sha256,
            "source_artifact_sha256": artifact["artifact_sha256"],
            "overlay_sha256": overlay_sha,
            "decision": decision,
            "dry_run": dry_evidence,
            "comparison_by_proposer_cap_explicitness": comparisons,
            "production_proposer_cap_explicitness": production_cap_key,
            "projection_uncertainty": projection_error,
            "actual_sent_temperature_contract": (
                "production openai._should_send_temperature with official provider host"
            ),
            "shuffle_behavior_field": "effective_shuffle_candidates",
            "production_budget_gate": budget_gated,
            "production_budget_projection_complete": complete_budget_proof,
            "member_request_budget_rebinding": False,
            "member_request_budget_rebinding_proven_disabled": bool(
                helpers["draco_request_budget_rebinding_disabled"]
            ),
            "context_cap_contract": (
                "member request-budget rebinding is false; production context-window "
                "bindings would only rederive provider_request_max_chars, not output max_tokens"
            ),
            "request_budget_binding": (
                "disabled by DRACO build_experiment_provider and production builder default"
                if complete_budget_proof
                else "unproven; arm forced live"
            ),
            "helper_source_sha256": {
                "ensemble": helpers["ensemble_source_sha256"],
                "openai": helpers["openai_source_sha256"],
                "runner": helpers["runner_source_sha256"],
                "aggregator_prompt": helpers["aggregator_prompt_source_sha256"],
            },
        }
        if temperature_analysis_scope is not None:
            receipt["temperature_analysis_scope"] = temperature_analysis_scope
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        destination = receipt_root / f"offline-overlay-{overlay_sha}.json"
        atomic_write_json(destination, receipt)
        unique_receipts[overlay_sha] = {
            **receipt,
            "path": str(destination),
            "file_sha256": file_sha256(destination),
        }
        for arm_id in arm_ids:
            offline_by_arm[arm_id] = {
                "decision": decision,
                "overlay_sha256": overlay_sha,
                "receipt_path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
            }
            if temperature_analysis_scope is not None:
                offline_by_arm[arm_id]["temperature_analysis_scope"] = temperature_analysis_scope
                # Exact reporter-facing convenience schema required by the
                # P0.5-11 contract. The richer scope above remains stable.
                offline_by_arm[arm_id]["tasks"] = copy.deepcopy(temperature_analysis_scope["tasks"])

    derived: dict[str, Any] = {
        "schema": DERIVED_SCHEMA,
        "created_at": utc_now(),
        "campaign_plan_sha256": plan_sha256,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "source_arm_id": source_arm.arm_id,
        "source_output_dir": str(source_dir),
        "frozen_analyzer_artifact": {
            "path": str(run_root / "frozen-analyzer-profiles.json"),
            "artifact_sha256": artifact["artifact_sha256"],
            "file_sha256": file_sha256(run_root / "frozen-analyzer-profiles.json"),
        },
        "p0_5_06": p99,
        "p0_5_07": noop,
        "baseline_dry_replay": {
            **baseline_dry_evidence,
            "equivalence": baseline_equivalence,
        },
        "offline_effect": offline_by_arm,
        "offline_unique_overlays": unique_receipts,
        "offline_unique_overlay_count": len(unique_receipts),
    }
    derived["derived_plan_sha256"] = canonical_sha256(derived)
    atomic_write_json(run_root / "derived-plan.json", derived)
    return derived


def load_derived(
    plan: Mapping[str, Any], plan_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_root = Path(str(plan["paths"]["run_root"]))
    derived = load_json(run_root / "derived-plan.json")
    if derived.get("schema") != DERIVED_SCHEMA:
        raise ControllerError("derived plan schema differs")
    if derived.get("campaign_plan_sha256") != plan_sha256:
        raise ControllerError("derived plan is bound to a different campaign plan")
    descriptor = derived["frozen_analyzer_artifact"]
    artifact_path = Path(str(descriptor["path"]))
    artifact = load_json(artifact_path)
    if descriptor.get("file_sha256") != file_sha256(artifact_path):
        raise ControllerError("frozen Analyzer artifact raw hash differs")
    if descriptor.get("artifact_sha256") != artifact.get("artifact_sha256"):
        raise ControllerError("frozen Analyzer artifact semantic hash differs")
    artifact_without_hash = dict(artifact)
    recorded_artifact_hash = artifact_without_hash.pop("artifact_sha256", None)
    if recorded_artifact_hash != canonical_sha256(artifact_without_hash):
        raise ControllerError("frozen Analyzer artifact self-hash differs")
    derived_without_hash = dict(derived)
    recorded_derived_hash = derived_without_hash.pop("derived_plan_sha256", None)
    if recorded_derived_hash != canonical_sha256(derived_without_hash):
        raise ControllerError("derived plan self-hash differs")
    if (
        derived.get("snapshot_commit") != plan["freeze"]["snapshot_commit"]
        or derived.get("snapshot_tree") != plan["freeze"]["snapshot_tree"]
        or derived.get("source_arm_id") != "common-E0-R1"
    ):
        raise ControllerError("derived plan source/snapshot identity differs")
    if artifact.get("task_ids") != sorted(plan["benchmark"]["task_ids"]):
        raise ControllerError("frozen Analyzer artifact task identity differs")
    source = artifact.get("source")
    if not isinstance(source, Mapping):
        raise ControllerError("frozen Analyzer artifact source binding is missing")
    source_dir = Path(str(derived.get("source_output_dir") or ""))
    if source_dir.resolve() != Path(str(source.get("output_dir") or "")).resolve():
        raise ControllerError("derived/source Analyzer output directory differs")
    source_arm = next(arm for arm in expand_arms(plan) if arm.arm_id == "common-E0-R1")
    snapshot = Path(str(plan["paths"]["snapshot"])).resolve()
    source_identity = arm_completion_identity(
        plan,
        source_arm,
        snapshot=snapshot,
        snapshot_identity={
            "commit": str(plan["freeze"]["snapshot_commit"]),
            "tree": str(plan["freeze"]["snapshot_tree"]),
        },
        override=source_arm.override,
    )
    source_complete, source_evidence = inspect_complete_arm(
        source_dir,
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        expected_task_concurrency=int(plan["execution"]["task_concurrency"]),
        expected_identity=source_identity,
    )
    if not source_complete:
        raise ControllerError(
            "derived Analyzer source arm is no longer authenticated: "
            + str(source_evidence.get("reason") or "unknown")
        )
    for field, filename in (
        ("manifest_sha256", "manifest.json"),
        ("trace_sha256", "trace.jsonl"),
    ):
        if source.get(field) != file_sha256(source_dir / filename):
            raise ControllerError(f"frozen Analyzer source {filename} binding differs")
    replay = artifact.get("replay_payload")
    if not isinstance(replay, Mapping) or replay.get("source_results_sha256") != file_sha256(
        source_dir / "results.jsonl"
    ):
        raise ControllerError("frozen Analyzer source results binding differs")

    for label, filename in (
        ("p0_5_06", "P0.5-06-analyzer-p99.json"),
        ("p0_5_07", "P0.5-07-no-op.json"),
    ):
        embedded = derived.get(label)
        if not isinstance(embedded, Mapping):
            raise ControllerError(f"derived {label} receipt is missing")
        verify_bare_document_self_hash(
            embedded,
            field="receipt_sha256",
            label=label,
        )
        receipt_path = run_root / "receipts" / filename
        if load_json(receipt_path) != embedded:
            raise ControllerError(f"derived {label} receipt file differs")

    unique = derived.get("offline_unique_overlays")
    offline = derived.get("offline_effect")
    if not isinstance(unique, Mapping) or not isinstance(offline, Mapping):
        raise ControllerError("derived offline-effect receipts are missing")
    expected_replay_arms = {
        arm.arm_id for arm in expand_arms(plan) if arm.analyzer_mode == "frozen_replay"
    }
    if set(offline) != expected_replay_arms:
        raise ControllerError("derived offline-effect arm coverage differs")
    if derived.get("offline_unique_overlay_count") != len(unique):
        raise ControllerError("derived unique overlay count differs")
    for overlay_sha, embedded_with_path in unique.items():
        if not isinstance(embedded_with_path, Mapping):
            raise ControllerError("derived offline receipt is malformed")
        embedded = dict(embedded_with_path)
        receipt_path = Path(str(embedded.pop("path", "")))
        recorded_file_sha = embedded.pop("file_sha256", None)
        if (
            str(overlay_sha) != embedded.get("overlay_sha256")
            or recorded_file_sha != file_sha256(receipt_path)
            or load_json(receipt_path) != embedded
        ):
            raise ControllerError("derived offline receipt raw binding differs")
        verify_bare_document_self_hash(
            embedded,
            field="receipt_sha256",
            label=f"offline overlay {overlay_sha}",
        )
    for arm_id, descriptor_row in offline.items():
        if not isinstance(descriptor_row, Mapping):
            raise ControllerError(f"derived offline descriptor {arm_id} is malformed")
        receipt = unique.get(descriptor_row.get("overlay_sha256"))
        if (
            not isinstance(receipt, Mapping)
            or descriptor_row.get("receipt_path") != receipt.get("path")
            or descriptor_row.get("receipt_sha256") != receipt.get("receipt_sha256")
            or descriptor_row.get("decision") not in {*RUN_DECISIONS, "deleted_no_live_run"}
        ):
            raise ControllerError(f"derived offline descriptor {arm_id} differs")
    return derived, artifact


def reconcile_status_from_derived(
    status: dict[str, Any],
    derived: Mapping[str, Any],
) -> None:
    """Idempotently close the derived-plan/status crash window."""

    status["derived_plan"] = {
        "path": str(
            Path(str(derived["frozen_analyzer_artifact"]["path"])).parent / "derived-plan.json"
        ),
        "sha256": derived["derived_plan_sha256"],
    }
    status["no_op_experiments"]["P0.5-07"] = {
        "state": "no_op_deleted",
        "receipt": copy.deepcopy(derived["p0_5_07"]),
    }
    for arm_id, descriptor in derived["offline_effect"].items():
        if descriptor.get("decision") != "deleted_no_live_run":
            continue
        arm_state = status["arms"][arm_id]
        arm_state.update(
            {
                "state": "no_op_deleted",
                "completed_at": arm_state.get("completed_at") or utc_now(),
                "offline_effect_receipt": descriptor["receipt_path"],
            }
        )
        experiment_id = arm_state["experiment_id"]
        status.setdefault("offline_no_op_arms", {})[arm_id] = {
            "experiment_id": experiment_id,
            "receipt": descriptor["receipt_path"],
            "receipt_sha256": descriptor["receipt_sha256"],
        }
        experiment = status["no_op_experiments"].setdefault(
            experiment_id,
            {
                "state": "contains_offline_no_op_arm",
                "receipt": None,
                "arm_ids": [],
            },
        )
        experiment["state"] = "contains_offline_no_op_arm"
        arm_ids = experiment.setdefault("arm_ids", [])
        if arm_id not in arm_ids:
            arm_ids.append(arm_id)
        arm_ids.sort()


def no_op_status_is_terminal(status: Mapping[str, Any]) -> bool:
    rows = status.get("no_op_experiments")
    if not isinstance(rows, Mapping) or not rows:
        return False
    return all(
        isinstance(row, Mapping)
        and row.get("state") in {"no_op_deleted", "contains_offline_no_op_arm"}
        for row in rows.values()
    )


def campaign_terminal_phase(
    status: Mapping[str, Any],
    *,
    reporting_complete: bool | None = None,
) -> str:
    arms = status.get("arms")
    terminal_states = (
        {row.get("state") for row in arms.values() if isinstance(row, Mapping)}
        if isinstance(arms, Mapping)
        else {"invalid"}
    )
    complete = (
        bool(terminal_states)
        and terminal_states <= {"succeeded", "no_op_deleted"}
        and no_op_status_is_terminal(status)
        and reporting_complete is not False
    )
    return "succeeded" if complete else "completed_with_failures"


def publish_terminal_status_input(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish the immutable status evidence consumed by the reporter."""

    frozen = copy.deepcopy(dict(status))
    frozen.pop("reporting", None)
    frozen.pop("terminal_status_input", None)
    frozen["terminal_freeze"] = {
        "schema": "opensquilla.draco-p0-p05-terminal-status-input/v1",
        "campaign_plan_sha256": frozen.get("campaign_plan_sha256"),
        "run_id": frozen.get("run_id"),
        "phase": frozen.get("phase"),
    }
    frozen["terminal_status_input_sha256"] = canonical_sha256(frozen)
    destination = Path(str(plan["paths"]["run_root"])) / "terminal-status-input.json"
    atomic_write_json(destination, frozen)
    return {
        "schema": "opensquilla.draco-p0-p05-terminal-status-input/v1",
        "path": str(destination),
        "semantic_sha256": frozen["terminal_status_input_sha256"],
        "file_sha256": file_sha256(destination),
        "campaign_plan_sha256": frozen["campaign_plan_sha256"],
    }


def run_terminal_report(
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    terminal_status_path: Path,
) -> tuple[dict[str, Any], bool]:
    """Run the frozen reporter after arm execution reaches a terminal phase."""

    run_root = Path(str(plan["paths"]["run_root"]))
    reporter = Path(str(plan["paths"]["reporter"])).resolve()
    command = [
        str(plan["paths"]["python"]),
        str(reporter),
        "--plan",
        str(plan_path.resolve()),
        "--status",
        str(terminal_status_path.resolve()),
        "--strict",
    ]
    error: dict[str, Any] | None = None
    returncode: int | None = None
    stdout = ""
    stderr = ""
    try:
        require_regular_file(reporter)
        if file_sha256(reporter) != plan["freeze"]["sources"]["reporter_raw_sha256"]:
            raise ControllerError("frozen reporter raw hash differs")
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
        )
        returncode = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except Exception as exc:  # noqa: BLE001 - preserve a terminal report receipt
        message = str(exc)
        error = {
            "error_class": type(exc).__name__,
            "message_sha256": hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest(),
        }
    report_root = Path(str(plan["paths"]["report_root"]))
    artifacts: dict[str, Any] = {}
    for name in ("EXPERIMENT_RESULTS.md", "EXPERIMENT_RESULTS.json"):
        path = report_root / name
        if path.exists():
            try:
                require_regular_file(path)
                artifacts[name] = {
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            except (ControllerError, OSError):
                pass
    success = error is None and returncode == 0 and len(artifacts) == 2
    partial = error is None and returncode == 2 and len(artifacts) == 2
    receipt: dict[str, Any] = {
        "schema": "opensquilla.draco-p0-p05-terminal-report/v1",
        "created_at": utc_now(),
        "reporter_path": str(reporter),
        "reporter_raw_sha256": (
            file_sha256(reporter) if reporter.is_file() and not reporter.is_symlink() else None
        ),
        "command_sha256": canonical_sha256(command),
        "strict": True,
        "terminal_status_input_path": str(terminal_status_path.resolve()),
        "terminal_status_input_file_sha256": file_sha256(terminal_status_path),
        "returncode": returncode,
        "status": "complete" if success else "partial" if partial else "failed",
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "error": error,
        "artifacts": artifacts,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    destination = run_root / "terminal-report-receipt.json"
    atomic_write_json(destination, receipt)
    return {**receipt, "path": str(destination), "file_sha256": file_sha256(destination)}, success


def run_campaign(plan_path: Path) -> int:
    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    plan_sha256 = canonical_sha256(plan)
    snapshot, snapshot_identity = validate_snapshot(plan)
    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    validate_static_overlays(plan, arms, snapshot)
    run_root = Path(str(plan["paths"]["run_root"]))
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / "controller.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError("another campaign controller is active") from exc
        status = load_or_initialize_status(
            plan,
            arms,
            plan_sha256=plan_sha256,
            snapshot_identity=snapshot_identity,
        )
        status_file = status_path(plan)
        status["phase"] = "running"
        status["started_at"] = status.get("started_at") or utc_now()
        status["completed_at"] = None
        update_status(status_file, status)

        expected_task_ids = set(plan["benchmark"]["task_ids"])
        source_arm = next(arm for arm in arms if arm.arm_id == "common-E0-R1")
        derived: dict[str, Any] | None = None
        artifact: dict[str, Any] | None = None
        derived_path = run_root / "derived-plan.json"
        if derived_path.exists():
            derived, artifact = load_derived(plan, plan_sha256)
            reconcile_status_from_derived(status, derived)
            update_status(status_file, status)

        any_failure = False
        for arm in arms:
            arm_state = status["arms"][arm.arm_id]
            directory = output_dir(plan, arm)
            override: dict[str, Any] | None = None
            expected_identity: dict[str, Any] | None = None
            try:
                override = resolve_arm_override(
                    plan,
                    arm,
                    artifact=artifact,
                    p99_receipt=(derived["p0_5_06"] if derived is not None else None),
                )
                expected_identity = arm_completion_identity(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception:  # noqa: BLE001 - handled as a prerequisite below
                override = None
                expected_identity = None
            complete, evidence = inspect_complete_arm(
                directory,
                expected_task_ids=expected_task_ids,
                expected_task_concurrency=6,
                expected_identity=expected_identity,
            )
            if complete:
                arm_state.update(
                    {
                        "state": "succeeded",
                        "completion_evidence": evidence,
                        "completed_at": arm_state.get("completed_at") or utc_now(),
                    }
                )
                update_status(status_file, status)
                if arm.arm_id == source_arm.arm_id and derived is None:
                    try:
                        derived = prepare_derived(
                            plan,
                            arms,
                            snapshot=snapshot,
                            snapshot_identity=snapshot_identity,
                            plan_sha256=plan_sha256,
                            source_arm=source_arm,
                            source_dir=directory,
                        )
                        derived, artifact = load_derived(plan, plan_sha256)
                        reconcile_status_from_derived(status, derived)
                    except Exception as exc:  # noqa: BLE001 - later live arms still run
                        message = str(exc)
                        derived = None
                        artifact = None
                        status["derived_failure"] = {
                            "error_class": type(exc).__name__,
                            "message_sha256": hashlib.sha256(
                                message.encode("utf-8", errors="replace")
                            ).hexdigest(),
                        }
                        any_failure = True
                    update_status(status_file, status)
                continue

            if arm.analyzer_mode == "frozen_replay" and derived is not None:
                offline = derived["offline_effect"].get(arm.arm_id)
                if not isinstance(offline, Mapping):
                    raise ControllerError(f"derived plan lacks offline receipt for {arm.arm_id}")
                decision = offline["decision"]
                if decision == "deleted_no_live_run":
                    arm_state.update(
                        {
                            "state": "no_op_deleted",
                            "completed_at": utc_now(),
                            "offline_effect_receipt": offline["receipt_path"],
                        }
                    )
                    status.setdefault("offline_no_op_arms", {})[arm.arm_id] = {
                        "experiment_id": arm.experiment_id,
                        "receipt": offline["receipt_path"],
                        "receipt_sha256": offline["receipt_sha256"],
                    }
                    experiment_noop = status["no_op_experiments"].setdefault(
                        arm.experiment_id,
                        {
                            "state": "contains_offline_no_op_arm",
                            "receipt": None,
                            "arm_ids": [],
                        },
                    )
                    no_op_arm_ids = experiment_noop.setdefault("arm_ids", [])
                    if arm.arm_id not in no_op_arm_ids:
                        no_op_arm_ids.append(arm.arm_id)
                    update_status(status_file, status)
                    continue
                if decision not in RUN_DECISIONS:
                    raise ControllerError(
                        f"unsupported offline decision for {arm.arm_id}: {decision!r}"
                    )

            if directory.exists():
                arm_state.update(
                    {
                        "state": "failed",
                        "completed_at": utc_now(),
                        "failure": {
                            "reason": "preexisting_incomplete_output",
                            "completion_evidence": evidence,
                            "policy": "never overwrite or append to an unverified formal output",
                        },
                    }
                )
                any_failure = True
                update_status(status_file, status)
                continue

            try:
                if override is None:
                    override = resolve_arm_override(
                        plan,
                        arm,
                        artifact=artifact,
                        p99_receipt=(derived["p0_5_06"] if derived is not None else None),
                    )
                expected_identity = arm_completion_identity(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001 - schema/import drift blocks only this arm
                arm_state.update(
                    {
                        "state": "blocked_prerequisite",
                        "completed_at": utc_now(),
                        "failure": {"reason": str(exc)},
                    }
                )
                any_failure = True
                update_status(status_file, status)
                continue

            # The snapshot is immutable, but the benchmark/reference config
            # lives outside it. Re-freeze immediately before the launcher can
            # open a paid account window.
            validate_runtime_freeze(
                plan,
                snapshot=snapshot,
                expected_snapshot_identity=snapshot_identity,
            )
            status["active_arm"] = arm.arm_id
            arm_state["state"] = "running"
            arm_state["started_at"] = utc_now()
            attempt = {
                "started_at": arm_state["started_at"],
                "override_sha256": canonical_sha256(override),
                "output_dir": str(directory),
            }
            arm_state["attempts"].append(attempt)
            update_status(status_file, status)
            launch_error: dict[str, Any] | None = None
            try:
                rc: int | None = launch_arm(
                    plan,
                    arm,
                    snapshot=snapshot,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001 - one launcher failure is arm-local
                message = str(exc)
                rc = None
                launch_error = {
                    "error_class": type(exc).__name__,
                    "message_sha256": hashlib.sha256(
                        message.encode("utf-8", errors="replace")
                    ).hexdigest(),
                }
            attempt["completed_at"] = utc_now()
            attempt["rc"] = rc
            if launch_error is not None:
                attempt["launcher_error"] = launch_error
            status["active_arm"] = None
            complete, evidence = inspect_complete_arm(
                directory,
                expected_task_ids=expected_task_ids,
                expected_task_concurrency=6,
                expected_identity=expected_identity,
            )
            arm_state["completion_evidence"] = evidence
            arm_state["completed_at"] = utc_now()
            if complete:
                arm_state["state"] = "succeeded"
            else:
                arm_state["state"] = "failed"
                arm_state["failure"] = {
                    "reason": "launcher_or_completion_contract_failed",
                    "rc": rc,
                    "launcher_error": launch_error,
                }
                any_failure = True
            update_status(status_file, status)

            if complete and arm.arm_id == source_arm.arm_id and derived is None:
                try:
                    derived = prepare_derived(
                        plan,
                        arms,
                        snapshot=snapshot,
                        snapshot_identity=snapshot_identity,
                        plan_sha256=plan_sha256,
                        source_arm=source_arm,
                        source_dir=directory,
                    )
                    derived, artifact = load_derived(plan, plan_sha256)
                    reconcile_status_from_derived(status, derived)
                except Exception as exc:  # noqa: BLE001 - later live arms still run
                    message = str(exc)
                    derived = None
                    artifact = None
                    status["derived_failure"] = {
                        "error_class": type(exc).__name__,
                        "message_sha256": hashlib.sha256(
                            message.encode("utf-8", errors="replace")
                        ).hexdigest(),
                    }
                    any_failure = True
                update_status(status_file, status)

        if not no_op_status_is_terminal(status):
            any_failure = True
        status["phase"] = campaign_terminal_phase(status)
        status["completed_at"] = utc_now()
        status["active_arm"] = None
        update_status(status_file, status)
        terminal_status = publish_terminal_status_input(plan, status)
        status["terminal_status_input"] = terminal_status
        update_status(status_file, status)
        validate_runtime_freeze(
            plan,
            snapshot=snapshot,
            expected_snapshot_identity=snapshot_identity,
        )
        report_receipt, report_complete = run_terminal_report(
            plan,
            plan_path=plan_path,
            terminal_status_path=Path(str(terminal_status["path"])),
        )
        status["reporting"] = report_receipt
        status["phase"] = campaign_terminal_phase(
            status,
            reporting_complete=report_complete,
        )
        if not report_complete:
            any_failure = True
        update_status(status_file, status)
        return 1 if any_failure else 0


def validate_only(plan_path: Path) -> dict[str, Any]:
    """Read-only production-interface and freeze validation; no output/model calls."""

    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    snapshot, snapshot_identity = validate_snapshot(plan)
    freeze_identity = validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    validate_static_overlays(plan, arms, snapshot)
    helpers = _runtime_helpers(snapshot)
    return {
        "status": "valid",
        "candidate_live_arm_count": len(arms),
        "live_experiment_group_count": len(
            {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
        ),
        "frozen_replay_arm_count": sum(arm.analyzer_mode == "frozen_replay" for arm in arms),
        "live_analyzer_arm_count": sum(arm.analyzer_mode == "live" for arm in arms),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "freeze_identity_sha256": canonical_sha256(freeze_identity),
        "production_helper_sources": {
            "ensemble": helpers["ensemble_source_sha256"],
            "openai": helpers["openai_source_sha256"],
            "runner": helpers["runner_source_sha256"],
        },
        "draco_request_budget_rebinding_disabled": helpers[
            "draco_request_budget_rebinding_disabled"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-plan")
    validate_parser.add_argument("plan", type=Path)
    validate_parser.add_argument("--allow-placeholders", action="store_true")
    expand_parser = subparsers.add_parser("expand-plan")
    expand_parser.add_argument("plan", type=Path)
    expand_parser.add_argument("--allow-placeholders", action="store_true")
    validate_only_parser = subparsers.add_parser("validate-only")
    validate_only_parser.add_argument("plan", type=Path)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("plan", type=Path)
    args = parser.parse_args()

    if args.command in {"validate-plan", "expand-plan"}:
        plan = load_json(args.plan)
        arms = validate_plan(plan, allow_placeholders=args.allow_placeholders)
        if args.command == "expand-plan":
            print(json.dumps([asdict(arm) for arm in arms], ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "candidate_live_arm_count": len(arms),
                        "live_experiment_group_count": len(
                            {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
                        ),
                        "common_e0_count": sum(arm.experiment_id == "common-E0" for arm in arms),
                        "campaign_plan_sha256": canonical_sha256(plan),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "validate-only":
        print(json.dumps(validate_only(args.plan), ensure_ascii=False, sort_keys=True))
        return 0
    return run_campaign(args.plan)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControllerError as exc:
        print(f"controller error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
