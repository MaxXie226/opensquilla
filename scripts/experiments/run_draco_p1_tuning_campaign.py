#!/usr/bin/env python3
"""Auditable, restart-safe controller for conditional-slice P1 DRACO mini arms.

The controller is intentionally separate from the P0/P0.5 campaign.  It reuses
only production-verification primitives from the frozen P0/P0.5 controller and
keeps all P1 scheduling, hit-gate, and progression semantics in this file.

No command except ``run`` launches the formal DRACO shell launcher.  In
particular, ``validate-plan``, ``expand-plan`` and ``validate-only`` are read
only and never inspect credentials or create campaign artifacts.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "opensquilla.draco-p1-campaign-plan/v1"
STATUS_SCHEMA = "opensquilla.draco-p1-controller-status/v1"
DERIVED_SCHEMA = "opensquilla.draco-p1-derived-plan/v1"
HIT_RECEIPT_SCHEMA = "opensquilla.draco-p1-hit-gate-receipt/v1"
SOURCE_ARM_ID = "common-E0-source"
REPLAY_CONTROL_IDS = ("common-E0-R1", "common-E0-R2", "common-E0-R3")
EXPECTED_TASK_COUNT = 10
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLACEHOLDER_PREFIX = "TODO_"
SCREENING_DESIGN_LABEL = "anchored_serial_not_task_interleaved"
CONFIRMATORY_DESIGN_LABEL = "strict_task_interleaved_confirmatory"

# These exclusions are facts about the frozen DRACO-mini slice/runtime, not
# tuning conclusions.  They must be represented in the plan as explicit
# receipts instead of silently disappearing from the matrix.
MISSING_FEATURE_GROUPS = frozenset({"P1-06", "P1-31", "P1-33", "P1-41"})
DETERMINISTIC_NO_HIT_GROUPS = frozenset(
    {"P1-01", "P1-02", "P1-09", "P1-10", "P1-11", "P1-12"}
)
SUPPORTED_GROUPS = frozenset(
    {
        "P1-04",
        "P1-05",
        "P1-14",
        "P1-15",
        "P1-16",
        "P1-17",
        "P1-18",
        "P1-19",
        "P1-20",
        "P1-21",
        "P1-22",
        "P1-23",
        "P1-24",
        "P1-25",
        "P1-29",
        "P1-30",
        "P1-32",
        "P1-34",
        "P1-35",
        "P1-36",
        "P1-42",
        "P1-43",
    }
)
TERMINAL_ARM_STATES = frozenset(
    {"succeeded", "failed", "blocked_prerequisite", "no_hit_skipped", "progression_skipped"}
)


class ControllerError(RuntimeError):
    pass


def screening_design_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the frozen screening-only methodology disclosure."""

    execution = plan.get("execution")
    schedule = execution.get("schedule") if isinstance(execution, Mapping) else None
    reporting = plan.get("reporting")
    return {
        "design_label": (
            schedule.get("design_label") if isinstance(schedule, Mapping) else None
        ),
        "strict_task_interleaving": (
            schedule.get("strict_task_interleaving")
            if isinstance(schedule, Mapping)
            else None
        ),
        "task_interleaving_contract_satisfied": False,
        "mini_diagnostic_screening_only": (
            reporting.get("mini_is_diagnostic_only")
            if isinstance(reporting, Mapping)
            else None
        ),
        "automatic_winner_promotion": (
            reporting.get("automatic_winner_promotion")
            if isinstance(reporting, Mapping)
            else None
        ),
        "winner_or_combination_requires": CONFIRMATORY_DESIGN_LABEL,
    }


@dataclass(frozen=True)
class HitGate:
    metric: str
    op: str
    threshold: Any
    minimum_tasks: int = 1


