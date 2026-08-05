#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate terminal P0/P0.5 DRACO-mini reports without making model calls.

The campaign arm finalizer remains authoritative for sealed results.  This
program validates those formal artifacts, compacts their per-task evidence,
and writes one Markdown report per tuning experiment plus a comprehensive
Markdown/JSON report at the campaign report root.

Cost policy is deliberately narrower than account spend: selected-generation
cost uses the final selected attempt only (including its Analyzer request when
live), excludes Judge and replaced/failed generation attempts, takes provider
actual USD first, and estimates a missing amount from frozen model prices and
input/output/cache tokens.  Judge and campaign account deltas are reported in
separate columns and are never added to selected-generation theoretical cost.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import random
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "opensquilla.draco-p0-p05-campaign-plan/v1"
STATUS_SCHEMA = "opensquilla.draco-p0-p05-controller-status/v1"
DERIVED_SCHEMA = "opensquilla.draco-p0-p05-derived-plan/v1"
REPORT_SCHEMA = "opensquilla.draco-p0-p05-comprehensive-report/v1"
GROUP_REPORT_SCHEMA = "opensquilla.draco-p0-p05-experiment-report/v1"
ACCOUNT_WINDOW_COHORT_SCHEMA = "opensquilla.draco.account-window-cohort/v1"
CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA = (
    "opensquilla.draco-confirmatory-report-input-index/v1"
)
CONFIRMATORY_COHORT_MANIFEST_SCHEMA = (
    "opensquilla.draco-confirmatory-cohort-manifest/v1"
)
CONFIRMATORY_SCHEDULE_SCHEMA = "opensquilla.draco-p0-p05-confirmatory-schedule/v1"
CONFIRMATORY_REPORT_INPUT_INDEX_NAME = "confirmatory-report-inputs.json"
TERMINAL_STATUS_INPUT_SCHEMA = "opensquilla.draco-p0-p05-terminal-status-input/v1"
BOOTSTRAP_SEED = 20260803
BOOTSTRAP_SAMPLES = 20_000
TERMINAL_PHASES = {"succeeded", "completed_with_failures"}
TERMINAL_ARM_STATES = {
    "succeeded",
    "failed",
    "blocked_prerequisite",
    "no_op_deleted",
}
PRICE_REGISTRY_RELATIVE = Path("src/opensquilla/provider/router_dynamic_model_profiles.json")
ANALYZER_ORIGIN_LIVE_SUCCESS = "live_success"
ANALYZER_ORIGIN_ROUTER_FALLBACK = "deterministic_router_fallback"
ANALYZER_ORIGIN_UNKNOWN = "unknown"
LEGACY_EVIDENCE: dict[str, dict[str, str]] = {
    "P0-01": {
        "status": "completed_existing_experiment",
        "path": (
            "/home/codex/code/opensquilla-agentic-routing/reports/draco/"
            "p0-mini-20260803-012853/P0-01/EXPERIMENT_RESULTS.md"
        ),
        "sha256": "2bb1bda791c226886875cde8ace9e64d3d9a3be8c5097b66a7ae52503c367e9b",
    },
    "P0-02": {
        "status": "completed_existing_experiment",
        "path": (
            "/home/codex/code/opensquilla-agentic-routing/reports/draco/"
            "p0-mini-20260803-012853/P0-02/EXPERIMENT_RESULTS.md"
        ),
        "sha256": "8926ef2884e13c0a80aa4f798c8ef0aa76fd46c25bf26415ebff942ef45ad20b",
    },
    "P0.5-31": {
        "status": "completed_existing_experiment",
        "path": (
            "/home/codex/code/opensquilla-agentic-routing/reports/draco/"
            "p0-mini-20260803-012853/P0-5-31/EXPERIMENT_RESULTS.md"
        ),
        "sha256": "8fe616ae1fa9a4c71cc44d0b29cb68b43a82f53d11240550bfb3ff16c0f98e6e",
    },
    "P0-15": {
        "status": "stopped_existing_experiment",
        "path": (
            "/home/codex/code/opensquilla-agentic-routing/reports/draco/"
            "p0-15-draco-mini-20260802-003324/EXPERIMENT_RESULTS.md"
        ),
        "sha256": "e08cdc3e10b0ba340ecf5096e187e391ba23799e5c9745ae1b7fac75b60abcfe",
    },
}


class ReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    experiment_id: str
    directory_name: str
    title: str
    variant: str
    replicate: int
    analyzer_mode: str
    control_arm_id: str | None
    override: dict[str, Any]
    dynamic: dict[str, Any] | None
    wire_gate: str | None
    output_name: str


@dataclass(frozen=True)
class FrozenControllerVerifier:
    """Authenticated, read-only verifier loaded from the campaign freeze."""

    module: Any
    path: Path
    raw_sha256: str
    arms: Mapping[str, Any]
    snapshot: Path
    snapshot_identity: Mapping[str, str]
    derived: Mapping[str, Any] | None
    artifact: Mapping[str, Any] | None
    derived_error: str | None


FROZEN_CONTROLLER_RELATIVE = Path("scripts/experiments/run_draco_p0_p05_tuning_campaign.py")


