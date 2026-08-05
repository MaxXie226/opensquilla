#!/usr/bin/env python3
"""Materialize an immutable P1 DRACO-mini campaign plan from a clean snapshot."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class BuildError(RuntimeError):
    pass


def _load_controller(snapshot: Path) -> Any:
    path = snapshot / "scripts/experiments/run_draco_p1_tuning_campaign.py"
    spec = importlib.util.spec_from_file_location("_frozen_draco_p1_controller", path)
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot import controller: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(snapshot: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(snapshot), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _task_ids(path: Path) -> list[str]:
    task_ids: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BuildError(f"invalid benchmark row {line_number}") from exc
            task_id = str(row.get("id") or "") if isinstance(row, dict) else ""
            if not task_id or task_id in task_ids:
                raise BuildError(f"benchmark row {line_number} has invalid task id")
            task_ids.append(task_id)
    if len(task_ids) != 10:
        raise BuildError("P1 campaign requires the exact ten-task DRACO mini input")
    return task_ids


def _raw_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _preexisting_source_contract(
    args: argparse.Namespace, *, controller: Any, common: Any
) -> dict[str, Any]:
    source_plan_path = args.e0_source_plan.resolve()
    source_snapshot = args.e0_source_snapshot.resolve()
    source_output = args.e0_source_output.resolve()
    if not source_plan_path.is_file() or source_plan_path.is_symlink():
        raise BuildError("E0 source plan must be a regular file")
    if not source_snapshot.is_dir() or source_snapshot.is_symlink():
        raise BuildError("E0 source snapshot must be a regular directory")
    if not source_output.is_dir() or source_output.is_symlink():
        raise BuildError("E0 source output must be a regular directory")
    try:
        source_plan = json.loads(source_plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("cannot load E0 source plan") from exc
    if not isinstance(source_plan, dict):
        raise BuildError("E0 source plan root must be an object")
    source_identity = common.git_identity(source_snapshot)
    if source_identity.get("status"):
        raise BuildError("E0 source snapshot must be a completely clean Git worktree")
    required_artifacts = ("manifest.json", "results.jsonl", "trace.jsonl")
    for name in required_artifacts:
        path = source_output / name
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"E0 source output lacks regular {name}")
    return {
        "schema": "opensquilla.draco-preexisting-analyzer-source/v1",
        "enabled": True,
        "source_plan_path": str(source_plan_path),
        "source_plan_raw_sha256": _raw_sha256(source_plan_path),
        "source_plan_canonical_sha256": controller.canonical_sha256(source_plan),
        "source_snapshot_path": str(source_snapshot),
        "source_snapshot_commit": source_identity["commit"],
        "source_snapshot_tree": source_identity["tree"],
        "source_output_dir": str(source_output),
        "source_manifest_sha256": _raw_sha256(source_output / "manifest.json"),
        "source_results_sha256": _raw_sha256(source_output / "results.jsonl"),
        "source_trace_sha256": _raw_sha256(source_output / "trace.jsonl"),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = args.snapshot.resolve()
    controller = _load_controller(snapshot)
    common = controller.common_controller(snapshot)
    identity = common.git_identity(snapshot)
    if identity["status"]:
        raise BuildError("snapshot must be a completely clean Git worktree")
    run_root = args.run_root.resolve()
    report_root = args.report_root.resolve()
    reference_repo = args.reference_repo.resolve()
    output = args.output.resolve() if args.output else run_root / "campaign-plan.json"
    if output != run_root / "campaign-plan.json":
        raise BuildError("output must be run_root/campaign-plan.json")
    if output.exists():
        raise BuildError("refusing to overwrite an existing campaign plan")
    benchmark_path = reference_repo / "data/draco/mini.jsonl"
    task_ids = _task_ids(benchmark_path)
    source_doc = args.source_document.resolve()
    source_repo = args.source_repo.resolve()
    try:
        relative_doc = source_doc.relative_to(source_repo)
    except ValueError as exc:
        raise BuildError("source document must be inside source_repo") from exc
    source_commit = _git(source_repo, "rev-parse", "HEAD")
    if _git(source_repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise BuildError("source-plan repository must be completely clean")
    source_blob_line = _git(source_repo, "ls-files", "-s", "--", str(relative_doc))
    source_blob = source_blob_line.split()[1] if source_blob_line else ""
    if len(source_blob) != 40:
        raise BuildError("source document is not tracked by the frozen docs commit")
    if _git(source_repo, "hash-object", str(source_doc)) != source_blob:
        raise BuildError("source document content differs from its frozen Git blob")
    contracts = controller.expected_variant_contracts()
    experiments: list[dict[str, Any]] = []
    for group in sorted(controller.SUPPORTED_GROUPS):
        variants = []
        for _, contract in sorted(contracts.items()):
            if contract["group"] != group:
                continue
            row = {
                "id": contract["variant"],
                "analyzer_mode": contract["analyzer_mode"],
                "override": contract["override"],
                "hit_gate": contract["hit_gate"],
                "priority": contract["priority"],
            }
            if contract["control_arm_id"] is not None:
                row["control_arm_id"] = contract["control_arm_id"]
            variants.append(row)
        experiments.append(
            {
                "id": group,
                "directory_name": group,
                "title": group,
                "variants": variants,
            }
        )
    live = sorted(
        arm_id
        for arm_id, contract in contracts.items()
        if contract["analyzer_mode"] == "live"
    )
    replay = sorted(
        arm_id
        for arm_id, contract in contracts.items()
        if contract["analyzer_mode"] == "frozen_replay"
        and arm_id not in {
            "P1-35-E1",
            "P1-15-E1",
            "P1-15-E2",
            "P1-21-E0",
            "P1-21-E1",
        }
    )
    tranches = {
        control: replay[index :: len(controller.REPLAY_CONTROL_IDS)]
        for index, control in enumerate(controller.REPLAY_CONTROL_IDS)
    }
    tranches["common-E0-R1"] = [
        "P1-35-E1",
        "P1-15-E1",
        "P1-15-E2",
        *tranches["common-E0-R1"],
    ]
    # Natural below-quorum evidence is shared with the source, but a serving
    # control is required before the P1-21 error-policy arm.
    tranches["common-E0-R2"] = [
        "P1-21-E0",
        "P1-21-E1",
        *tranches["common-E0-R2"],
    ]
    order = [controller.SOURCE_ARM_ID, *live]
    anchors: dict[str, str] = {
        controller.SOURCE_ARM_ID: controller.SOURCE_ARM_ID,
        **{arm_id: controller.SOURCE_ARM_ID for arm_id in live},
    }
    for control in controller.REPLAY_CONTROL_IDS:
        order.append(control)
        anchors[control] = control
        for arm_id in tranches[control]:
            order.append(arm_id)
            anchors[arm_id] = (
                arm_id
                if arm_id == "P1-21-E0"
                else "P1-21-E0"
                if arm_id == "P1-21-E1"
                else control
            )
    controls = {
        arm_id: anchor
        for arm_id, anchor in anchors.items()
        if arm_id not in {controller.SOURCE_ARM_ID, *controller.REPLAY_CONTROL_IDS, "P1-21-E0"}
    }
    plan: dict[str, Any] = {
        "schema": controller.PLAN_SCHEMA,
        "run_id": args.run_id,
        "created_at": controller.utc_now(),
        "source_plan": {
            "repository": args.source_repository_label,
            "path": str(relative_doc),
            "commit": source_commit,
            "blob_sha": source_blob,
            "raw_sha256": controller.file_sha256(source_doc),
        },
        "freeze": {
            "snapshot_commit": identity["commit"],
            "snapshot_tree": identity["tree"],
            "ranking_thinking_assignment_enabled": False,
            "inputs": {},
            "sources": {},
            "model_registry": {
                "path": "src/opensquilla/provider/router_dynamic_model_profiles.json"
            },
            "ranking_config": {
                "path": "src/opensquilla/provider/router_dynamic_ranking_config.json"
            },
        },
        "paths": {
            "run_root": str(run_root),
            "snapshot": str(snapshot),
            "report_root": str(report_root),
            "reference_repo": str(reference_repo),
            "python": str(args.python.resolve()),
            "reporter": str(
                snapshot / "scripts/experiments/generate_draco_p1_reports.py"
            ),
            "experiment_config_relative": "configs/benchmarks/draco_b2_g12.json",
            "launcher_relative": "scripts/experiments/run_draco_mini_b0_b1_b2_b4_g1_campaign.sh",
        },
        "benchmark": {
            "name": "DRACO mini",
            "input_sha256": controller.file_sha256(benchmark_path),
            "task_count": len(task_ids),
            "task_ids": task_ids,
            "groups": ["G1"],
        },
        "execution": {
            "serial_arms": True,
            "task_concurrency": 6,
            "judge_concurrency": 6,
            "generation_max_attempts": 3,
            "continue_after_arm_failure": True,
            "restart_policy": "authenticate complete roots; never overwrite incomplete output",
            "controller_unit": args.controller_unit,
            "global_openrouter_lock": str(args.global_openrouter_lock),
            "openrouter_secret_file": str(args.openrouter_secret_file),
            "openrouter_credential_scope": "dedicated_key",
            "schedule": {
                "mode": "hit_gated_serial",
                "arm_order": order,
                "anchor_by_arm_id": anchors,
            },
        },
        "comparison_controls": {
            "source_arm_id": controller.SOURCE_ARM_ID,
            "default_control_arm_id": "common-E0-R1",
            "replay_control_arm_ids": list(controller.REPLAY_CONTROL_IDS),
            "require_same_analyzer_mode": True,
            "arm_control_overrides": controls,
        },
        "common_e0": [
            {
                "arm_id": controller.SOURCE_ARM_ID,
                "variant": "E0-current-G1-C-source",
                "analyzer_mode": "live",
                "override": {},
                "priority": 0,
            },
            *[
                {
                    "arm_id": arm_id,
                    "variant": "E0-current-G1-C-replay",
                    "analyzer_mode": "frozen_replay",
                    "override": {},
                    "priority": 1,
                }
                for arm_id in controller.REPLAY_CONTROL_IDS
            ],
        ],
        "experiments": experiments,
        "excluded": [
            {
                "id": group,
                "kind": "missing_feature",
                "reason": "required runtime feature/schedule/schema is absent",
            }
            for group in sorted(controller.MISSING_FEATURE_GROUPS)
        ]
        + [
            {
                "id": group,
                "kind": "deterministic_no_hit",
                "reason": "the frozen ten-task DRACO mini source cannot reach this path",
            }
            for group in sorted(controller.DETERMINISTIC_NO_HIT_GROUPS)
        ],
        "progression": {
            "schema": "opensquilla.draco-p1-progression/v1",
            "first_arm_id": "P1-35-E1",
            "conditional_arm_ids": ["P1-15-E1", "P1-15-E2"],
            "control_arm_id": "common-E0-R1",
            "skip_p1_15_if_cost_reduction_fraction_at_least": args.p1_35_sufficient_cost_reduction,
            "minimum_mean_delta_quality": 0.0,
        },
        "runtime_contract": {
            "frozen_replay": {
                "schema": "opensquilla.draco.frozen-task-analysis/v2",
                "mode_path": ["g1_routing", "task_analysis_execution", "mode"],
                "mode_value": "frozen_replay",
                "payload_path": ["g1_routing", "task_analysis_execution"],
                "artifact_projection_key": "replay_payload",
                "expected_physical_analyzer_requests": 0,
            },
            "analyzer_source": {
                "schema": "opensquilla.draco-analyzer-source-policy/v1",
                "allow_deterministic_router_fallback": True,
            },
            "preexisting_source": _preexisting_source_contract(
                args, controller=controller, common=common
            ),
            "selected_generation_cost": {
                "priority": [
                    "actual_usd",
                    "cache_aware_token_estimate",
                    "ignored_when_money_and_tokens_missing",
                ],
                "exclude_judge": True,
                "exclude_failed_or_replaced_generation_attempts": True,
            },
        },
        "reporting": {
            "mini_is_diagnostic_only": True,
            "automatic_winner_promotion": False,
            "independent_safety_gate_available": False,
            "pairing_key": "task_id",
            "bootstrap_repetitions": 20_000,
        },
    }
    runtime_freeze = controller.compute_runtime_freeze_identity(plan, snapshot=snapshot)
    for section, value in runtime_freeze.items():
        plan["freeze"][section] = value
    controller.validate_plan(plan, allow_placeholders=False)
    controller.validate_runtime_freeze(
        plan, snapshot=snapshot, expected_snapshot_identity=identity
    )
    # Authenticate the imported E0 publication before creating a new run root.
    # The live Analyzer control is never paid for again by this P1 campaign.
    controller.common_controller(snapshot).authenticate_preexisting_source(plan)
    run_root.mkdir(parents=True, exist_ok=False)
    controller.atomic_write_json(output, plan)
    return plan


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reference-repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--source-document", type=Path, required=True)
    parser.add_argument("--e0-source-plan", type=Path, required=True)
    parser.add_argument("--e0-source-snapshot", type=Path, required=True)
    parser.add_argument("--e0-source-output", type=Path, required=True)
    parser.add_argument(
        "--source-repository-label", default="opensquilla/agentic-routing-docs"
    )
    parser.add_argument("--controller-unit", required=True)
    parser.add_argument("--global-openrouter-lock", type=Path, required=True)
    parser.add_argument("--openrouter-secret-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--p1-35-sufficient-cost-reduction",
        type=float,
        default=0.10,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    plan = build(parse_args(argv))
    print(
        json.dumps(
            {
                "status": "created",
                "run_id": plan["run_id"],
                "campaign_plan_sha256": _load_controller(
                    Path(plan["paths"]["snapshot"])
                ).canonical_sha256(plan),
                "arm_count": len(plan["execution"]["schedule"]["arm_order"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"plan build error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