@dataclass(frozen=True)
class Arm:
    arm_id: str
    experiment_id: str
    directory_name: str
    title: str
    variant: str
    analyzer_mode: str
    override: dict[str, Any]
    output_name: str
    control_arm_id: str | None
    hit_gate: HitGate | None
    priority: int


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


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ControllerError(f"not a regular JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot load JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"JSON root must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_module(path: Path, prefix: str) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ControllerError(f"frozen helper is not a regular file: {path}")
    raw_hash = file_sha256(path)
    name = f"_{prefix}_{raw_hash}"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ControllerError(f"cannot import frozen helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def common_controller(snapshot: Path | None = None) -> Any:
    root = snapshot if snapshot is not None else Path(__file__).resolve().parents[2]
    return _load_module(
        root / "scripts/experiments/run_draco_p0_p05_tuning_campaign.py",
        "draco_p1_common_controller",
    )


def _ranking_override(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"router_dynamic_ranking_override": copy.deepcopy(dict(value))}


def expected_variant_contracts() -> dict[str, dict[str, Any]]:
    """Return the authoritative P1 candidate contract from the tuning plan."""

    specs: dict[str, dict[str, Any]] = {}

    def add(
        group: str,
        variant: str,
        override: Mapping[str, Any],
        gate: HitGate,
        *,
        analyzer_mode: str = "frozen_replay",
        control: str | None = None,
        priority: int = 100,
    ) -> None:
        specs[f"{group}-{variant}"] = {
            "group": group,
            "variant": variant,
            "override": copy.deepcopy(dict(override)),
            "hit_gate": asdict(gate),
            "analyzer_mode": analyzer_mode,
            "control_arm_id": control,
            "priority": priority,
        }

    retry_gate = HitGate("analyzer_retry_or_fallback", "true", True)
    add(
        "P1-04",
        "E1",
        _ranking_override({"task_analyzer": {"timeout_seconds": 10.0}}),
        retry_gate,
        analyzer_mode="live",
        control=SOURCE_ARM_ID,
        priority=20,
    )
    add(
        "P1-04",
        "E2",
        _ranking_override({"task_analyzer": {"timeout_seconds": 40.0}}),
        retry_gate,
        analyzer_mode="live",
        control=SOURCE_ARM_ID,
        priority=21,
    )
    add(
        "P1-05",
        "E1",
        _ranking_override({"task_analyzer": {"max_retries": 0}}),
        retry_gate,
        analyzer_mode="live",
        control=SOURCE_ARM_ID,
        priority=22,
    )
    add(
        "P1-05",
        "E2",
        _ranking_override({"task_analyzer": {"max_retries": 1}}),
        retry_gate,
        analyzer_mode="live",
        control=SOURCE_ARM_ID,
        priority=23,
    )
    add(
        "P1-14",
        "E1",
        {"ensemble": {"aggregator_tools": False, "proposer_tools": False}},
        HitGate("aggregator_tool_calls", "gt", 0),
    )
    add(
        "P1-15",
        "E1",
        {"runner": {"agent_max_iterations": 4, "max_iterations_includes_finalization": False}},
        HitGate("agent_iterations", "gt", 4),
        priority=31,
    )
    add(
        "P1-15",
        "E2",
        {"runner": {"agent_max_iterations": 3, "max_iterations_includes_finalization": False}},
        HitGate("agent_iterations", "gt", 3),
        priority=32,
    )
    for variant, seconds, priority in (("E1", 180.0, 110), ("E2", 300.0, 111)):
        add(
            "P1-16",
            variant,
            {"timeouts": {"proposer_seconds": seconds}},
            HitGate("max_proposer_elapsed_ms", "gt", int(seconds * 1000)),
            priority=priority,
        )
    for variant, seconds in (("E1", 600.0), ("E2", 1200.0)):
        add(
            "P1-17",
            variant,
            {"timeouts": {"aggregator_seconds": seconds}},
            HitGate("estimated_aggregator_elapsed_ms", "gt", int(seconds * 1000)),
        )
    for variant, attempts in (("E1", 1), ("E2", 2)):
        add(
            "P1-18",
            variant,
            {"generation": {"max_attempts": attempts, "retry_backoff_seconds": 2.0}},
            HitGate("generation_attempt_count", "gt", attempts),
        )
    for variant, seconds in (("E1", 0.0), ("E2", 1.0), ("E3", 4.0)):
        add(
            "P1-19",
            variant,
            {"generation": {"max_attempts": 3, "retry_backoff_seconds": seconds}},
            HitGate("generation_retry_count", "gt", 0),
        )
    for variant, grace in (("E1", 10.0), ("E2", 30.0)):
        add(
            "P1-20",
            variant,
            {"ensemble": {"wait_for_all_proposers": False, "quorum_grace_seconds": grace}},
            HitGate("quorum_tail_ms", "gt", int(grace * 1000)),
        )
    # P1-21 needs its own serving-mode E0 because common G1-C uses experiment
    # recovery.  Neither arm may run without a naturally observed below-quorum
    # task; the controller never injects a provider failure.
    add(
        "P1-21",
        "E0",
        {
            "ensemble": {
                "all_failed_policy": "fallback_single",
                "aggregator_recovery_mode": "serving",
            }
        },
        HitGate("below_quorum_or_single_fallback", "true", True),
        control=None,
    )
    add(
        "P1-21",
        "E1",
        {"ensemble": {"all_failed_policy": "error", "aggregator_recovery_mode": "serving"}},
        HitGate("below_quorum_or_single_fallback", "true", True),
        control="P1-21-E0",
    )
    add(
        "P1-22",
        "E1",
        {"tools": {"web_search": {"provider": "duckduckgo", "api_key_env": "", "max_results": 5}}},
        HitGate("web_search_calls", "gt", 0),
    )
    for variant, count in (("E1", 3), ("E2", 8)):
        add(
            "P1-23",
            variant,
            {"tools": {"web_search": {"provider": "brave", "max_results": count}}},
            HitGate("web_search_calls", "gt", 0),
        )
    for variant, tokens in (("E1", 10_000), ("E2", 25_000)):
        add(
            "P1-24",
            variant,
            {"tools": {"web_fetch": {"max_content_tokens": tokens}}},
            HitGate("web_fetch_calls", "gt", 0),
        )
    add(
        "P1-25",
        "E1",
        {"ensemble": {"candidate_max_chars": 16_000}},
        HitGate("max_candidate_chars", "gt", 16_000),
    )
    add(
        "P1-25",
        "E2",
        {"ensemble": {"candidate_max_chars": 32_000}},
        HitGate("candidate_at_current_cap", "true", True),
    )
    add(
        "P1-29",
        "E1",
        {"runner": {"deadline_wrapup_margin_seconds": 0, "deadline_wrapup_disable_tools": True}},
        HitGate("deadline_wrapup_observed", "true", True),
    )
    add(
        "P1-29",
        "E2",
        {"runner": {"deadline_wrapup_margin_seconds": 600, "deadline_wrapup_disable_tools": True}},
        HitGate("total_elapsed_ms", "ge", 10_200_000),
    )
    add(
        "P1-30",
        "E1",
        {"runner": {"deadline_wrapup_margin_seconds": 300, "deadline_wrapup_disable_tools": False}},
        HitGate("deadline_wrapup_observed", "true", True),
    )
    for variant, seconds in (("E1", 7200.0), ("E2", 5400.0)):
        add(
            "P1-32",
            variant,
            {"timeouts": {"task_seconds": seconds, "task_margin_seconds": 30.0}},
            HitGate("total_elapsed_ms", "gt", int(seconds * 1000)),
        )
    add(
        "P1-34",
        "E1",
        {"runner": {"max_iterations_includes_finalization": True}},
        HitGate("agent_iterations", "ge", 20),
    )
    add(
        "P1-35",
        "E1",
        {"runner": {"retrieval_loop_finalization_threshold": 3}},
        HitGate("max_consecutive_retrieval_iterations", "ge", 3),
        priority=30,
    )
    add(
        "P1-36",
        "E1",
        {"runner": {"finalization_disable_thinking": True}},
        HitGate("soft_deadline_finalizer_with_thinking", "true", True),
    )
    add(
        "P1-42",
        "E2",
        _ranking_override({"proposer_count": {"constrained_max": 3}}),
        HitGate("constrained_at_two_proposers", "true", True),
    )
    add(
        "P1-43",
        "E1",
        _ranking_override(
            {
                "proposer_count": {
                    "constrained_cost_values": ["hard_limit"],
                    "constrained_latency_values": ["hard_timeout"],
                }
            }
        ),
        HitGate("constraint_low_or_interactive", "true", True),
    )
    add(
        "P1-43",
        "E2",
        _ranking_override(
            {
                "proposer_count": {
                    "constrained_cost_values": ["low", "hard_limit"],
                    "constrained_latency_values": ["hard_timeout"],
                }
            }
        ),
        HitGate("constraint_interactive", "true", True),
    )
    add(
        "P1-43",
        "E3",
        _ranking_override(
            {
                "proposer_count": {
                    "constrained_cost_values": ["hard_limit"],
                    "constrained_latency_values": ["interactive", "hard_timeout"],
                }
            }
        ),
        HitGate("constraint_low", "true", True),
    )
    return specs


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith(PLACEHOLDER_PREFIX)
    if isinstance(value, Mapping):
        return any(
            _contains_placeholder(key) or _contains_placeholder(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_placeholder(item) for item in value)
    return False


def preexisting_source_contract(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    runtime = plan.get("runtime_contract")
    raw = runtime.get("preexisting_source") if isinstance(runtime, Mapping) else None
    if raw is None:
        return None
    fields = {
        "schema",
        "enabled",
        "source_plan_path",
        "source_plan_raw_sha256",
        "source_plan_canonical_sha256",
        "source_snapshot_path",
        "source_snapshot_commit",
        "source_snapshot_tree",
        "source_output_dir",
        "source_manifest_sha256",
        "source_results_sha256",
        "source_trace_sha256",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or raw.get("schema")
        != "opensquilla.draco-preexisting-analyzer-source/v1"
        or raw.get("enabled") is not True
    ):
        raise ControllerError("runtime_contract.preexisting_source is invalid")
    for key in ("source_plan_path", "source_snapshot_path", "source_output_dir"):
        if not Path(str(raw.get(key) or "")).is_absolute():
            raise ControllerError(f"runtime_contract.preexisting_source.{key} is invalid")
    for key in (
        "source_plan_raw_sha256",
        "source_plan_canonical_sha256",
        "source_manifest_sha256",
        "source_results_sha256",
        "source_trace_sha256",
    ):
        value = str(raw.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ControllerError(f"runtime_contract.preexisting_source.{key} is invalid")
    for key in ("source_snapshot_commit", "source_snapshot_tree"):
        value = str(raw.get(key) or "")
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ControllerError(f"runtime_contract.preexisting_source.{key} is invalid")
    return copy.deepcopy(dict(raw))


def expand_arms(plan: Mapping[str, Any]) -> list[Arm]:
    run_id = str(plan.get("run_id") or "")
    controls = plan.get("comparison_controls")
    control_overrides = (
        dict(controls.get("arm_control_overrides") or {}) if isinstance(controls, Mapping) else {}
    )
    default_control = (
        str(controls.get("default_control_arm_id") or REPLAY_CONTROL_IDS[0])
        if isinstance(controls, Mapping)
        else REPLAY_CONTROL_IDS[0]
    )
    arms: list[Arm] = []
    for raw in plan.get("common_e0") or []:
        if not isinstance(raw, Mapping):
            raise ControllerError("common_e0 entries must be objects")
        arm_id = str(raw.get("arm_id") or "")
        arms.append(
            Arm(
                arm_id=arm_id,
                experiment_id="common-E0",
                directory_name="common",
                title="G1-C control",
                variant=str(raw.get("variant") or ""),
                analyzer_mode=str(raw.get("analyzer_mode") or ""),
                override=copy.deepcopy(dict(raw.get("override") or {})),
                output_name=f"{arm_id}-{run_id}",
                control_arm_id=None,
                hit_gate=None,
                priority=int(raw.get("priority") or 0),
            )
        )
    for experiment in plan.get("experiments") or []:
        if not isinstance(experiment, Mapping):
            raise ControllerError("experiments entries must be objects")
        group = str(experiment.get("id") or "")
        for raw in experiment.get("variants") or []:
            if not isinstance(raw, Mapping):
                raise ControllerError(f"{group} variant must be an object")
            variant = str(raw.get("id") or "")
            arm_id = f"{group}-{variant}"
            gate_raw = raw.get("hit_gate")
            if not isinstance(gate_raw, Mapping):
                raise ControllerError(f"{arm_id} lacks hit_gate")
            gate = HitGate(
                metric=str(gate_raw.get("metric") or ""),
                op=str(gate_raw.get("op") or ""),
                threshold=copy.deepcopy(gate_raw.get("threshold")),
                minimum_tasks=int(gate_raw.get("minimum_tasks") or 0),
            )
            declared_control = raw.get("control_arm_id")
            control = control_overrides.get(
                arm_id,
                declared_control
                if declared_control is not None
                else experiment.get("control_arm_id", default_control),
            )
            arms.append(
                Arm(
                    arm_id=arm_id,
                    experiment_id=group,
                    directory_name=str(experiment.get("directory_name") or group),
                    title=str(experiment.get("title") or group),
                    variant=variant,
                    analyzer_mode=str(raw.get("analyzer_mode") or "frozen_replay"),
                    override=copy.deepcopy(dict(raw.get("override") or {})),
                    output_name=f"{arm_id}-{run_id}",
                    control_arm_id=str(control) if control is not None else None,
                    hit_gate=gate,
                    priority=int(raw.get("priority") or 100),
                )
            )
    return arms


def validate_plan(plan: Mapping[str, Any], *, allow_placeholders: bool) -> list[Arm]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ControllerError("campaign plan schema differs")
    if not allow_placeholders and _contains_placeholder(plan):
        raise ControllerError("campaign plan still contains TODO placeholders")
    run_id = str(plan.get("run_id") or "")
    if SAFE_COMPONENT_RE.fullmatch(run_id) is None:
        raise ControllerError("run_id is unsafe")
    benchmark = plan.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ControllerError("benchmark contract is missing")
    task_ids = benchmark.get("task_ids")
    if (
        benchmark.get("task_count") != EXPECTED_TASK_COUNT
        or not isinstance(task_ids, list)
        or len(task_ids) != EXPECTED_TASK_COUNT
        or len(set(task_ids)) != EXPECTED_TASK_COUNT
        or any(not isinstance(item, str) or not item for item in task_ids)
        or benchmark.get("groups") != ["G1"]
    ):
        raise ControllerError("benchmark must freeze exactly ten unique G1 tasks")
    execution = plan.get("execution")
    if not isinstance(execution, Mapping):
        raise ControllerError("execution contract is missing")
    if (
        execution.get("serial_arms") is not True
        or execution.get("task_concurrency") != 6
        or execution.get("judge_concurrency") != 6
        or execution.get("generation_max_attempts") != 3
        or execution.get("continue_after_arm_failure") is not True
    ):
        raise ControllerError("execution contract differs from formal DRACO mini")
    lock_path = Path(str(execution.get("global_openrouter_lock") or ""))
    secret_path = Path(str(execution.get("openrouter_secret_file") or ""))
    if not lock_path.is_absolute():
        raise ControllerError("execution must bind an absolute OpenRouter lock path")
    if not secret_path.is_absolute():
        raise ControllerError("execution must bind an absolute OpenRouter secret-file path")
    if (
        execution.get("openrouter_credential_scope") != "dedicated_key"
        or secret_path == lock_path
    ):
        raise ControllerError("execution OpenRouter credential contract differs")
    paths = plan.get("paths")
    required_paths = {
        "run_root",
        "snapshot",
        "report_root",
        "reference_repo",
        "python",
        "reporter",
        "experiment_config_relative",
        "launcher_relative",
    }
    if not isinstance(paths, Mapping) or not required_paths.issubset(paths):
        raise ControllerError("paths contract is incomplete")
    for key in ("run_root", "snapshot", "report_root", "reference_repo", "python", "reporter"):
        if not Path(str(paths[key])).is_absolute():
            raise ControllerError(f"paths.{key} must be absolute")
    source = plan.get("source_plan")
    if not isinstance(source, Mapping):
        raise ControllerError("source_plan freeze is missing")
    for key in ("commit", "blob_sha", "raw_sha256"):
        value = str(source.get(key) or "").removeprefix("sha256:")
        expected_len = 40 if key in {"commit", "blob_sha"} else 64
        if len(value) != expected_len or any(char not in "0123456789abcdef" for char in value):
            raise ControllerError(f"source_plan.{key} is malformed")
    excluded = plan.get("excluded")
    if not isinstance(excluded, list):
        raise ControllerError("excluded matrix is missing")
    excluded_ids = {str(item.get("id") or "") for item in excluded if isinstance(item, Mapping)}
    expected_excluded = MISSING_FEATURE_GROUPS | DETERMINISTIC_NO_HIT_GROUPS
    if excluded_ids != expected_excluded:
        raise ControllerError("excluded matrix does not match missing/no-hit P1 groups")
    for item in excluded:
        if not isinstance(item, Mapping) or not str(item.get("reason") or "").strip():
            raise ControllerError("every excluded P1 group needs a reason")
        kind = str(item.get("kind") or "")
        group = str(item.get("id") or "")
        expected_kind = (
            "missing_feature"
            if group in MISSING_FEATURE_GROUPS
            else "deterministic_no_hit"
        )
        if kind != expected_kind:
            raise ControllerError(f"{group} exclusion kind differs")
    arms = expand_arms(plan)
    if len({arm.arm_id for arm in arms}) != len(arms):
        raise ControllerError("expanded arm ids are not unique")
    common_ids = {arm.arm_id for arm in arms if arm.experiment_id == "common-E0"}
    if common_ids != {SOURCE_ARM_ID, *REPLAY_CONTROL_IDS}:
        raise ControllerError("campaign requires one live source and three replay controls")
    by_id = {arm.arm_id: arm for arm in arms}
    if by_id[SOURCE_ARM_ID].analyzer_mode != "live" or any(
        by_id[arm_id].analyzer_mode != "frozen_replay" for arm_id in REPLAY_CONTROL_IDS
    ):
        raise ControllerError("common E0 Analyzer modes differ")
    expected = expected_variant_contracts()
    actual_candidate_ids = {arm.arm_id for arm in arms if arm.experiment_id != "common-E0"}
    if actual_candidate_ids != set(expected):
        raise ControllerError("candidate arm inventory differs from authoritative P1 matrix")
    if {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"} != SUPPORTED_GROUPS:
        raise ControllerError("supported experiment group inventory differs")
    for arm_id, contract in expected.items():
        arm = by_id[arm_id]
        expected_control = contract["control_arm_id"]
        if expected_control is None:
            if arm_id == "P1-21-E0":
                expected_control = None
            else:
                expected_control = REPLAY_CONTROL_IDS[0]
        if (
            arm.override != contract["override"]
            or asdict(arm.hit_gate) != contract["hit_gate"]
            or arm.analyzer_mode != contract["analyzer_mode"]
            or arm.priority != contract["priority"]
        ):
            raise ControllerError(f"{arm_id} differs from authoritative parameter contract")
        if (
            contract["control_arm_id"] is not None
            and arm.control_arm_id != contract["control_arm_id"]
        ):
            raise ControllerError(f"{arm_id} explicit control differs")
    schedule = execution.get("schedule")
    if (
        not isinstance(schedule, Mapping)
        or schedule.get("mode") != "hit_gated_serial"
        or schedule.get("strict_task_interleaving") is not False
        or schedule.get("design_label") != SCREENING_DESIGN_LABEL
    ):
        raise ControllerError(
            "execution.schedule must freeze the anchored serial screening design"
        )
    arm_order = schedule.get("arm_order")
    if (
        not isinstance(arm_order, list)
        or set(arm_order) != set(by_id)
        or len(arm_order) != len(by_id)
    ):
        raise ControllerError("schedule must cover every expanded arm exactly once")
    if arm_order[0] != SOURCE_ARM_ID:
        raise ControllerError("live source must be first")
    positions = {arm_id: index for index, arm_id in enumerate(arm_order)}
    if not (
        positions["P1-35-E1"] < positions["P1-15-E1"] < positions["P1-15-E2"]
        and positions["P1-21-E0"] < positions["P1-21-E1"]
    ):
        raise ControllerError("P1-35/P1-15 or P1-21 dependency order differs")
    anchors = schedule.get("anchor_by_arm_id")
    if not isinstance(anchors, Mapping) or set(anchors) != set(by_id):
        raise ControllerError("schedule anchor mapping is incomplete")
    seen: set[str] = set()
    for arm_id in arm_order:
        anchor = str(anchors.get(arm_id) or "")
        if anchor not in {SOURCE_ARM_ID, *REPLAY_CONTROL_IDS, "P1-21-E0"}:
            raise ControllerError(f"{arm_id} has an invalid schedule anchor")
        if anchor not in seen and anchor != arm_id:
            raise ControllerError(f"{arm_id} precedes its schedule anchor")
        arm = by_id[arm_id]
        if arm_id not in {SOURCE_ARM_ID, *REPLAY_CONTROL_IDS, "P1-21-E0"}:
            if arm.control_arm_id != anchor:
                raise ControllerError(f"{arm_id} comparison control differs from schedule anchor")
        seen.add(arm_id)
    progression = plan.get("progression")
    if (
        not isinstance(progression, Mapping)
        or progression.get("schema") != "opensquilla.draco-p1-progression/v1"
    ):
        raise ControllerError("P1 progression contract is missing")
    if (
        progression.get("first_arm_id") != "P1-35-E1"
        or progression.get("conditional_arm_ids") != ["P1-15-E1", "P1-15-E2"]
        or not isinstance(
            progression.get("skip_p1_15_if_cost_reduction_fraction_at_least"),
            int | float,
        )
        or not 0 <= float(progression["skip_p1_15_if_cost_reduction_fraction_at_least"]) <= 1
        or not isinstance(progression.get("minimum_mean_delta_quality"), int | float)
    ):
        raise ControllerError("P1-35/P1-15 progression contract differs")
    runtime = plan.get("runtime_contract")
    replay = runtime.get("frozen_replay") if isinstance(runtime, Mapping) else None
    if not isinstance(replay, Mapping) or replay.get("schema") not in {
        "opensquilla.draco.frozen-task-analysis/v1",
        "opensquilla.draco.frozen-task-analysis/v2",
    }:
        raise ControllerError("frozen Analyzer replay contract is missing")
    freeze = plan.get("freeze")
    if not isinstance(freeze, Mapping):
        raise ControllerError("freeze contract is missing")
    if freeze.get("ranking_thinking_assignment_enabled") is not False:
        raise ControllerError("P1 campaign requires thinking assignment OFF")
    analyzer_source = runtime.get("analyzer_source") if isinstance(runtime, Mapping) else None
    if (
        not isinstance(analyzer_source, Mapping)
        or analyzer_source.get("schema")
        != "opensquilla.draco-analyzer-source-policy/v1"
        or type(analyzer_source.get("allow_deterministic_router_fallback")) is not bool
    ):
        raise ControllerError("Analyzer source policy is missing or malformed")
    imported_source = preexisting_source_contract(plan)
    if imported_source is None:
        raise ControllerError(
            "P1 campaign requires an authenticated preexisting E0 source"
        )
    reporting = plan.get("reporting")
    if (
        not isinstance(reporting, Mapping)
        or reporting.get("mini_is_diagnostic_only") is not True
        or reporting.get("automatic_winner_promotion") is not False
        or reporting.get("independent_safety_gate_available") is not False
        or reporting.get("pairing_key") != "task_id"
        or reporting.get("bootstrap_repetitions") != 20_000
    ):
        raise ControllerError("reporting contract differs from DRACO mini policy")
    if screening_design_contract(plan) != {
        "design_label": SCREENING_DESIGN_LABEL,
        "strict_task_interleaving": False,
        "task_interleaving_contract_satisfied": False,
        "mini_diagnostic_screening_only": True,
        "automatic_winner_promotion": False,
        "winner_or_combination_requires": CONFIRMATORY_DESIGN_LABEL,
    }:
        raise ControllerError("P1 screening methodology contract differs")
    if not allow_placeholders:
        for key in ("snapshot_commit", "snapshot_tree"):
            value = str(freeze.get(key) or "")
            if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
                raise ControllerError(f"freeze.{key} is malformed")
    return [by_id[arm_id] for arm_id in arm_order]


def output_dir(plan: Mapping[str, Any], arm: Arm) -> Path:
    if arm.arm_id == SOURCE_ARM_ID:
        imported = preexisting_source_contract(plan)
        if imported is not None:
            return Path(str(imported["source_output_dir"]))
    return Path(str(plan["paths"]["report_root"])) / arm.directory_name / arm.output_name


def artifact_dir(plan: Mapping[str, Any], arm: Arm) -> Path:
    """Return the controller-owned source package after it has been frozen."""

    if arm.arm_id == SOURCE_ARM_ID:
        package_dir = Path(str(plan["paths"]["run_root"])) / "preexisting-source-package"
        if package_dir.is_dir() and not package_dir.is_symlink():
            return package_dir
    return output_dir(plan, arm)


def validate_snapshot(plan: Mapping[str, Any]) -> tuple[Path, dict[str, str]]:
    common = common_controller(Path(str(plan["paths"]["snapshot"])))
    return common.validate_snapshot(plan)


def compute_runtime_freeze_identity(plan: Mapping[str, Any], *, snapshot: Path) -> dict[str, Any]:
    common = common_controller(snapshot)
    base = common.compute_runtime_freeze_identity(plan, snapshot=snapshot)
    sources = dict(base["sources"])
    sources["common_controller_raw_sha256"] = sources.pop("controller_raw_sha256")
    sources["controller_raw_sha256"] = file_sha256(Path(__file__).resolve())
    common_reporter = snapshot / "scripts/experiments/generate_draco_p0_p05_reports.py"
    if not common_reporter.is_file() or common_reporter.is_symlink():
        raise ControllerError("frozen common reporter is unavailable")
    sources["common_reporter_raw_sha256"] = file_sha256(common_reporter)
    return {**base, "sources": sources}


def validate_runtime_freeze(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    expected_snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    common = common_controller(snapshot)
    if common.git_identity(snapshot) != dict(expected_snapshot_identity):
        raise ControllerError("snapshot identity or cleanliness drifted")
    actual = compute_runtime_freeze_identity(plan, snapshot=snapshot)
    frozen = plan["freeze"]
    for section in ("inputs", "sources", "model_registry", "ranking_config"):
        expected = frozen.get(section)
        if not isinstance(expected, Mapping) or dict(expected) != actual[section]:
            differing = sorted(
                key
                for key in set(actual[section]) | set(expected or {})
                if actual[section].get(key) != (expected or {}).get(key)
            )
            raise ControllerError(f"freeze.{section} drifted at: {', '.join(differing)}")
    if actual["inputs"]["benchmark_input_raw_sha256"] != plan["benchmark"]["input_sha256"]:
        raise ControllerError("benchmark input hash differs")
    return actual


def validate_static_overlays(plan: Mapping[str, Any], arms: Sequence[Arm], snapshot: Path) -> None:
    common = common_controller(snapshot)
    base = snapshot / str(plan["paths"]["experiment_config_relative"])
    for arm in arms:
        common.load_effective_experiment_config(snapshot, base, arm.override)


def resolve_arm_override(
    plan: Mapping[str, Any], arm: Arm, *, artifact: Mapping[str, Any] | None
) -> dict[str, Any]:
    override = copy.deepcopy(arm.override)
    if arm.analyzer_mode == "frozen_replay":
        if artifact is None:
            raise ControllerError(f"{arm.arm_id} requires the frozen Analyzer artifact")
        common = common_controller(Path(str(plan["paths"]["snapshot"])))
        override = deep_merge(override, common.make_replay_overlay(plan, artifact))
    return override


def arm_completion_identity(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    common = common_controller(snapshot)
    if arm.arm_id == SOURCE_ARM_ID:
        imported = common.preexisting_source_identity(plan)
        if imported is not None:
            return copy.deepcopy(imported[0])
    config = common.load_effective_experiment_config(
        snapshot,
        snapshot / str(plan["paths"]["experiment_config_relative"]),
        override,
    )
    config_payload = config.model_dump(mode="json")
    runner_payload = config_payload.get("runner")
    concurrency = plan.get("execution", {}).get("task_concurrency")
    if (
        not isinstance(runner_payload, Mapping)
        or "concurrency" not in runner_payload
        or isinstance(runner_payload.get("concurrency"), bool)
        or not isinstance(runner_payload.get("concurrency"), int)
        or isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency <= 0
    ):
        raise ControllerError("arm identity cannot project runner.concurrency")
    runner_copy = copy.deepcopy(dict(runner_payload))
    runner_copy["concurrency"] = concurrency
    config_payload["runner"] = runner_copy
    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    runner = snapshot / "scripts/run_draco_routing_experiment.py"
    resume = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    return {
        "arm_id": arm.arm_id,
        "output_name": arm.output_name,
        "run_id": str(plan["run_id"]),
        "output_dir": str(output_dir(plan, arm).resolve()),
        "snapshot": str(snapshot.resolve()),
        "snapshot_commit": snapshot_identity["commit"],
        "runner_identities": {
            str(runner.resolve()): file_sha256(runner),
            str(resume.resolve()): file_sha256(resume),
        },
        "benchmark_path": str((reference_repo / "data/draco/mini.jsonl").resolve()),
        "reference_config_path": str((reference_repo / ".local-state/config.toml").resolve()),
        "benchmark_sha256": str(plan["benchmark"]["input_sha256"]),
        "task_ids": sorted(plan["benchmark"]["task_ids"]),
        "task_concurrency": int(plan["execution"]["task_concurrency"]),
        "judge_concurrency": int(plan["execution"]["judge_concurrency"]),
        # The shell launcher resolves this value from the per-arm effective
        # config.  P1-18 deliberately varies it, so publication identity must
        # bind the resolved arm value rather than the campaign-wide ceiling.
        "generation_max_attempts": int(config.generation.max_attempts),
        "override_sha256": canonical_sha256(override),
        # The production launcher always applies runner.concurrency from
        # DRACO_CAMPAIGN_TASK_CONCURRENCY after loading the sparse overlay.
        "effective_config_sha256": canonical_sha256(config_payload),
        "candidate_order_seed_evidence": {
            "required": False,
            "configured_candidate_order_seed": config.ensemble.candidate_order_seed,
            "effective_candidate_order_seed": (
                config.ensemble.candidate_order_seed if config.ensemble.shuffle_candidates else None
            ),
        },
    }


def inspect_complete_arm(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    override: Mapping[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    if override is None:
        return False, {"reason": "override_unavailable"}
    expected = arm_completion_identity(
        plan,
        arm,
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        override=override,
    )
    return common_controller(snapshot).inspect_complete_arm(
        artifact_dir(plan, arm),
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        expected_task_concurrency=int(plan["execution"]["task_concurrency"]),
        expected_identity=expected,
    )


def _result_rows(source_dir: Path, snapshot: Path) -> list[dict[str, Any]]:
    common = common_controller(snapshot)
    _, _, _, paths = common.authenticate_published_arm_artifacts(source_dir)
    rows: list[dict[str, Any]] = []
    with paths["results.jsonl"].open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid result row {line_number}") from exc
            if not isinstance(row, dict):
                raise ControllerError(f"result row {line_number} is not an object")
            common.verify_result_row_evidence(row)
            rows.append(row)
    return rows


def _selection_plan(row: Mapping[str, Any]) -> Mapping[str, Any]:
    routing = row.get("routing_trace")
    plan = routing.get("selection_plan") if isinstance(routing, Mapping) else None
    if not isinstance(plan, Mapping):
        ensemble = row.get("ensemble_trace")
        plan = ensemble.get("selection_plan") if isinstance(ensemble, Mapping) else None
    return plan if isinstance(plan, Mapping) else {}


def _last_stop_reason(call: Mapping[str, Any]) -> str:
    recovery = call.get("aggregator_recovery")
    attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if isinstance(attempt, Mapping) and str(attempt.get("stop_reason") or "").strip():
                return str(attempt.get("stop_reason") or "").strip().casefold()
    return ""


def _soft_deadline_finalizer_with_thinking(trace: Mapping[str, Any]) -> bool:
    if trace.get("soft_deadline_triggered") is not True:
        return False
    final_request = trace.get("final_request")
    if not isinstance(final_request, Mapping):
        return False
    if final_request.get("soft_deadline_replacement") is not True:
        return False
    execution = final_request.get("execution")
    return bool(
        isinstance(execution, Mapping)
        and execution.get("effective_thinking") is True
    )


def derive_source_task_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id or task_id in metrics:
            raise ControllerError("source result task ids are missing or duplicated")
        ensemble = (
            row.get("ensemble_trace")
            if isinstance(row.get("ensemble_trace"), Mapping)
            else {}
        )
        calls = ensemble.get("calls") if isinstance(ensemble.get("calls"), list) else []
        candidate_elapsed: list[int] = []
        candidate_chars: list[int] = []
        candidate_at_cap = False
        aggregator_estimates: list[int] = []
        quorum_tails: list[int] = []
        retrieval_streak = 0
        max_retrieval_streak = 0
        soft_finalizer_with_thinking = _soft_deadline_finalizer_with_thinking(ensemble)
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            soft_finalizer_with_thinking = (
                soft_finalizer_with_thinking
                or _soft_deadline_finalizer_with_thinking(call)
            )
            call_candidates = (
                call.get("candidates")
                if isinstance(call.get("candidates"), list)
                else []
            )
            call_elapsed: list[int] = []
            call_usable_elapsed: list[int] = []
            for candidate in call_candidates:
                if not isinstance(candidate, Mapping):
                    continue
                elapsed = candidate.get("elapsed_ms")
                if (
                    isinstance(elapsed, int | float)
                    and not isinstance(elapsed, bool)
                    and elapsed >= 0
                ):
                    call_elapsed.append(int(elapsed))
                    candidate_elapsed.append(int(elapsed))
                    if candidate.get("usable_for_aggregation") is True:
                        call_usable_elapsed.append(int(elapsed))
                content = candidate.get("content")
                content_chars = content.get("chars") if isinstance(content, Mapping) else None
                if not isinstance(content_chars, int):
                    content_chars = len(str(candidate.get("text") or ""))
                candidate_chars.append(content_chars)
                if (
                    content_chars >= 24_000
                    or (isinstance(content, Mapping) and content.get("truncated") is True)
                ):
                    candidate_at_cap = True
            duration = call.get("agent_call_duration_ms")
            if isinstance(duration, int | float) and not isinstance(duration, bool):
                aggregator_estimates.append(max(0, int(duration) - max(call_elapsed or [0])))
            call_quorum = int(call.get("execution_quorum_required") or 2)
            ordered_usable_elapsed = sorted(call_usable_elapsed)
            if call_elapsed and len(ordered_usable_elapsed) >= call_quorum:
                quorum_tails.append(
                    max(call_elapsed) - ordered_usable_elapsed[call_quorum - 1]
                )
            if _last_stop_reason(call) in {"tool_calls", "tool_use"}:
                retrieval_streak += 1
                max_retrieval_streak = max(max_retrieval_streak, retrieval_streak)
            else:
                retrieval_streak = 0
        run_events = (
            row.get("run_trace", {}).get("events", [])
            if isinstance(row.get("run_trace"), Mapping)
            else []
        )
        named_tool_events = [
            event
            for event in run_events
            if isinstance(event, Mapping) and str(event.get("tool_name") or "")
        ]
        start_tool_events = [
            event for event in named_tool_events if event.get("kind") == "tool_use_start"
        ]
        result_tool_events = [
            event for event in named_tool_events if event.get("kind") == "tool_result"
        ]
        counted_tool_events = start_tool_events or result_tool_events or named_tool_events
        tool_names = [str(event.get("tool_name")) for event in counted_tool_events]
        event_text = " ".join(
            str(event.get(key) or "")
            for event in run_events
            if isinstance(event, Mapping)
            for key in ("phase", "message", "event_type")
        ).casefold()
        plan = _selection_plan(row)
        analyzer = (
            plan.get("task_analyzer")
            if isinstance(plan.get("task_analyzer"), Mapping)
            else {}
        )
        analyzer_usage = (
            analyzer.get("usage") if isinstance(analyzer.get("usage"), Mapping) else {}
        )
        attempts = (
            analyzer_usage.get("physical_attempts")
            if isinstance(analyzer_usage.get("physical_attempts"), list)
            else []
        )
        profile = (
            plan.get("task_profile_pre_escalation")
            if isinstance(plan.get("task_profile_pre_escalation"), Mapping)
            else {}
        )
        constraints = (
            profile.get("constraints")
            if isinstance(profile.get("constraints"), Mapping)
            else {}
        )
        cost = str(constraints.get("cost") or "")
        latency = str(constraints.get("latency") or "")
        selected = plan.get("selected_P") if isinstance(plan.get("selected_P"), list) else []
        usable = int(ensemble.get("usable_proposers") or 0)
        quorum = int(ensemble.get("execution_quorum_required") or 2)
        metrics[task_id] = {
            "analyzer_retry_or_fallback": len(attempts) > 1
            or str(analyzer.get("source") or "") == "router_fallback"
            or any(
                isinstance(item, Mapping) and item.get("usage_unknown") is True
                for item in attempts
            ),
            "aggregator_tool_calls": sum(
                name in {"web_search", "web_fetch"} for name in tool_names
            ),
            "agent_iterations": int(ensemble.get("agent_iterations") or len(calls)),
            "max_proposer_elapsed_ms": max(candidate_elapsed or [0]),
            "estimated_aggregator_elapsed_ms": max(aggregator_estimates or [0]),
            "generation_attempt_count": int(row.get("generation_attempt_count") or 1),
            "generation_retry_count": max(0, int(row.get("generation_attempt_count") or 1) - 1),
            "quorum_tail_ms": max(quorum_tails or [0]),
            "below_quorum_or_single_fallback": usable < quorum
            or bool(ensemble.get("fallback_used")),
            "web_search_calls": tool_names.count("web_search"),
            "web_fetch_calls": tool_names.count("web_fetch"),
            "max_candidate_chars": max(candidate_chars or [0]),
            "candidate_at_current_cap": candidate_at_cap,
            "deadline_wrapup_observed": "deadline_wrapup" in event_text,
            "total_elapsed_ms": int(row.get("total_elapsed_ms") or row.get("latency_ms") or 0),
            "max_consecutive_retrieval_iterations": (
                max_retrieval_streak
                if tool_names and set(tool_names) <= {"web_search", "web_fetch"}
                else 0
            ),
            "soft_deadline_finalizer_with_thinking": soft_finalizer_with_thinking,
            "constrained_at_two_proposers": (
                cost in {"low", "hard_limit"}
                or latency in {"interactive", "hard_timeout"}
            )
            and len(selected) == 2,
            "constraint_low_or_interactive": cost == "low" or latency == "interactive",
            "constraint_interactive": latency == "interactive",
            "constraint_low": cost == "low",
        }
    return metrics


def _matches_gate(value: Any, gate: HitGate) -> bool:
    if gate.op == "true":
        return value is True
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    threshold = gate.threshold
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise ControllerError(f"numeric hit gate {gate.metric} has a nonnumeric threshold")
    if gate.op == "gt":
        return float(value) > float(threshold)
    if gate.op == "ge":
        return float(value) >= float(threshold)
    if gate.op == "eq":
        return math.isclose(float(value), float(threshold), rel_tol=0.0, abs_tol=1e-12)
    raise ControllerError(f"unknown hit-gate operator: {gate.op}")


def derive_hit_receipt(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    rows: Sequence[Mapping[str, Any]],
    source_dir: Path,
) -> dict[str, Any]:
    metrics = derive_source_task_metrics(rows)
    decisions: dict[str, Any] = {}
    for arm in arms:
        if arm.hit_gate is None:
            continue
        matched = sorted(
            task_id
            for task_id, task_metrics in metrics.items()
            if _matches_gate(task_metrics.get(arm.hit_gate.metric), arm.hit_gate)
        )
        decisions[arm.arm_id] = {
            "decision": "eligible" if len(matched) >= arm.hit_gate.minimum_tasks else "no_hit",
            "gate": asdict(arm.hit_gate),
            "matched_task_ids": matched,
            "matched_task_count": len(matched),
        }
    receipt: dict[str, Any] = {
        "schema": HIT_RECEIPT_SCHEMA,
        "created_at": utc_now(),
        "campaign_plan_sha256": canonical_sha256(plan),
        "source_arm_id": SOURCE_ARM_ID,
        "source_output_dir": str(source_dir.resolve()),
        "source_manifest_sha256": file_sha256(source_dir / "manifest.json"),
        "source_results_sha256": file_sha256(source_dir / "results.jsonl"),
        "source_trace_sha256": file_sha256(source_dir / "trace.jsonl"),
        "task_metrics": metrics,
        "task_metrics_sha256": canonical_sha256(metrics),
        "decisions": decisions,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def prepare_derived(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_root = Path(str(plan["paths"]["run_root"]))
    source_arm = next(arm for arm in arms if arm.arm_id == SOURCE_ARM_ID)
    source_dir = output_dir(plan, source_arm)
    artifact_path = run_root / "frozen-task-analysis.json"
    common = common_controller(snapshot)
    source_import = common.materialize_preexisting_source(plan)
    if source_import is None:
        raise ControllerError("preexisting E0 source import is unavailable")
    if Path(str(source_import["source_output_dir"])).resolve() != source_dir.resolve():
        raise ControllerError("preexisting E0 source import/output differs")
    source_consumption_dir = Path(str(source_import["package_dir"]))
    artifact = common.extract_analyzer_artifact(
        source_arm=source_arm,
        source_dir=source_consumption_dir,
        destination=artifact_path,
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        plan_sha256=canonical_sha256(plan),
        replay_schema=str(plan["runtime_contract"]["frozen_replay"]["schema"]),
        allow_deterministic_router_fallback=bool(
            plan.get("runtime_contract", {}).get("analyzer_source", {}).get(
                "allow_deterministic_router_fallback", False
            )
        ),
        source_import_evidence=source_import,
    )
    rows = _result_rows(source_consumption_dir, snapshot)
    hit_receipt = derive_hit_receipt(
        plan, arms, rows=rows, source_dir=source_consumption_dir
    )
    hit_path = run_root / "p1-hit-gates.json"
    atomic_write_json(hit_path, hit_receipt)
    derived: dict[str, Any] = {
        "schema": DERIVED_SCHEMA,
        "created_at": utc_now(),
        "campaign_plan_sha256": canonical_sha256(plan),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "source_arm_id": SOURCE_ARM_ID,
        "screening_design": screening_design_contract(plan),
        "preexisting_source_import_receipt_sha256": source_import["receipt_sha256"],
        "analyzer_artifact_path": str(artifact_path.resolve()),
        "analyzer_artifact_raw_sha256": file_sha256(artifact_path),
        "analyzer_artifact_sha256": artifact.get("artifact_sha256"),
        "hit_receipt_path": str(hit_path.resolve()),
        "hit_receipt_raw_sha256": file_sha256(hit_path),
        "hit_receipt_sha256": hit_receipt["receipt_sha256"],
        "hit_decisions": copy.deepcopy(hit_receipt["decisions"]),
    }
    derived["derived_sha256"] = canonical_sha256(derived)
    atomic_write_json(run_root / "derived-plan.json", derived)
    return derived, artifact


def load_derived(plan: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    run_root = Path(str(plan["paths"]["run_root"]))
    derived_path = run_root / "derived-plan.json"
    derived = load_json(derived_path)
    embedded = derived.get("derived_sha256")
    unsigned = dict(derived)
    unsigned.pop("derived_sha256", None)
    if (
        derived.get("schema") != DERIVED_SCHEMA
        or embedded != canonical_sha256(unsigned)
        or derived.get("campaign_plan_sha256") != canonical_sha256(plan)
        or derived.get("screening_design") != screening_design_contract(plan)
    ):
        raise ControllerError("derived plan identity differs")
    artifact_path = Path(str(derived.get("analyzer_artifact_path") or ""))
    artifact = load_json(artifact_path)
    artifact_unsigned = dict(artifact)
    artifact_hash = artifact_unsigned.pop("artifact_sha256", None)
    if (
        file_sha256(artifact_path) != derived.get("analyzer_artifact_raw_sha256")
        or artifact_hash != canonical_sha256(artifact_unsigned)
        or artifact_hash != derived.get("analyzer_artifact_sha256")
    ):
        raise ControllerError("frozen Analyzer artifact identity differs")
    hit_path = Path(str(derived.get("hit_receipt_path") or ""))
    hit = load_json(hit_path)
    hit_unsigned = dict(hit)
    hit_hash = hit_unsigned.pop("receipt_sha256", None)
    if (
        file_sha256(hit_path) != derived.get("hit_receipt_raw_sha256")
        or hit_hash != canonical_sha256(hit_unsigned)
        or hit_hash != derived.get("hit_receipt_sha256")
        or hit.get("decisions") != derived.get("hit_decisions")
    ):
        raise ControllerError("P1 hit-gate receipt identity differs")
    snapshot = Path(str(plan["paths"]["snapshot"]))
    source_import = common_controller(snapshot).materialize_preexisting_source(plan)
    if (
        source_import is None
        or derived.get("preexisting_source_import_receipt_sha256")
        != source_import.get("receipt_sha256")
    ):
        raise ControllerError("preexisting E0 source import identity differs")
    return derived, artifact


def _selected_cost(
    row: Mapping[str, Any],
    *,
    reporter: Any | None = None,
    prices: Mapping[str, Any] | None = None,
) -> float | None:
    for value in (
        row.get("selected_attempt_billed_cost_usd"),
        row.get("selected_attempt_metrics", {}).get("billed_cost_usd")
        if isinstance(row.get("selected_attempt_metrics"), Mapping)
        else None,
    ):
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            return float(value)
    if reporter is not None and prices is not None:
        evidence = reporter.selected_generation_cost(row, prices)
        value = evidence.get("usd") if isinstance(evidence, Mapping) else None
        if (
            isinstance(evidence, Mapping)
            and evidence.get("complete") is True
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            return float(value)
    return None


def _progression_cost_runtime(
    plan: Mapping[str, Any], snapshot: Path
) -> tuple[Any, Mapping[str, Any]]:
    reporter_path = snapshot / "scripts/experiments/generate_draco_p0_p05_reports.py"
    reporter = _load_module(reporter_path, "draco_p1_progression_cost")
    registry_path = snapshot / str(plan["freeze"]["model_registry"]["path"])
    prices, _ = reporter.load_prices(
        registry_path, plan["freeze"]["model_registry"]
    )
    return reporter, prices


def p1_15_progression_decision(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    control_dir: Path,
    p1_35_dir: Path,
) -> dict[str, Any]:
    reporter, prices = _progression_cost_runtime(plan, snapshot)
    control = {str(row.get("task_id") or ""): row for row in _result_rows(control_dir, snapshot)}
    variant = {str(row.get("task_id") or ""): row for row in _result_rows(p1_35_dir, snapshot)}
    common_ids = sorted(set(control) & set(variant))
    quality_deltas = [
        float(variant[task_id]["quality_total"]) - float(control[task_id]["quality_total"])
        for task_id in common_ids
        if isinstance(control[task_id].get("quality_total"), int | float)
        and isinstance(variant[task_id].get("quality_total"), int | float)
    ]
    left_costs = [
        _selected_cost(control[task_id], reporter=reporter, prices=prices)
        for task_id in common_ids
    ]
    right_costs = [
        _selected_cost(variant[task_id], reporter=reporter, prices=prices)
        for task_id in common_ids
    ]
    complete_cost = all(
        value is not None for value in [*left_costs, *right_costs]
    ) and bool(common_ids)
    left_total = sum(value or 0.0 for value in left_costs)
    right_total = sum(value or 0.0 for value in right_costs)
    reduction = (
        (left_total - right_total) / left_total
        if complete_cost and left_total > 0
        else None
    )
    mean_delta = (
        sum(quality_deltas) / len(quality_deltas)
        if len(quality_deltas) == EXPECTED_TASK_COUNT
        else None
    )
    progression = plan["progression"]
    sufficient = (
        reduction is not None
        and reduction >= float(progression["skip_p1_15_if_cost_reduction_fraction_at_least"])
        and mean_delta is not None
        and mean_delta >= float(progression["minimum_mean_delta_quality"])
    )
    receipt = {
        "schema": "opensquilla.draco-p1-15-progression-receipt/v1",
        "created_at": utc_now(),
        "control_arm_id": str(progression.get("control_arm_id") or REPLAY_CONTROL_IDS[0]),
        "predecessor_arm_id": "P1-35-E1",
        "paired_task_count": len(common_ids),
        "quality_pair_count": len(quality_deltas),
        "mean_delta_quality": mean_delta,
        "selected_generation_cost_evidence_complete": complete_cost,
        "control_selected_generation_cost_usd": left_total if complete_cost else None,
        "candidate_selected_generation_cost_usd": right_total if complete_cost else None,
        "cost_reduction_fraction": reduction,
        "decision": (
            "skip_p1_15_sufficient"
            if sufficient
            else "run_p1_15_insufficient_or_uncertain"
        ),
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def p1_15_uncertain_progression_decision(
    plan: Mapping[str, Any], *, reason: str
) -> dict[str, Any]:
    """Record why P1-15 remains eligible without fabricating paired evidence."""

    receipt = {
        "schema": "opensquilla.draco-p1-15-progression-receipt/v1",
        "created_at": utc_now(),
        "control_arm_id": str(
            plan["progression"].get("control_arm_id") or REPLAY_CONTROL_IDS[0]
        ),
        "predecessor_arm_id": "P1-35-E1",
        "paired_task_count": 0,
        "quality_pair_count": 0,
        "mean_delta_quality": None,
        "selected_generation_cost_evidence_complete": False,
        "control_selected_generation_cost_usd": None,
        "candidate_selected_generation_cost_usd": None,
        "cost_reduction_fraction": None,
        "decision": "run_p1_15_insufficient_or_uncertain",
        "uncertainty_reason": reason,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def load_progression_receipt(plan: Mapping[str, Any], path: Path) -> dict[str, Any]:
    receipt = load_json(path)
    embedded = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if (
        receipt.get("schema")
        != "opensquilla.draco-p1-15-progression-receipt/v1"
        or embedded != canonical_sha256(unsigned)
        or receipt.get("predecessor_arm_id") != "P1-35-E1"
        or receipt.get("control_arm_id")
        != str(plan["progression"].get("control_arm_id") or REPLAY_CONTROL_IDS[0])
        or receipt.get("decision")
        not in {
            "skip_p1_15_sufficient",
            "run_p1_15_insufficient_or_uncertain",
        }
    ):
        raise ControllerError("P1-15 progression receipt identity differs")
    return receipt


def launch_arm(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    override: Mapping[str, Any],
) -> int:
    directory = output_dir(plan, arm)
    directory.parent.mkdir(parents=True, exist_ok=True)
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
    # The dedicated campaign credential is selected by file path.  Never let
    # an unrelated inherited raw key silently override that frozen binding.
    environment.pop("OPENROUTER_API_KEY", None)
    environment.pop("OPENROUTER_API_TOKEN", None)
    environment.update(
        {
            "DRACO_CAMPAIGN_REPORT_ROOT": str(directory.parent),
            "DRACO_CAMPAIGN_REFERENCE_REPO": str(plan["paths"]["reference_repo"]),
            "DRACO_CAMPAIGN_PYTHON": str(plan["paths"]["python"]),
            "DRACO_CAMPAIGN_TASK_CONCURRENCY": str(plan["execution"]["task_concurrency"]),
            "DRACO_OPENROUTER_LOCK_FILE": str(
                plan["execution"]["global_openrouter_lock"]
            ),
            "OPENSQUILLA_OPENROUTER_SECRET_FILE": str(
                plan["execution"]["openrouter_secret_file"]
            ),
        }
    )
    print(f"[{utc_now()}] START {arm.arm_id}", flush=True)
    completed = subprocess.run(command, env=environment, check=False)
    print(f"[{utc_now()}] END {arm.arm_id} rc={completed.returncode}", flush=True)
    return int(completed.returncode)


def initialize_status(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    schedule = list(plan["execution"]["schedule"]["arm_order"])
    anchors = plan["execution"]["schedule"]["anchor_by_arm_id"]
    return {
        "schema": STATUS_SCHEMA,
        "run_id": plan["run_id"],
        "campaign_plan_sha256": canonical_sha256(plan),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "screening_design": screening_design_contract(plan),
        "phase": "initialized",
        "created_at": utc_now(),
        "started_at": None,
        "completed_at": None,
        "active_arm": None,
        "arms": {
            arm.arm_id: {
                "state": "pending",
                "experiment_id": arm.experiment_id,
                "variant": arm.variant,
                "analyzer_mode": arm.analyzer_mode,
                "control_arm_id": arm.control_arm_id,
                "schedule_ordinal": schedule.index(arm.arm_id),
                "anchor_arm_id": anchors[arm.arm_id],
                "output_dir": str(output_dir(plan, arm)),
                "attempts": [],
            }
            for arm in arms
        },
        "excluded": copy.deepcopy(plan["excluded"]),
    }


def load_or_initialize_status(
    plan: Mapping[str, Any], arms: Sequence[Arm], snapshot_identity: Mapping[str, str]
) -> dict[str, Any]:
    path = Path(str(plan["paths"]["run_root"])) / "status.json"
    if not path.exists():
        return initialize_status(plan, arms, snapshot_identity)
    status = load_json(path)
    if (
        status.get("schema") != STATUS_SCHEMA
        or status.get("run_id") != plan["run_id"]
        or status.get("campaign_plan_sha256") != canonical_sha256(plan)
        or status.get("snapshot_commit") != snapshot_identity["commit"]
        or status.get("snapshot_tree") != snapshot_identity["tree"]
        or status.get("screening_design") != screening_design_contract(plan)
        or set(status.get("arms") or {}) != {arm.arm_id for arm in arms}
    ):
        raise ControllerError("existing controller status belongs to another campaign")
    return status


def terminal_phase(status: Mapping[str, Any], *, report_complete: bool | None = None) -> str:
    states = [str(row.get("state") or "") for row in (status.get("arms") or {}).values()]
    if not states or any(state not in TERMINAL_ARM_STATES for state in states):
        return "running"
    if report_complete is False:
        return "completed_with_failures"
    if any(state in {"failed", "blocked_prerequisite"} for state in states):
        return "completed_with_failures"
    return "completed"


def run_terminal_report(plan: Mapping[str, Any], status_path: Path) -> tuple[dict[str, Any], bool]:
    reporter = Path(str(plan["paths"]["reporter"]))
    command = [
        str(plan["paths"]["python"]),
        str(reporter),
        "--plan",
        str(Path(str(plan["paths"]["run_root"])) / "campaign-plan.json"),
        "--status",
        str(status_path),
        "--output-root",
        str(plan["paths"]["report_root"]),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    receipt = {
        "command_sha256": canonical_sha256(command),
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }
    return receipt, completed.returncode == 0


def run_campaign(plan_path: Path) -> int:
    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    snapshot, snapshot_identity = validate_snapshot(plan)
    validate_runtime_freeze(plan, snapshot=snapshot, expected_snapshot_identity=snapshot_identity)
    validate_static_overlays(plan, arms, snapshot)
    run_root = Path(str(plan["paths"]["run_root"]))
    run_root.mkdir(parents=True, exist_ok=True)
    canonical_plan_path = run_root / "campaign-plan.json"
    if plan_path.resolve() != canonical_plan_path.resolve():
        raise ControllerError("run command requires the frozen plan at run_root/campaign-plan.json")
    lock_fd = os.open(run_root / "controller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError("another P1 campaign controller is active") from exc
        status = load_or_initialize_status(plan, arms, snapshot_identity)
        status_path = run_root / "status.json"
        source_import = common_controller(snapshot).materialize_preexisting_source(plan)
        if source_import is None:
            raise ControllerError("preexisting E0 source import is unavailable")
        status.update(
            {
                "phase": "running",
                "started_at": status.get("started_at") or utc_now(),
                "completed_at": None,
                "preexisting_source_import_receipt_sha256": source_import[
                    "receipt_sha256"
                ],
            }
        )
        atomic_write_json(status_path, status)
        derived: dict[str, Any] | None = None
        artifact: dict[str, Any] | None = None
        if (run_root / "derived-plan.json").exists():
            derived, artifact = load_derived(plan)
        any_failure = False
        by_id = {arm.arm_id: arm for arm in arms}
        anchors = plan["execution"]["schedule"]["anchor_by_arm_id"]
        authenticated: set[str] = set()
        progression_receipt: dict[str, Any] | None = None
        progression_path = run_root / "p1-15-progression.json"
        if progression_path.exists():
            progression_receipt = load_progression_receipt(plan, progression_path)
        for arm in arms:
            state = status["arms"][arm.arm_id]
            override: dict[str, Any] | None = None
            try:
                override = resolve_arm_override(plan, arm, artifact=artifact)
                complete, evidence = inspect_complete_arm(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001 - recorded as arm-local prerequisite
                complete, evidence = False, {"reason": "inspection_failed", "detail": str(exc)}
            if complete:
                state.update(
                    {
                        "state": "succeeded",
                        "completion_evidence": evidence,
                        "completed_at": state.get("completed_at") or utc_now(),
                    }
                )
                authenticated.add(arm.arm_id)
                atomic_write_json(status_path, status)
                if arm.arm_id == SOURCE_ARM_ID and derived is None:
                    try:
                        derived, artifact = prepare_derived(
                            plan, arms, snapshot=snapshot, snapshot_identity=snapshot_identity
                        )
                    except Exception as exc:  # noqa: BLE001
                        state["state"] = "failed"
                        state["failure"] = {
                            "reason": "source_derivation_failed",
                            "detail": str(exc),
                        }
                        any_failure = True
                    atomic_write_json(status_path, status)
                continue
            if arm.arm_id != SOURCE_ARM_ID:
                if derived is None:
                    state.update(
                        {
                            "state": "blocked_prerequisite",
                            "completed_at": utc_now(),
                            "failure": {
                                "reason": "source_or_derived_plan_unavailable"
                            },
                        }
                    )
                    any_failure = True
                    atomic_write_json(status_path, status)
                    continue
                decision = derived["hit_decisions"].get(arm.arm_id)
                if not isinstance(decision, Mapping):
                    raise ControllerError(f"derived plan lacks hit decision for {arm.arm_id}")
                if decision.get("decision") == "no_hit":
                    state.update(
                        {
                            "state": "no_hit_skipped",
                            "completed_at": utc_now(),
                            "hit_gate_evidence": copy.deepcopy(dict(decision)),
                        }
                    )
                    atomic_write_json(status_path, status)
                    continue
            if arm.experiment_id == "P1-15":
                predecessor = status["arms"]["P1-35-E1"]
                if predecessor["state"] == "no_hit_skipped":
                    if progression_receipt is None:
                        progression_receipt = p1_15_uncertain_progression_decision(
                            plan, reason="p1_35_source_slice_no_hit"
                        )
                        atomic_write_json(progression_path, progression_receipt)
                elif predecessor["state"] != "succeeded":
                    state.update(
                        {
                            "state": "blocked_prerequisite",
                            "completed_at": utc_now(),
                            "failure": {"reason": "P1-35-E1 did not complete"},
                        }
                    )
                    any_failure = True
                    atomic_write_json(status_path, status)
                    continue
                if progression_receipt is None:
                    control_id = str(
                        plan["progression"].get("control_arm_id")
                        or anchors["P1-35-E1"]
                    )
                    progression_receipt = p1_15_progression_decision(
                        plan,
                        snapshot=snapshot,
                        control_dir=output_dir(plan, by_id[control_id]),
                        p1_35_dir=output_dir(plan, by_id["P1-35-E1"]),
                    )
                    atomic_write_json(progression_path, progression_receipt)
                if progression_receipt.get("decision") == "skip_p1_15_sufficient":
                    state.update(
                        {
                            "state": "progression_skipped",
                            "completed_at": utc_now(),
                            "progression_receipt_sha256": progression_receipt[
                                "receipt_sha256"
                            ],
                        }
                    )
                    atomic_write_json(status_path, status)
                    continue
            anchor = str(anchors[arm.arm_id])
            if anchor != arm.arm_id and anchor not in authenticated:
                state.update(
                    {
                        "state": "blocked_prerequisite",
                        "completed_at": utc_now(),
                        "failure": {
                            "reason": "schedule_anchor_not_authenticated",
                            "anchor_arm_id": anchor,
                        },
                    }
                )
                any_failure = True
                atomic_write_json(status_path, status)
                continue
            directory = output_dir(plan, arm)
            if directory.exists():
                state.update(
                    {
                        "state": "failed",
                        "completed_at": utc_now(),
                        "failure": {
                            "reason": "preexisting_incomplete_output",
                            "completion_evidence": evidence,
                        },
                    }
                )
                any_failure = True
                atomic_write_json(status_path, status)
                continue
            try:
                if override is None:
                    override = resolve_arm_override(plan, arm, artifact=artifact)
                common_controller(snapshot).load_effective_experiment_config(
                    snapshot,
                    snapshot / str(plan["paths"]["experiment_config_relative"]),
                    override,
                )
            except Exception as exc:  # noqa: BLE001
                state.update(
                    {
                        "state": "blocked_prerequisite",
                        "completed_at": utc_now(),
                        "failure": {"reason": str(exc)},
                    }
                )
                any_failure = True
                atomic_write_json(status_path, status)
                continue
            validate_runtime_freeze(
                plan,
                snapshot=snapshot,
                expected_snapshot_identity=snapshot_identity,
            )
            status["active_arm"] = arm.arm_id
            state.update({"state": "running", "started_at": utc_now()})
            attempt = {
                "started_at": state["started_at"],
                "override_sha256": canonical_sha256(override),
                "output_dir": str(directory),
            }
            state["attempts"].append(attempt)
            atomic_write_json(status_path, status)
            try:
                rc = launch_arm(plan, arm, snapshot=snapshot, override=override)
                launch_error = None
            except Exception as exc:  # noqa: BLE001
                rc = None
                launch_error = {
                    "error_class": type(exc).__name__,
                    "message_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
                }
            attempt.update({"completed_at": utc_now(), "rc": rc})
            if launch_error:
                attempt["launcher_error"] = launch_error
            status["active_arm"] = None
            try:
                complete, evidence = inspect_complete_arm(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001
                complete, evidence = False, {"reason": "inspection_failed", "detail": str(exc)}
            state.update({"completed_at": utc_now(), "completion_evidence": evidence})
            if complete:
                state["state"] = "succeeded"
                authenticated.add(arm.arm_id)
            else:
                state["state"] = "failed"
                state["failure"] = {
                    "reason": "launcher_or_completion_contract_failed",
                    "rc": rc,
                    "launcher_error": launch_error,
                }
                any_failure = True
            atomic_write_json(status_path, status)
            if complete and arm.arm_id == SOURCE_ARM_ID:
                try:
                    derived, artifact = prepare_derived(
                        plan, arms, snapshot=snapshot, snapshot_identity=snapshot_identity
                    )
                except Exception as exc:  # noqa: BLE001
                    state["state"] = "failed"
                    state["failure"] = {"reason": "source_derivation_failed", "detail": str(exc)}
                    any_failure = True
                atomic_write_json(status_path, status)
        status.update(
            {
                "phase": terminal_phase(status),
                "completed_at": utc_now(),
                "active_arm": None,
            }
        )
        atomic_write_json(status_path, status)
        validate_runtime_freeze(
            plan,
            snapshot=snapshot,
            expected_snapshot_identity=snapshot_identity,
        )
        report_receipt, report_complete = run_terminal_report(plan, status_path)
        status["reporting"] = report_receipt
        status["phase"] = terminal_phase(status, report_complete=report_complete)
        atomic_write_json(status_path, status)
        return 1 if any_failure or not report_complete else 0


def validate_only(plan_path: Path) -> dict[str, Any]:
    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    snapshot, snapshot_identity = validate_snapshot(plan)
    freeze = validate_runtime_freeze(
        plan, snapshot=snapshot, expected_snapshot_identity=snapshot_identity
    )
    validate_static_overlays(plan, arms, snapshot)
    imported_source = common_controller(snapshot).authenticate_preexisting_source(plan)
    if imported_source is None:
        raise ControllerError("preexisting E0 source import is unavailable")
    return {
        "status": "valid",
        "candidate_live_arm_count": len(arms),
        "supported_group_count": len(SUPPORTED_GROUPS),
        "candidate_arm_count": sum(arm.experiment_id != "common-E0" for arm in arms),
        "live_analyzer_candidate_count": sum(
            arm.experiment_id != "common-E0" and arm.analyzer_mode == "live" for arm in arms
        ),
        "frozen_replay_arm_count": sum(arm.analyzer_mode == "frozen_replay" for arm in arms),
        "excluded_missing_feature_count": len(MISSING_FEATURE_GROUPS),
        "excluded_deterministic_no_hit_count": len(DETERMINISTIC_NO_HIT_GROUPS),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "freeze_identity_sha256": canonical_sha256(freeze),
        "preexisting_source_import": {
            "status": "authenticated",
            "receipt_sha256": imported_source["receipt_sha256"],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-plan", "expand-plan"):
        child = subparsers.add_parser(command)
        child.add_argument("plan", type=Path)
        child.add_argument("--allow-placeholders", action="store_true")
    validate = subparsers.add_parser("validate-only")
    validate.add_argument("plan", type=Path)
    run = subparsers.add_parser("run")
    run.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
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
                        "campaign_plan_sha256": canonical_sha256(plan),
                        "arm_count": len(arms),
                        "candidate_arm_count": sum(
                            arm.experiment_id != "common-E0" for arm in arms
                        ),
                        "supported_group_count": len(SUPPORTED_GROUPS),
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