def load_frozen_controller_verifier(
    plan: Mapping[str, Any],
    *,
    plan_sha256: str,
) -> FrozenControllerVerifier:
    """Import only the hash-frozen controller and run its read-only gates."""

    freeze = plan.get("freeze")
    sources = freeze.get("sources") if isinstance(freeze, Mapping) else None
    expected_sha = (
        str(sources.get("controller_raw_sha256") or "").removeprefix("sha256:")
        if isinstance(sources, Mapping)
        else ""
    )
    if len(expected_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise ReportError("plan.freeze.sources.controller_raw_sha256 is malformed")
    snapshot_root = Path(str(plan.get("paths", {}).get("snapshot") or ""))
    controller_path = snapshot_root / FROZEN_CONTROLLER_RELATIVE
    regular_file(controller_path)
    actual_sha = file_sha256(controller_path)
    if actual_sha != expected_sha:
        raise ReportError(
            "frozen controller raw hash differs from plan.freeze.sources.controller_raw_sha256"
        )
    module_name = f"_draco_p0_p05_frozen_controller_{actual_sha}"
    spec = importlib.util.spec_from_file_location(module_name, controller_path)
    if spec is None or spec.loader is None:
        raise ReportError("cannot create frozen controller import specification")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ReportError(f"cannot import frozen controller: {exc}") from exc
    required = (
        "validate_plan",
        "validate_snapshot",
        "validate_runtime_freeze",
        "output_dir",
        "resolve_arm_override",
        "arm_completion_identity",
        "inspect_complete_arm",
        "load_derived",
    )
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if missing:
        raise ReportError("frozen controller verifier API is incomplete: " + ", ".join(missing))
    try:
        controller_arms = module.validate_plan(plan, allow_placeholders=False)
        snapshot, snapshot_identity = module.validate_snapshot(plan)
        module.validate_runtime_freeze(
            plan,
            snapshot=snapshot,
            expected_snapshot_identity=snapshot_identity,
        )
    except Exception as exc:
        raise ReportError(f"frozen controller validation failed: {exc}") from exc
    if not isinstance(controller_arms, Sequence):
        raise ReportError("frozen controller returned a malformed arm inventory")
    arms: dict[str, Any] = {}
    for arm in controller_arms:
        arm_id = str(getattr(arm, "arm_id", "") or "")
        if not arm_id or arm_id in arms:
            raise ReportError("frozen controller returned invalid or duplicate arm ids")
        arms[arm_id] = arm
    reporter_ids = {arm.arm_id for arm in expand_arms(plan)}
    if set(arms) != reporter_ids:
        raise ReportError("reporter/controller expanded arm inventories differ")

    derived: Mapping[str, Any] | None = None
    artifact: Mapping[str, Any] | None = None
    derived_error: str | None = None
    derived_path = Path(str(plan["paths"]["run_root"])) / "derived-plan.json"
    if derived_path.exists():
        try:
            loaded_derived, loaded_artifact = module.load_derived(plan, plan_sha256)
            if not isinstance(loaded_derived, Mapping) or not isinstance(loaded_artifact, Mapping):
                raise ReportError("frozen controller returned malformed derived evidence")
            derived = loaded_derived
            artifact = loaded_artifact
        except Exception as exc:  # noqa: BLE001 - retained as partial evidence
            derived_error = str(exc)
    return FrozenControllerVerifier(
        module=module,
        path=controller_path.resolve(),
        raw_sha256=actual_sha,
        arms=arms,
        snapshot=Path(snapshot).resolve(),
        snapshot_identity=dict(snapshot_identity),
        derived=derived,
        artifact=artifact,
        derived_error=derived_error,
    )


def now_iso() -> str:
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


def regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReportError(f"missing artifact: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReportError(f"artifact is not a regular non-symlink file: {path}")


def regular_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReportError(f"missing artifact directory: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReportError(f"artifact directory is not a non-symlink directory: {path}")


def raw_sha256(value: Any) -> str | None:
    text = str(value or "")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return None
    return text


def prefixed_sha256(value: Any) -> str | None:
    text = str(value or "")
    if not text.startswith("sha256:") or raw_sha256(text[7:]) is None:
        return None
    return text


def absolute_receipt_path(value: Any, *, label: str) -> Path:
    text = str(value or "")
    path = Path(text)
    if not text or not path.is_absolute():
        raise ReportError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReportError(f"{label} does not exist: {path}") from exc
    if resolved != path:
        raise ReportError(f"{label} is not a canonical non-symlink path: {path}")
    return path


def load_json(path: Path) -> dict[str, Any]:
    regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"JSON root is not an object: {path}")
    return value


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def integer(value: Any) -> int:
    parsed = number(value)
    return int(parsed) if parsed is not None else 0


def mean(values: Iterable[Any]) -> float | None:
    clean = [item for value in values if (item := number(value)) is not None]
    return sum(clean) / len(clean) if clean else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def fmt(value: Any, digits: int = 4) -> str:
    parsed = number(value)
    return "—" if parsed is None else f"{parsed:.{digits}f}"


def pct(value: Any) -> str:
    parsed = number(value)
    return "—" if parsed is None else f"{100.0 * parsed:.2f}%"


def code_json(value: Any) -> str:
    return "`" + json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "`"


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def validate_embedded_hash(
    document: Mapping[str, Any],
    field: str,
    *,
    prefixed: bool = True,
) -> bool:
    recorded = str(document.get(field) or "")
    payload = {key: value for key, value in document.items() if key != field}
    expected = canonical_sha256(payload)
    # The campaign finalizer/controller contract uses an explicit algorithm
    # prefix.  Accepting an unqualified digest would silently weaken that
    # contract and make a malformed document look authenticated.
    return recorded == (f"sha256:{expected}" if prefixed else expected)


def result_evidence_valid(row: Mapping[str, Any]) -> bool:
    schema = row.get("result_evidence_schema")
    if not isinstance(schema, str) or not schema:
        return False
    payload = {
        "schema": schema,
        "result": {key: value for key, value in row.items() if key != "result_evidence_sha256"},
    }
    return row.get("result_evidence_sha256") == "sha256:" + canonical_sha256(payload)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
    )


def expand_arms(plan: Mapping[str, Any]) -> list[ArmSpec]:
    run_id = str(plan.get("run_id") or "")
    comparison_controls = plan.get("comparison_controls")
    if not isinstance(comparison_controls, Mapping):
        raise ReportError("comparison_controls contract is missing")
    source_control = str(comparison_controls.get("source_arm_id") or "")
    default_control = str(comparison_controls.get("default_control_arm_id") or "")
    replay_controls = comparison_controls.get("replay_control_arm_ids")
    control_overrides = comparison_controls.get("arm_control_overrides")
    if (
        not source_control
        or not default_control
        or not isinstance(replay_controls, list)
        or len(replay_controls) != 3
        or len({str(value) for value in replay_controls}) != 3
        or not isinstance(control_overrides, Mapping)
        or comparison_controls.get("require_same_analyzer_mode") is not True
    ):
        raise ReportError("comparison_controls contract is malformed")
    result: list[ArmSpec] = []
    for row in plan.get("common_e0") or []:
        arm_id = str(row["arm_id"])
        result.append(
            ArmSpec(
                arm_id=arm_id,
                experiment_id="common-E0",
                directory_name="common",
                title="Current G1-C common control",
                variant=str(row["variant"]),
                replicate=int(row["replicate"]),
                analyzer_mode=str(row["analyzer_mode"]),
                control_arm_id=None,
                override=dict(row.get("override") or {}),
                dynamic=None,
                wire_gate=None,
                output_name=f"{arm_id}-{run_id}",
            )
        )
    for experiment in plan.get("experiments") or []:
        experiment_id = str(experiment["id"])
        title = str(experiment.get("title") or experiment_id)
        directory_name = str(experiment.get("directory_name") or experiment_id.replace(".", "-"))
        for variant in experiment.get("variants") or []:
            replicates = int(variant.get("replicates", 1))
            replicate_overrides = variant.get("replicate_overrides")
            if replicate_overrides is not None and (
                not isinstance(replicate_overrides, list)
                or len(replicate_overrides) != replicates
                or any(not isinstance(value, Mapping) for value in replicate_overrides)
            ):
                raise ReportError(f"{experiment_id} replicate_overrides/replicates differ")
            for replicate in range(1, replicates + 1):
                suffix = f"-R{replicate}" if replicates > 1 else ""
                arm_id = f"{experiment_id}-{variant['id']}{suffix}"
                control = str(control_overrides.get(arm_id) or default_control)
                override = copy.deepcopy(dict(variant.get("override") or {}))
                if isinstance(replicate_overrides, list):
                    override = deep_merge(override, replicate_overrides[replicate - 1])
                result.append(
                    ArmSpec(
                        arm_id=arm_id,
                        experiment_id=experiment_id,
                        directory_name=directory_name,
                        title=title,
                        variant=str(variant["id"]),
                        replicate=replicate,
                        analyzer_mode=str(variant.get("analyzer_mode", "frozen_replay")),
                        control_arm_id=control,
                        override=override,
                        dynamic=(
                            dict(variant["dynamic"])
                            if isinstance(variant.get("dynamic"), Mapping)
                            else None
                        ),
                        wire_gate=str(variant["wire_gate"]) if variant.get("wire_gate") else None,
                        output_name=f"{arm_id}-{run_id}",
                    )
                )
    ids = [arm.arm_id for arm in result]
    if len(ids) != len(set(ids)):
        raise ReportError("expanded arm ids are not unique")
    specs_by_id = {arm.arm_id: arm for arm in result}
    candidate_ids = {
        arm.arm_id for arm in result if arm.experiment_id != "common-E0"
    }
    if set(str(key) for key in control_overrides) != candidate_ids:
        raise ReportError("comparison_controls.arm_control_overrides must cover every candidate")
    if source_control not in specs_by_id or specs_by_id[source_control].analyzer_mode != "live":
        raise ReportError("comparison source control must be one live Analyzer common E0")
    replay_ids = [str(value) for value in replay_controls]
    if default_control not in replay_ids:
        raise ReportError("default comparison control must be a replay control")
    if any(
        control_id not in specs_by_id
        or specs_by_id[control_id].experiment_id != "common-E0"
        or specs_by_id[control_id].analyzer_mode != "frozen_replay"
        for control_id in replay_ids
    ):
        raise ReportError("replay comparison controls must be frozen-replay common E0 arms")
    for arm in result:
        if arm.experiment_id == "common-E0":
            continue
        control = specs_by_id.get(str(arm.control_arm_id or ""))
        if control is None or control.experiment_id != "common-E0":
            raise ReportError(f"{arm.arm_id} comparison control is unavailable")
        if control.analyzer_mode != arm.analyzer_mode:
            raise ReportError(
                f"{arm.arm_id} comparison control analyzer_mode differs: "
                f"{control.analyzer_mode} != {arm.analyzer_mode}"
            )
    return result


def comparison_control_contract(
    plan: Mapping[str, Any], specs: Sequence[ArmSpec]
) -> dict[str, Any]:
    raw = plan.get("comparison_controls")
    if not isinstance(raw, Mapping):
        raise ReportError("comparison_controls contract is missing")
    by_id = {spec.arm_id: spec for spec in specs}
    source = str(raw.get("source_arm_id") or "")
    default = str(raw.get("default_control_arm_id") or "")
    replay = [str(value) for value in raw.get("replay_control_arm_ids") or []]
    overrides = {
        str(key): str(value)
        for key, value in (raw.get("arm_control_overrides") or {}).items()
    }
    if source not in by_id or default not in by_id or len(replay) != 3:
        raise ReportError("comparison control ids are malformed")
    return {
        "source_arm_id": source,
        "default_control_arm_id": default,
        "replay_control_arm_ids": replay,
        "require_same_analyzer_mode": raw.get("require_same_analyzer_mode") is True,
        "arm_control_overrides": overrides,
    }


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_schedule_evidence(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    specs: Sequence[ArmSpec],
) -> dict[str, Any]:
    """Authenticate the relaxed anchored-serial order without calling it AB/BA."""

    execution = plan.get("execution") if isinstance(plan.get("execution"), Mapping) else {}
    schedule = execution.get("schedule") if isinstance(execution.get("schedule"), Mapping) else {}
    order = [str(value) for value in schedule.get("arm_order") or []]
    anchors = (
        {str(key): str(value) for key, value in schedule.get("anchor_by_arm_id", {}).items()}
        if isinstance(schedule.get("anchor_by_arm_id"), Mapping)
        else {}
    )
    expected_ids = [spec.arm_id for spec in specs]
    expected_set = set(expected_ids)
    expected_sha = canonical_sha256(schedule) if schedule else ""
    reasons: list[str] = []
    if schedule.get("mode") != "anchored_serial":
        reasons.append("execution schedule mode is not anchored_serial")
    if schedule.get("strict_task_interleaving") is not False:
        reasons.append("execution schedule must explicitly disclose non-strict task interleaving")
    if len(order) != len(expected_ids) or len(order) != len(set(order)) or set(order) != expected_set:
        reasons.append("execution schedule arm_order is not an exact arm permutation")
    if set(anchors) != expected_set or any(anchor not in expected_set for anchor in anchors.values()):
        reasons.append("execution schedule anchor coverage differs from arm inventory")
    if str(status.get("schedule_sha256") or "").removeprefix("sha256:") != expected_sha:
        reasons.append("status schedule SHA differs from the frozen plan")
    state_map = status.get("arms") if isinstance(status.get("arms"), Mapping) else {}
    # The controller freezes and persists 1-based ordinals so operator-facing
    # status agrees with the rendered schedule table.
    ordinals = {arm_id: index for index, arm_id in enumerate(order, start=1)}
    if status.get("schedule_mode") != schedule.get("mode"):
        reasons.append("status schedule mode differs from the frozen plan")
    if status.get("strict_task_interleaving") is not schedule.get(
        "strict_task_interleaving"
    ):
        reasons.append("status task-interleaving disclosure differs from the frozen plan")
    if set(state_map) != expected_set:
        reasons.append("status schedule arm inventory differs from the frozen plan")
    rows: dict[str, dict[str, Any]] = {}
    for arm_id in expected_ids:
        state = state_map.get(arm_id) if isinstance(state_map.get(arm_id), Mapping) else {}
        expected_ordinal = ordinals.get(arm_id)
        expected_anchor = anchors.get(arm_id)
        if state.get("schedule_ordinal") != expected_ordinal:
            reasons.append(f"status schedule ordinal differs: {arm_id}")
        if str(state.get("anchor_arm_id") or "") != str(expected_anchor or ""):
            reasons.append(f"status schedule anchor differs: {arm_id}")
        anchor_state = (
            state_map.get(expected_anchor)
            if expected_anchor and isinstance(state_map.get(expected_anchor), Mapping)
            else {}
        )
        started = parse_iso(state.get("started_at"))
        anchor_completed = parse_iso(anchor_state.get("completed_at"))
        lag_seconds = (
            (started - anchor_completed).total_seconds()
            if (
                expected_anchor != arm_id
                and started is not None
                and anchor_completed is not None
            )
            else None
        )
        if (
            expected_ordinal is not None
            and expected_anchor in ordinals
            and ordinals[expected_anchor] > expected_ordinal
        ):
            reasons.append(f"schedule anchor follows candidate: {arm_id}")
        rows[arm_id] = {
            "schedule_ordinal": expected_ordinal,
            "anchor_arm_id": expected_anchor,
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "anchor_completed_at": anchor_state.get("completed_at"),
            "anchor_lag_seconds": lag_seconds,
        }
    controls = comparison_control_contract(plan, specs)
    for spec in specs:
        expected_anchor = anchors.get(spec.arm_id)
        expected_control = (
            spec.arm_id if spec.experiment_id == "common-E0" else spec.control_arm_id
        )
        if expected_anchor != expected_control:
            reasons.append(f"schedule anchor/comparison control differs: {spec.arm_id}")
    return {
        "valid": not reasons,
        "mode": schedule.get("mode"),
        "strict_task_interleaving": False,
        "task_interleaving_contract_satisfied": False,
        "design_label": "anchored_serial_not_task_interleaved",
        "schedule_sha256": expected_sha or None,
        "status_schedule_sha256": status.get("schedule_sha256"),
        "source_arm_id": controls["source_arm_id"],
        "default_control_arm_id": controls["default_control_arm_id"],
        "replay_control_arm_ids": controls["replay_control_arm_ids"],
        "arm_timing": rows,
        "reasons": reasons,
        "limitation": (
            "E0 controls and candidates use separate serial account windows and are not "
            "interleaved AB/BA within each task; paired deltas may retain temporal drift."
        ),
    }


def p0_20_e3_promotion_evidence(
    schedule_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Disclose why this mini arm cannot satisfy the source C3 promotion gate."""

    timings = (
        schedule_evidence.get("arm_timing")
        if isinstance(schedule_evidence.get("arm_timing"), Mapping)
        else {}
    )
    arm = timings.get("P0-20-E3") if isinstance(timings.get("P0-20-E3"), Mapping) else {}
    anchor = (
        timings.get("common-E0-R1")
        if isinstance(timings.get("common-E0-R1"), Mapping)
        else {}
    )
    ordinal = integer(arm.get("schedule_ordinal"))
    anchor_ordinal = integer(anchor.get("schedule_ordinal"))
    return {
        "arm_id": "P0-20-E3",
        "status": "mini_diagnostic_only",
        "eligible_as_c3_promotion_evidence": False,
        "required_strict_task_interleaving": True,
        "observed_strict_task_interleaving": (
            schedule_evidence.get("strict_task_interleaving") is True
        ),
        "anchor_arm_id": arm.get("anchor_arm_id"),
        "scheduled_after_r1_anchor": bool(
            arm.get("anchor_arm_id") == "common-E0-R1"
            and ordinal > 0
            and anchor_ordinal > 0
            and ordinal > anchor_ordinal
        ),
        "schedule_ordinal": ordinal or None,
        "anchor_schedule_ordinal": anchor_ordinal or None,
        "schedule_ordinal_gap": (
            ordinal - anchor_ordinal if ordinal > anchor_ordinal > 0 else None
        ),
        "limitation": (
            "P0-20-E3 is placed after the R1 anchor in the nearby anchored-serial "
            "tranche to reduce temporal drift, but whole-arm serial execution is not "
            "per-task E0/candidate interleaving and cannot satisfy the source C3 "
            "promotion gate."
        ),
    }


Price = tuple[float, float, float | None, float | None]


def registry_identities(models: Sequence[Any], *, label: str) -> list[str]:
    identities: list[str] = []
    for index, raw in enumerate(models):
        if not isinstance(raw, Mapping):
            raise ReportError(f"{label} registry model {index} is malformed")
        facts = raw.get("registry_facts")
        source = facts if isinstance(facts, Mapping) else raw
        explicit = str(source.get("identity") or "").strip().casefold()
        provider = str(source.get("provider") or "").strip().casefold()
        model = str(source.get("model_id") or source.get("model") or "").strip().casefold()
        identity = explicit or (f"{provider}:{model}" if provider and model else "")
        if ":" not in identity:
            raise ReportError(f"{label} registry model {index} has no canonical identity")
        parsed_provider, parsed_model = identity.split(":", 1)
        if not parsed_provider or not parsed_model:
            raise ReportError(f"{label} registry model {index} has no canonical identity")
        identities.append(f"{parsed_provider}:{parsed_model}")
    if len(identities) != len(set(identities)):
        raise ReportError(f"{label} registry identities are not unique")
    return sorted(identities)


def formal_registry_projection(registry: Mapping[str, Any]) -> dict[str, Any]:
    projected = copy.deepcopy(dict(registry))
    if projected.get("schema_version") != "step2-model-registry-v2":
        return projected
    projected["schema_version"] = "step2-model-registry-v1"
    if projected.get("snapshot_version") == "curated-openrouter-step2-2026-07-27.1":
        projected["snapshot_version"] = "curated-openrouter-step2-2026-07-24.3"
    rows = projected.get("models")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            facts = row.get("registry_facts")
            if isinstance(facts, dict):
                facts.pop("thinking_levels", None)
                facts.pop("thinking_level_mapping", None)
    return projected


def validate_registry_contract(
    registry: Mapping[str, Any],
    *,
    path: Path,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    models = registry.get("models")
    if not isinstance(models, list):
        raise ReportError("frozen price registry models are malformed")
    formal = formal_registry_projection(registry)
    formal_models = formal.get("models")
    if not isinstance(formal_models, list):
        raise ReportError("formal frozen price registry models are malformed")
    full_ids = registry_identities(models, label="full")
    formal_ids = registry_identities(formal_models, label="formal")
    actual = {
        "raw_sha256": file_sha256(path),
        "full_snapshot_version": str(registry.get("snapshot_version") or ""),
        "full_canonical_sha256": canonical_sha256(registry),
        "formal_snapshot_version": str(formal.get("snapshot_version") or ""),
        "formal_canonical_sha256": canonical_sha256(formal),
        "model_count": len(models),
        "full_model_count": len(full_ids),
        "formal_model_count": len(formal_ids),
        "full_identities_sha256": canonical_sha256(full_ids),
        "formal_identities_sha256": canonical_sha256(formal_ids),
    }
    fields = tuple(actual)
    differences = [field for field in fields if contract.get(field) != actual[field]]
    if differences:
        raise ReportError(
            "frozen price registry differs from plan.freeze.model_registry: "
            + ", ".join(differences)
        )
    return actual


def load_prices(
    path: Path,
    contract: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Price], dict[str, Any]]:
    registry = load_json(path)
    if not isinstance(contract, Mapping):
        raise ReportError("plan.freeze.model_registry contract is missing")
    frozen_identity = validate_registry_contract(registry, path=path, contract=contract)
    prices: dict[str, Price] = {}
    for item in registry.get("models") or []:
        if not isinstance(item, Mapping):
            continue
        facts = item.get("registry_facts")
        price = facts.get("price") if isinstance(facts, Mapping) else None
        model = (
            str(facts.get("model_id") or "").strip().casefold()
            if isinstance(facts, Mapping)
            else ""
        )
        if not model or not isinstance(price, Mapping):
            continue
        input_rate = number(price.get("input_per_million"))
        output_rate = number(price.get("output_per_million"))
        if input_rate is None or output_rate is None or input_rate < 0 or output_rate < 0:
            continue
        cache_read = number(
            price.get("cache_read_per_million", price.get("input_cache_read_per_million"))
        )
        cache_write = number(
            price.get("cache_write_per_million", price.get("input_cache_write_per_million"))
        )
        prices[model] = (input_rate, output_rate, cache_read, cache_write)
    metadata = {
        "path": str(path),
        "sha256": file_sha256(path),
        "schema_version": registry.get("schema_version"),
        "snapshot_version": registry.get("snapshot_version"),
        "model_count": len(registry.get("models") or []),
        "priced_model_count": len(prices),
        "cache_price_model_count": sum(
            read is not None or write is not None for _, _, read, write in prices.values()
        ),
        "freeze_contract": frozen_identity,
        "freeze_contract_valid": True,
    }
    return prices, metadata


def find_price(model: Any, prices: Mapping[str, Price]) -> Price | None:
    normalized = str(model or "").strip().casefold()
    aliases = [normalized]
    if normalized.startswith("openrouter:"):
        aliases.append(normalized.split(":", 1)[1])
    return next((prices[key] for key in aliases if key in prices), None)


def usage_tokens(unit: Mapping[str, Any]) -> tuple[int, int, int, int]:
    provider = unit.get("provider_usage") if isinstance(unit.get("provider_usage"), Mapping) else {}
    details = (
        provider.get("prompt_tokens_details")
        if isinstance(provider.get("prompt_tokens_details"), Mapping)
        else {}
    )
    input_tokens = max(integer(unit.get("input_tokens")), integer(provider.get("prompt_tokens")))
    output_tokens = max(
        integer(unit.get("output_tokens")), integer(provider.get("completion_tokens"))
    )
    cache_read = max(
        integer(unit.get("cache_read_tokens")),
        integer(unit.get("cached_tokens")),
        integer(details.get("cached_tokens")),
    )
    cache_write = max(
        integer(unit.get("cache_write_tokens")), integer(details.get("cache_write_tokens"))
    )
    cache_read = min(max(0, cache_read), input_tokens)
    cache_write = min(max(0, cache_write), max(0, input_tokens - cache_read))
    return input_tokens, output_tokens, cache_read, cache_write


def unit_actual_usd(unit: Mapping[str, Any]) -> float | None:
    provider = unit.get("provider_usage") if isinstance(unit.get("provider_usage"), Mapping) else {}
    receipt = unit.get("billing_receipt")
    if receipt is None:
        receipt = provider.get("billing_receipt")
    receipt_present = receipt is not None
    receipt_usd: float | None = None
    if (
        isinstance(receipt, Mapping)
        and str(receipt.get("status") or "").strip().casefold() == "confirmed"
    ):
        nanos = receipt.get("usd_equivalent_nanos")
        if isinstance(nanos, int) and not isinstance(nanos, bool) and nanos >= 0:
            receipt_usd = nanos / 1_000_000_000
    if receipt_present and receipt_usd is None:
        return None
    reported = number(provider.get("provider_reported_cost"))
    billed = number(unit.get("billed_cost"))
    outer_source = str(unit.get("cost_source") or "").strip().casefold()
    provider_source = str(provider.get("cost_source") or "").strip().casefold()
    actual_sources = {"provider_billed", "openrouter_usage"}
    legacy_sources = {"", "none", "unavailable"}
    actual = receipt_usd
    source_is_actual = outer_source in actual_sources or (
        outer_source in legacy_sources and provider_source in actual_sources
    )
    source_allows_legacy = (
        outer_source in legacy_sources and provider_source in legacy_sources
    )
    if actual is None and source_is_actual:
        actual = reported if reported is not None else billed
    if actual is None and source_allows_legacy:
        actual = next(
            (value for value in (reported, billed) if value is not None and value > 0),
            None,
        )
    # OpenRouter's zero-dollar BYOK receipt is not the underlying provider
    # spend.  It is therefore estimated from tokens instead of called actual.
    if provider.get("is_byok") is True and actual == 0:
        return None
    return actual if actual is not None and actual >= 0 else None


def price_unit(unit: Mapping[str, Any], prices: Mapping[str, Price]) -> dict[str, Any]:
    requests = max(0, integer(unit.get("request_count")) or 1)
    actual = unit_actual_usd(unit)
    if actual is not None:
        return {"usd": actual, "requests": requests, "mode": "actual", "tokens": usage_tokens(unit)}
    input_tokens, output_tokens, cache_read, cache_write = usage_tokens(unit)
    price = find_price(unit.get("requested_model") or unit.get("model"), prices)
    if price is None or not any((input_tokens, output_tokens, cache_read, cache_write)):
        return {
            "usd": None,
            "requests": requests,
            "mode": "ignored",
            "tokens": (input_tokens, output_tokens, cache_read, cache_write),
        }
    input_rate, output_rate, cache_read_rate, cache_write_rate = price
    fresh = max(0, input_tokens - cache_read - cache_write)
    missing_cache_rate = bool(
        (cache_read and cache_read_rate is None) or (cache_write and cache_write_rate is None)
    )
    effective_read_rate = input_rate if cache_read_rate is None else cache_read_rate
    effective_write_rate = input_rate if cache_write_rate is None else cache_write_rate
    estimated = (
        fresh * input_rate
        + cache_read * effective_read_rate
        + cache_write * effective_write_rate
        + output_tokens * output_rate
    ) / 1_000_000
    if missing_cache_rate:
        mode = "estimated_cache_price_fallback"
    elif cache_read or cache_write:
        mode = "estimated_cache_aware"
    else:
        mode = "estimated_no_cache"
    return {
        "usd": estimated,
        "requests": requests,
        "mode": mode,
        "tokens": (input_tokens, output_tokens, cache_read, cache_write),
    }


def aggregate_cost(
    units: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Price],
    accounting: Mapping[str, Any],
    *,
    selected_scope: bool,
) -> dict[str, Any]:
    expected_requests = integer(accounting.get("request_count"))
    aggregate_actual = number(accounting.get("recorded_cost_usd"))
    aggregate_claims_exact = bool(
        aggregate_actual is not None
        and aggregate_actual >= 0
        and accounting.get("cost_complete") is True
        and accounting.get("cost_exact") is True
        and integer(accounting.get("unknown_request_count")) == 0
    )
    priced = [price_unit(unit, prices) for unit in units]
    observed_requests = sum(item["requests"] for item in priced)
    independently_exact = bool(
        aggregate_claims_exact
        and units
        and observed_requests == expected_requests
        and all(item["mode"] == "actual" for item in priced)
        and math.isclose(
            sum(float(item["usd"]) for item in priced if item["usd"] is not None),
            float(aggregate_actual),
            rel_tol=0,
            abs_tol=1e-12,
        )
    )
    if independently_exact:
        return {
            "usd": aggregate_actual,
            "complete": True,
            "exact": True,
            "request_count": expected_requests,
            "actual_requests": expected_requests,
            "estimated_requests": 0,
            "estimated_cache_aware_requests": 0,
            "estimated_cache_price_fallback_requests": 0,
            "estimated_no_cache_requests": 0,
            "ignored_requests": 0,
            "source": "selected_scope_exact_aggregate"
            if selected_scope
            else "judge_exact_aggregate",
        }
    if aggregate_claims_exact and not units:
        # Preserve the recorded amount as a disclosed lower bound, but never
        # claim every physical call was checked without its usage units.
        return {
            "usd": aggregate_actual,
            "complete": False,
            "exact": False,
            "request_count": expected_requests,
            "actual_requests": integer(accounting.get("exact_request_count")),
            "estimated_requests": 0,
            "estimated_cache_aware_requests": 0,
            "estimated_cache_price_fallback_requests": 0,
            "estimated_no_cache_requests": 0,
            "ignored_requests": max(
                0,
                expected_requests - integer(accounting.get("exact_request_count")),
            ),
            "source": "exact_aggregate_without_physical_units_lower_bound",
        }
    # Extra units make selected-attempt scope ambiguous and risk including a
    # replaced retry.  Never guess which paid request to retain.
    if selected_scope and expected_requests and observed_requests > expected_requests:
        return {
            "usd": None,
            "complete": False,
            "exact": False,
            "request_count": expected_requests,
            "actual_requests": 0,
            "estimated_requests": 0,
            "estimated_cache_aware_requests": 0,
            "estimated_cache_price_fallback_requests": 0,
            "estimated_no_cache_requests": 0,
            "ignored_requests": expected_requests,
            "source": "ambiguous_selected_scope_ignored",
        }
    known = [item for item in priced if item["usd"] is not None]
    missing_units = max(0, expected_requests - observed_requests)
    mode_counts = Counter()
    for item in priced:
        mode_counts[item["mode"]] += item["requests"]
    ignored = mode_counts["ignored"] + missing_units
    total = sum(float(item["usd"]) for item in known)
    effective_requests = expected_requests or observed_requests
    actual_requests = mode_counts["actual"]
    estimated_requests = sum(
        count for mode, count in mode_counts.items() if mode.startswith("estimated_")
    )
    if aggregate_actual is not None and aggregate_actual >= 0 and not units:
        total = aggregate_actual
        actual_requests = integer(accounting.get("exact_request_count"))
        ignored = max(0, effective_requests - actual_requests)
    return {
        "usd": total if known or aggregate_actual is not None else None,
        "complete": bool(
            effective_requests
            and ignored == 0
            and actual_requests + estimated_requests == effective_requests
        ),
        "exact": bool(effective_requests and actual_requests == effective_requests),
        "request_count": effective_requests,
        "actual_requests": actual_requests,
        "estimated_requests": estimated_requests,
        "estimated_cache_aware_requests": mode_counts["estimated_cache_aware"],
        "estimated_cache_price_fallback_requests": mode_counts["estimated_cache_price_fallback"],
        "estimated_no_cache_requests": mode_counts["estimated_no_cache"],
        "ignored_requests": ignored,
        "source": "physical_usage_units",
    }


def selected_attempt_usage_binding(row: Mapping[str, Any]) -> dict[str, Any]:
    """Bind root usage to the finalizer-selected generation attempt."""

    finalization = (
        row.get("campaign_finalization")
        if isinstance(row.get("campaign_finalization"), Mapping)
        else {}
    )
    selection = (
        finalization.get("selection") if isinstance(finalization.get("selection"), Mapping) else {}
    )
    selected_id = str(selection.get("selected_generation_attempt_id") or "")
    execution = row.get("execution") if isinstance(row.get("execution"), Mapping) else {}
    attempts = [
        attempt
        for attempt in execution.get("generation_attempts") or []
        if isinstance(attempt, Mapping) and str(attempt.get("attempt_id") or "") == selected_id
    ]
    accounting_root = (
        row.get("cost_accounting") if isinstance(row.get("cost_accounting"), Mapping) else {}
    )
    accounting = (
        accounting_root.get("selected_generation_attempt")
        if isinstance(accounting_root.get("selected_generation_attempt"), Mapping)
        else {}
    )
    reasons: list[str] = []
    if len(selected_id) != 32 or any(char not in "0123456789abcdef" for char in selected_id):
        reasons.append("selected generation attempt identity is missing or malformed")
    if len(attempts) != 1:
        reasons.append("selected generation attempt identity is not unique in execution evidence")
    if accounting.get("scope") != "selected_generation_attempt":
        reasons.append("cost accounting lacks selected_generation_attempt scope")
    root_usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else None
    selected_usage: Mapping[str, Any] | None = None
    if len(attempts) == 1:
        run = attempts[0].get("run") if isinstance(attempts[0].get("run"), Mapping) else {}
        selected_usage = run.get("usage") if isinstance(run.get("usage"), Mapping) else None
        if attempts[0].get("attempt_kind") != "generation":
            reasons.append("selected attempt kind is not generation")
    if root_usage is None or selected_usage is None:
        reasons.append("selected generation attempt lacks bound usage")
    elif canonical_bytes(root_usage) != canonical_bytes(selected_usage):
        reasons.append("root usage differs from selected generation attempt usage")
    if row.get("selected_generation_succeeded") is not True:
        reasons.append("selected generation attempt is not marked succeeded")
    return {
        "valid": not reasons,
        "attempt_id": selected_id or None,
        "usage": root_usage if not reasons else None,
        "reasons": reasons,
    }


def ignored_selected_scope(accounting: Mapping[str, Any], reason: str) -> dict[str, Any]:
    request_count = max(0, integer(accounting.get("request_count")))
    return {
        "usd": None,
        "complete": False,
        "exact": False,
        "request_count": request_count,
        "actual_requests": 0,
        "estimated_requests": 0,
        "estimated_cache_aware_requests": 0,
        "estimated_cache_price_fallback_requests": 0,
        "estimated_no_cache_requests": 0,
        "ignored_requests": request_count,
        "source": reason,
    }


def selected_generation_cost(row: Mapping[str, Any], prices: Mapping[str, Price]) -> dict[str, Any]:
    cost = row.get("cost_accounting") if isinstance(row.get("cost_accounting"), Mapping) else {}
    accounting = (
        cost.get("selected_generation_attempt")
        if isinstance(cost.get("selected_generation_attempt"), Mapping)
        else {}
    )
    binding = selected_attempt_usage_binding(row)
    if binding.get("valid") is not True:
        return ignored_selected_scope(accounting, "selected_attempt_identity_unverified_ignored")
    usage = binding.get("usage") if isinstance(binding.get("usage"), Mapping) else {}
    breakdown = usage.get("model_usage_breakdown")
    units: list[Mapping[str, Any]] = []
    if isinstance(breakdown, list):
        for item in breakdown:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").casefold()
            if "judge" in role:
                continue
            if item.get("selected") is False:
                continue
            units.append(item)
    elif usage:
        units = [usage]
    return aggregate_cost(units, prices, accounting, selected_scope=True)


def selected_model_usage(
    row: Mapping[str, Any], prices: Mapping[str, Price]
) -> dict[str, dict[str, Any]]:
    """Compact final-selected proposer/aggregator physical units by model."""
    binding = selected_attempt_usage_binding(row)
    if binding.get("valid") is not True:
        return {}
    usage = binding.get("usage") if isinstance(binding.get("usage"), Mapping) else {}
    breakdown = usage.get("model_usage_breakdown")
    result: dict[str, dict[str, Any]] = {}
    for unit in breakdown if isinstance(breakdown, list) else []:
        if not isinstance(unit, Mapping) or unit.get("selected") is False:
            continue
        role = str(unit.get("role") or "").casefold()
        if role not in {"proposer", "aggregator"}:
            continue
        model = normalize_model(unit.get("requested_model") or unit.get("model"))
        if not model:
            continue
        priced = price_unit(unit, prices)
        input_tokens, output_tokens, cache_read, cache_write = priced["tokens"]
        target = result.setdefault(
            model,
            {
                "model": model,
                "roles": set(),
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_counted_usd": 0.0,
                "actual_requests": 0,
                "estimated_requests": 0,
                "ignored_requests": 0,
            },
        )
        requests = integer(priced["requests"])
        target["roles"].add(role)
        target["request_count"] += requests
        target["input_tokens"] += input_tokens
        target["output_tokens"] += output_tokens
        target["cache_read_tokens"] += cache_read
        target["cache_write_tokens"] += cache_write
        if priced["usd"] is not None:
            target["cost_counted_usd"] += float(priced["usd"])
        if priced["mode"] == "actual":
            target["actual_requests"] += requests
        elif str(priced["mode"]).startswith("estimated_"):
            target["estimated_requests"] += requests
        else:
            target["ignored_requests"] += requests
    for value in result.values():
        value["roles"] = sorted(value["roles"])
    return result


def judge_usage_units(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    judge = row.get("judge") if isinstance(row.get("judge"), Mapping) else {}
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for criterion in judge.get("criterion_judgments") or []:
        if not isinstance(criterion, Mapping):
            continue
        for attempt in criterion.get("judge_attempts") or []:
            if not isinstance(attempt, Mapping):
                continue
            run = attempt.get("run") if isinstance(attempt.get("run"), Mapping) else {}
            usage = run.get("usage") if isinstance(run.get("usage"), Mapping) else {}
            breakdown = usage.get("model_usage_breakdown")
            candidates = breakdown if isinstance(breakdown, list) else [usage] if usage else []
            for unit in candidates:
                if not isinstance(unit, Mapping):
                    continue
                provider = (
                    unit.get("provider_usage")
                    if isinstance(unit.get("provider_usage"), Mapping)
                    else {}
                )
                identity = str(
                    unit.get("physical_attempt_id")
                    or provider.get("physical_attempt_id")
                    or attempt.get("attempt_id")
                    or ""
                )
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                result.append(unit)
    return result


def judge_cost(row: Mapping[str, Any], prices: Mapping[str, Price]) -> dict[str, Any]:
    cost = row.get("cost_accounting") if isinstance(row.get("cost_accounting"), Mapping) else {}
    accounting = cost.get("judge") if isinstance(cost.get("judge"), Mapping) else {}
    return aggregate_cost(judge_usage_units(row), prices, accounting, selected_scope=False)


def selection_plan(row: Mapping[str, Any]) -> Mapping[str, Any]:
    for trace_key in ("routing_trace", "ensemble_trace"):
        trace = row.get(trace_key)
        if isinstance(trace, Mapping) and isinstance(trace.get("selection_plan"), Mapping):
            return trace["selection_plan"]
    ensemble = row.get("ensemble_trace")
    if isinstance(ensemble, Mapping):
        for call in ensemble.get("calls") or []:
            if isinstance(call, Mapping) and isinstance(call.get("selection_plan"), Mapping):
                return call["selection_plan"]
    return {}


def normalize_model(value: Any) -> str:
    model = str(value or "").strip().casefold()
    return model.split(":", 1)[1] if model.startswith("openrouter:") else model


def task_analyzer_origin_evidence(analyzer: Mapping[str, Any]) -> dict[str, Any]:
    """Expose Analyzer origin without laundering legacy rows into fallbacks.

    Frozen replay v2 is authoritative because it binds ``origin_outcome`` in
    the replay proof.  A v1 replay (or a live Analyzer trace) may still be
    classified as ``live_success`` when the row carries affirmative success
    evidence.  Everything else remains ``unknown``: a legacy fallback reason
    alone is useful reporting evidence, but it must not be used to invent a
    deterministic-router-fallback provenance that the row did not bind.
    """

    replay = analyzer.get("replay") if isinstance(analyzer.get("replay"), Mapping) else {}
    fallback_reason = str(analyzer.get("fallback_reason") or "").strip()
    raw_origin = replay.get("origin_outcome")
    explicit_origin = (
        str(raw_origin).strip()
        if isinstance(raw_origin, str) and str(raw_origin).strip()
        else ""
    )
    if explicit_origin:
        outcome = explicit_origin
        evidence = "replay.origin_outcome"
        explicit = True
    else:
        replay_schema = str(replay.get("schema") or "").strip()
        v1_replay_success = bool(
            replay_schema == "opensquilla.draco.frozen-task-analysis/v1"
            and analyzer.get("schema_valid") is not False
            and not fallback_reason
        )
        affirmative_live_success = bool(
            analyzer.get("schema_valid") is True and not fallback_reason
        )
        if v1_replay_success or affirmative_live_success:
            outcome = ANALYZER_ORIGIN_LIVE_SUCCESS
            evidence = (
                "legacy_v1_replay_success"
                if v1_replay_success
                else "legacy_live_success"
            )
        else:
            outcome = ANALYZER_ORIGIN_UNKNOWN
            evidence = "origin_outcome_missing"
        explicit = False
    return {
        "origin_outcome": outcome,
        "origin_outcome_explicit": explicit,
        "origin_evidence": evidence,
        "fallback_reason": fallback_reason,
        "is_explicit_router_fallback": bool(
            explicit and outcome == ANALYZER_ORIGIN_ROUTER_FALLBACK
        ),
    }


def compact_row(row: Mapping[str, Any], prices: Mapping[str, Price]) -> dict[str, Any]:
    judge = row.get("judge") if isinstance(row.get("judge"), Mapping) else {}
    usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
    metrics = (
        row.get("selected_attempt_metrics")
        if isinstance(row.get("selected_attempt_metrics"), Mapping)
        else {}
    )
    execution = (
        row.get("execution_status") if isinstance(row.get("execution_status"), Mapping) else {}
    )
    completion = (
        row.get("completion_status") if isinstance(row.get("completion_status"), Mapping) else {}
    )
    ensemble = row.get("ensemble_trace") if isinstance(row.get("ensemble_trace"), Mapping) else {}
    plan = selection_plan(row)
    pass_rate = number(judge.get("valid_pass_rate"))
    if pass_rate is None:
        pass_rate = number(judge.get("pass_rate"))
    if pass_rate is not None and pass_rate > 1:
        pass_rate /= 100.0
    generation_cost = selected_generation_cost(row, prices)
    separated_judge_cost = judge_cost(row, prices)
    selected_binding = selected_attempt_usage_binding(row)
    input_tokens = number(usage.get("input_tokens"))
    output_tokens = number(usage.get("output_tokens"))
    reasoning_tokens = number(usage.get("reasoning_tokens"))
    selected_p = list(plan.get("selected_P") or [])
    selected_a = (
        plan.get("selected_A") or plan.get("aggregator_model") or ensemble.get("executed_A")
    )
    analyzer = plan.get("task_analyzer") if isinstance(plan.get("task_analyzer"), Mapping) else {}
    analyzer_origin = task_analyzer_origin_evidence(analyzer)
    routing = row.get("routing_trace") if isinstance(row.get("routing_trace"), Mapping) else {}
    routing_analyzer = (
        routing.get("task_analyzer") if isinstance(routing.get("task_analyzer"), Mapping) else {}
    )
    execution_ok = bool(
        execution.get("success") is True
        or row.get("selected_generation_succeeded") is True
        or completion.get("execution_pass") is True
    )
    judge_complete = bool(
        completion.get("judge_complete") is True or judge.get("score_status") == "complete"
    )
    proposer_recovery = (
        ensemble.get("proposer_recovery")
        if isinstance(ensemble.get("proposer_recovery"), Mapping)
        else {}
    )
    aggregator_recovery = (
        ensemble.get("aggregator_recovery")
        if isinstance(ensemble.get("aggregator_recovery"), Mapping)
        else {}
    )
    assembled = (
        ensemble.get("assembled_output")
        if isinstance(ensemble.get("assembled_output"), Mapping)
        else {}
    )
    nonbyok = (
        row.get("openrouter_non_byok_audit")
        if isinstance(row.get("openrouter_non_byok_audit"), Mapping)
        else {}
    )
    analyzer_usage = analyzer.get("usage") if isinstance(analyzer.get("usage"), Mapping) else {}
    analyzer_config = (
        plan.get("ranking_parameters", {}).get("task_analyzer", {})
        if isinstance(plan.get("ranking_parameters"), Mapping)
        and isinstance(plan.get("ranking_parameters", {}).get("task_analyzer"), Mapping)
        else {}
    )
    analyzer_stop_reasons: list[str] = []
    for attempt in analyzer_usage.get("physical_attempts") or []:
        if not isinstance(attempt, Mapping):
            continue
        provider_usage = (
            attempt.get("provider_usage")
            if isinstance(attempt.get("provider_usage"), Mapping)
            else {}
        )
        stop = (
            str(attempt.get("stop_reason") or provider_usage.get("stop_reason") or "")
            .strip()
            .casefold()
        )
        if stop:
            analyzer_stop_reasons.append(stop)
    root_analyzer_stop = str(analyzer_usage.get("stop_reason") or "").strip().casefold()
    if root_analyzer_stop:
        analyzer_stop_reasons.append(root_analyzer_stop)
    length_markers = {"length", "max_tokens", "max_output_tokens", "token_limit", "max_token_limit"}
    analyzer_output_tokens = integer(analyzer_usage.get("output_tokens"))
    analyzer_max_output_tokens = integer(analyzer_config.get("max_output_tokens"))
    analyzer_explicit_truncated = bool(
        analyzer.get("truncated") is True
        or analyzer_usage.get("truncated") is True
        or any(
            isinstance(attempt, Mapping) and attempt.get("truncated") is True
            for attempt in analyzer_usage.get("physical_attempts") or []
        )
    )
    analyzer_length_stop = any(
        stop in length_markers or "length" in stop or "max_token" in stop
        for stop in analyzer_stop_reasons
    )
    return {
        "task_id": str(row.get("task_id") or ""),
        "group": str(row.get("group") or ""),
        "domain": str(row.get("domain") or ""),
        "quality": number(row.get("quality_total")),
        "pass_rate": pass_rate,
        "judge_errors": integer(judge.get("judge_error_count")),
        "done": execution_ok and judge_complete,
        "execution_ok": execution_ok,
        "judge_complete": judge_complete,
        "input": input_tokens,
        "output": output_tokens,
        "reason": reasoning_tokens,
        "cache": number(usage.get("cached_tokens")),
        "visible": max(0.0, output_tokens - reasoning_tokens)
        if output_tokens is not None and reasoning_tokens is not None
        else None,
        "tokens": input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None,
        "tools": number(metrics.get("total_tool_call_count")),
        "tool_used": integer(metrics.get("total_tool_call_count")) > 0,
        "steps": number(metrics.get("trajectory_steps")),
        "llm_req": number(metrics.get("llm_request_count")),
        "latency": number(metrics.get("latency_ms") or row.get("latency_ms")),
        "generation_cost": generation_cost,
        "selected_attempt_binding_valid": selected_binding.get("valid") is True,
        "selected_attempt_id": selected_binding.get("attempt_id"),
        "selected_attempt_binding_reasons": selected_binding.get("reasons") or [],
        "judge_cost": separated_judge_cost,
        "model_generation": selected_model_usage(row, prices),
        "selected_p": selected_p,
        "selected_a": str(selected_a or ""),
        "ap_overlap": bool(
            str(selected_a or "") and str(selected_a or "") in {str(item) for item in selected_p}
        ),
        "n": len(selected_p) or integer(plan.get("proposer_count")),
        "stop_reason": str(plan.get("stop_reason") or "unknown"),
        "fallback": bool(
            ensemble.get("fallback_used") or ensemble.get("any_intermediate_fallback")
        ),
        "outer_retry": max(0, integer(row.get("generation_attempt_count")) - 1),
        "proposer_recovery": integer(proposer_recovery.get("additional_physical_requests_started")),
        "aggregator_attempts": len(aggregator_recovery.get("attempts") or []),
        "partial_proposers": integer(ensemble.get("partial_proposers")),
        "degraded": execution.get("status") == "degraded_success",
        "assembled_truncated": assembled.get("truncated") is True,
        "request_context_hash": str(
            plan.get("request_context_hash") or routing_analyzer.get("request_context_hash") or ""
        ),
        "task_profile_hash": str(plan.get("task_profile_hash") or ""),
        "analyzer_source": str(analyzer.get("source") or routing_analyzer.get("source") or ""),
        "analyzer_origin_outcome": analyzer_origin["origin_outcome"],
        "analyzer_origin_outcome_explicit": analyzer_origin["origin_outcome_explicit"],
        "analyzer_origin_evidence": analyzer_origin["origin_evidence"],
        "analyzer_fallback_reason": analyzer_origin["fallback_reason"],
        "analyzer_origin_is_fallback": analyzer_origin["is_explicit_router_fallback"],
        "analyzer_output_tokens": analyzer_output_tokens,
        "analyzer_max_output_tokens": analyzer_max_output_tokens,
        "analyzer_stop_reasons": sorted(set(analyzer_stop_reasons)),
        "analyzer_length_stop": analyzer_length_stop,
        "analyzer_truncated": analyzer_explicit_truncated or analyzer_length_stop,
        "analyzer_at_or_above_cap": bool(
            analyzer_max_output_tokens and analyzer_output_tokens >= analyzer_max_output_tokens
        ),
        "row_policy_pass": nonbyok.get("pass"),
        "explicit_byok_requests": integer(nonbyok.get("explicit_byok_request_count")),
        "error": str(row.get("error") or ""),
    }


def artifact_entry(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    entry = artifacts.get(name)
    return entry if isinstance(entry, Mapping) else None


def artifact_binding_valid(root: Path, manifest: Mapping[str, Any], name: str) -> tuple[bool, str]:
    path = root / name
    try:
        regular_file(path)
    except ReportError as exc:
        return False, str(exc)
    entry = artifact_entry(manifest, name)
    if entry is None:
        return False, f"manifest omits {name}"
    if entry.get("path") != name:
        return False, f"manifest path binding differs for {name}"
    recorded_size = entry.get("size_bytes")
    if (
        isinstance(recorded_size, bool)
        or not isinstance(recorded_size, int)
        or recorded_size != path.stat().st_size
    ):
        return False, f"manifest size differs for {name}"
    expected = str(entry.get("sha256") or "").removeprefix("sha256:")
    if expected != file_sha256(path):
        return False, f"manifest sha256 differs for {name}"
    return True, "ok"


def read_compact_rows(
    path: Path,
    prices: Mapping[str, Price],
) -> tuple[list[dict[str, Any]], list[str]]:
    regular_file(path)
    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise ReportError(f"empty JSONL record {path}:{line_number}")
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReportError(f"invalid JSONL {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ReportError(f"non-object JSONL row {path}:{line_number}")
            if not result_evidence_valid(row):
                reasons.append(f"result evidence hash invalid at line {line_number}")
            compact = compact_row(row, prices)
            compact["result_evidence_schema"] = row.get("result_evidence_schema")
            compact["result_evidence_sha256"] = row.get("result_evidence_sha256")
            rows.append(compact)
    return rows, reasons


def aggregator_request_started(call: Mapping[str, Any]) -> bool:
    recovery = (
        call.get("aggregator_recovery")
        if isinstance(call.get("aggregator_recovery"), Mapping)
        else {}
    )
    return any(
        isinstance(attempt, Mapping)
        and (
            attempt.get("request_started") is True
            or integer(attempt.get("physical_request_count")) > 0
        )
        for attempt in recovery.get("attempts") or []
    )


def trace_candidate_order_calls(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    ensemble = row.get("ensemble_trace") if isinstance(row.get("ensemble_trace"), Mapping) else {}
    raw_calls = ensemble.get("calls")
    calls = (
        [call for call in raw_calls if isinstance(call, Mapping)]
        if isinstance(raw_calls, list)
        else [ensemble]
        if ensemble
        else []
    )
    result: list[dict[str, Any]] = []
    for index, call in enumerate(calls, start=1):
        display_order = call.get("candidate_display_order")
        request_started = aggregator_request_started(call)
        applicable = bool(
            call.get("shuffle_candidates") is True
            and isinstance(display_order, list)
            and display_order
            and request_started
        )
        result.append(
            {
                "call_index": integer(call.get("agent_call_index")) or index,
                "shuffle_candidates": call.get("shuffle_candidates"),
                "configured_candidate_order_seed": call.get(
                    "configured_candidate_order_seed"
                ),
                "candidate_order_seed": call.get("candidate_order_seed"),
                "candidate_display_order_present": bool(
                    isinstance(display_order, list) and display_order
                ),
                "aggregator_request_started": request_started,
                "applicable": applicable,
                "not_applicable_reason": (
                    None
                    if applicable
                    else "shuffle_disabled"
                    if call.get("shuffle_candidates") is not True
                    else "candidate_display_order_absent"
                    if not isinstance(display_order, list) or not display_order
                    else "aggregator_request_not_started"
                ),
            }
        )
    return result


def read_trace_evidence(
    path: Path,
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]], list[str]]:
    """Stream trace JSONL and retain task bindings plus shuffle seed evidence.

    Trace files are large, and U+2028/U+2029 are valid characters inside JSON
    strings rather than JSONL record separators.  Iterating the text handle is
    therefore intentional; this function must never be rewritten with
    ``read_text().splitlines()``.
    """

    regular_file(path)
    bindings: dict[str, str] = {}
    candidate_order_calls: dict[str, list[dict[str, Any]]] = {}
    reasons: list[str] = []
    row_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise ReportError(f"empty JSONL record {path}:{line_number}")
            row_count += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReportError(f"invalid JSONL {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ReportError(f"non-object JSONL row {path}:{line_number}")
            if row.get("result_evidence_schema") is not None and not result_evidence_valid(row):
                reasons.append(f"trace result evidence hash invalid at line {line_number}")
            task_id = str(row.get("task_id") or "")
            evidence_sha = str(row.get("result_evidence_sha256") or "")
            if not task_id:
                reasons.append(f"trace task id missing at line {line_number}")
                continue
            if task_id in bindings:
                reasons.append(f"duplicate trace task id {task_id}")
                continue
            bindings[task_id] = evidence_sha
            candidate_order_calls[task_id] = trace_candidate_order_calls(row)
    if row_count != 10:
        reasons.append(f"trace row count is {row_count}, expected 10")
    return bindings, candidate_order_calls, reasons


def p0_5_36_seed_evidence(
    spec: ArmSpec,
    calls_by_task: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    expected_task_ids: Sequence[str],
) -> dict[str, Any] | None:
    if spec.experiment_id != "P0.5-36":
        return None
    ensemble = spec.override.get("ensemble") if isinstance(spec.override.get("ensemble"), Mapping) else {}
    configured = ensemble.get("candidate_order_seed")
    configuration_valid = (
        isinstance(configured, int)
        and not isinstance(configured, bool)
        and configured in {0, 1, 4}
        and ensemble.get("shuffle_candidates") is True
    )
    valid_tasks: list[str] = []
    invalid_tasks: list[str] = []
    not_applicable_tasks: list[str] = []
    applicable_call_count = 0
    invalid_call_count = 0
    per_task: dict[str, Any] = {}
    for task_id in expected_task_ids:
        calls = [call for call in calls_by_task.get(task_id, ()) if isinstance(call, Mapping)]
        applicable = [call for call in calls if call.get("applicable") is True]
        mismatches = [
            call
            for call in applicable
            if (
                not configuration_valid
                or call.get("configured_candidate_order_seed") != configured
                or call.get("candidate_order_seed") != configured
            )
        ]
        applicable_call_count += len(applicable)
        invalid_call_count += len(mismatches)
        if not applicable:
            not_applicable_tasks.append(task_id)
            state = "not_applicable"
        elif mismatches:
            invalid_tasks.append(task_id)
            state = "invalid"
        else:
            valid_tasks.append(task_id)
            state = "valid"
        per_task[task_id] = {
            "state": state,
            "applicable_call_count": len(applicable),
            "invalid_call_count": len(mismatches),
            "observed_configured_seeds": sorted(
                {
                    call.get("configured_candidate_order_seed")
                    for call in applicable
                    if isinstance(call.get("configured_candidate_order_seed"), int)
                    and not isinstance(call.get("configured_candidate_order_seed"), bool)
                }
            ),
            "observed_effective_seeds": sorted(
                {
                    call.get("candidate_order_seed")
                    for call in applicable
                    if isinstance(call.get("candidate_order_seed"), int)
                    and not isinstance(call.get("candidate_order_seed"), bool)
                }
            ),
            "not_applicable_call_count": len(calls) - len(applicable),
        }
    return {
        "configured_seed": configured,
        "effective_seed_expected": configured if configuration_valid else None,
        "configuration_valid": configuration_valid,
        "expected_seed_set": [0, 1, 4],
        "valid_task_ids": valid_tasks,
        "invalid_task_ids": invalid_tasks,
        "not_applicable_task_ids": not_applicable_tasks,
        "valid_task_count": len(valid_tasks),
        "invalid_task_count": len(invalid_tasks),
        "not_applicable_task_count": len(not_applicable_tasks),
        "applicable_call_count": applicable_call_count,
        "invalid_call_count": invalid_call_count,
        "comparison_slice_available": configuration_valid and bool(valid_tasks),
        "comparison_evidence_complete": bool(
            configuration_valid
            and len(valid_tasks) == len(expected_task_ids)
            and not invalid_tasks
            and not not_applicable_tasks
        ),
        "per_task": per_task,
        "policy": (
            "Only calls that actually shuffled a non-empty candidate display order and "
            "started an Aggregator request require the configured seed. Calls that never "
            "entered candidate aggregation are disclosed as not_applicable, not execution failures."
        ),
    }


def status_dimension(values: Sequence[Any], *, warning_status: str = "warning") -> str:
    present = [value for value in values if value is not None]
    if any(value is False for value in present):
        return warning_status
    if present and all(value is True for value in present):
        return "pass"
    return "unknown"


def validate_account_window_cohort(
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    proof: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Authenticate one role's projection of a shared two-arm account window."""

    values = [
        source.get("account_window_cohort")
        for source in (manifest, audit, proof)
    ]
    present = [value for value in values if value is not None]
    if not present:
        return None, []
    reasons: list[str] = []
    if len(present) != len(values) or any(not isinstance(value, Mapping) for value in present):
        return None, ["account-window cohort evidence is not bound by all formal artifacts"]
    cohort = copy.deepcopy(dict(present[0]))
    if any(dict(value) != cohort for value in present[1:] if isinstance(value, Mapping)):
        reasons.append("account-window cohort evidence differs across formal artifacts")
    expected_fields = {
        "schema",
        "cohort_id",
        "members",
        "account_evidence",
        "role",
        "companion_role",
        "cohort_sha256",
    }
    if set(cohort) != expected_fields:
        reasons.append("account-window cohort field set differs")
        return cohort, reasons
    cohort_id = str(cohort.get("cohort_id") or "")
    safe_chars = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    if (
        not 1 <= len(cohort_id) <= 128
        or not cohort_id[0].isalnum()
        or any(char not in safe_chars for char in cohort_id)
    ):
        reasons.append("account-window cohort id is invalid")
    role = str(cohort.get("role") or "")
    companion_role = str(cohort.get("companion_role") or "")
    if {role, companion_role} != {"control", "candidate"}:
        reasons.append("account-window cohort roles are invalid")
    members = cohort.get("members")
    if not isinstance(members, Mapping) or set(members) != {"control", "candidate"}:
        reasons.append("account-window cohort members are incomplete")
    else:
        for member_role, member in members.items():
            if not isinstance(member, Mapping) or set(member) != {
                "results",
                "manifests",
                "selected_generation_attempt_bindings_sha256",
            }:
                reasons.append(f"account-window cohort {member_role} member is invalid")
                continue
            results = member.get("results")
            manifests = member.get("manifests")
            if (
                not isinstance(results, list)
                or not results
                or not isinstance(manifests, list)
                or len(results) != len(manifests)
            ):
                reasons.append(
                    f"account-window cohort {member_role} source cardinality differs"
                )
            for kind, sources in (("result", results), ("manifest", manifests)):
                if not isinstance(sources, list):
                    continue
                source_indexes: list[int] = []
                for source in sources:
                    source_index = (
                        source.get("source_index")
                        if isinstance(source, Mapping)
                        else None
                    )
                    digest = (
                        str(source.get("sha256") or "")
                        if isinstance(source, Mapping)
                        else ""
                    )
                    if (
                        not isinstance(source, Mapping)
                        or set(source) != {"source_index", "sha256"}
                        or isinstance(source_index, bool)
                        or not isinstance(source_index, int)
                        or source_index < 0
                        or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)
                    ):
                        reasons.append(
                            f"account-window cohort {member_role} {kind} hash is invalid"
                        )
                    if isinstance(source_index, int) and not isinstance(source_index, bool):
                        source_indexes.append(source_index)
                if source_indexes != list(range(len(sources))):
                    reasons.append(
                        f"account-window cohort {member_role} {kind} order differs"
                    )
            binding_sha = str(
                member.get("selected_generation_attempt_bindings_sha256") or ""
            )
            if (
                len(binding_sha) != 71
                or not binding_sha.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in binding_sha[7:])
            ):
                reasons.append(
                    f"account-window cohort {member_role} selection hash is invalid"
                )
    account_evidence = cohort.get("account_evidence")
    expected_account_fields = {
        "account_before_sha256",
        "account_after_sha256",
        "account_reconciliation_sha256",
        "runtime_environment_sha256",
    }
    if not isinstance(account_evidence, Mapping) or set(account_evidence) != expected_account_fields:
        reasons.append("account-window cohort account evidence is invalid")
    elif any(
        len(str(value or "")) != 64
        or any(char not in "0123456789abcdef" for char in str(value or ""))
        for value in account_evidence.values()
    ):
        reasons.append("account-window cohort account evidence hash is invalid")
    stable_projection = {
        "schema": cohort.get("schema"),
        "cohort_id": cohort.get("cohort_id"),
        "members": cohort.get("members"),
        "account_evidence": cohort.get("account_evidence"),
    }
    if cohort.get("schema") != ACCOUNT_WINDOW_COHORT_SCHEMA:
        reasons.append("account-window cohort schema differs")
    if str(cohort.get("cohort_sha256") or "") != "sha256:" + canonical_sha256(
        stable_projection
    ):
        reasons.append("account-window cohort hash differs")
    return cohort, list(dict.fromkeys(reasons))


def account_evidence(
    manifest: Mapping[str, Any],
    proof: Mapping[str, Any],
    *,
    cohort: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attribution = (
        manifest.get("cost_attribution")
        if isinstance(manifest.get("cost_attribution"), Mapping)
        else {}
    )
    proof_account = proof.get("account") if isinstance(proof.get("account"), Mapping) else {}
    reconciliation = (
        manifest.get("reconciliation")
        if isinstance(manifest.get("reconciliation"), Mapping)
        else {}
    )
    account_delta = number(attribution.get("campaign_bound_account_window_total_usd"))
    if account_delta is None:
        account_delta = number(attribution.get("account_window_delta_usd"))
    if account_delta is None:
        account_delta = number(proof_account.get("campaign_usage_delta_usd"))
    byok_delta = number(proof_account.get("campaign_byok_usage_delta_usd"))
    if byok_delta is None:
        byok_delta = number(proof_account.get("byok_usage_delta_usd"))
    stable = reconciliation.get("stable")
    if stable is None:
        windows = attribution.get("account_windows")
        stable = bool(windows) and all(
            isinstance(item, Mapping)
            and integer(item.get("stable_poll_count"))
            >= integer(item.get("required_stable_poll_count"))
            for item in windows or []
        )
    result = {
        "account_delta_usd": account_delta,
        "byok_delta_usd": byok_delta,
        "reconciliation_status": str(
            reconciliation.get("status") or proof.get("status") or "unknown"
        ),
        "reconciliation_stable": bool(stable),
        "account_window_count": len(attribution.get("account_windows") or []),
        "scope": "campaign account delta including Judge; separate from selected generation",
    }
    if cohort is not None:
        result["account_window_cohort"] = copy.deepcopy(dict(cohort))
        result["cohort_id"] = cohort.get("cohort_id")
        result["cohort_role"] = cohort.get("role")
        result["cohort_sha256"] = cohort.get("cohort_sha256")
    return result


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    generation_known = [
        number(row["generation_cost"].get("usd"))
        for row in rows
        if number(row["generation_cost"].get("usd")) is not None
    ]
    judge_known = [
        number(row["judge_cost"].get("usd"))
        for row in rows
        if number(row["judge_cost"].get("usd")) is not None
    ]
    generation_total = sum(value for value in generation_known if value is not None)
    judge_total = sum(value for value in judge_known if value is not None)
    generation_request_count = sum(
        integer(row["generation_cost"].get("request_count")) for row in rows
    )
    generation_actual_requests = sum(
        integer(row["generation_cost"].get("actual_requests")) for row in rows
    )
    generation_estimated_requests = sum(
        integer(row["generation_cost"].get("estimated_requests")) for row in rows
    )
    generation_ignored_requests = sum(
        integer(row["generation_cost"].get("ignored_requests")) for row in rows
    )
    generation_complete = bool(rows) and all(
        row["generation_cost"].get("complete") is True for row in rows
    )
    judge_complete = bool(rows) and all(row["judge_cost"].get("complete") is True for row in rows)
    latencies = [value for row in rows if (value := number(row.get("latency"))) is not None]
    model_generation: dict[str, dict[str, Any]] = {}
    for row in rows:
        for model, source in (row.get("model_generation") or {}).items():
            target = model_generation.setdefault(
                model,
                {
                    "model": model,
                    "roles": set(),
                    "request_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_counted_usd": 0.0,
                    "actual_requests": 0,
                    "estimated_requests": 0,
                    "ignored_requests": 0,
                },
            )
            target["roles"].update(source.get("roles") or [])
            for field in (
                "request_count",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_counted_usd",
                "actual_requests",
                "estimated_requests",
                "ignored_requests",
            ):
                target[field] += source.get(field) or 0
    for value in model_generation.values():
        value["roles"] = sorted(value["roles"])
    return {
        "row_count": len(rows),
        "done_count": sum(bool(row.get("done")) for row in rows),
        "execution_success_count": sum(bool(row.get("execution_ok")) for row in rows),
        "judge_complete_count": sum(bool(row.get("judge_complete")) for row in rows),
        "avg_quality_total": mean(row.get("quality") for row in rows),
        "avg_pass_rate": mean(row.get("pass_rate") for row in rows),
        "judge_error_count": sum(integer(row.get("judge_errors")) for row in rows),
        "avg_input_tokens": mean(row.get("input") for row in rows),
        "avg_output_tokens": mean(row.get("output") for row in rows),
        "avg_reasoning_tokens": mean(row.get("reason") for row in rows),
        "avg_cached_tokens": mean(row.get("cache") for row in rows),
        "avg_visible_tokens": mean(row.get("visible") for row in rows),
        "avg_total_tokens": mean(row.get("tokens") for row in rows),
        "avg_tool_calls": mean(row.get("tools") for row in rows),
        "tool_task_rate": mean(1.0 if row.get("tool_used") else 0.0 for row in rows),
        "avg_trajectory_steps": mean(row.get("steps") for row in rows),
        "avg_llm_requests": mean(row.get("llm_req") for row in rows),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "selected_generation_cost_counted_usd": generation_total if generation_known else None,
        "avg_selected_generation_cost_usd": generation_total / len(rows)
        if generation_known and rows
        else None,
        "selected_generation_cost_complete": generation_complete,
        "selected_generation_cost_is_lower_bound": bool(rows and not generation_complete),
        "selected_generation_cost_exact_task_count": sum(
            row["generation_cost"].get("exact") is True for row in rows
        ),
        "selected_generation_cost_reported_task_count": len(generation_known),
        "selected_generation_cost_request_count": generation_request_count,
        "selected_generation_cost_actual_request_count": generation_actual_requests,
        "selected_generation_cost_estimated_request_count": generation_estimated_requests,
        "selected_generation_cost_cache_aware_estimated_request_count": sum(
            integer(row["generation_cost"].get("estimated_cache_aware_requests")) for row in rows
        ),
        "selected_generation_cost_cache_price_fallback_request_count": sum(
            integer(row["generation_cost"].get("estimated_cache_price_fallback_requests"))
            for row in rows
        ),
        "selected_generation_cost_no_cache_estimated_request_count": sum(
            integer(row["generation_cost"].get("estimated_no_cache_requests")) for row in rows
        ),
        "selected_generation_cost_ignored_request_count": generation_ignored_requests,
        "selected_attempt_binding_valid_task_count": sum(
            row.get("selected_attempt_binding_valid") is True for row in rows
        ),
        "judge_cost_counted_usd": judge_total if judge_known else None,
        "avg_judge_cost_usd": judge_total / len(rows) if judge_known and rows else None,
        "judge_cost_complete": judge_complete,
        "judge_cost_is_lower_bound": bool(rows and not judge_complete),
        "judge_cost_request_count": sum(
            integer(row["judge_cost"].get("request_count")) for row in rows
        ),
        "judge_cost_actual_request_count": sum(
            integer(row["judge_cost"].get("actual_requests")) for row in rows
        ),
        "judge_cost_estimated_request_count": sum(
            integer(row["judge_cost"].get("estimated_requests")) for row in rows
        ),
        "judge_cost_ignored_request_count": sum(
            integer(row["judge_cost"].get("ignored_requests")) for row in rows
        ),
        "fallback_task_count": sum(bool(row.get("fallback")) for row in rows),
        "outer_retry_count": sum(integer(row.get("outer_retry")) for row in rows),
        "proposer_recovery_request_count": sum(
            integer(row.get("proposer_recovery")) for row in rows
        ),
        "partial_proposer_task_count": sum(
            integer(row.get("partial_proposers")) > 0 for row in rows
        ),
        "degraded_task_count": sum(bool(row.get("degraded")) for row in rows),
        "assembly_truncated_task_count": sum(bool(row.get("assembled_truncated")) for row in rows),
        "n_distribution": dict(sorted(Counter(str(integer(row.get("n"))) for row in rows).items())),
        "selected_proposer_sets": dict(
            sorted(
                Counter(
                    json.dumps(
                        row.get("selected_p") or [], ensure_ascii=False, separators=(",", ":")
                    )
                    for row in rows
                ).items()
            )
        ),
        "selected_aggregators": dict(
            sorted(Counter(str(row.get("selected_a") or "") for row in rows).items())
        ),
        "analyzer_sources": dict(
            sorted(Counter(str(row.get("analyzer_source") or "") for row in rows).items())
        ),
        "analyzer_origin_outcome_distribution": dict(
            sorted(
                Counter(
                    str(row.get("analyzer_origin_outcome") or ANALYZER_ORIGIN_UNKNOWN)
                    for row in rows
                ).items()
            )
        ),
        "analyzer_fallback_reason_distribution": dict(
            sorted(
                Counter(
                    str(row.get("analyzer_fallback_reason") or "")
                    for row in rows
                    if str(row.get("analyzer_fallback_reason") or "").strip()
                ).items()
            )
        ),
        "analyzer_fallback_task_ids": sorted(
            str(row.get("task_id") or "")
            for row in rows
            if row.get("analyzer_origin_is_fallback") is True
        ),
        "analyzer_unknown_origin_task_ids": sorted(
            str(row.get("task_id") or "")
            for row in rows
            if str(row.get("analyzer_origin_outcome") or ANALYZER_ORIGIN_UNKNOWN)
            == ANALYZER_ORIGIN_UNKNOWN
        ),
        "analyzer_output_tokens": [
            integer(row.get("analyzer_output_tokens"))
            for row in rows
            if integer(row.get("analyzer_output_tokens")) > 0
        ],
        "analyzer_max_output_token_values": dict(
            sorted(
                Counter(
                    str(integer(row.get("analyzer_max_output_tokens")))
                    for row in rows
                    if integer(row.get("analyzer_max_output_tokens")) > 0
                ).items()
            )
        ),
        "analyzer_length_stop_count": sum(bool(row.get("analyzer_length_stop")) for row in rows),
        "analyzer_truncated_count": sum(bool(row.get("analyzer_truncated")) for row in rows),
        "analyzer_at_or_above_cap_count": sum(
            bool(row.get("analyzer_at_or_above_cap")) for row in rows
        ),
        "analyzer_length_stop_task_ids": sorted(
            str(row.get("task_id") or "") for row in rows if row.get("analyzer_length_stop")
        ),
        "analyzer_truncated_task_ids": sorted(
            str(row.get("task_id") or "") for row in rows if row.get("analyzer_truncated")
        ),
        "analyzer_at_or_above_cap_task_ids": sorted(
            str(row.get("task_id") or "") for row in rows if row.get("analyzer_at_or_above_cap")
        ),
        "analyzer_stop_reasons": dict(
            sorted(
                Counter(
                    stop for row in rows for stop in row.get("analyzer_stop_reasons") or []
                ).items()
            )
        ),
        "row_policy_warning_count": sum(row.get("row_policy_pass") is False for row in rows),
        "explicit_byok_request_count": sum(
            integer(row.get("explicit_byok_requests")) for row in rows
        ),
        "selected_model_generation": dict(sorted(model_generation.items())),
        "failed_tasks": [
            {"task_id": row.get("task_id"), "error": row.get("error")}
            for row in rows
            if not row.get("done")
        ],
    }


def load_formal_arm(
    spec: ArmSpec,
    state: Mapping[str, Any],
    prices: Mapping[str, Price],
    plan: Mapping[str, Any],
    derived: Mapping[str, Any],
    verifier: FrozenControllerVerifier,
) -> dict[str, Any]:
    controller_arm = verifier.arms[spec.arm_id]
    root = Path(verifier.module.output_dir(plan, controller_arm)).resolve()
    declared_root = Path(str(state.get("output_dir") or ""))
    initial_reasons: list[str] = []
    identity_mismatch = False
    if not str(state.get("output_dir") or "") or declared_root.resolve() != root.resolve():
        initial_reasons.append("terminal status arm output_dir differs from frozen controller")
        identity_mismatch = True
    expected_identity: Mapping[str, Any] | None = None
    try:
        override = verifier.module.resolve_arm_override(
            plan,
            controller_arm,
            artifact=verifier.artifact,
            p99_receipt=(
                verifier.derived.get("p0_5_06") if isinstance(verifier.derived, Mapping) else None
            ),
        )
        expected_identity = verifier.module.arm_completion_identity(
            plan,
            controller_arm,
            snapshot=verifier.snapshot,
            snapshot_identity=verifier.snapshot_identity,
            override=override,
        )
    except Exception as exc:  # noqa: BLE001 - failed prerequisites are reportable
        initial_reasons.append("frozen controller could not resolve arm identity: " + str(exc))
    try:
        independently_complete, independent_evidence = verifier.module.inspect_complete_arm(
            root,
            expected_task_ids={
                str(value) for value in plan.get("benchmark", {}).get("task_ids") or []
            },
            expected_task_concurrency=int(plan.get("execution", {}).get("task_concurrency") or 0),
            expected_identity=expected_identity,
        )
    except Exception as exc:  # noqa: BLE001 - frozen verifier is authoritative
        independently_complete = False
        independent_evidence = {
            "reason": "frozen_controller_inspection_failed",
            "detail": str(exc),
        }
    declared_state = str(state.get("state") or "unknown")
    declared_evidence = state.get("completion_evidence")
    evidence_matches = isinstance(declared_evidence, Mapping) and dict(declared_evidence) == dict(
        independent_evidence
    )
    if isinstance(declared_evidence, Mapping) and not evidence_matches:
        initial_reasons.append(
            "terminal status completion evidence differs from frozen controller reinspection"
        )
        identity_mismatch = True
    if declared_state == "succeeded":
        if not independently_complete:
            initial_reasons.append(
                "terminal status labels arm succeeded but frozen controller does not"
            )
            identity_mismatch = True
        if not evidence_matches:
            initial_reasons.append(
                "succeeded arm lacks matching frozen controller completion evidence"
            )
            identity_mismatch = True
    elif independently_complete:
        initial_reasons.append(
            "frozen controller finds a complete arm under a non-succeeded status label"
        )
        identity_mismatch = True
    authenticated_state = "controller_identity_mismatch" if identity_mismatch else declared_state
    result: dict[str, Any] = {
        "spec": asdict(spec),
        "state": authenticated_state,
        "declared_state": declared_state,
        "output_dir": str(root),
        "completion_evidence": declared_evidence,
        "controller_reinspection": {
            "complete": bool(independently_complete),
            "evidence": independent_evidence,
            "terminal_evidence_matches": evidence_matches,
        },
        "failure": state.get("failure"),
        "formal": False,
        "formal_evidence_valid": False,
        "formal_evidence_reasons": initial_reasons,
        "rows": [],
        "metrics": summarize_rows([]),
        "manifest": {},
        "audit": {},
        "proof": {},
        "statuses": {"execution": "unknown", "policy": "unknown", "audit": "unknown"},
        "account": {},
        "candidate_order_seed_evidence": None,
    }
    if declared_state == "no_op_deleted" and not initial_reasons:
        result["no_op_receipt"] = state.get("offline_effect_receipt")
        return result
    if not independently_complete or declared_state != "succeeded":
        return result
    required = [
        root / name
        for name in (
            "manifest.json",
            "results.jsonl",
            "trace.jsonl",
            "audit.json",
            "openrouter-non-byok-campaign-proof.json",
        )
    ]
    if not all(path.is_file() and not path.is_symlink() for path in required):
        result["formal_evidence_reasons"].append("formal root artifacts are missing")
        return result
    manifest = load_json(root / "manifest.json")
    audit = load_json(root / "audit.json")
    proof = load_json(root / "openrouter-non-byok-campaign-proof.json")
    reasons: list[str] = list(initial_reasons)
    account_window_cohort, cohort_reasons = validate_account_window_cohort(
        manifest,
        audit,
        proof,
    )
    reasons.extend(cohort_reasons)
    if not validate_embedded_hash(manifest, "manifest_sha256"):
        reasons.append("manifest self-hash differs")
    if not validate_embedded_hash(audit, "audit_sha256"):
        reasons.append("audit self-hash differs")
    if not validate_embedded_hash(proof, "proof_sha256"):
        reasons.append("non-BYOK proof self-hash differs")
    for name in (
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    ):
        valid, detail = artifact_binding_valid(root, manifest, name)
        if not valid:
            reasons.append(detail)
    try:
        rows, row_reasons = read_compact_rows(root / "results.jsonl", prices)
    except ReportError as exc:
        rows, row_reasons = [], [str(exc)]
    reasons.extend(row_reasons)
    try:
        trace_bindings, trace_candidate_calls, trace_reasons = read_trace_evidence(
            root / "trace.jsonl"
        )
    except ReportError as exc:
        trace_bindings, trace_candidate_calls, trace_reasons = {}, {}, [str(exc)]
    reasons.extend(trace_reasons)
    task_ids = [str(row.get("task_id") or "") for row in rows]
    expected_task_ids = [str(value) for value in plan.get("benchmark", {}).get("task_ids") or []]
    if len(rows) != 10:
        reasons.append(f"results row count is {len(rows)}, expected 10")
    if len(task_ids) != len(set(task_ids)):
        reasons.append("result task ids are not unique")
    if expected_task_ids and set(task_ids) != set(expected_task_ids):
        reasons.append("result task ids differ from frozen benchmark")
    if {row.get("group") for row in rows} != {"G1"}:
        reasons.append("results are not exactly G1")
    invalid_selected_bindings = [
        str(row.get("task_id") or "")
        for row in rows
        if row.get("selected_attempt_binding_valid") is not True
    ]
    if invalid_selected_bindings:
        reasons.append(
            "selected generation attempt usage binding invalid for tasks: "
            + ",".join(sorted(invalid_selected_bindings))
        )
    selected_ids = [str(row.get("selected_attempt_id") or "") for row in rows]
    if len(selected_ids) != len(set(selected_ids)):
        reasons.append("selected generation attempt ids are not unique by task")
    expected_selected_bindings = {
        f"G1/{row.get('task_id')}": row.get("selected_attempt_id") for row in rows
    }
    if manifest.get("selected_generation_attempt_bindings") != expected_selected_bindings:
        reasons.append("manifest/result selected generation attempt bindings differ")
    result_bindings = {
        str(row.get("task_id") or ""): str(row.get("result_evidence_sha256") or "") for row in rows
    }
    if trace_bindings != result_bindings:
        reasons.append("trace/result evidence bindings differ")
    seed_evidence = p0_5_36_seed_evidence(
        spec,
        trace_candidate_calls,
        expected_task_ids=expected_task_ids,
    )
    if manifest.get("schema") != "opensquilla.draco.campaign-final-manifest/v1":
        reasons.append("manifest schema differs")
    if manifest.get("status") != "complete":
        reasons.append("manifest status is not complete")
    if manifest.get("execution_pass") is not True:
        reasons.append("manifest execution_pass is not true")
    if manifest.get("result_count") != 10 or manifest.get("task_count") != 10:
        reasons.append("manifest result/task counts differ from 10")
    if manifest.get("groups") != ["G1"]:
        reasons.append("manifest groups differ from [G1]")
    source_manifests = manifest.get("source_manifests")
    scheduling_ok = bool(source_manifests) and all(
        isinstance(source, Mapping)
        and isinstance(source.get("execution_scheduling"), Mapping)
        and source["execution_scheduling"].get("task_concurrency") == 6
        for source in source_manifests or []
    )
    if not scheduling_ok:
        reasons.append("source manifest task_concurrency is not uniformly 6")
    audit_binding = str(manifest.get("audit_sha256") or "")
    if audit_binding != audit.get("audit_sha256"):
        reasons.append("manifest/audit hash binding differs")
    proof_binding = str(manifest.get("openrouter_non_byok_campaign_proof_sha256") or "")
    if proof_binding != proof.get("proof_sha256"):
        reasons.append("manifest/non-BYOK proof hash binding differs")
    execution_status = status_dimension(
        [manifest.get("execution_pass"), audit.get("execution_pass"), proof.get("execution_pass")],
        warning_status="fail",
    )
    policy_status = status_dimension(
        [manifest.get("policy_pass"), audit.get("policy_pass"), proof.get("policy_pass")],
        warning_status="warning",
    )
    audit_status = (
        "pass"
        if manifest.get("audit_pass") is True and audit.get("pass") is True
        else "warning"
        if execution_status == "pass"
        else "fail"
    )
    result.update(
        {
            "formal": True,
            "formal_evidence_valid": not reasons,
            "formal_evidence_reasons": reasons,
            "rows": rows,
            "metrics": summarize_rows(rows),
            "manifest": {
                "schema": manifest.get("schema"),
                "status": manifest.get("status"),
                "execution_pass": manifest.get("execution_pass"),
                "policy_pass": manifest.get("policy_pass"),
                "audit_pass": manifest.get("audit_pass"),
                "manifest_sha256": manifest.get("manifest_sha256"),
                "warnings": manifest.get("warnings") or [],
                **(
                    {"account_window_cohort": account_window_cohort}
                    if account_window_cohort is not None
                    else {}
                ),
            },
            "audit": {
                "schema": audit.get("schema"),
                "status": audit.get("status"),
                "pass": audit.get("pass"),
                "execution_pass": audit.get("execution_pass"),
                "policy_pass": audit.get("policy_pass"),
                "audit_sha256": audit.get("audit_sha256"),
                "warnings": audit.get("warnings") or [],
            },
            "proof": {
                "schema": proof.get("schema"),
                "status": proof.get("status"),
                "pass": proof.get("pass"),
                "execution_pass": proof.get("execution_pass"),
                "policy_pass": proof.get("policy_pass"),
                "proof_sha256": proof.get("proof_sha256"),
                "warnings": proof.get("warnings") or [],
            },
            "statuses": {
                "execution": execution_status,
                "policy": policy_status,
                "audit": audit_status,
            },
            "account": account_evidence(
                manifest,
                proof,
                cohort=account_window_cohort,
            ),
            "candidate_order_seed_evidence": seed_evidence,
        }
    )
    return result


def load_confirmatory_formal_role(
    *,
    role: str,
    receipt: Mapping[str, Any],
    spec: ArmSpec,
    root: Path,
    prices: Mapping[str, Price],
    plan: Mapping[str, Any],
    cohort_id: str,
    expected_cohort_sha256: str,
) -> dict[str, Any]:
    """Load one receipt-authenticated role without legacy controller assumptions."""

    reasons: list[str] = []
    result: dict[str, Any] = {
        "spec": asdict(spec),
        "state": "formal_evidence_invalid",
        "declared_state": str(receipt.get("publication_status") or "unknown"),
        "output_dir": str(root),
        "completion_evidence": {
            "source": CONFIRMATORY_REPORT_INPUT_INDEX_NAME,
            "role": role,
            "manifest_sha256": receipt.get("manifest_sha256"),
        },
        "controller_reinspection": {
            "complete": False,
            "evidence": {
                "source": "confirmatory_receipt_and_cohort_manifest",
                "cohort_id": cohort_id,
                "role": role,
            },
            "terminal_evidence_matches": False,
        },
        "failure": None,
        "formal": False,
        "formal_evidence_valid": False,
        "formal_evidence_reasons": reasons,
        "rows": [],
        "metrics": summarize_rows([]),
        "manifest": {},
        "audit": {},
        "proof": {},
        "statuses": {"execution": "unknown", "policy": "unknown", "audit": "unknown"},
        "account": {},
        "candidate_order_seed_evidence": None,
        "confirmatory_role": role,
        "confirmatory_cohort_id": cohort_id,
    }
    if receipt.get("publication_status") != "complete":
        reasons.append(f"confirmatory {role} publication_status is not complete")
        return result
    expected_manifest_sha = raw_sha256(receipt.get("manifest_sha256"))
    if expected_manifest_sha is None:
        reasons.append(f"confirmatory {role} manifest hash is invalid")
        return result
    try:
        regular_directory(root)
    except ReportError as exc:
        reasons.append(str(exc))
        return result
    required_names = (
        "manifest.json",
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    )
    required = [root / name for name in required_names]
    try:
        for path in required:
            regular_file(path)
    except ReportError as exc:
        reasons.append(str(exc))
        return result
    if file_sha256(root / "manifest.json") != expected_manifest_sha:
        reasons.append(f"confirmatory {role} manifest file hash differs from receipt")
        return result

    manifest = load_json(root / "manifest.json")
    audit = load_json(root / "audit.json")
    proof = load_json(root / "openrouter-non-byok-campaign-proof.json")
    account_window_cohort, cohort_reasons = validate_account_window_cohort(
        manifest,
        audit,
        proof,
    )
    reasons.extend(cohort_reasons)
    if account_window_cohort is None:
        reasons.append("confirmatory formal artifacts lack account-window cohort evidence")
    else:
        if account_window_cohort.get("cohort_id") != cohort_id:
            reasons.append("confirmatory formal cohort id differs from receipt")
        if account_window_cohort.get("role") != role:
            reasons.append("confirmatory formal cohort role differs from receipt")
        if account_window_cohort.get("cohort_sha256") != expected_cohort_sha256:
            reasons.append("confirmatory formal cohort hash differs from receipt")
    if not validate_embedded_hash(manifest, "manifest_sha256"):
        reasons.append("manifest self-hash differs")
    if not validate_embedded_hash(audit, "audit_sha256"):
        reasons.append("audit self-hash differs")
    if not validate_embedded_hash(proof, "proof_sha256"):
        reasons.append("non-BYOK proof self-hash differs")
    for name in (
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    ):
        valid, detail = artifact_binding_valid(root, manifest, name)
        if not valid:
            reasons.append(detail)
    try:
        rows, row_reasons = read_compact_rows(root / "results.jsonl", prices)
    except ReportError as exc:
        rows, row_reasons = [], [str(exc)]
    reasons.extend(row_reasons)
    try:
        trace_bindings, trace_candidate_calls, trace_reasons = read_trace_evidence(
            root / "trace.jsonl"
        )
    except ReportError as exc:
        trace_bindings, trace_candidate_calls, trace_reasons = {}, {}, [str(exc)]
    reasons.extend(trace_reasons)
    task_ids = [str(row.get("task_id") or "") for row in rows]
    expected_task_ids = [
        str(value) for value in plan.get("benchmark", {}).get("task_ids") or []
    ]
    if len(rows) != 10:
        reasons.append(f"results row count is {len(rows)}, expected 10")
    if len(task_ids) != len(set(task_ids)):
        reasons.append("result task ids are not unique")
    if expected_task_ids and set(task_ids) != set(expected_task_ids):
        reasons.append("result task ids differ from frozen benchmark")
    if {row.get("group") for row in rows} != {"G1"}:
        reasons.append("results are not exactly G1")
    invalid_selected_bindings = [
        str(row.get("task_id") or "")
        for row in rows
        if row.get("selected_attempt_binding_valid") is not True
    ]
    if invalid_selected_bindings:
        reasons.append(
            "selected generation attempt usage binding invalid for tasks: "
            + ",".join(sorted(invalid_selected_bindings))
        )
    selected_ids = [str(row.get("selected_attempt_id") or "") for row in rows]
    if len(selected_ids) != len(set(selected_ids)):
        reasons.append("selected generation attempt ids are not unique by task")
    expected_selected_bindings = {
        f"G1/{row.get('task_id')}": row.get("selected_attempt_id") for row in rows
    }
    if manifest.get("selected_generation_attempt_bindings") != expected_selected_bindings:
        reasons.append("manifest/result selected generation attempt bindings differ")
    result_bindings = {
        str(row.get("task_id") or ""): str(row.get("result_evidence_sha256") or "")
        for row in rows
    }
    if trace_bindings != result_bindings:
        reasons.append("trace/result evidence bindings differ")
    seed_evidence = p0_5_36_seed_evidence(
        spec,
        trace_candidate_calls,
        expected_task_ids=expected_task_ids,
    )
    if manifest.get("schema") != "opensquilla.draco.campaign-final-manifest/v1":
        reasons.append("manifest schema differs")
    if manifest.get("status") != "complete":
        reasons.append("manifest status is not complete")
    if manifest.get("execution_pass") is not True:
        reasons.append("manifest execution_pass is not true")
    if manifest.get("result_count") != 10 or manifest.get("task_count") != 10:
        reasons.append("manifest result/task counts differ from 10")
    if manifest.get("groups") != ["G1"]:
        reasons.append("manifest groups differ from [G1]")
    source_manifests = manifest.get("source_manifests")
    if not isinstance(source_manifests, list) or not source_manifests or any(
        not isinstance(source, Mapping) for source in source_manifests
    ):
        reasons.append("confirmatory source manifest set is missing or malformed")
    else:
        archive_root = root / "archive"
        seen_archived_sources: set[Path] = set()
        for source_index, source in enumerate(source_manifests):
            for path_field, hash_field, label in (
                ("path", "sha256", "manifest"),
                ("result_path", "result_sha256", "result"),
            ):
                try:
                    source_path = absolute_receipt_path(
                        source.get(path_field),
                        label=f"confirmatory source {source_index} {label}",
                    )
                    source_path.relative_to(archive_root)
                    regular_file(source_path)
                except (ReportError, ValueError) as exc:
                    reasons.append(str(exc))
                    continue
                expected_source_sha = raw_sha256(source.get(hash_field))
                if (
                    expected_source_sha is None
                    or file_sha256(source_path) != expected_source_sha
                    or source_path in seen_archived_sources
                ):
                    reasons.append(
                        f"confirmatory source {source_index} {label} hash/path differs"
                    )
                seen_archived_sources.add(source_path)
    audit_binding = str(manifest.get("audit_sha256") or "")
    if audit_binding != audit.get("audit_sha256"):
        reasons.append("manifest/audit hash binding differs")
    proof_binding = str(manifest.get("openrouter_non_byok_campaign_proof_sha256") or "")
    if proof_binding != proof.get("proof_sha256"):
        reasons.append("manifest/non-BYOK proof hash binding differs")
    execution_status = status_dimension(
        [manifest.get("execution_pass"), audit.get("execution_pass"), proof.get("execution_pass")],
        warning_status="fail",
    )
    policy_status = status_dimension(
        [manifest.get("policy_pass"), audit.get("policy_pass"), proof.get("policy_pass")],
        warning_status="warning",
    )
    audit_status = (
        "pass"
        if manifest.get("audit_pass") is True and audit.get("pass") is True
        else "warning"
        if execution_status == "pass"
        else "fail"
    )
    result.update(
        {
            "state": "succeeded" if not reasons else "formal_evidence_invalid",
            "controller_reinspection": {
                "complete": not reasons,
                "evidence": {
                    "source": "confirmatory_receipt_and_cohort_manifest",
                    "cohort_id": cohort_id,
                    "role": role,
                    "manifest_sha256": expected_manifest_sha,
                },
                "terminal_evidence_matches": not reasons,
            },
            "formal": True,
            "formal_evidence_valid": not reasons,
            "formal_evidence_reasons": list(dict.fromkeys(reasons)),
            "rows": rows,
            "metrics": summarize_rows(rows),
            "manifest": {
                "schema": manifest.get("schema"),
                "status": manifest.get("status"),
                "execution_pass": manifest.get("execution_pass"),
                "policy_pass": manifest.get("policy_pass"),
                "audit_pass": manifest.get("audit_pass"),
                "manifest_sha256": manifest.get("manifest_sha256"),
                "warnings": manifest.get("warnings") or [],
                **(
                    {"account_window_cohort": account_window_cohort}
                    if account_window_cohort is not None
                    else {}
                ),
            },
            "audit": {
                "schema": audit.get("schema"),
                "status": audit.get("status"),
                "pass": audit.get("pass"),
                "execution_pass": audit.get("execution_pass"),
                "policy_pass": audit.get("policy_pass"),
                "audit_sha256": audit.get("audit_sha256"),
                "warnings": audit.get("warnings") or [],
            },
            "proof": {
                "schema": proof.get("schema"),
                "status": proof.get("status"),
                "pass": proof.get("pass"),
                "execution_pass": proof.get("execution_pass"),
                "policy_pass": proof.get("policy_pass"),
                "proof_sha256": proof.get("proof_sha256"),
                "warnings": proof.get("warnings") or [],
            },
            "statuses": {
                "execution": execution_status,
                "policy": policy_status,
                "audit": audit_status,
            },
            "account": account_evidence(
                manifest,
                proof,
                cohort=account_window_cohort,
            ),
            "candidate_order_seed_evidence": seed_evidence,
        }
    )
    return result


def bootstrap_ci(deltas: Sequence[float]) -> tuple[float | None, float | None]:
    if not deltas:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(deltas)
    samples = [
        sum(deltas[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    return percentile(samples, 0.025), percentile(samples, 0.975)


def paired(
    control: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    scope: str = "all_tasks",
    allowed_task_ids: set[str] | None = None,
) -> dict[str, Any]:
    control_mode = str(control.get("spec", {}).get("analyzer_mode") or "")
    variant_mode = str(variant.get("spec", {}).get("analyzer_mode") or "")
    if not control_mode or control_mode != variant_mode:
        raise ReportError(
            "paired comparison requires identical non-empty analyzer_mode: "
            f"{control_mode!r} != {variant_mode!r}"
        )
    control_rows = {row["task_id"]: row for row in control.get("rows") or []}
    variant_rows = {row["task_id"]: row for row in variant.get("rows") or []}
    common_ids = sorted(set(control_rows) & set(variant_rows))
    task_rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    paired_ids: list[str] = []
    for task_id in common_ids:
        left, right = control_rows[task_id], variant_rows[task_id]
        if allowed_task_ids is not None and task_id not in allowed_task_ids:
            continue
        if "ap_non_overlap" in scope and (left.get("ap_overlap") or right.get("ap_overlap")):
            continue
        before, after = number(left.get("quality")), number(right.get("quality"))
        if before is None or after is None:
            continue
        delta = after - before
        deltas.append(delta)
        paired_ids.append(task_id)
        task_rows.append(
            {
                "task_id": task_id,
                "domain": right.get("domain") or left.get("domain"),
                "control_quality": before,
                "variant_quality": after,
                "delta_quality": delta,
            }
        )
    ci_low, ci_high = bootstrap_ci(deltas)
    route_ids = paired_ids
    return {
        "control_arm_id": control.get("spec", {}).get("arm_id"),
        "variant_arm_id": variant.get("spec", {}).get("arm_id"),
        "scope": scope,
        "analyzer_mode": control_mode,
        "same_analyzer_mode": True,
        "pair_count": len(deltas),
        "complete_task_id_pairing": len(deltas) == 10,
        "missing_from_control": sorted(set(variant_rows) - set(control_rows)),
        "missing_from_variant": sorted(set(control_rows) - set(variant_rows)),
        "mean_delta_quality": mean(deltas),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_ci95": [ci_low, ci_high],
        "wins": sum(delta > 1e-12 for delta in deltas),
        "ties": sum(abs(delta) <= 1e-12 for delta in deltas),
        "losses": sum(delta < -1e-12 for delta in deltas),
        "request_context_match_count": sum(
            bool(control_rows[task_id].get("request_context_hash"))
            and control_rows[task_id].get("request_context_hash")
            == variant_rows[task_id].get("request_context_hash")
            for task_id in route_ids
        ),
        "task_profile_match_count": sum(
            bool(control_rows[task_id].get("task_profile_hash"))
            and control_rows[task_id].get("task_profile_hash")
            == variant_rows[task_id].get("task_profile_hash")
            for task_id in route_ids
        ),
        "proposer_changed_count": sum(
            control_rows[task_id].get("selected_p") != variant_rows[task_id].get("selected_p")
            for task_id in route_ids
        ),
        "aggregator_changed_count": sum(
            control_rows[task_id].get("selected_a") != variant_rows[task_id].get("selected_a")
            for task_id in route_ids
        ),
        "n_changed_count": sum(
            control_rows[task_id].get("n") != variant_rows[task_id].get("n")
            for task_id in route_ids
        ),
        "task_rows": task_rows,
    }


def repeated_pairing(comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    usable = [
        comparison for comparison in comparisons if isinstance(comparison.get("task_rows"), list)
    ]
    if len(usable) < 2:
        return None
    by_task: dict[str, list[float]] = {}
    for comparison in usable:
        for row in comparison.get("task_rows") or []:
            by_task.setdefault(str(row["task_id"]), []).append(float(row["delta_quality"]))
    expected_repeats = len(usable)
    averaged = {
        task_id: sum(values) / len(values)
        for task_id, values in by_task.items()
        if len(values) == expected_repeats
    }
    deltas = [averaged[task_id] for task_id in sorted(averaged)]
    low, high = bootstrap_ci(deltas)
    return {
        "kind": "repeat_task_mean_pairing",
        "replicate_count": expected_repeats,
        "task_count": len(deltas),
        "complete_task_id_pairing": len(deltas) == 10,
        "mean_delta_quality": mean(deltas),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_ci95": [low, high],
        "wins": sum(delta > 1e-12 for delta in deltas),
        "ties": sum(abs(delta) <= 1e-12 for delta in deltas),
        "losses": sum(delta < -1e-12 for delta in deltas),
        "per_task_mean_delta": averaged,
    }


def replay_control_drift(
    plan: Mapping[str, Any],
    specs: Sequence[ArmSpec],
    arms: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    controls = comparison_control_contract(plan, specs)
    replay_ids = list(controls["replay_control_arm_ids"])
    comparisons: list[dict[str, Any]] = []
    missing: list[str] = []
    for left_id, right_id in zip(replay_ids, replay_ids[1:], strict=False):
        left, right = arms.get(left_id), arms.get(right_id)
        if (
            not isinstance(left, Mapping)
            or not isinstance(right, Mapping)
            or left.get("formal_evidence_valid") is not True
            or right.get("formal_evidence_valid") is not True
        ):
            missing.append(f"{right_id}-{left_id}")
            continue
        comparisons.append(paired(left, right, scope="replay_control_temporal_drift"))
    return {
        "source_arm_id": controls["source_arm_id"],
        "replay_control_arm_ids": replay_ids,
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
        "missing_comparisons": missing,
        "note": (
            "Replay-control deltas diagnose provider/time drift under the same frozen Analyzer "
            "mode. They do not convert the anchored-serial schedule into strict per-task AB/BA."
        ),
    }


def receipt_hash_valid(receipt: Mapping[str, Any]) -> bool:
    recorded = str(receipt.get("receipt_sha256") or "").removeprefix("sha256:")
    payload = {
        key: value for key, value in receipt.items() if key not in {"receipt_sha256", "path"}
    }
    return bool(recorded) and recorded == canonical_sha256(payload)


def parse_temperature_wire_receipt(
    receipt: Mapping[str, Any],
    *,
    task_ids: set[str],
    arm_ids: set[str],
) -> dict[str, Any]:
    """Extract exact per-task/member wire facts without inferring from config."""

    records: list[dict[str, Any]] = []
    receipt_arms = [str(value) for value in receipt.get("arm_ids") or [] if str(value) in arm_ids]

    # Current controller receipts expose this exact production-compatibility
    # projection.  Parse it directly so ``role`` and every selected/recovery
    # member remain distinguishable.  The recursive fallback below only
    # supports older authenticated receipts.
    temperature_scope = receipt.get("temperature_analysis_scope")
    tasks = (
        temperature_scope.get("tasks")
        if isinstance(temperature_scope, Mapping)
        and isinstance(temperature_scope.get("tasks"), list)
        else receipt.get("tasks")
        if isinstance(receipt.get("tasks"), list)
        else None
    )
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, Mapping):
                records.append(
                    {
                        "task_id": None,
                        "arm_id": None,
                        "model": None,
                        "role": None,
                        "temperature_parameter_sent": None,
                        "wire_temperature": None,
                        "state": "unknown",
                    }
                )
                continue
            task_id = str(task.get("task_id") or "") or None
            members = task.get("members")
            if not isinstance(members, list):
                members = []
            for member in members:
                if not isinstance(member, Mapping):
                    continue
                sent = member.get("temperature_parameter_sent")
                wire_temperature = number(member.get("wire_temperature"))
                if sent is True and wire_temperature is not None:
                    state = "sent"
                elif sent is False and member.get("wire_temperature") is None:
                    state = "omitted"
                else:
                    state = "unknown"
                targets = receipt_arms or [None]
                for arm_id in targets:
                    records.append(
                        {
                            "task_id": task_id,
                            "arm_id": arm_id,
                            "model": normalize_model(
                                member.get("requested_model")
                                or member.get("model")
                                or member.get("identity")
                            )
                            or None,
                            "role": str(member.get("role") or "") or None,
                            "temperature_parameter_sent": (
                                sent if isinstance(sent, bool) else None
                            ),
                            "wire_temperature": wire_temperature,
                            "state": state,
                        }
                    )

    def walk(
        value: Any,
        *,
        task_id: str | None = None,
        arm_id: str | None = None,
        model: str | None = None,
    ) -> None:
        if isinstance(value, Mapping):
            local_task = str(value.get("task_id") or task_id or "") or None
            local_arm = str(value.get("arm_id") or arm_id or "") or None
            local_model = (
                normalize_model(
                    value.get("requested_model")
                    or value.get("model")
                    or value.get("identity")
                    or model
                )
                or None
            )
            if "temperature_parameter_sent" in value:
                sent = value.get("temperature_parameter_sent")
                wire_temperature = number(value.get("wire_temperature"))
                if sent is True and wire_temperature is not None:
                    state = "sent"
                elif sent is False and value.get("wire_temperature") is None:
                    state = "omitted"
                else:
                    state = "unknown"
                records.append(
                    {
                        "task_id": local_task,
                        "arm_id": local_arm,
                        "model": local_model,
                        "role": value.get("role"),
                        "recovery": value.get("recovery") or value.get("kind") == "recovery",
                        "temperature_parameter_sent": sent if isinstance(sent, bool) else None,
                        "wire_temperature": wire_temperature,
                        "state": state,
                    }
                )
            for key, child in value.items():
                if key in {"temperature_parameter_sent", "wire_temperature"}:
                    continue
                key_text = str(key)
                next_task = key_text if key_text in task_ids else local_task
                next_arm = key_text if key_text in arm_ids else local_arm
                next_model = normalize_model(key_text) if "/" in key_text else local_model
                walk(
                    child,
                    task_id=next_task,
                    arm_id=next_arm,
                    model=next_model,
                )
        elif isinstance(value, list):
            for child in value:
                walk(child, task_id=task_id, arm_id=arm_id, model=model)

    if not isinstance(tasks, list):
        walk(receipt)
    by_arm_task_model: dict[str, dict[str, dict[str, str]]] = {}
    by_arm_task_members: dict[str, dict[str, list[dict[str, Any]]]] = {}
    model_states: dict[str, Counter[str]] = {}
    unresolved_records = 0
    for record in records:
        arm_id = str(record.get("arm_id") or "")
        task_id = str(record.get("task_id") or "")
        model = normalize_model(record.get("model"))
        state = str(record.get("state") or "unknown")
        if (
            not arm_id
            or arm_id not in arm_ids
            or not task_id
            or task_id not in task_ids
            or not model
        ):
            unresolved_records += 1
            continue
        normalized_record = {
            **record,
            "arm_id": arm_id,
            "task_id": task_id,
            "model": model,
        }
        by_arm_task_members.setdefault(arm_id, {}).setdefault(task_id, []).append(normalized_record)
        prior = by_arm_task_model.setdefault(arm_id, {}).setdefault(task_id, {}).get(model)
        if prior is None or prior == state:
            resolved = state
        else:
            resolved = "unknown"
        by_arm_task_model[arm_id][task_id][model] = resolved
        model_states.setdefault(model, Counter())[state] += 1
    return {
        "record_count": len(records),
        "unresolved_record_count": unresolved_records,
        "by_arm_task_model": by_arm_task_model,
        "by_arm_task_members": by_arm_task_members,
        "model_state_counts": {
            model: dict(sorted(counter.items())) for model, counter in sorted(model_states.items())
        },
        "scope_verifiable": bool(records) and unresolved_records == 0,
    }


def offline_receipt_change_slice(receipt: Mapping[str, Any]) -> dict[str, Any]:
    comparisons = receipt.get("comparison_by_proposer_cap_explicitness")
    by_context: dict[str, list[str]] = {}
    count_mismatches: list[str] = []
    if isinstance(comparisons, Mapping):
        for context, comparison in comparisons.items():
            if not isinstance(comparison, Mapping):
                continue
            task_ids = sorted(
                {
                    str(item.get("task_id"))
                    for item in comparison.get("changed_tasks") or []
                    if isinstance(item, Mapping) and item.get("task_id")
                }
            )
            by_context[str(context)] = task_ids
            if integer(comparison.get("changed_task_count")) != len(task_ids):
                count_mismatches.append(str(context))
    effective = sorted({task_id for values in by_context.values() for task_id in values})
    return {
        "changed_task_ids_by_context": dict(sorted(by_context.items())),
        "effective_changed_task_ids": effective,
        "effective_changed_task_count": len(effective),
        "declared_count_mismatch_contexts": sorted(count_mismatches),
    }


def load_offline_effect_receipts(
    derived: Mapping[str, Any],
    *,
    specs: Sequence[ArmSpec],
    task_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any] | None]:
    """Load and bind every controller ``offline_effect`` receipt."""

    raw_inventory = derived.get("offline_effect")
    if not isinstance(raw_inventory, Mapping):
        return {}, ["derived plan lacks offline_effect receipt inventory"], None
    specs_by_id = {spec.arm_id: spec for spec in specs}
    expected_ids = {spec.arm_id for spec in specs if spec.analyzer_mode == "frozen_replay"}
    actual_ids = {str(value) for value in raw_inventory}
    reasons: list[str] = []
    if actual_ids != expected_ids:
        reasons.append(
            "offline-effect receipt inventory differs: "
            f"missing={sorted(expected_ids - actual_ids)}, "
            f"extra={sorted(actual_ids - expected_ids)}"
        )
    unique = (
        derived.get("offline_unique_overlays")
        if isinstance(derived.get("offline_unique_overlays"), Mapping)
        else {}
    )
    loaded_by_path: dict[str, Mapping[str, Any]] = {}
    inventory: dict[str, dict[str, Any]] = {}
    temperature_scopes: dict[str, dict[str, Any]] = {}
    for arm_id, raw in raw_inventory.items():
        arm_id = str(arm_id)
        if not isinstance(raw, Mapping):
            reasons.append(f"offline-effect descriptor is not an object: {arm_id}")
            continue
        receipt_path_text = str(raw.get("receipt_path") or raw.get("path") or "")
        receipt: Mapping[str, Any] | None = None
        if not receipt_path_text:
            reasons.append(f"offline-effect receipt path missing: {arm_id}")
        elif receipt_path_text in loaded_by_path:
            receipt = loaded_by_path[receipt_path_text]
        else:
            receipt_path = Path(receipt_path_text)
            try:
                receipt = load_json(receipt_path)
            except ReportError as exc:
                reasons.append(f"offline-effect receipt unavailable for {arm_id}: {exc}")
            else:
                loaded_by_path[receipt_path_text] = receipt
        descriptor_hash = str(raw.get("receipt_sha256") or "").removeprefix("sha256:")
        overlay_sha = str(raw.get("overlay_sha256") or "").removeprefix("sha256:")
        valid = receipt is not None
        if receipt is not None:
            receipt_hash = str(receipt.get("receipt_sha256") or "").removeprefix("sha256:")
            if not receipt_hash_valid(receipt):
                reasons.append(f"offline-effect receipt self-hash differs: {arm_id}")
                valid = False
            if not descriptor_hash or descriptor_hash != receipt_hash:
                reasons.append(f"offline-effect descriptor/receipt hash differs: {arm_id}")
                valid = False
            if str(raw.get("decision") or "") != str(receipt.get("decision") or ""):
                reasons.append(f"offline-effect descriptor/receipt decision differs: {arm_id}")
                valid = False
            receipt_arms = {str(value) for value in receipt.get("arm_ids") or []}
            if arm_id not in receipt_arms:
                reasons.append(f"offline-effect receipt arm binding differs: {arm_id}")
                valid = False
            spec = specs_by_id.get(arm_id)
            receipt_experiments = {str(value) for value in receipt.get("experiment_ids") or []}
            if spec is None or spec.experiment_id not in receipt_experiments:
                reasons.append(f"offline-effect receipt experiment binding differs: {arm_id}")
                valid = False
            if overlay_sha != str(receipt.get("overlay_sha256") or "").removeprefix("sha256:"):
                reasons.append(f"offline-effect overlay binding differs: {arm_id}")
                valid = False
            embedded = unique.get(overlay_sha)
            if not isinstance(embedded, Mapping):
                reasons.append(f"offline unique-overlay binding missing: {arm_id}")
                valid = False
            elif str(embedded.get("receipt_sha256") or "").removeprefix(
                "sha256:"
            ) != receipt_hash or not receipt_hash_valid(embedded):
                reasons.append(f"offline unique-overlay receipt binding differs: {arm_id}")
                valid = False
        change_slice = offline_receipt_change_slice(receipt or {})
        if change_slice["declared_count_mismatch_contexts"]:
            reasons.append(f"offline-effect changed-task count differs: {arm_id}")
            valid = False
        normalized = {
            "valid": valid,
            "path": receipt_path_text or None,
            "decision": raw.get("decision"),
            "overlay_sha256": raw.get("overlay_sha256"),
            "receipt_sha256": raw.get("receipt_sha256"),
            **change_slice,
        }
        inventory[arm_id] = normalized
        spec = specs_by_id.get(arm_id)
        if receipt is not None and spec is not None and spec.experiment_id == "P0.5-11":
            scope = parse_temperature_wire_receipt(
                receipt,
                task_ids=task_ids,
                arm_ids=set(specs_by_id),
            )
            normalized["temperature_scope_verifiable"] = scope.get("scope_verifiable")
            temperature_scopes.setdefault(descriptor_hash, scope)
            if scope.get("scope_verifiable") is not True:
                reasons.append(
                    f"P0.5-11 temperature wire scope contains missing/unknown identity: {arm_id}"
                )
                normalized["valid"] = False

    merged_temperature: dict[str, Any] | None = None
    if temperature_scopes:
        merged_members: dict[str, dict[str, list[dict[str, Any]]]] = {}
        merged_models: dict[str, dict[str, dict[str, str]]] = {}
        model_states: dict[str, Counter[str]] = {}
        record_count = 0
        unresolved = 0
        for scope in temperature_scopes.values():
            record_count += integer(scope.get("record_count"))
            unresolved += integer(scope.get("unresolved_record_count"))
            for arm_id, tasks_by_id in (scope.get("by_arm_task_members") or {}).items():
                for task_id, members in tasks_by_id.items():
                    target = merged_members.setdefault(str(arm_id), {}).setdefault(str(task_id), [])
                    for member in members:
                        if member not in target:
                            target.append(member)
            for arm_id, tasks_by_id in (scope.get("by_arm_task_model") or {}).items():
                for task_id, models in tasks_by_id.items():
                    target = merged_models.setdefault(str(arm_id), {}).setdefault(str(task_id), {})
                    for model, state in models.items():
                        prior = target.get(str(model))
                        target[str(model)] = (
                            str(state) if prior in {None, str(state)} else "unknown"
                        )
                        model_states.setdefault(str(model), Counter())[str(state)] += 1
        merged_temperature = {
            "record_count": record_count,
            "unresolved_record_count": unresolved,
            "by_arm_task_members": merged_members,
            "by_arm_task_model": merged_models,
            "model_state_counts": {
                model: dict(sorted(states.items()))
                for model, states in sorted(model_states.items())
            },
            "scope_verifiable": unresolved == 0 and bool(record_count),
            "receipt_sha256s": sorted(
                {
                    str(inventory[arm_id].get("receipt_sha256") or "")
                    for arm_id in inventory
                    if specs_by_id.get(arm_id) and specs_by_id[arm_id].experiment_id == "P0.5-11"
                }
            ),
        }
    return inventory, reasons, merged_temperature


def load_derived_evidence(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    verifier: FrozenControllerVerifier,
) -> dict[str, Any]:
    run_root = Path(str(plan["paths"]["run_root"]))
    descriptor = (
        status.get("derived_plan") if isinstance(status.get("derived_plan"), Mapping) else {}
    )
    path = run_root / "derived-plan.json"
    result: dict[str, Any] = {
        "available": False,
        "valid": False,
        "path": str(path),
        "reasons": [],
        "p0_5_06": None,
        "p0_5_07": None,
        "p0_5_11": None,
        "p0_5_11_scope": None,
        "offline_effect": {},
        "frozen_analyzer_artifact": None,
    }
    declared_path = str(descriptor.get("path") or "")
    if declared_path and Path(declared_path).resolve() != path.resolve():
        result["reasons"].append("status derived plan path differs from frozen controller location")
    if not path.is_file() or path.is_symlink():
        result["reasons"].append("derived-plan.json is unavailable")
        return result
    derived = load_json(path)
    result["available"] = True
    if verifier.derived_error is not None:
        result["reasons"].append(
            "frozen controller rejected derived plan: " + verifier.derived_error
        )
    elif verifier.derived is None:
        result["reasons"].append(
            "frozen controller did not authenticate the available derived plan"
        )
    elif dict(verifier.derived) != derived:
        result["reasons"].append("reporter/controller derived plan views differ")
    if derived.get("schema") != DERIVED_SCHEMA:
        result["reasons"].append("derived plan schema differs")
    if derived.get("campaign_plan_sha256") != canonical_sha256(plan):
        result["reasons"].append("derived plan/campaign plan binding differs")
    if not validate_embedded_hash(derived, "derived_plan_sha256", prefixed=False):
        result["reasons"].append("derived plan self-hash differs")
    expected_hash = str(descriptor.get("sha256") or "").removeprefix("sha256:")
    if expected_hash and expected_hash != str(
        derived.get("derived_plan_sha256") or ""
    ).removeprefix("sha256:"):
        result["reasons"].append("status/derived plan hash binding differs")
    p99 = derived.get("p0_5_06") if isinstance(derived.get("p0_5_06"), Mapping) else None
    experiment_ids = {
        str(item.get("id")) for item in plan.get("experiments") or [] if isinstance(item, Mapping)
    }
    if "P0.5-06" in experiment_ids and p99 is None:
        result["reasons"].append("P0.5-06 derivation receipt is missing")
    if p99 is not None and not receipt_hash_valid(p99):
        result["reasons"].append("P0.5-06 receipt hash differs")
    noop = derived.get("p0_5_07") if isinstance(derived.get("p0_5_07"), Mapping) else None
    noop_ids = {
        str(item.get("id"))
        for item in plan.get("no_op_experiments") or []
        if isinstance(item, Mapping)
    }
    if "P0.5-07" in noop_ids and noop is None:
        result["reasons"].append("P0.5-07 no-op receipt is missing")
    if noop is not None and not receipt_hash_valid(noop):
        result["reasons"].append("P0.5-07 receipt hash differs")
    specs = expand_arms(plan)
    controls = comparison_control_contract(plan, specs)
    if derived.get("source_arm_id") != controls["source_arm_id"]:
        result["reasons"].append("derived Analyzer source differs from comparison_controls")
    offline_effect, offline_reasons, temperature_scope = load_offline_effect_receipts(
        derived,
        specs=specs,
        task_ids={str(value) for value in plan.get("benchmark", {}).get("task_ids") or []},
    )
    result["reasons"].extend(offline_reasons)
    if "P0.5-11" in experiment_ids and temperature_scope is None:
        result["reasons"].append("P0.5-11 temperature wire receipt is missing")
    frozen = (
        derived.get("frozen_analyzer_artifact")
        if isinstance(derived.get("frozen_analyzer_artifact"), Mapping)
        else None
    )
    if any(arm.analyzer_mode == "frozen_replay" for arm in specs) and frozen is None:
        result["reasons"].append("frozen Analyzer artifact descriptor is missing")
    if frozen is not None:
        artifact_path = Path(str(frozen.get("path") or ""))
        if not artifact_path.is_file() or artifact_path.is_symlink():
            result["reasons"].append("frozen Analyzer artifact is unavailable")
        elif str(frozen.get("file_sha256") or "").removeprefix("sha256:") != file_sha256(
            artifact_path
        ):
            result["reasons"].append("frozen Analyzer artifact raw hash differs")
        else:
            artifact = load_json(artifact_path)
            recorded_artifact_hash = str(artifact.get("artifact_sha256") or "").removeprefix(
                "sha256:"
            )
            if recorded_artifact_hash != str(frozen.get("artifact_sha256") or "").removeprefix(
                "sha256:"
            ):
                result["reasons"].append("derived/frozen Analyzer semantic hash binding differs")
            if not validate_embedded_hash(artifact, "artifact_sha256", prefixed=False):
                result["reasons"].append("frozen Analyzer artifact self-hash differs")
    result.update(
        {
            "valid": not result["reasons"],
            "derived_plan_sha256": derived.get("derived_plan_sha256"),
            "source_arm_id": derived.get("source_arm_id"),
            "source_output_dir": derived.get("source_output_dir"),
            "p0_5_06": dict(p99) if p99 is not None else None,
            "p0_5_07": dict(noop) if noop is not None else None,
            "p0_5_11": (
                {
                    "receipt_sha256s": temperature_scope.get("receipt_sha256s") or [],
                    "scope_verifiable": temperature_scope.get("scope_verifiable"),
                }
                if temperature_scope is not None
                else None
            ),
            "p0_5_11_scope": temperature_scope,
            "offline_effect": offline_effect,
            "frozen_analyzer_artifact": dict(frozen) if frozen is not None else None,
        }
    )
    return result


def validate_legacy_evidence(
    plan: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    configured = (
        plan.get("reporting", {}).get("legacy_evidence")
        if isinstance(plan.get("reporting"), Mapping)
        else None
    )
    inventory = configured if isinstance(configured, Mapping) else LEGACY_EVIDENCE
    result: dict[str, dict[str, Any]] = {}
    excluded = {
        str(item.get("id")): str(item.get("reason") or "")
        for item in plan.get("excluded") or []
        if isinstance(item, Mapping)
    }
    for experiment_id, reason in excluded.items():
        raw = inventory.get(experiment_id) if isinstance(inventory, Mapping) else None
        entry = dict(raw) if isinstance(raw, Mapping) else {}
        path = Path(str(entry.get("path") or ""))
        expected = str(entry.get("sha256") or "").removeprefix("sha256:")
        exists = bool(str(path)) and path.is_file() and not path.is_symlink()
        actual = file_sha256(path) if exists else None
        result[experiment_id] = {
            "experiment_id": experiment_id,
            "reason": reason,
            "status": entry.get("status") or "excluded_existing_evidence",
            "path": str(path) if str(path) else None,
            "expected_sha256": expected or None,
            "actual_sha256": actual,
            "valid": bool(exists and expected and actual == expected),
        }
    return result


def temperature_member_state(
    scope: Mapping[str, Any],
    *,
    arm_id: str,
    task_id: str,
    model: str,
) -> str:
    inventory = (
        scope.get("by_arm_task_model")
        if isinstance(scope.get("by_arm_task_model"), Mapping)
        else {}
    )
    normalized = normalize_model(model)
    tasks = inventory.get(arm_id) if isinstance(inventory.get(arm_id), Mapping) else {}
    models = tasks.get(task_id) if isinstance(tasks.get(task_id), Mapping) else {}
    return str(models.get(normalized) or "unknown")


def temperature_task_members(
    scope: Mapping[str, Any], *, arm_id: str, task_id: str
) -> list[Mapping[str, Any]]:
    inventory = (
        scope.get("by_arm_task_members")
        if isinstance(scope.get("by_arm_task_members"), Mapping)
        else {}
    )
    tasks = inventory.get(arm_id) if isinstance(inventory.get(arm_id), Mapping) else {}
    members = tasks.get(task_id)
    return [item for item in members or [] if isinstance(item, Mapping)]


def temperature_task_all_selected_sent(
    scope: Mapping[str, Any],
    arm: Mapping[str, Any],
    row: Mapping[str, Any],
) -> bool:
    arm_id = str(arm.get("spec", {}).get("arm_id") or "")
    task_id = str(row.get("task_id") or "")
    expected = Counter(
        model
        for model in (
            [normalize_model(value) for value in row.get("selected_p") or []]
            + [normalize_model(row.get("selected_a"))]
        )
        if model
    )
    selected_members = [
        member
        for member in temperature_task_members(scope, arm_id=arm_id, task_id=task_id)
        if str(member.get("role") or "") in {"proposer", "aggregator"}
    ]
    observed = Counter(normalize_model(member.get("model")) for member in selected_members)
    return (
        bool(expected)
        and observed == expected
        and all(member.get("state") == "sent" for member in selected_members)
    )


def temperature_model_subanalysis(
    scope: Mapping[str, Any],
    arm: Mapping[str, Any],
) -> dict[str, Any]:
    arm_id = str(arm.get("spec", {}).get("arm_id") or "")
    rows = arm.get("rows") or []
    model_states: dict[str, Counter[str]] = {}
    wire_temperatures: dict[str, Counter[str]] = {}
    sent_usage: dict[str, dict[str, Any]] = {}
    all_sent_tasks: list[str] = []
    for row in rows:
        if temperature_task_all_selected_sent(scope, arm, row):
            all_sent_tasks.append(str(row.get("task_id") or ""))
        task_id = str(row.get("task_id") or "")
        members = temperature_task_members(scope, arm_id=arm_id, task_id=task_id)
        for model, usage in (row.get("model_generation") or {}).items():
            normalized = normalize_model(model)
            matching = [
                member for member in members if normalize_model(member.get("model")) == normalized
            ]
            states = {str(member.get("state") or "unknown") for member in matching}
            state = next(iter(states)) if len(states) == 1 else "unknown"
            model_states.setdefault(normalized, Counter())[state] += 1
            for member in matching:
                if member.get("state") == "sent":
                    wire_temperatures.setdefault(normalized, Counter())[
                        str(member.get("wire_temperature"))
                    ] += 1
            if state != "sent":
                continue
            target = sent_usage.setdefault(
                normalized,
                {
                    "model": normalized,
                    "roles": set(),
                    "request_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_counted_usd": 0.0,
                    "actual_requests": 0,
                    "estimated_requests": 0,
                    "ignored_requests": 0,
                    "sent_task_count": 0,
                },
            )
            target["roles"].update(usage.get("roles") or [])
            for field in (
                "request_count",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_counted_usd",
                "actual_requests",
                "estimated_requests",
                "ignored_requests",
            ):
                target[field] += usage.get(field) or 0
            target["sent_task_count"] += 1
    models: dict[str, Any] = {}
    for model, usage in sent_usage.items():
        models[model] = {
            **usage,
            "roles": sorted(usage["roles"]),
            "temperature_scope": "sent_task_member_slices_only",
            "temperature_state_counts": dict(sorted(model_states.get(model, {}).items())),
            "wire_temperature_counts": dict(sorted(wire_temperatures.get(model, {}).items())),
            "included_in_formal_temperature_subanalysis": True,
            "quality_score_fields_present": False,
        }
    coverage = {
        model: {
            "temperature_state_counts": dict(sorted(states.items())),
            "wire_temperature_counts": dict(sorted(wire_temperatures.get(model, {}).items())),
        }
        for model, states in sorted(model_states.items())
    }
    included = list(models.values())
    return {
        "arm_id": arm_id,
        "scope_verifiable": scope.get("scope_verifiable") is True,
        "models": dict(sorted(models.items())),
        "wire_coverage": coverage,
        "formal_sent_model_count": len(included),
        "formal_sent_request_count": sum(integer(value.get("request_count")) for value in included),
        "formal_sent_input_tokens": sum(integer(value.get("input_tokens")) for value in included),
        "formal_sent_output_tokens": sum(integer(value.get("output_tokens")) for value in included),
        "formal_sent_cache_read_tokens": sum(
            integer(value.get("cache_read_tokens")) for value in included
        ),
        "formal_sent_cache_write_tokens": sum(
            integer(value.get("cache_write_tokens")) for value in included
        ),
        "formal_sent_cost_counted_usd": sum(
            number(value.get("cost_counted_usd")) or 0.0 for value in included
        ),
        "formal_sent_ignored_request_count": sum(
            integer(value.get("ignored_requests")) for value in included
        ),
        "all_selected_pa_temperature_sent_task_ids": sorted(all_sent_tasks),
        "all_selected_pa_temperature_sent_task_count": len(all_sent_tasks),
        "quality_attribution_note": "task quality cannot be decomposed into model-level causal scores",
    }


def display_arm(arm: Mapping[str, Any]) -> str:
    spec = arm.get("spec") if isinstance(arm.get("spec"), Mapping) else arm
    arm_id = str(spec.get("arm_id") or "")
    if arm_id.startswith("common-E0-"):
        return arm_id.removeprefix("common-")
    experiment = str(spec.get("experiment_id") or "")
    return arm_id.removeprefix(experiment + "-") if experiment else arm_id


def arm_artifact_label(arm: Mapping[str, Any]) -> str:
    if arm.get("state") == "no_op_deleted":
        return "no live (wire no-op)"
    if arm.get("formal") and arm.get("formal_evidence_valid"):
        return "formal complete"
    if arm.get("formal"):
        return "formal invalid"
    return str(arm.get("state") or "unavailable")


def offline_noop_evidence_valid(arm: Mapping[str, Any], derived: Mapping[str, Any]) -> bool:
    arm_id = str(arm.get("spec", {}).get("arm_id") or "")
    receipt = derived.get("offline_effect", {}).get(arm_id, {})
    return bool(
        isinstance(receipt, Mapping)
        and receipt.get("valid") is True
        and receipt.get("decision") == "deleted_no_live_run"
        and str(arm.get("no_op_receipt") or "") == str(receipt.get("path") or "")
    )


def build_experiment_inventory(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    arms: Mapping[str, Mapping[str, Any]],
    derived: Mapping[str, Any],
    legacy: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    control_contract = comparison_control_contract(plan, expand_arms(plan))
    default_control_id = str(control_contract["default_control_arm_id"])
    result: dict[str, dict[str, Any]] = {}
    for experiment in plan.get("experiments") or []:
        experiment_id = str(experiment["id"])
        variant_arms = [
            arm
            for arm in arms.values()
            if arm.get("spec", {}).get("experiment_id") == experiment_id
        ]
        variant_arms.sort(
            key=lambda arm: (
                str(arm.get("spec", {}).get("variant")),
                integer(arm.get("spec", {}).get("replicate")),
            )
        )
        control_ids: list[str] = []
        for arm in variant_arms:
            control_id = arm.get("spec", {}).get("control_arm_id")
            if control_id and control_id not in control_ids:
                control_ids.append(str(control_id))
        controls = [arms[arm_id] for arm_id in control_ids if arm_id in arms]
        comparisons: list[dict[str, Any]] = []
        scoped_comparisons: list[dict[str, Any]] = []
        comparison_invalid_reasons: list[str] = []
        shuffle_seed_protocol: dict[str, Any] | None = None
        if experiment_id == "P0.5-36":
            configured_by_arm = {
                str(arm.get("spec", {}).get("arm_id") or ""): (
                    (arm.get("spec", {}).get("override") or {}).get("ensemble", {}).get(
                        "candidate_order_seed"
                    )
                    if isinstance(
                        (arm.get("spec", {}).get("override") or {}).get("ensemble"),
                        Mapping,
                    )
                    else None
                )
                for arm in variant_arms
            }
            configured_values = list(configured_by_arm.values())
            configuration_valid = bool(
                len(configured_values) == 3
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in configured_values
                )
                and sorted(configured_values) == [0, 1, 4]
            )
            shuffle_seed_protocol = {
                "expected_seeds": [0, 1, 4],
                "configured_seed_by_arm": configured_by_arm,
                "configuration_valid": configuration_valid,
                "trace_evidence_by_arm": {
                    str(arm.get("spec", {}).get("arm_id") or ""): arm.get(
                        "candidate_order_seed_evidence"
                    )
                    for arm in variant_arms
                },
            }
            if not configuration_valid:
                comparison_invalid_reasons.append(
                    "P0.5-36 replicate candidate_order_seed configuration is not exactly 0/1/4"
                )
        for variant in variant_arms:
            control_id = variant.get("spec", {}).get("control_arm_id")
            control = arms.get(str(control_id)) if control_id else None
            if (
                control is not None
                and control.get("formal")
                and variant.get("formal")
                and control.get("formal_evidence_valid") is True
                and variant.get("formal_evidence_valid") is True
                and control.get("rows")
                and variant.get("rows")
            ):
                if experiment_id == "P0.5-36":
                    seed_evidence = variant.get("candidate_order_seed_evidence")
                    if isinstance(seed_evidence, Mapping) and integer(
                        seed_evidence.get("invalid_task_count")
                    ):
                        comparison_invalid_reasons.append(
                            "P0.5-36 configured/effective candidate-order seed mismatch: "
                            f"{variant.get('spec', {}).get('arm_id')}"
                        )
                    if (
                        not isinstance(shuffle_seed_protocol, Mapping)
                        or shuffle_seed_protocol.get("configuration_valid") is not True
                        or not isinstance(seed_evidence, Mapping)
                        or seed_evidence.get("comparison_slice_available") is not True
                    ):
                        comparison_invalid_reasons.append(
                            f"P0.5-36 candidate-order seed evidence unavailable: "
                            f"{variant.get('spec', {}).get('arm_id')}"
                        )
                        continue
                    allowed = set(seed_evidence.get("valid_task_ids") or [])
                    comparisons.append(
                        paired(
                            control,
                            variant,
                            scope="candidate_order_seed_verified",
                            allowed_task_ids=allowed,
                        )
                    )
                    scoped_comparisons.append(
                        paired(
                            control,
                            variant,
                            scope="candidate_order_seed_verified_ap_non_overlap",
                            allowed_task_ids=allowed,
                        )
                    )
                else:
                    comparisons.append(paired(control, variant))
        temperature_subanalysis: dict[str, Any] | None = None
        if experiment_id == "P0.5-11":
            temperature_scope = derived.get("p0_5_11_scope")
            if isinstance(temperature_scope, Mapping):
                per_arm = {
                    str(arm.get("spec", {}).get("arm_id")): temperature_model_subanalysis(
                        temperature_scope, arm
                    )
                    for arm in variant_arms
                    if arm.get("formal_evidence_valid") is True
                }
                for variant in variant_arms:
                    control_id = str(variant.get("spec", {}).get("control_arm_id") or "")
                    control = arms.get(control_id)
                    if (
                        not control
                        or control.get("formal_evidence_valid") is not True
                        or variant.get("formal_evidence_valid") is not True
                    ):
                        continue
                    variant_id = str(variant.get("spec", {}).get("arm_id") or "")
                    variant_scope = per_arm.get(variant_id) or {}
                    allowed = set(
                        variant_scope.get("all_selected_pa_temperature_sent_task_ids") or []
                    )
                    scoped_comparisons.append(
                        paired(
                            control,
                            variant,
                            scope="all_selected_pa_temperature_sent",
                            allowed_task_ids=allowed,
                        )
                    )
                temperature_subanalysis = {
                    "receipt_valid": all(
                        (
                            derived.get("offline_effect", {}).get(
                                str(arm.get("spec", {}).get("arm_id") or ""), {}
                            )
                            or {}
                        ).get("valid")
                        is True
                        for arm in variant_arms
                    ),
                    "receipt_sha256s": (derived.get("p0_5_11") or {}).get("receipt_sha256s") or [],
                    "scope_verifiable": temperature_scope.get("scope_verifiable") is True,
                    "receipt_record_count": temperature_scope.get("record_count"),
                    "unresolved_receipt_record_count": temperature_scope.get(
                        "unresolved_record_count"
                    ),
                    "per_arm": per_arm,
                    "quality_attribution_note": (
                        "model-level request/usage/cost is reported only for temperature_parameter_sent=true; "
                        "task score cannot be causally assigned to one model"
                    ),
                    "sampling_seed_protocol": {
                        "supported": False,
                        "configured": False,
                        "wire_sent": False,
                        "exact_replay_possible": False,
                        "note": (
                            "The current production experiment schema/provider path does not "
                            "support or send a frozen model-sampling seed. The three temperature "
                            "replicates are stochastic diagnostics and cannot be replayed bit-for-bit."
                        ),
                    },
                }
        if experiment_id in {"P0.5-10", "P0.5-38", "P0.5-39"}:
            for variant in variant_arms:
                if variant.get("formal_evidence_valid") is not True:
                    continue
                variant_id = str(variant.get("spec", {}).get("arm_id") or "")
                control_id = str(variant.get("spec", {}).get("control_arm_id") or "")
                control = arms.get(control_id)
                receipt = derived.get("offline_effect", {}).get(variant_id, {})
                if (
                    not control
                    or control.get("formal_evidence_valid") is not True
                    or receipt.get("valid") is not True
                ):
                    continue
                allowed = set(receipt.get("effective_changed_task_ids") or [])
                scoped_comparisons.append(
                    paired(
                        control,
                        variant,
                        scope="offline_effective_wire_changed_tasks",
                        allowed_task_ids=allowed,
                    )
                )
        replicated_variant = bool(
            len(variant_arms) > 1
            and len({str(arm.get("spec", {}).get("variant") or "") for arm in variant_arms}) == 1
            and len({integer(arm.get("spec", {}).get("replicate")) for arm in variant_arms})
            == len(variant_arms)
        )
        repeat_summary = repeated_pairing(comparisons) if replicated_variant else None
        scoped_repeat_summary = repeated_pairing(scoped_comparisons) if replicated_variant else None
        analyzer_effect: dict[str, Any] | None = None
        if experiment_id == "P0.5-06":
            evidence = {}
            for arm in controls + variant_arms:
                if not arm.get("formal") or not arm.get("rows"):
                    continue
                metric = arm.get("metrics") or {}
                evidence[str(arm.get("spec", {}).get("arm_id") or "")] = {
                    "max_output_token_values": metric.get("analyzer_max_output_token_values") or {},
                    "observed_output_tokens": metric.get("analyzer_output_tokens") or [],
                    "length_stop_count": integer(metric.get("analyzer_length_stop_count")),
                    "truncated_count": integer(metric.get("analyzer_truncated_count")),
                    "at_or_above_cap_count": integer(metric.get("analyzer_at_or_above_cap_count")),
                    "length_stop_task_ids": metric.get("analyzer_length_stop_task_ids") or [],
                    "truncated_task_ids": metric.get("analyzer_truncated_task_ids") or [],
                    "at_or_above_cap_task_ids": metric.get("analyzer_at_or_above_cap_task_ids")
                    or [],
                    "stop_reasons": metric.get("analyzer_stop_reasons") or {},
                }
            signatures = {
                (
                    tuple(row["length_stop_task_ids"]),
                    tuple(row["truncated_task_ids"]),
                    tuple(row["at_or_above_cap_task_ids"]),
                )
                for row in evidence.values()
            }
            evidence_complete = bool(controls and variant_arms) and all(
                arm.get("formal_evidence_valid") is True
                and integer(arm.get("metrics", {}).get("row_count")) == 10
                for arm in controls + variant_arms
            )
            unchanged = evidence_complete and len(signatures) == 1
            analyzer_effect = {
                "decision": (
                    "delete_no_observed_effect"
                    if unchanged
                    else "observed_effect_keep_for_analysis"
                    if evidence_complete
                    else "insufficient_evidence_no_delete_decision"
                ),
                "evidence_complete": evidence_complete,
                "truncation_length_stop_signature_unchanged": unchanged,
                "arms": evidence,
                "note": "already-run artifacts are retained regardless of the tuning decision",
            }
        states = [str(arm.get("state") or "unknown") for arm in variant_arms]
        evidence_valid = all(
            (arm.get("formal_evidence_valid") is True if arm.get("state") == "succeeded" else True)
            and (
                offline_noop_evidence_valid(arm, derived)
                if arm.get("state") == "no_op_deleted"
                else True
            )
            for arm in variant_arms
        )
        if any(state in {"failed", "blocked_prerequisite"} for state in states):
            state = "partial_or_failed"
        elif (
            states
            and all(item in {"succeeded", "no_op_deleted"} for item in states)
            and evidence_valid
        ):
            state = "complete"
        else:
            state = "incomplete"
        result[experiment_id] = {
            "experiment_id": experiment_id,
            "directory_name": str(
                experiment.get("directory_name") or experiment_id.replace(".", "-")
            ),
            "title": str(experiment.get("title") or experiment_id),
            "analysis_scope": experiment.get("analysis_scope"),
            "state": state,
            "controls": controls,
            "variants": variant_arms,
            "comparisons": comparisons,
            "repeated_pairing": repeat_summary,
            "scoped_comparisons": scoped_comparisons,
            "scoped_repeated_pairing": scoped_repeat_summary,
            "temperature_subanalysis": temperature_subanalysis,
            "shuffle_seed_protocol": shuffle_seed_protocol,
            "comparison_evidence_valid": not comparison_invalid_reasons,
            "comparison_invalid_reasons": sorted(set(comparison_invalid_reasons)),
            "analyzer_effect": analyzer_effect,
        }
    noop_status = derived.get("p0_5_07") if isinstance(derived.get("p0_5_07"), Mapping) else None
    controller_noops = (
        status.get("no_op_experiments")
        if isinstance(status.get("no_op_experiments"), Mapping)
        else {}
    )
    for experiment in plan.get("no_op_experiments") or []:
        experiment_id = str(experiment["id"])
        controller_noop = (
            controller_noops.get(experiment_id)
            if isinstance(controller_noops.get(experiment_id), Mapping)
            else {}
        )
        controller_receipt = (
            controller_noop.get("receipt")
            if isinstance(controller_noop.get("receipt"), Mapping)
            else None
        )
        receipt_binding_ok = bool(
            noop_status
            and controller_receipt
            and controller_receipt.get("receipt_sha256") == noop_status.get("receipt_sha256")
        )
        result[experiment_id] = {
            "experiment_id": experiment_id,
            "directory_name": experiment_id.replace(".", "-"),
            "title": str(experiment.get("title") or experiment_id),
            "state": (
                "no_op_deleted"
                if noop_status
                and receipt_hash_valid(noop_status)
                and controller_noop.get("state") == "no_op_deleted"
                and receipt_binding_ok
                else "incomplete_no_op_evidence"
            ),
            "controls": [arms[default_control_id]] if default_control_id in arms else [],
            "variants": [],
            "comparisons": [],
            "repeated_pairing": None,
            "scoped_comparisons": [],
            "scoped_repeated_pairing": None,
            "temperature_subanalysis": None,
            "analyzer_effect": None,
            "no_op_declaration": dict(experiment),
            "no_op_receipt": dict(noop_status) if noop_status else None,
            "controller_no_op": dict(controller_noop),
            "controller_receipt_binding_valid": receipt_binding_ok,
        }
    for experiment_id, evidence in legacy.items():
        result[experiment_id] = {
            "experiment_id": experiment_id,
            "directory_name": experiment_id.replace(".", "-"),
            "title": experiment_id,
            "state": "excluded_with_valid_evidence"
            if evidence.get("valid")
            else "excluded_evidence_invalid",
            "controls": [],
            "variants": [],
            "comparisons": [],
            "repeated_pairing": None,
            "scoped_comparisons": [],
            "scoped_repeated_pairing": None,
            "temperature_subanalysis": None,
            "analyzer_effect": None,
            "legacy_evidence": dict(evidence),
        }
    return result


def metric_cost_text(metric: Mapping[str, Any], prefix: str) -> str:
    value = metric.get(prefix + "_counted_usd")
    if value is None:
        return "—"
    marker = ""
    if metric.get(prefix + "_is_lower_bound") is True:
        marker = "≥"
    elif integer(metric.get(prefix + "_estimated_request_count")) > 0:
        marker = "≈"
    return marker + fmt(value, 6)


def metric_avg_generation_text(metric: Mapping[str, Any]) -> str:
    value = metric.get("avg_selected_generation_cost_usd")
    if value is None:
        return "—"
    marker = ""
    if metric.get("selected_generation_cost_is_lower_bound") is True:
        marker = "≥"
    elif integer(metric.get("selected_generation_cost_estimated_request_count")) > 0:
        marker = "≈"
    return marker + fmt(value, 6)


def metric_table_row(arm: Mapping[str, Any]) -> str:
    if arm.get("formal_evidence_valid") is not True:
        return "| " + display_arm(arm) + " | " + " | ".join(["—"] * 20) + " |"
    metric = arm.get("metrics") or {}
    return (
        "| "
        + " | ".join(
            [
                display_arm(arm),
                str(integer(metric.get("row_count"))),
                str(integer(metric.get("done_count"))),
                fmt(metric.get("avg_quality_total"), 4),
                pct(metric.get("avg_pass_rate")),
                str(integer(metric.get("judge_error_count"))),
                metric_avg_generation_text(metric),
                metric_cost_text(metric, "selected_generation_cost"),
                f"{integer(metric.get('selected_generation_cost_exact_task_count'))}/{integer(metric.get('row_count'))}",
                fmt(metric.get("avg_input_tokens"), 1),
                fmt(metric.get("avg_output_tokens"), 1),
                fmt(metric.get("avg_reasoning_tokens"), 1),
                fmt(metric.get("avg_cached_tokens"), 1),
                fmt(metric.get("avg_visible_tokens"), 1),
                fmt(metric.get("avg_total_tokens"), 1),
                fmt(metric.get("avg_tool_calls"), 2),
                pct(metric.get("tool_task_rate")),
                fmt(metric.get("avg_trajectory_steps"), 2),
                fmt(metric.get("avg_llm_requests"), 2),
                fmt(metric.get("latency_p50_ms"), 0),
                fmt(metric.get("latency_p95_ms"), 0),
            ]
        )
        + " |"
    )


METRIC_HEADER = (
    "| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | Total Gen$ | Gen exact | "
    "Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | "
    "Avg Steps | Avg LLMReq | p50 ms | p95 ms |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)


def build_group_markdown(
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    derived: Mapping[str, Any],
) -> str:
    experiment_id = str(experiment["experiment_id"])
    title = str(experiment.get("title") or experiment_id)
    lines = [f"# {experiment_id} DRACO Mini 实验结果", "", "## 结论与范围", ""]
    if experiment.get("legacy_evidence"):
        evidence = experiment["legacy_evidence"]
        lines.extend(
            [
                f"- 本组按冻结计划不重跑：`{evidence.get('reason')}`。",
                f"- 既有证据状态：`{evidence.get('status')}`；hash 校验：`{'pass' if evidence.get('valid') else 'FAIL'}`。",
                f"- 既有报告：`{evidence.get('path')}`；SHA-256=`{evidence.get('actual_sha256') or 'missing'}`。",
                "- 本文件只作新 campaign 的不可重复执行/来源索引，不把旧结果伪装成新运行。",
                "- DRACO mini 只有 10 题且没有独立 SafetyGate；不能据此自动推广 winner。",
                "",
            ]
        )
        return "\n".join(lines) + "\n"
    if experiment.get("no_op_declaration"):
        declaration = experiment["no_op_declaration"]
        receipt = experiment.get("no_op_receipt") or {}
        lines.extend(
            [
                f"- 状态：`{experiment.get('state')}`；该变量在当前 provider/model 兼容策略下不会进入 wire，因此没有 live arm。",
                f"- 请求值：{code_json(declaration.get('requested_values'))}；provider/model=`{declaration.get('provider_kind')}:{declaration.get('model')}`。",
                f"- no-op receipt：`{receipt.get('receipt_sha256', 'missing')}`；原因：{receipt.get('reason') or declaration.get('reason')}。",
                "- 删除 no-op 实验避免为字节等价请求付费；这不是零分、失败或缺失运行。",
                "- DRACO mini 没有独立 SafetyGate；10 题仅作诊断，不自动推广 winner。",
                "",
                "## 冻结取证",
                "",
                f"- Campaign plan：`{plan['paths']['run_root']}/campaign-plan.json`",
                f"- Derived plan：`{derived.get('path')}`",
            ]
        )
        return "\n".join(lines) + "\n"
    controls = list(experiment.get("controls") or [])
    variants = list(experiment.get("variants") or [])
    display_arms = controls + variants
    lines.extend(
        [
            f"- 变量：{title}；组状态：`{experiment.get('state')}`；controller=`{status.get('phase')}`。",
            "- 所有质量比较只按 `task_id` 配对；缺题不会伪装成完整均值。",
            "- 候选只与 `analyzer_mode` 完全相同的冻结 comparison control 比较；live source 不与 frozen-replay candidate 混配。",
            "- 执行为 anchored-serial、每臂独立串行账户窗口，不是逐题 AB/BA 交错；时间/provider 漂移仍可能混入 ΔQ。",
            f"- 固定 seed `{BOOTSTRAP_SEED}`，每个 paired bootstrap 使用 `{BOOTSTRAP_SAMPLES}` 次重采样。",
            "- execution、policy、audit 三条状态彼此独立；BYOK/费用缺口不自动改判已有答案执行失败。",
            "- DRACO mini 没有独立 SafetyGate；10 题是调参诊断，不自动推广 winner。",
            "",
            "## 实验臂与配置",
            "",
            "| Arm | Role | Analyzer | Override / dynamic | Artifact | Root |",
            "|---|---|---|---|---|---|",
        ]
    )
    for arm in display_arms:
        spec = arm.get("spec") or {}
        role = "control" if spec.get("experiment_id") == "common-E0" else "variant"
        configuration: Any = spec.get("override") or spec.get("dynamic") or {}
        lines.append(
            f"| {display_arm(arm)} | {role} | `{spec.get('analyzer_mode')}` | {code_json(configuration)} | "
            f"{arm_artifact_label(arm)} | `{arm.get('output_dir')}` |"
        )
    if experiment_id == "P0-20":
        c3 = next(
            (
                arm.get("c3_promotion_evidence")
                for arm in variants
                if arm.get("spec", {}).get("arm_id") == "P0-20-E3"
            ),
            {},
        )
        lines.extend(
            [
                "",
                "### C3 晋级证据边界",
                "",
                "- `P0-20-E3` 仅为 `mini_diagnostic_only`，不得作为降本链 C3 晋级证据。源计划要求 E0/候选逐题交错，而本 campaign 明确 `strict_task_interleaving=false`。",
                f"- 它位于 R1 anchor 后的近邻 anchored-serial tranche（ordinal gap={c3.get('schedule_ordinal_gap', 'unknown')}）以减小时漂，但整臂串行不等同 task interleaving。",
            ]
        )
    lines.extend(
        [
            "",
            "### Anchored-serial 调度",
            "",
            "| Arm | Ordinal | Anchor/control | Start | Anchor complete | Lag s |",
            "|---|---:|---|---|---|---:|",
        ]
    )
    for arm in display_arms:
        timing = arm.get("schedule") if isinstance(arm.get("schedule"), Mapping) else {}
        lines.append(
            f"| {display_arm(arm)} | {integer(timing.get('schedule_ordinal'))} | "
            f"`{timing.get('anchor_arm_id') or 'missing'}` | `{timing.get('started_at') or 'not-started'}` | "
            f"`{timing.get('anchor_completed_at') or 'not-complete'}` | {fmt(timing.get('anchor_lag_seconds'), 1)} |"
        )
    if experiment.get("analysis_scope"):
        lines.extend(
            [
                "",
                f"- 冻结 analysis scope：`{experiment.get('analysis_scope')}`。",
            ]
        )
    if isinstance(experiment.get("analyzer_effect"), Mapping):
        analyzer_effect = experiment["analyzer_effect"]
        lines.append(
            f"- Analyzer truncation/length-stop 决策：`{analyzer_effect.get('decision')}`；已运行证据始终保留。"
        )
    wire_noops = [arm for arm in variants if arm.get("state") == "no_op_deleted"]
    if wire_noops:
        lines.extend(
            [
                "",
                "### Wire-effect no-op receipts",
                "",
                "| Arm | Decision | Changed tasks | Receipt SHA | Receipt path |",
                "|---|---|---:|---|---|",
            ]
        )
        for arm in wire_noops:
            arm_id = str(arm.get("spec", {}).get("arm_id") or "")
            receipt = derived.get("offline_effect", {}).get(arm_id, {})
            lines.append(
                f"| {display_arm(arm)} | `{receipt.get('decision', 'missing')}` | "
                f"{integer(receipt.get('effective_changed_task_count'))} | `{receipt.get('receipt_sha256', 'missing')}` | "
                f"`{receipt.get('path', 'missing')}` |"
            )
        lines.extend(
            [
                "",
                "这些 arm 只在冻结 E0 的 selected/recovery P/A 上投影生产 output-budget 解析；零 changed task 才删除 live run。",
            ]
        )
    if experiment_id == "P0.5-06" and isinstance(derived.get("p0_5_06"), Mapping):
        p99 = derived["p0_5_06"]
        lines.extend(
            [
                "",
                "### Analyzer p99 动态参数 receipt",
                "",
                f"- method=`{p99.get('method')}`；p99=`{p99.get('p99')}`；derived max output={code_json(p99.get('derived_max_output_tokens'))}。",
                f"- receipt SHA=`{p99.get('receipt_sha256')}`；source Analyzer artifact=`{p99.get('source_artifact_sha256')}`。",
            ]
        )
        analyzer_effect = experiment.get("analyzer_effect") or {}
        lines.extend(
            [
                "",
                "| Arm | Length-stop | Truncated | At/above cap | Max-output values | Stop reasons |",
                "|---|---:|---:|---:|---|---|",
            ]
        )
        for arm_id, evidence in (analyzer_effect.get("arms") or {}).items():
            lines.append(
                f"| {arm_id} | {integer(evidence.get('length_stop_count'))} | "
                f"{integer(evidence.get('truncated_count'))} | "
                f"{integer(evidence.get('at_or_above_cap_count'))} | "
                f"{code_json(evidence.get('max_output_token_values') or {})} | "
                f"{code_json(evidence.get('stop_reasons') or {})} |"
            )
    if experiment_id in {"P0.5-10", "P0.5-38", "P0.5-39"}:
        lines.extend(
            [
                "",
                "### Offline effective-change task receipts",
                "",
                "只有下表 union task slice 进入 primary paired analysis；全 10 题仅保留为 diagnostic。",
                "",
                "| Arm | Decision | Effective tasks | By cap-explicitness | Receipt SHA |",
                "|---|---|---|---|---|",
            ]
        )
        for arm in variants:
            arm_id = str(arm.get("spec", {}).get("arm_id") or "")
            receipt = derived.get("offline_effect", {}).get(arm_id, {})
            lines.append(
                f"| {display_arm(arm)} | `{receipt.get('decision', 'missing')}` | "
                f"{code_json(receipt.get('effective_changed_task_ids') or [])} | "
                f"{code_json(receipt.get('changed_task_ids_by_context') or {})} | "
                f"`{receipt.get('receipt_sha256', 'missing')}` |"
            )
    temperature = experiment.get("temperature_subanalysis")
    if isinstance(temperature, Mapping):
        seed_protocol = temperature.get("sampling_seed_protocol") or {}
        lines.extend(
            [
                "",
                "### Temperature wire 正式子分析",
                "",
                f"- receipt SHA list={code_json(temperature.get('receipt_sha256s') or [])}；scope verifiable=`{temperature.get('scope_verifiable')}`；records={integer(temperature.get('receipt_record_count'))}，unresolved={integer(temperature.get('unresolved_receipt_record_count'))}。",
                "- 只有 offline receipt 明确 `temperature_parameter_sent=true` 且给出 `wire_temperature` 的模型进入正式子分析。模型级 usage/cost 可统计，但 task score 无法拆分归因到单个模型，因此不伪造模型级质量分。",
                f"- Sampling seed：supported=`{seed_protocol.get('supported')}`、wire sent=`{seed_protocol.get('wire_sent')}`、exact replay=`{seed_protocol.get('exact_replay_possible')}`。当前协议不支持/不发送模型 sampling seed，三次重复不能精确重放。",
                "",
                "| Arm | Model | Wire scope | Wire temperatures | Requests | Input | Output | Cache read/write | Cost$ | Actual/estimated/ignored |",
                "|---|---|---|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        model_rows = 0
        for arm_id, arm_scope in (temperature.get("per_arm") or {}).items():
            for model, model_row in (arm_scope.get("models") or {}).items():
                if not model_row.get("included_in_formal_temperature_subanalysis"):
                    continue
                model_rows += 1
                lines.append(
                    f"| {arm_id} | `{model}` | `sent-only slices` | {code_json(model_row.get('wire_temperature_counts') or {})} | {integer(model_row.get('request_count'))} | "
                    f"{integer(model_row.get('input_tokens'))} | {integer(model_row.get('output_tokens'))} | "
                    f"{integer(model_row.get('cache_read_tokens'))}/{integer(model_row.get('cache_write_tokens'))} | "
                    f"{fmt(model_row.get('cost_counted_usd'), 6)} | {integer(model_row.get('actual_requests'))}/"
                    f"{integer(model_row.get('estimated_requests'))}/{integer(model_row.get('ignored_requests'))} |"
                )
        if model_rows == 0:
            lines.append(
                "| — | — | `scope_unverifiable_or_no_sent_model` | — | 0 | 0 | 0 | 0/0 | — | — |"
            )
    shuffle_seed = experiment.get("shuffle_seed_protocol")
    if isinstance(shuffle_seed, Mapping):
        lines.extend(
            [
                "",
                "### Candidate-order seed 证据",
                "",
                f"- 冻结 replicate seed 必须恰为 `0/1/4`；configuration valid=`{shuffle_seed.get('configuration_valid')}`。只有 trace 中 `shuffle_candidates=true`、存在 candidate display order 且 Aggregator 物理请求已启动的调用才适用 seed gate。未进入候选聚合的调用记为 `not_applicable`，不改判 execution。",
                "",
                "| Arm | Config seed | Valid tasks | Invalid tasks | Not applicable | Applicable calls | Invalid calls |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for arm_id, evidence in (shuffle_seed.get("trace_evidence_by_arm") or {}).items():
            evidence = evidence if isinstance(evidence, Mapping) else {}
            lines.append(
                f"| {arm_id} | {evidence.get('configured_seed', 'missing')} | "
                f"{integer(evidence.get('valid_task_count'))} | {integer(evidence.get('invalid_task_count'))} | "
                f"{integer(evidence.get('not_applicable_task_count'))} | {integer(evidence.get('applicable_call_count'))} | "
                f"{integer(evidence.get('invalid_call_count'))} |"
            )
        if experiment.get("comparison_evidence_valid") is not True:
            lines.append(
                "\n- Candidate-order 比较证据无效/不完整："
                + code_json(experiment.get("comparison_invalid_reasons") or [])
            )
    lines.extend(["", "## 总表", "", METRIC_HEADER])
    for arm in display_arms:
        lines.append(metric_table_row(arm))
    lines.extend(
        [
            "",
            "Gen$ 只统计最终成功且 selected 的 generation attempt（live Analyzer + proposer + aggregator）；排除 Judge、失败/被替换 retry。provider actual USD 优先；缺 USD 且有 tokens 时按冻结 79 模型价格拆分 fresh/cache-read/cache-write/output 后估算。冻结表若没有 cache 专属单价，则缓存 token 以普通 input 单价作保守回退并单独披露。费用和 tokens 都缺失的物理请求忽略金额、计入 ignored，Total/Avg 标记为下界，绝不写成 $0。",
            "",
            "### Selected generation 与 Judge 计费证据（分列）",
            "",
            "| Arm | Gen req | Gen actual | Gen estimated | Cache-aware est | Cache-rate fallback | No-cache est | Gen ignored | Judge$ | Judge req | Judge ignored |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm in display_arms:
        if arm.get("formal_evidence_valid") is not True:
            lines.append(f"| {display_arm(arm)} | — | — | — | — | — | — | — | — | — | — |")
            continue
        metric = arm.get("metrics") or {}
        lines.append(
            f"| {display_arm(arm)} | {integer(metric.get('selected_generation_cost_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_actual_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_estimated_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_cache_aware_estimated_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_cache_price_fallback_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_no_cache_estimated_request_count'))} | "
            f"{integer(metric.get('selected_generation_cost_ignored_request_count'))} | "
            f"{metric_cost_text(metric, 'judge_cost')} | {integer(metric.get('judge_cost_request_count'))} | "
            f"{integer(metric.get('judge_cost_ignored_request_count'))} |"
        )
    lines.extend(
        [
            "",
            "Judge$ 是独立 Judge 调用成本，不进入 Gen$。账户 Δ 包含实际 campaign 窗口内的 generation、Judge 及其他可归属调用；它与 Gen$ 理论口径不能相加。",
            "",
            "## Completion、Execution、Policy、Audit 与账户",
            "",
            "| Arm | Artifact | Execution | Policy | Audit | Account Δ$ | BYOK Δ$ | Reconciliation | Formal evidence |",
            "|---|---|---|---|---|---:|---:|---|---|",
        ]
    )
    for arm in display_arms:
        statuses, account = arm.get("statuses") or {}, arm.get("account") or {}
        reasons = arm.get("formal_evidence_reasons") or []
        lines.append(
            f"| {display_arm(arm)} | `{arm_artifact_label(arm)}` | `{statuses.get('execution', 'unknown')}` | "
            f"`{statuses.get('policy', 'unknown')}` | `{statuses.get('audit', 'unknown')}` | "
            f"{fmt(account.get('account_delta_usd'), 9)} | {fmt(account.get('byok_delta_usd'), 9)} | "
            f"`{account.get('reconciliation_status', 'unknown')}` / stable={account.get('reconciliation_stable', 'unknown')} | "
            f"{'pass' if arm.get('formal_evidence_valid') else code_json(reasons)} |"
        )
    primary_comparisons = list(
        experiment.get("scoped_comparisons") or experiment.get("comparisons") or []
    )
    primary_repeated = experiment.get("scoped_repeated_pairing") or experiment.get(
        "repeated_pairing"
    )
    primary_scope = (
        str(primary_comparisons[0].get("scope") or "all_tasks")
        if primary_comparisons
        else "all_tasks"
    )
    lines.extend(
        [
            "",
            f"## 同题配对质量（primary scope: `{primary_scope}`）",
            "",
            "| Variant - control | Pairs | Mean ΔQ | Paired bootstrap 95% CI | W/T/L | Request context match | Task profile match | Route P/A/N changed |",
            "|---|---:|---:|---|---|---:|---:|---|",
        ]
    )
    for comparison in primary_comparisons:
        low, high = comparison.get("bootstrap_ci95") or [None, None]
        lines.append(
            f"| {comparison.get('variant_arm_id')} - {comparison.get('control_arm_id')} | {integer(comparison.get('pair_count'))} | "
            f"{fmt(comparison.get('mean_delta_quality'), 4)} | [{fmt(low, 4)}, {fmt(high, 4)}] | "
            f"{integer(comparison.get('wins'))}/{integer(comparison.get('ties'))}/{integer(comparison.get('losses'))} | "
            f"{integer(comparison.get('request_context_match_count'))}/{integer(comparison.get('pair_count'))} | "
            f"{integer(comparison.get('task_profile_match_count'))}/{integer(comparison.get('pair_count'))} | "
            f"{integer(comparison.get('proposer_changed_count'))}/{integer(comparison.get('aggregator_changed_count'))}/{integer(comparison.get('n_changed_count'))} |"
        )
    repeated = primary_repeated
    if isinstance(repeated, Mapping):
        low, high = repeated.get("bootstrap_ci95") or [None, None]
        lines.append(
            f"| R1–R3 task mean | {integer(repeated.get('task_count'))} | {fmt(repeated.get('mean_delta_quality'), 4)} | "
            f"[{fmt(low, 4)}, {fmt(high, 4)}] | {integer(repeated.get('wins'))}/{integer(repeated.get('ties'))}/{integer(repeated.get('losses'))} | — | — | — |"
        )
    if not primary_comparisons:
        lines.append("| — | 0 | — | — | — | — | — | — |")
    if experiment.get("scoped_comparisons"):
        lines.extend(
            [
                "",
                "### 全 10 题 diagnostic 配对（非受限主分析）",
                "",
                "| Variant - control | Pairs | Mean ΔQ | 95% CI | W/T/L |",
                "|---|---:|---:|---|---|",
            ]
        )
        for comparison in experiment.get("comparisons") or []:
            low, high = comparison.get("bootstrap_ci95") or [None, None]
            lines.append(
                f"| {comparison.get('variant_arm_id')} - {comparison.get('control_arm_id')} | {integer(comparison.get('pair_count'))} | "
                f"{fmt(comparison.get('mean_delta_quality'), 4)} | [{fmt(low, 4)}, {fmt(high, 4)}] | "
                f"{integer(comparison.get('wins'))}/{integer(comparison.get('ties'))}/{integer(comparison.get('losses'))} |"
            )
    lines.extend(
        [
            "",
            "### 逐题质量差",
            "",
            "| Variant | Domain | Task | Control Q | Variant Q | ΔQ |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for comparison in primary_comparisons:
        for row in comparison.get("task_rows") or []:
            lines.append(
                f"| {comparison.get('variant_arm_id')} | {row.get('domain')} | `{str(row.get('task_id'))[:8]}…` | "
                f"{fmt(row.get('control_quality'), 4)} | {fmt(row.get('variant_quality'), 4)} | {fmt(row.get('delta_quality'), 4)} |"
            )
    if not primary_comparisons:
        lines.append("| — | — | — | — | — | — |")
    lines.extend(
        [
            "",
            "## 路由与容错摘要",
            "",
            "| Arm | N distribution | Selected A | Retry | Fallback tasks | Proposer recovery | Partial/degraded/truncated | Analyzer source |",
            "|---|---|---|---:|---:|---:|---|---|",
        ]
    )
    for arm in display_arms:
        if arm.get("formal_evidence_valid") is not True:
            lines.append(f"| {display_arm(arm)} | — | — | — | — | — | — | — | — |")
            continue
        metric = arm.get("metrics") or {}
        lines.append(
            f"| {display_arm(arm)} | {code_json(metric.get('n_distribution') or {})} | "
            f"{code_json(metric.get('selected_aggregators') or {})} | {integer(metric.get('outer_retry_count'))} | "
            f"{integer(metric.get('fallback_task_count'))} | {integer(metric.get('proposer_recovery_request_count'))} | "
            f"{integer(metric.get('partial_proposer_task_count'))}/{integer(metric.get('degraded_task_count'))}/{integer(metric.get('assembly_truncated_task_count'))} | "
            f"{code_json(metric.get('analyzer_sources') or {})} |"
        )
    lines.extend(
        [
            "",
            "### Analyzer origin / fallback 取证",
            "",
            "只有 `task_analyzer.replay.origin_outcome=deterministic_router_fallback` 的 v2 证据才计入 fallback task；旧行缺失 origin 时不会根据失败原因反向猜测 fallback。",
            "",
            "| Arm | Origin outcome distribution | Fallback reason distribution | Fallback task IDs | Unknown-origin task IDs |",
            "|---|---|---|---|---|",
        ]
    )
    for arm in display_arms:
        if arm.get("formal_evidence_valid") is not True:
            lines.append(f"| {display_arm(arm)} | — | — | — | — |")
            continue
        metric = arm.get("metrics") or {}
        lines.append(
            f"| {display_arm(arm)} | "
            f"{code_json(metric.get('analyzer_origin_outcome_distribution') or {})} | "
            f"{code_json(metric.get('analyzer_fallback_reason_distribution') or {})} | "
            f"{code_json(metric.get('analyzer_fallback_task_ids') or [])} | "
            f"{code_json(metric.get('analyzer_unknown_origin_task_ids') or [])} |"
        )
    lines.extend(
        [
            "",
            "## 证据路径",
            "",
            f"- Campaign plan：`{plan['paths']['run_root']}/campaign-plan.json`",
            f"- Immutable terminal status input：`{plan['paths']['run_root']}/terminal-status-input.json`",
            f"- Derived plan：`{derived.get('path')}`；valid=`{derived.get('valid')}`。",
            "",
            "本报告由终态只读生成器离线构建；没有调用模型、没有修改 Wave，也没有把 mini 结果自动推广为正式默认配置。",
        ]
    )
    return "\n".join(lines) + "\n"


def experiment_summary_line(experiment: Mapping[str, Any]) -> str:
    comparisons = experiment.get("scoped_comparisons") or experiment.get("comparisons") or []
    complete_pairs = [
        comparison for comparison in comparisons if comparison.get("complete_task_id_pairing")
    ]
    deltas = [comparison.get("mean_delta_quality") for comparison in complete_pairs]
    variant_states = Counter(
        str(arm.get("state") or "unknown") for arm in experiment.get("variants") or []
    )
    return (
        f"| {experiment.get('experiment_id')} | {experiment.get('title')} | `{experiment.get('state')}` | "
        f"{len(experiment.get('variants') or [])} | {code_json(dict(sorted(variant_states.items())))} | "
        f"{len(complete_pairs)}/{len(comparisons)} | {code_json([round(float(value), 4) for value in deltas if number(value) is not None])} | "
        f"[MD]({experiment.get('directory_name')}/EXPERIMENT_RESULTS.md) / "
        f"[JSON]({experiment.get('directory_name')}/EXPERIMENT_RESULTS.json) |"
    )


def confirmatory_markdown_lines(report: Mapping[str, Any]) -> list[str]:
    evidence = report.get("confirmatory_cohorts")
    if not isinstance(evidence, Mapping):
        return []
    lines = [
        "",
        "## 确认性逐题 AB/BA paired cohorts",
        "",
        (
            f"- receipt index=`{evidence.get('path')}`；status=`{evidence.get('status')}`；"
            f"cohorts={integer(evidence.get('cohort_count'))}；complete="
            f"{integer(evidence.get('complete_cohort_count'))}；partial="
            f"{integer(evidence.get('partial_cohort_count'))}。"
        ),
        "- 这里只摄取 controller 固定路径发布且通过 self-hash、plan binding、schedule、cohort manifest 与正式 artifact 校验的收据；不会扫描目录猜测实验。",
    ]
    if evidence.get("status") == "absent":
        lines.append("- 本次没有 confirmatory receipt index；保留原 screening 报告语义。")
        return lines
    cohorts = evidence.get("cohorts") if isinstance(evidence.get("cohorts"), Mapping) else {}
    for cohort_id, cohort in cohorts.items():
        if not isinstance(cohort, Mapping):
            continue
        lines.extend(
            [
                "",
                f"### `{cohort_id}`",
                "",
                f"- state=`{cohort.get('state')}`；schedule=`{cohort.get('schedule_sha256')}`；account cohort=`{cohort.get('account_window_cohort_sha256')}`。",
                f"- evidence reasons={code_json(cohort.get('reasons') or [])}。",
            ]
        )
        roles = cohort.get("roles") if isinstance(cohort.get("roles"), Mapping) else {}
        formal_roles = {
            role: value
            for role, value in roles.items()
            if isinstance(value, Mapping) and isinstance(value.get("metrics"), Mapping)
        }
        if formal_roles:
            lines.extend(["", METRIC_HEADER])
            for role in ("control", "candidate"):
                if role in formal_roles:
                    lines.append(metric_table_row(formal_roles[role]))
        else:
            lines.append(
                "- role publications="
                + code_json(
                    {
                        role: value.get("publication_status")
                        for role, value in roles.items()
                        if isinstance(value, Mapping)
                    }
                )
                + "。"
            )
        comparison = cohort.get("comparison")
        if isinstance(comparison, Mapping):
            low, high = comparison.get("bootstrap_ci95") or [None, None]
            lines.extend(
                [
                    "",
                    (
                        f"- paired tasks={integer(comparison.get('pair_count'))}/10；"
                        f"mean ΔQ={fmt(comparison.get('mean_delta_quality'), 4)}；"
                        f"95% CI=[{fmt(low, 4)}, {fmt(high, 4)}]；W/T/L="
                        f"{integer(comparison.get('wins'))}/{integer(comparison.get('ties'))}/"
                        f"{integer(comparison.get('losses'))}。"
                    ),
                    "",
                    "| Task | Order | Control Q | Candidate Q | ΔQ |",
                    "|---|---|---:|---:|---:|",
                ]
            )
            for row in comparison.get("task_rows") or []:
                if not isinstance(row, Mapping):
                    continue
                lines.append(
                    f"| {row.get('task_id')} | {row.get('execution_order')} | "
                    f"{fmt(row.get('control_quality'), 4)} | "
                    f"{fmt(row.get('variant_quality'), 4)} | "
                    f"{fmt(row.get('delta_quality'), 4)} |"
                )
            order_balance = comparison.get("order_balance") or {}
            lines.append(f"- AB/BA 分层 ΔQ={code_json(order_balance)}。")
        costs = cohort.get("costs")
        if isinstance(costs, Mapping) and cohort.get("state") == "complete":
            lines.append(
                "- cohort costs：selected generation="
                f"${fmt(costs.get('selected_generation_counted_usd'), 6)}；Judge="
                f"${fmt(costs.get('judge_counted_usd'), 6)}；shared account actual="
                f"${fmt(costs.get('account_delta_usd'), 6)}（仅计一次）。"
            )
    return lines


def build_root_markdown(report: Mapping[str, Any]) -> str:
    experiments = report["experiments"]
    arms = report["arms"]
    completion = report["completion"]
    costs = report["unique_arm_costs"]
    schedule = report.get("schedule_evidence") or {}
    drift = report.get("replay_control_drift") or {}
    controls = report.get("comparison_controls") or {}
    c3 = (report.get("promotion") or {}).get("P0-20-E3") or {}
    paired_cohort_count = integer(costs.get("paired_cohort_count"))
    account_window_design = (
        f"确认性 control/candidate 使用 {paired_cohort_count} 个认证 paired cohort 共享账户窗口；"
        "selected generation 与 Judge 仍逐臂统计，账户 delta 按 cohort_id 只计一次。"
        if paired_cohort_count
        else (
            "每个正式臂保留独立的 10 行 finalizer 和串行账户窗口；"
            "因此只能做 anchored-serial 时间控制。"
        )
    )
    lines = [
        "# P0 / P0.5 DRACO Mini 综合实验结果",
        "",
        "## 总结",
        "",
        f"- Campaign：`{report['run_id']}`；controller phase=`{report['controller_phase']}`；报告状态=`{completion['status']}`。",
        f"- 冻结提交/tree：`{report['freeze'].get('snapshot_commit')}` / `{report['freeze'].get('snapshot_tree')}`；DRACO mini 10 题，task concurrency=`{report['execution'].get('task_concurrency')}`。",
        f"- 计划候选 live arms={completion['planned_arm_count']}；formal succeeded={completion['formal_succeeded_arm_count']}；wire no-op={completion['wire_no_op_arm_count']}；failed/blocked={completion['failed_or_blocked_arm_count']}。",
        f"- Active experiment comparison evidence=`{completion['comparison_evidence_valid']}`；invalid={code_json(completion['comparison_evidence_invalid_experiment_ids'])}。",
        f"- Confirmatory receipt evidence=`{completion.get('confirmatory_evidence_status')}`；complete cohorts={integer(completion.get('confirmatory_complete_cohort_count'))}；partial cohorts={integer(completion.get('confirmatory_partial_cohort_count'))}。",
        "- 31 个新 live experiment groups、P0.5-07 formal no-op，以及 P0-01/P0-02/P0.5-31/P0-15 既有证据均单独索引；任何缺失/失败均保持显式，不伪装完整。",
        "- 本任务集没有独立 SafetyGate。所有结果只是固定 10 题 mini 调参诊断，不能自动推广 winner 或直接改写默认配置。",
        f"- Screening 调度=`{schedule.get('design_label')}`、schedule evidence=`{schedule.get('valid')}`、strict task interleaving=`False`。Screening 是三 replay E0 锚定的整臂串行近似；任何逐题 AB/BA 结果只在下方认证 confirmatory cohort 中报告。",
        f"- `P0-20-E3`=`{c3.get('status') or 'mini_diagnostic_only'}`，不得作为降本链 C3 晋级证据。它位于 R1 anchor 后的近邻串行 tranche（ordinal gap={c3.get('schedule_ordinal_gap', 'unknown')}）以减小时漂，但不满足源计划要求的逐题 E0/候选交错。",
        "- source control 使用 live Analyzer；三个 replay controls 与所有 frozen candidates 使用同一 frozen-replay Analyzer mode，跨 mode 不生成 paired 比较。",
        "",
        "## 调度、E0 漂移与时间局限",
        "",
        f"- Schedule SHA=`{schedule.get('schedule_sha256')}`；status SHA=`{schedule.get('status_schedule_sha256')}`；source=`{controls.get('source_arm_id')}`；replay controls={code_json(controls.get('replay_control_arm_ids') or [])}。",
        f"- {account_window_design} 候选与 anchor 的 lag 如实列出，ΔQ 仍可能包含 provider/time drift。",
        f"- Schedule validation reasons={code_json(schedule.get('reasons') or [])}。",
        "",
        "### Frozen-replay E0 漂移",
        "",
        "| Later - earlier | Pairs | Mean ΔQ | 95% CI | W/T/L |",
        "|---|---:|---:|---|---|",
    ]
    for comparison in drift.get("comparisons") or []:
        low, high = comparison.get("bootstrap_ci95") or [None, None]
        lines.append(
            f"| {comparison.get('variant_arm_id')} - {comparison.get('control_arm_id')} | "
            f"{integer(comparison.get('pair_count'))} | {fmt(comparison.get('mean_delta_quality'), 4)} | "
            f"[{fmt(low, 4)}, {fmt(high, 4)}] | {integer(comparison.get('wins'))}/"
            f"{integer(comparison.get('ties'))}/{integer(comparison.get('losses'))} |"
        )
    if not drift.get("comparisons"):
        lines.append("| — | 0 | — | — | — |")
    lines.extend(
        [
            "",
            "### 每臂 anchor lag",
            "",
            "| Ordinal | Arm | Anchor/control | Start | Anchor complete | Lag s |",
            "|---:|---|---|---|---|---:|",
        ]
    )
    timing_rows = schedule.get("arm_timing") if isinstance(schedule.get("arm_timing"), Mapping) else {}
    for arm_id, timing in sorted(
        timing_rows.items(),
        key=lambda item: integer(item[1].get("schedule_ordinal")) if isinstance(item[1], Mapping) else 0,
    ):
        timing = timing if isinstance(timing, Mapping) else {}
        lines.append(
            f"| {integer(timing.get('schedule_ordinal'))} | {arm_id} | `{timing.get('anchor_arm_id') or 'missing'}` | "
            f"`{timing.get('started_at') or 'not-started'}` | `{timing.get('anchor_completed_at') or 'not-complete'}` | "
            f"{fmt(timing.get('anchor_lag_seconds'), 1)} |"
        )
    lines.extend(confirmatory_markdown_lines(report))
    lines.extend(
        [
        "",
        "## 全组完成矩阵",
        "",
        "| Group | Variable | State | Variant arms | Arm states | Complete pairings | Mean ΔQ list | Report |",
        "|---|---|---|---:|---|---:|---|---|",
        ]
    )
    for experiment_id in sorted(
        experiments, key=lambda item: (item.replace("P0.5", "P0.50"), item)
    ):
        lines.append(experiment_summary_line(experiments[experiment_id]))
    lines.extend(
        [
            "",
            "## 所有唯一 live arm 总表",
            "",
            METRIC_HEADER,
        ]
    )
    for arm_id in sorted(arms):
        lines.append(metric_table_row(arms[arm_id]))
    lines.extend(
        [
            "",
            "Selected generation 费用严格采用最终 selected attempt：actual USD 优先，缺 USD 才用冻结 79 模型 input/output/cache token 补价；Judge、失败/被替换 generation retry 排除。无 USD 且无 token 的请求金额忽略并计入下界。Judge 理论费用、账户实际窗口增量与 selected generation 分列，互不叠加。",
            "",
            "## 唯一 arm 成本（不重复累计共享 E0）",
            "",
            "| Scope | Counted USD | Complete | Estimated requests | Ignored requests | Note |",
            "|---|---:|---|---:|---:|---|",
            f"| Selected generation theoretical | {fmt(costs.get('selected_generation_counted_usd'), 6)} | `{costs.get('selected_generation_complete')}` | {integer(costs.get('selected_generation_estimated_requests'))} | {integer(costs.get('selected_generation_ignored_requests'))} | final selected only; Judge/replaced retry excluded |",
            f"| Judge theoretical | {fmt(costs.get('judge_counted_usd'), 6)} | `{costs.get('judge_complete')}` | {integer(costs.get('judge_estimated_requests'))} | {integer(costs.get('judge_ignored_requests'))} | all Judge physical attempts, separate |",
            f"| Campaign account actual | {fmt(costs.get('account_delta_usd'), 6)} | `{costs.get('account_windows_complete')}` | — | — | includes Judge; never add to theoretical totals |",
            f"| Campaign BYOK delta | {fmt(costs.get('byok_delta_usd'), 6)} | — | — | — | policy/audit evidence, not execution status |",
            "",
            (
                "source 与三个 replay control 均只按唯一 arm 各计一次；它们虽被多个实验组引用，绝不按引用次数重复累计。"
                + (
                    " Paired cohort 的账户实际增量按 cohort_id 去重，control/candidate 不会双算。"
                    if paired_cohort_count
                    else ""
                )
            ),
            "",
            "## 重复实验配对",
            "",
            f"P0.5-11 和 P0.5-36 的三次 E1 分别配对动态冻结的 replay controls {code_json(controls.get('replay_control_arm_ids') or [])}；比较前强制 analyzer_mode 相同。另以每题三次 ΔQ 的均值形成最多 10 个独立 task-level delta，再做固定 seed、20,000 次 paired bootstrap，没有把 30 个相关观测伪装成 30 个独立题目。P0.5-11 当前不支持/不发送 model sampling seed，不能精确重放；P0.5-36 仅纳入 trace candidate-order seed 与配置 0/1/4 一致且实际进入 aggregation 的 task slice。",
            "",
            "| Group | Replicates | Tasks | Mean ΔQ | 95% CI | W/T/L |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for experiment_id, experiment in experiments.items():
        repeated = experiment.get("repeated_pairing")
        if not isinstance(repeated, Mapping):
            continue
        low, high = repeated.get("bootstrap_ci95") or [None, None]
        lines.append(
            f"| {experiment_id} | {integer(repeated.get('replicate_count'))} | {integer(repeated.get('task_count'))} | "
            f"{fmt(repeated.get('mean_delta_quality'), 4)} | [{fmt(low, 4)}, {fmt(high, 4)}] | "
            f"{integer(repeated.get('wins'))}/{integer(repeated.get('ties'))}/{integer(repeated.get('losses'))} |"
        )
    lines.extend(
        [
            "",
            "## Execution / Policy / Audit",
            "",
            "- `execution` 只回答是否形成可 Judge 的可用答案；`policy` 记录 BYOK/provider/路由政策；`audit` 记录物理请求、usage、费用与 artifact 完整性。三者不会互相覆盖。",
            "- BYOK 或缺失美元金额不会单独把任务判为 execution failure；有 token 时按冻结价格估算，无 token 时忽略金额并披露。",
            "- no-op receipt 证明 wire 字节等价的组不发 live 请求；它不是失败，也不产生虚构的 0 美元实验结果。",
            "",
            "## 冻结与取证",
            "",
            f"- Campaign plan：`{report['paths']['campaign_plan']}`；SHA-256=`{report['campaign_plan_file_sha256']}`；canonical=`{report['campaign_plan_sha256']}`。",
            f"- Immutable terminal status input：`{report['paths']['terminal_status_input']}`；raw SHA-256=`{report['terminal_status_input_file_sha256']}`；semantic=`{report['terminal_status_input_sha256']}`。",
            f"- Derived plan：`{report['paths'].get('derived_plan')}`；valid=`{report['derived'].get('valid')}`；reasons={code_json(report['derived'].get('reasons') or [])}。",
            f"- Frozen price registry：`{report['price_registry']['path']}`；SHA-256=`{report['price_registry']['sha256']}`；models={report['price_registry']['model_count']}。",
            "",
            "机器可读综合结果见同目录 `EXPERIMENT_RESULTS.json`。本报告离线生成，不调用模型，不修改任何 Wave/arm 正式 artifact。",
        ]
    )
    return "\n".join(lines) + "\n"


def compact_arm_for_json(arm: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arm.items() if key not in {"manifest", "audit", "proof"}}


def build_group_json(
    experiment: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    derived: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": GROUP_REPORT_SCHEMA,
        "generated_at": generated_at,
        "run_id": plan.get("run_id"),
        "experiment_id": experiment.get("experiment_id"),
        "controller_phase": status.get("phase"),
        "campaign_plan_sha256": canonical_sha256(plan),
        "derived_plan_sha256": derived.get("derived_plan_sha256"),
        "mini_is_diagnostic_only": True,
        "independent_safety_gate_available": False,
        "experiment": experiment,
    }
    document["group_report_sha256"] = "sha256:" + canonical_sha256(document)
    return document


def compute_unique_costs(arms: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    formal_items = [
        (arm_id, arm)
        for arm_id, arm in arms.items()
        if arm.get("formal_evidence_valid") is True and arm.get("rows")
    ]
    formal = [arm for _, arm in formal_items]
    generation_values = [
        number(arm.get("metrics", {}).get("selected_generation_cost_counted_usd"))
        for arm in formal
        if number(arm.get("metrics", {}).get("selected_generation_cost_counted_usd")) is not None
    ]
    judge_values = [
        number(arm.get("metrics", {}).get("judge_cost_counted_usd"))
        for arm in formal
        if number(arm.get("metrics", {}).get("judge_cost_counted_usd")) is not None
    ]
    account_groups: dict[str, dict[str, Any]] = {}
    cohort_arm_count = 0
    for arm_id, arm in formal_items:
        account = arm.get("account") if isinstance(arm.get("account"), Mapping) else {}
        cohort = (
            account.get("account_window_cohort")
            if isinstance(account.get("account_window_cohort"), Mapping)
            else None
        )
        if cohort is None:
            key = f"arm:{arm_id}"
            cohort_id = None
            cohort_sha = None
            role = None
        else:
            cohort_arm_count += 1
            cohort_id = str(cohort.get("cohort_id") or "")
            cohort_sha = str(cohort.get("cohort_sha256") or "")
            role = str(cohort.get("role") or "")
            key = f"cohort:{cohort_id}"
        delta = number(account.get("account_delta_usd"))
        byok = number(account.get("byok_delta_usd"))
        current = account_groups.get(key)
        if current is None:
            account_groups[key] = {
                "cohort_id": cohort_id,
                "cohort_sha256": cohort_sha,
                "roles": {role} if role else set(),
                "account_delta_usd": delta,
                "byok_delta_usd": byok,
                "stable": account.get("reconciliation_stable") is True,
                "arm_ids": [arm_id],
            }
            continue
        if (
            current["cohort_sha256"] != cohort_sha
            or delta != current["account_delta_usd"]
            or byok != current["byok_delta_usd"]
            or role in current["roles"]
        ):
            raise ReportError(
                f"account-window cohort {cohort_id!r} has conflicting role/hash/delta evidence"
            )
        current["roles"].add(role)
        current["stable"] = current["stable"] and account.get("reconciliation_stable") is True
        current["arm_ids"].append(arm_id)

    account_values = [
        value["account_delta_usd"]
        for value in account_groups.values()
        if value["account_delta_usd"] is not None
    ]
    byok_values = [
        value["byok_delta_usd"]
        for value in account_groups.values()
        if value["byok_delta_usd"] is not None
    ]
    account_complete = bool(formal) and all(
        value["account_delta_usd"] is not None
        and value["stable"] is True
        and (
            value["cohort_id"] is None
            or value["roles"] == {"control", "candidate"}
        )
        for value in account_groups.values()
    )
    result = {
        "unique_formal_arm_count": len(formal),
        "selected_generation_counted_usd": sum(
            value for value in generation_values if value is not None
        )
        if generation_values
        else None,
        "selected_generation_complete": bool(formal)
        and all(
            arm.get("metrics", {}).get("selected_generation_cost_complete") is True
            for arm in formal
        ),
        "selected_generation_estimated_requests": sum(
            integer(arm.get("metrics", {}).get("selected_generation_cost_estimated_request_count"))
            for arm in formal
        ),
        "selected_generation_ignored_requests": sum(
            integer(arm.get("metrics", {}).get("selected_generation_cost_ignored_request_count"))
            for arm in formal
        ),
        "judge_counted_usd": sum(value for value in judge_values if value is not None)
        if judge_values
        else None,
        "judge_complete": bool(formal)
        and all(arm.get("metrics", {}).get("judge_cost_complete") is True for arm in formal),
        "judge_estimated_requests": sum(
            integer(arm.get("metrics", {}).get("judge_cost_estimated_request_count"))
            for arm in formal
        ),
        "judge_ignored_requests": sum(
            integer(arm.get("metrics", {}).get("judge_cost_ignored_request_count"))
            for arm in formal
        ),
        "account_delta_usd": sum(value for value in account_values if value is not None)
        if account_values
        else None,
        "byok_delta_usd": sum(value for value in byok_values if value is not None)
        if byok_values
        else None,
        "account_windows_complete": account_complete,
        "note": "unique arms only; shared common E0 is counted once; account actual includes Judge and is never added to theoretical cost",
    }
    if cohort_arm_count:
        result.update(
            {
                "unique_account_window_count": len(account_groups),
                "paired_cohort_count": sum(
                    value["cohort_id"] is not None for value in account_groups.values()
                ),
                "paired_cohort_arm_count": cohort_arm_count,
                "account_window_groups": [
                    {
                        "cohort_id": value["cohort_id"],
                        "cohort_sha256": value["cohort_sha256"],
                        "roles": sorted(value["roles"]),
                        "arm_ids": sorted(value["arm_ids"]),
                        "account_delta_usd": value["account_delta_usd"],
                        "byok_delta_usd": value["byok_delta_usd"],
                        "stable": value["stable"],
                    }
                    for value in sorted(
                        account_groups.values(),
                        key=lambda item: (str(item["cohort_id"] or ""), item["arm_ids"]),
                    )
                ],
                "note": (
                    "selected generation and Judge remain per-arm; shared account-window "
                    "deltas are counted once per authenticated cohort_id"
                ),
            }
        )
    return result


def _confirmatory_task_order_evidence(
    comparison: Mapping[str, Any],
    schedule: Mapping[str, Any],
) -> dict[str, Any]:
    task_schedule = schedule.get("task_schedule")
    order_by_task = {
        str(item.get("task_id") or ""): str(item.get("order") or "")
        for item in task_schedule or []
        if isinstance(item, Mapping)
    }
    task_rows = []
    by_order: dict[str, list[float]] = {"AB": [], "BA": []}
    for raw in comparison.get("task_rows") or []:
        row = dict(raw)
        order = order_by_task.get(str(row.get("task_id") or ""), "unknown")
        row["execution_order"] = order
        task_rows.append(row)
        delta = number(row.get("delta_quality"))
        if order in by_order and delta is not None:
            by_order[order].append(delta)
    result = copy.deepcopy(dict(comparison))
    result["task_rows"] = task_rows
    result["order_balance"] = {
        order: {
            "pair_count": len(values),
            "mean_delta_quality": mean(values),
        }
        for order, values in by_order.items()
    }
    return result


def _partial_confirmatory_entry(
    entry: Mapping[str, Any],
    *,
    reasons: Sequence[str],
) -> dict[str, Any]:
    roles = entry.get("roles") if isinstance(entry.get("roles"), Mapping) else {}
    role_receipts = {
        role: {
            "arm_id": value.get("arm_id"),
            "experiment_id": value.get("experiment_id"),
            "root": value.get("root"),
            "manifest_sha256": value.get("manifest_sha256"),
            "publication_status": value.get("publication_status"),
            "formal_evidence_valid": False,
            "formal_evidence_reasons": ["confirmatory cohort is partial; role not loaded"],
        }
        for role, value in roles.items()
        if role in {"control", "candidate"} and isinstance(value, Mapping)
    }
    return {
        "cohort_id": entry.get("cohort_id"),
        "state": "partial",
        "formal_evidence_valid": False,
        "reasons": list(dict.fromkeys(str(reason) for reason in reasons if str(reason))),
        "status": entry.get("status"),
        "schedule_sha256": entry.get("schedule_sha256"),
        "schedule_path": entry.get("schedule_path"),
        "schedule_file_sha256": entry.get("schedule_file_sha256"),
        "cohort_root": entry.get("cohort_root"),
        "cohort_manifest_path": entry.get("cohort_manifest_path"),
        "cohort_manifest_sha256": entry.get("cohort_manifest_sha256"),
        "account_window_cohort_sha256": entry.get("account_window_cohort_sha256"),
        "entry_sha256": entry.get("entry_sha256"),
        "failure": copy.deepcopy(entry.get("failure")),
        "roles": role_receipts,
        "comparison": None,
        "costs": compute_unique_costs({}),
    }


def load_confirmatory_report_input_entry(
    entry: Mapping[str, Any],
    *,
    specs_by_id: Mapping[str, ArmSpec],
    prices: Mapping[str, Price],
    plan: Mapping[str, Any],
    plan_sha256: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Authenticate and load exactly one receipt-declared confirmatory cohort."""

    reasons: list[str] = []
    cohort_id = str(entry.get("cohort_id") or "")
    safe_chars = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    if (
        not 1 <= len(cohort_id) <= 128
        or not cohort_id[0].isalnum()
        or any(char not in safe_chars for char in cohort_id)
    ):
        reasons.append("confirmatory cohort id is invalid")
    if not validate_embedded_hash(entry, "entry_sha256", prefixed=False):
        reasons.append("confirmatory report-input entry self-hash differs")
    if entry.get("campaign_plan_sha256") != plan_sha256:
        reasons.append("confirmatory report-input entry is bound to another campaign plan")
    roles = entry.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != {"control", "candidate"}:
        reasons.append("confirmatory report-input entry roles are incomplete")
        roles = {}
    else:
        for role in ("control", "candidate"):
            if not isinstance(roles.get(role), Mapping):
                reasons.append(f"confirmatory {role} receipt is malformed")
    status = str(entry.get("status") or "")
    if status not in {"complete", "partial"}:
        reasons.append("confirmatory report-input entry status is invalid")
    if status == "partial":
        failure = entry.get("failure")
        if isinstance(failure, Mapping) and failure.get("reason"):
            reasons.append(f"launcher failure: {failure.get('reason')}")
        for role in ("control", "candidate"):
            receipt = roles.get(role) if isinstance(roles.get(role), Mapping) else {}
            publication_status = str(receipt.get("publication_status") or "missing")
            if publication_status != "complete":
                reasons.append(f"{role} publication_status={publication_status}")
        if not reasons:
            reasons.append("controller declared a partial confirmatory cohort")
        return _partial_confirmatory_entry(entry, reasons=reasons), {}
    if reasons:
        return _partial_confirmatory_entry(entry, reasons=reasons), {}

    schedule_sha = raw_sha256(entry.get("schedule_sha256"))
    schedule_file_sha = raw_sha256(entry.get("schedule_file_sha256"))
    cohort_manifest_sha = raw_sha256(entry.get("cohort_manifest_sha256"))
    account_cohort_sha = prefixed_sha256(entry.get("account_window_cohort_sha256"))
    if schedule_sha is None:
        reasons.append("confirmatory schedule semantic hash is invalid")
    if schedule_file_sha is None:
        reasons.append("confirmatory schedule file hash is invalid")
    if cohort_manifest_sha is None:
        reasons.append("confirmatory cohort manifest file hash is invalid")
    if account_cohort_sha is None:
        reasons.append("confirmatory account-window cohort hash is invalid")
    try:
        cohort_root = absolute_receipt_path(entry.get("cohort_root"), label="cohort root")
        regular_directory(cohort_root)
        schedule_path = absolute_receipt_path(
            entry.get("schedule_path"), label="confirmatory schedule"
        )
        cohort_manifest_path = absolute_receipt_path(
            entry.get("cohort_manifest_path"), label="confirmatory cohort manifest"
        )
    except ReportError as exc:
        reasons.append(str(exc))
        return _partial_confirmatory_entry(entry, reasons=reasons), {}
    if schedule_path != cohort_root / "archive" / "confirmatory-schedule.json":
        reasons.append("confirmatory schedule path is outside its fixed cohort location")
    if cohort_manifest_path != cohort_root / "cohort-manifest.json":
        reasons.append("confirmatory cohort manifest path is outside its fixed cohort location")
    try:
        regular_file(schedule_path)
        regular_file(cohort_manifest_path)
    except ReportError as exc:
        reasons.append(str(exc))
    if schedule_file_sha is not None and file_sha256(schedule_path) != schedule_file_sha:
        reasons.append("confirmatory schedule file hash differs from receipt")
    if cohort_manifest_sha is not None and file_sha256(cohort_manifest_path) != cohort_manifest_sha:
        reasons.append("confirmatory cohort manifest file hash differs from receipt")
    if reasons:
        return _partial_confirmatory_entry(entry, reasons=reasons), {}

    schedule = load_json(schedule_path)
    if schedule.get("schema") != CONFIRMATORY_SCHEDULE_SCHEMA:
        reasons.append("confirmatory schedule schema differs")
    if not validate_embedded_hash(schedule, "schedule_sha256", prefixed=False):
        reasons.append("confirmatory schedule self-hash differs")
    if schedule.get("schedule_sha256") != schedule_sha:
        reasons.append("confirmatory schedule hash differs from receipt")
    if schedule.get("campaign_plan_sha256") != plan_sha256:
        reasons.append("confirmatory schedule is bound to another campaign plan")
    if schedule.get("cohort_id") != cohort_id or schedule.get("output_name") != cohort_id:
        reasons.append("confirmatory schedule cohort identity differs")
    benchmark_task_ids = [
        str(value) for value in plan.get("benchmark", {}).get("task_ids") or []
    ]
    schedule_task_ids = [
        str(item.get("task_id") or "")
        for item in schedule.get("task_schedule") or []
        if isinstance(item, Mapping)
    ]
    if (
        len(schedule_task_ids) != 10
        or schedule_task_ids != benchmark_task_ids
        or len(set(schedule_task_ids)) != 10
    ):
        reasons.append("confirmatory schedule task order differs from the frozen benchmark")
    schedule_orders = [
        str(item.get("order") or "")
        for item in schedule.get("task_schedule") or []
        if isinstance(item, Mapping)
    ]
    if Counter(schedule_orders) != Counter({"AB": 5, "BA": 5}):
        reasons.append("confirmatory schedule is not balanced 5 AB / 5 BA")

    cohort_manifest = load_json(cohort_manifest_path)
    if cohort_manifest.get("schema") != CONFIRMATORY_COHORT_MANIFEST_SCHEMA:
        reasons.append("confirmatory cohort manifest schema differs")
    if not validate_embedded_hash(cohort_manifest, "manifest_sha256", prefixed=False):
        reasons.append("confirmatory cohort manifest self-hash differs")
    if cohort_manifest.get("status") != "complete":
        reasons.append("confirmatory cohort manifest is not complete")
    if cohort_manifest.get("cohort_id") != cohort_id:
        reasons.append("confirmatory cohort manifest id differs")
    if cohort_manifest.get("schedule_sha256") != schedule_sha:
        reasons.append("confirmatory cohort manifest schedule hash differs")
    if cohort_manifest.get("account_delta_report_scope") != "paired_cohort_once":
        reasons.append("confirmatory cohort account-delta scope differs")
    if cohort_manifest.get("screening_is_diagnostic_only") is not True:
        reasons.append("confirmatory cohort diagnostic-only declaration differs")
    manifest_roles = cohort_manifest.get("roles")
    if not isinstance(manifest_roles, Mapping) or set(manifest_roles) != {
        "control",
        "candidate",
    }:
        reasons.append("confirmatory cohort manifest roles are incomplete")
        manifest_roles = {}

    schedule_roles = schedule.get("roles")
    if not isinstance(schedule_roles, Mapping) or set(schedule_roles) != {
        "control",
        "candidate",
    }:
        reasons.append("confirmatory schedule roles are incomplete")
        schedule_roles = {}
    role_context: dict[str, tuple[ArmSpec, Mapping[str, Any], Path]] = {}
    for role in ("control", "candidate"):
        receipt = roles.get(role) if isinstance(roles.get(role), Mapping) else {}
        if receipt.get("publication_status") != "complete":
            reasons.append(f"confirmatory {role} publication is not complete")
            continue
        arm_id = str(receipt.get("arm_id") or "")
        experiment_id = str(receipt.get("experiment_id") or "")
        spec = specs_by_id.get(arm_id)
        if spec is None:
            reasons.append(f"confirmatory {role} arm is absent from the frozen plan")
            continue
        if spec.experiment_id != experiment_id:
            reasons.append(f"confirmatory {role} experiment identity differs from the plan")
        schedule_role = (
            schedule_roles.get(role) if isinstance(schedule_roles.get(role), Mapping) else {}
        )
        if (
            schedule_role.get("arm_id") != arm_id
            or schedule_role.get("experiment_id") != experiment_id
            or schedule_role.get("analyzer_mode") != spec.analyzer_mode
        ):
            reasons.append(f"confirmatory {role} schedule identity differs from receipt")
        manifest_sha = raw_sha256(receipt.get("manifest_sha256"))
        if manifest_sha is None:
            reasons.append(f"confirmatory {role} manifest hash is invalid")
            continue
        try:
            root = absolute_receipt_path(receipt.get("root"), label=f"{role} root")
            regular_directory(root)
        except ReportError as exc:
            reasons.append(str(exc))
            continue
        if root != cohort_root / role:
            reasons.append(f"confirmatory {role} root is outside its fixed cohort location")
        formal_manifest_path = root / "manifest.json"
        try:
            regular_file(formal_manifest_path)
        except ReportError as exc:
            reasons.append(str(exc))
            continue
        if file_sha256(formal_manifest_path) != manifest_sha:
            reasons.append(f"confirmatory {role} manifest hash differs from receipt")
        manifest_role = (
            manifest_roles.get(role)
            if isinstance(manifest_roles.get(role), Mapping)
            else {}
        )
        relative_path = Path(str(manifest_role.get("path") or ""))
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or cohort_root / relative_path != formal_manifest_path
            or manifest_role.get("sha256") != manifest_sha
        ):
            reasons.append(f"confirmatory {role} cohort-manifest binding differs")
        role_context[role] = (spec, receipt, root)
    if set(role_context) == {"control", "candidate"}:
        control_spec = role_context["control"][0]
        candidate_spec = role_context["candidate"][0]
        if (
            candidate_spec.control_arm_id != control_spec.arm_id
            or candidate_spec.analyzer_mode != control_spec.analyzer_mode
        ):
            reasons.append("confirmatory control/candidate plan binding differs")
    if reasons:
        return _partial_confirmatory_entry(entry, reasons=reasons), {}

    internal_roles: dict[str, dict[str, Any]] = {}
    for role, (spec, receipt, root) in role_context.items():
        internal_roles[role] = load_confirmatory_formal_role(
            role=role,
            receipt=receipt,
            spec=spec,
            root=root,
            prices=prices,
            plan=plan,
            cohort_id=cohort_id,
            expected_cohort_sha256=str(account_cohort_sha),
        )
        reasons.extend(internal_roles[role].get("formal_evidence_reasons") or [])
    if all(role in internal_roles for role in ("control", "candidate")):
        descriptors = {
            role: internal_roles[role].get("account", {}).get(
                "account_window_cohort"
            )
            for role in ("control", "candidate")
        }
        if any(not isinstance(value, Mapping) for value in descriptors.values()):
            reasons.append("confirmatory role account-window cohort evidence is missing")
        else:
            control_descriptor = descriptors["control"]
            candidate_descriptor = descriptors["candidate"]
            if (
                control_descriptor.get("members") != candidate_descriptor.get("members")
                or control_descriptor.get("account_evidence")
                != candidate_descriptor.get("account_evidence")
                or control_descriptor.get("cohort_sha256")
                != candidate_descriptor.get("cohort_sha256")
            ):
                reasons.append("confirmatory role cohort descriptors differ")
    comparison: dict[str, Any] | None = None
    costs = compute_unique_costs({})
    if not reasons:
        try:
            comparison = _confirmatory_task_order_evidence(
                paired(internal_roles["control"], internal_roles["candidate"]),
                schedule,
            )
            if comparison.get("pair_count") != 10:
                reasons.append("confirmatory paired comparison does not cover all ten tasks")
            costs = compute_unique_costs(internal_roles)
            if costs.get("account_windows_complete") is not True:
                reasons.append("confirmatory cohort account window is incomplete")
        except ReportError as exc:
            reasons.append(str(exc))
    if reasons:
        partial = _partial_confirmatory_entry(entry, reasons=reasons)
        partial["roles"] = {
            role: compact_arm_for_json(value) for role, value in internal_roles.items()
        }
        partial["comparison"] = comparison
        partial["costs"] = costs
        return partial, {}
    public = {
        "cohort_id": cohort_id,
        "state": "complete",
        "formal_evidence_valid": True,
        "reasons": [],
        "status": status,
        "schedule_sha256": schedule_sha,
        "schedule_path": str(schedule_path),
        "schedule_file_sha256": schedule_file_sha,
        "schedule": {
            "seed": schedule.get("seed"),
            "task_schedule": copy.deepcopy(schedule.get("task_schedule") or []),
            "order_balance": copy.deepcopy(schedule.get("order_balance") or {}),
            "execution_contract": copy.deepcopy(schedule.get("execution_contract") or {}),
        },
        "cohort_root": str(cohort_root),
        "cohort_manifest_path": str(cohort_manifest_path),
        "cohort_manifest_sha256": cohort_manifest_sha,
        "account_window_cohort_sha256": account_cohort_sha,
        "entry_sha256": entry.get("entry_sha256"),
        "failure": None,
        "roles": {
            role: compact_arm_for_json(value) for role, value in internal_roles.items()
        },
        "comparison": comparison,
        "costs": costs,
    }
    cost_arms = {
        f"confirmatory:{cohort_id}:{role}": value
        for role, value in internal_roles.items()
    }
    return public, cost_arms


def load_confirmatory_report_inputs(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    plan_sha256: str,
    specs: Sequence[ArmSpec],
    prices: Mapping[str, Price],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load only the controller-published fixed receipt; never scan directories."""

    index_path = run_root / CONFIRMATORY_REPORT_INPUT_INDEX_NAME
    try:
        mode = index_path.lstat().st_mode
    except FileNotFoundError:
        return {
            "status": "absent",
            "available": False,
            "valid": True,
            "path": str(index_path),
            "cohort_count": 0,
            "complete_cohort_count": 0,
            "partial_cohort_count": 0,
            "cohorts": {},
        }, {}
    except OSError as exc:
        raise ReportError(f"cannot inspect confirmatory report-input index: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReportError("confirmatory report-input index is not a regular non-symlink file")
    index = load_json(index_path)
    if index.get("schema") != CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA:
        raise ReportError("confirmatory report-input index schema differs")
    if index.get("campaign_plan_sha256") != plan_sha256:
        raise ReportError("confirmatory report-input index is bound to another campaign plan")
    if not validate_embedded_hash(index, "index_sha256", prefixed=False):
        raise ReportError("confirmatory report-input index self-hash differs")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ReportError("confirmatory report-input index entries are malformed")
    if any(not isinstance(entry, Mapping) for entry in entries):
        raise ReportError("confirmatory report-input index entry is not an object")
    cohort_ids = [str(entry.get("cohort_id") or "") for entry in entries]
    if cohort_ids != sorted(cohort_ids) or len(cohort_ids) != len(set(cohort_ids)):
        raise ReportError("confirmatory report-input entries are not uniquely cohort-sorted")
    specs_by_id = {spec.arm_id: spec for spec in specs}
    cohorts: dict[str, Any] = {}
    cost_arms: dict[str, dict[str, Any]] = {}
    for entry in entries:
        cohort, entry_cost_arms = load_confirmatory_report_input_entry(
            entry,
            specs_by_id=specs_by_id,
            prices=prices,
            plan=plan,
            plan_sha256=plan_sha256,
        )
        key = str(cohort.get("cohort_id") or f"invalid-{len(cohorts) + 1}")
        cohorts[key] = cohort
        cost_arms.update(entry_cost_arms)
    complete_count = sum(
        cohort.get("state") == "complete" for cohort in cohorts.values()
    )
    partial_count = len(cohorts) - complete_count
    status = "complete" if entries and not partial_count else "partial"
    return {
        "schema": CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA,
        "status": status,
        "available": True,
        "valid": status == "complete",
        "path": str(index_path),
        "file_sha256": file_sha256(index_path),
        "index_sha256": index.get("index_sha256"),
        "campaign_plan_sha256": index.get("campaign_plan_sha256"),
        "cohort_count": len(cohorts),
        "complete_cohort_count": complete_count,
        "partial_cohort_count": partial_count,
        "cohorts": cohorts,
    }, cost_arms


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("/home/codex/draco-runs/p0-p05-20260804-234508/campaign-plan.json"),
    )
    parser.add_argument("--status", type=Path)
    parser.add_argument("--derived-plan", type=Path)
    parser.add_argument("--price-registry", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--allow-nonterminal", action="store_true")
    parser.add_argument(
        "--strict", action="store_true", help="return 2 when completion/evidence is partial"
    )
    return parser.parse_args(argv)


def generate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    plan = load_json(args.plan)
    if plan.get("schema") != PLAN_SCHEMA:
        raise ReportError(f"campaign plan schema differs: {plan.get('schema')}")
    plan_sha = canonical_sha256(plan)
    run_root = Path(str(plan["paths"]["run_root"]))
    status_path = args.status or run_root / (
        "status.json" if args.allow_nonterminal else "terminal-status-input.json"
    )
    status = load_json(status_path)
    if status.get("schema") != STATUS_SCHEMA:
        raise ReportError(f"controller status schema differs: {status.get('schema')}")
    if status.get("campaign_plan_sha256") != plan_sha:
        raise ReportError("controller status is bound to another campaign plan")
    phase = str(status.get("phase") or "unknown")
    if phase not in TERMINAL_PHASES and not args.allow_nonterminal:
        raise ReportError(f"controller is not terminal: {phase}")
    if not args.allow_nonterminal:
        terminal_freeze = (
            status.get("terminal_freeze")
            if isinstance(status.get("terminal_freeze"), Mapping)
            else {}
        )
        if not validate_embedded_hash(status, "terminal_status_input_sha256", prefixed=False):
            raise ReportError("terminal status input self-hash differs")
        if (
            terminal_freeze.get("schema") != TERMINAL_STATUS_INPUT_SCHEMA
            or terminal_freeze.get("campaign_plan_sha256") != plan_sha
            or terminal_freeze.get("run_id") != plan.get("run_id")
            or terminal_freeze.get("phase") != phase
        ):
            raise ReportError("terminal status input freeze contract differs")
    specs = expand_arms(plan)
    schedule_evidence = validate_schedule_evidence(plan, status, specs)
    canonical_derived_path = run_root / "derived-plan.json"
    if (
        args.derived_plan is not None
        and args.derived_plan.resolve() != canonical_derived_path.resolve()
    ):
        raise ReportError(
            "--derived-plan must name the frozen controller run_root/derived-plan.json"
        )
    verifier = load_frozen_controller_verifier(plan, plan_sha256=plan_sha)
    derived = load_derived_evidence(plan, status, verifier=verifier)
    state_map = status.get("arms") if isinstance(status.get("arms"), Mapping) else {}
    registry_path = (
        args.price_registry or Path(str(plan["paths"]["snapshot"])) / PRICE_REGISTRY_RELATIVE
    )
    registry_contract = (
        plan.get("freeze", {}).get("model_registry")
        if isinstance(plan.get("freeze"), Mapping)
        else None
    )
    prices, price_metadata = load_prices(registry_path, registry_contract)
    if price_metadata["model_count"] != 79:
        raise ReportError(
            f"frozen price registry must contain 79 models, got {price_metadata['model_count']}"
        )
    arms: dict[str, dict[str, Any]] = {}
    for spec in specs:
        raw_state = state_map.get(spec.arm_id)
        if not isinstance(raw_state, Mapping):
            raw_state = {"state": "missing_from_status"}
        arms[spec.arm_id] = load_formal_arm(
            spec,
            raw_state,
            prices,
            plan,
            derived,
            verifier,
        )
        arms[spec.arm_id]["schedule"] = copy.deepcopy(
            schedule_evidence.get("arm_timing", {}).get(spec.arm_id, {})
        )
    confirmatory_cohorts, confirmatory_cost_arms = load_confirmatory_report_inputs(
        run_root=run_root,
        plan=plan,
        plan_sha256=plan_sha,
        specs=specs,
        prices=prices,
    )
    c3_evidence = p0_20_e3_promotion_evidence(schedule_evidence)
    if "P0-20-E3" in arms:
        arms["P0-20-E3"]["c3_promotion_evidence"] = c3_evidence
    legacy = validate_legacy_evidence(plan)
    experiments = build_experiment_inventory(plan, status, arms, derived, legacy)
    active_experiment_ids = [
        str(experiment.get("id") or "")
        for experiment in plan.get("experiments") or []
        if isinstance(experiment, Mapping)
    ]
    comparison_evidence_invalid_experiment_ids = sorted(
        experiment_id
        for experiment_id in active_experiment_ids
        if not experiment_id
        or experiments.get(experiment_id, {}).get("comparison_evidence_valid") is not True
    )
    comparison_evidence_valid = not comparison_evidence_invalid_experiment_ids
    arm_states = [str(arm.get("state") or "unknown") for arm in arms.values()]
    terminal_arms = all(state in TERMINAL_ARM_STATES for state in arm_states)
    succeeded = [arm for arm in arms.values() if arm.get("state") == "succeeded"]
    formal_valid = all(arm.get("formal_evidence_valid") is True for arm in succeeded)
    no_op_arms = [arm for arm in arms.values() if arm.get("state") == "no_op_deleted"]
    no_op_valid = all(offline_noop_evidence_valid(arm, derived) for arm in no_op_arms)
    declared_noop_valid = all(
        experiments.get(str(item.get("id")), {}).get("state") == "no_op_deleted"
        for item in plan.get("no_op_experiments") or []
        if isinstance(item, Mapping)
    )
    legacy_valid = all(entry.get("valid") is True for entry in legacy.values())
    failures = sum(state in {"failed", "blocked_prerequisite"} for state in arm_states)
    report_status = (
        "complete"
        if phase == "succeeded"
        and terminal_arms
        and formal_valid
        and no_op_valid
        and declared_noop_valid
        and legacy_valid
        and derived.get("valid") is True
        and schedule_evidence.get("valid") is True
        and comparison_evidence_valid
        and confirmatory_cohorts.get("valid") is True
        else "partial_or_failed"
        if phase in TERMINAL_PHASES
        else "nonterminal_snapshot"
    )
    output_root = args.output_root or Path(str(plan["paths"]["report_root"]))
    completion = {
        "status": report_status,
        "controller_terminal": phase in TERMINAL_PHASES,
        "all_arms_terminal": terminal_arms,
        "formal_evidence_valid": formal_valid,
        "no_op_evidence_valid": no_op_valid and declared_noop_valid,
        "legacy_evidence_valid": legacy_valid,
        "derived_evidence_valid": derived.get("valid") is True,
        "schedule_evidence_valid": schedule_evidence.get("valid") is True,
        "comparison_evidence_valid": comparison_evidence_valid,
        "comparison_evidence_invalid_experiment_ids": (
            comparison_evidence_invalid_experiment_ids
        ),
        "confirmatory_evidence_status": confirmatory_cohorts.get("status"),
        "confirmatory_evidence_valid": confirmatory_cohorts.get("valid") is True,
        "confirmatory_cohort_count": confirmatory_cohorts.get("cohort_count"),
        "confirmatory_complete_cohort_count": confirmatory_cohorts.get(
            "complete_cohort_count"
        ),
        "confirmatory_partial_cohort_count": confirmatory_cohorts.get(
            "partial_cohort_count"
        ),
        "planned_arm_count": len(arms),
        "formal_succeeded_arm_count": len(succeeded),
        "wire_no_op_arm_count": len(no_op_arms),
        "failed_or_blocked_arm_count": failures,
        "pending_or_running_arm_count": sum(
            state not in TERMINAL_ARM_STATES for state in arm_states
        ),
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": now_iso(),
        "run_id": plan.get("run_id"),
        "controller_phase": phase,
        "campaign_plan_sha256": plan_sha,
        "campaign_plan_file_sha256": file_sha256(args.plan),
        "frozen_controller": {
            "path": str(verifier.path),
            "raw_sha256": verifier.raw_sha256,
            "runtime_freeze_valid": True,
            "derived_authenticated": verifier.derived is not None,
            "derived_error": verifier.derived_error,
        },
        "terminal_status_input_file_sha256": file_sha256(status_path),
        "terminal_status_input_sha256": status.get("terminal_status_input_sha256"),
        "freeze": dict(plan.get("freeze") or {}),
        "execution": dict(plan.get("execution") or {}),
        "comparison_controls": comparison_control_contract(plan, specs),
        "schedule_evidence": schedule_evidence,
        "replay_control_drift": replay_control_drift(plan, specs, arms),
        "benchmark": dict(plan.get("benchmark") or {}),
        "paths": {
            "campaign_plan": str(args.plan),
            "terminal_status_input": str(status_path),
            "mutable_status": str(run_root / "status.json"),
            "derived_plan": derived.get("path"),
            "confirmatory_report_inputs": confirmatory_cohorts.get("path"),
            "output_root": str(output_root),
        },
        "price_registry": price_metadata,
        "cost_policy": {
            "selected_generation": "final selected attempt only; actual USD then cache-aware token estimate; ignore and disclose no-money/no-token units",
            "includes_live_analyzer": True,
            "excludes_judge": True,
            "excludes_failed_or_replaced_generation_retries": True,
            "judge_separate": True,
            "account_actual_separate": True,
            "cache_price_fallback": "when frozen registry lacks a cache-specific rate, use normal input rate conservatively and label it",
        },
        "safety_gate": {
            "independent_available": False,
            "note": "DRACO mini does not expose an independent SafetyGate field",
        },
        "promotion": {
            "mini_is_diagnostic_only": True,
            "automatic_winner_promotion": False,
            "P0-20-E3": c3_evidence,
        },
        "completion": completion,
        "derived": derived,
        "legacy_evidence": legacy,
        "confirmatory_cohorts": confirmatory_cohorts,
        "arms": {arm_id: compact_arm_for_json(arm) for arm_id, arm in arms.items()},
        "experiments": experiments,
        "unique_arm_costs": compute_unique_costs(
            {
                **arms,
                **confirmatory_cost_arms,
            }
        ),
    }
    # Group Markdown is written first so the root report can link only to
    # artifacts produced by this exact in-memory evidence snapshot.
    group_artifacts: dict[str, Any] = {}
    for experiment_id, experiment in experiments.items():
        group_root = output_root / str(experiment["directory_name"])
        destination = group_root / "EXPERIMENT_RESULTS.md"
        json_group_destination = group_root / "EXPERIMENT_RESULTS.json"
        content = build_group_markdown(experiment, plan, status, derived)
        atomic_write(destination, content)
        atomic_write_json(
            json_group_destination,
            build_group_json(
                experiment,
                plan=plan,
                status=status,
                derived=derived,
                generated_at=report["generated_at"],
            ),
        )
        group_artifacts[experiment_id] = {
            "markdown": {
                "path": str(destination),
                "sha256": file_sha256(destination),
                "size_bytes": destination.stat().st_size,
            },
            "json": {
                "path": str(json_group_destination),
                "sha256": file_sha256(json_group_destination),
                "size_bytes": json_group_destination.stat().st_size,
            },
        }
    report["group_report_artifacts"] = group_artifacts
    json_destination = output_root / "EXPERIMENT_RESULTS.json"
    markdown_destination = output_root / "EXPERIMENT_RESULTS.md"
    atomic_write(markdown_destination, build_root_markdown(report))
    report["root_artifacts"] = {
        "markdown": {
            "path": str(markdown_destination),
            "sha256": file_sha256(markdown_destination),
            "size_bytes": markdown_destination.stat().st_size,
        },
    }
    report["report_sha256"] = "sha256:" + canonical_sha256(report)
    atomic_write_json(json_destination, report)
    exit_code = 2 if args.strict and report_status != "complete" else 0
    return report, exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report, exit_code = generate(args)
    print(
        json.dumps(
            {
                "status": report["completion"]["status"],
                "run_id": report["run_id"],
                "planned_arm_count": report["completion"]["planned_arm_count"],
                "formal_succeeded_arm_count": report["completion"]["formal_succeeded_arm_count"],
                "output_root": report["paths"]["output_root"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"report error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
