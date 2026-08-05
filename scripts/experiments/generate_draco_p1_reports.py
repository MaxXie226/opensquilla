#!/usr/bin/env python3
"""Generate terminal, evidence-bound reports for the P1 DRACO-mini campaign."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "opensquilla.draco-p1-campaign-plan/v1"
STATUS_SCHEMA = "opensquilla.draco-p1-controller-status/v1"
REPORT_SCHEMA = "opensquilla.draco-p1-campaign-report/v1"
TERMINAL_PHASES = {"completed", "completed_with_failures"}


class ReportError(RuntimeError):
    pass


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
        raise ReportError(f"not a regular JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot load JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"JSON root must be an object: {path}")
    return value


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _load_module(path: Path, prefix: str, expected_sha: str) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ReportError(f"frozen module is not a regular file: {path}")
    actual = file_sha256(path)
    if actual != expected_sha:
        raise ReportError(f"frozen module hash differs: {path}")
    name = f"_{prefix}_{actual}"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReportError(f"cannot import frozen module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_frozen_modules(plan: Mapping[str, Any]) -> tuple[Any, Any]:
    snapshot = Path(str(plan["paths"]["snapshot"]))
    sources = plan.get("freeze", {}).get("sources", {})
    if not isinstance(sources, Mapping):
        raise ReportError("freeze.sources is missing")
    controller = _load_module(
        snapshot / "scripts/experiments/run_draco_p1_tuning_campaign.py",
        "draco_p1_controller",
        str(sources.get("controller_raw_sha256") or ""),
    )
    common = _load_module(
        snapshot / "scripts/experiments/generate_draco_p0_p05_reports.py",
        "draco_p1_common_report",
        str(sources.get("common_reporter_raw_sha256") or ""),
    )
    return controller, common


def fmt(value: Any, digits: int = 4) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    ):
        return "—"
    rendered = f"{float(value):.{digits}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def pct(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return f"{100.0 * float(value):.2f}%"


def load_arm(
    plan: Mapping[str, Any],
    status_row: Mapping[str, Any],
    arm: Any,
    *,
    controller: Any,
    common: Any,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    artifact: Mapping[str, Any] | None,
    prices: Mapping[str, Any],
) -> dict[str, Any]:
    state = str(status_row.get("state") or "unknown")
    result: dict[str, Any] = {
        "arm_id": arm.arm_id,
        "experiment_id": arm.experiment_id,
        "variant": arm.variant,
        "analyzer_mode": arm.analyzer_mode,
        "control_arm_id": arm.control_arm_id,
        "state": state,
        "output_dir": str(controller.artifact_dir(plan, arm)),
        "formal": False,
        "formal_evidence_valid": False,
        "formal_evidence_reasons": [],
        "rows": [],
        "metrics": common.summarize_rows([]),
        "statuses": {"execution": "unknown", "policy": "unknown", "audit": "unknown"},
        "account": {},
        "hit_gate_evidence": copy.deepcopy(status_row.get("hit_gate_evidence")),
        "failure": copy.deepcopy(status_row.get("failure")),
    }
    if state != "succeeded":
        return result
    try:
        override = controller.resolve_arm_override(plan, arm, artifact=artifact)
        complete, completion_evidence = controller.inspect_complete_arm(
            plan,
            arm,
            snapshot=snapshot,
            snapshot_identity=snapshot_identity,
            override=override,
        )
    except Exception as exc:  # noqa: BLE001
        result["formal_evidence_reasons"].append(f"frozen controller reinspection failed: {exc}")
        return result
    result["completion_evidence"] = completion_evidence
    if not complete:
        result["formal_evidence_reasons"].append(
            "frozen controller does not authenticate completion"
        )
        return result
    declared = status_row.get("completion_evidence")
    if not isinstance(declared, Mapping) or dict(declared) != dict(completion_evidence):
        result["formal_evidence_reasons"].append("status/controller completion evidence differs")
        return result
    root = Path(result["output_dir"])
    required = {
        name: root / name
        for name in (
            "manifest.json",
            "results.jsonl",
            "trace.jsonl",
            "audit.json",
            "openrouter-non-byok-campaign-proof.json",
        )
    }
    if not all(path.is_file() and not path.is_symlink() for path in required.values()):
        result["formal_evidence_reasons"].append("formal root package is incomplete")
        return result
    manifest = load_json(required["manifest.json"])
    audit = load_json(required["audit.json"])
    proof = load_json(required["openrouter-non-byok-campaign-proof.json"])
    reasons: list[str] = []
    for document, key, label in (
        (manifest, "manifest_sha256", "manifest"),
        (audit, "audit_sha256", "audit"),
        (proof, "proof_sha256", "non-BYOK proof"),
    ):
        if not common.validate_embedded_hash(document, key):
            reasons.append(f"{label} self-hash differs")
    for name in (
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    ):
        valid, detail = common.artifact_binding_valid(root, manifest, name)
        if not valid:
            reasons.append(detail)
    try:
        rows, row_reasons = common.read_compact_rows(required["results.jsonl"], prices)
    except Exception as exc:  # noqa: BLE001
        rows, row_reasons = [], [str(exc)]
    reasons.extend(row_reasons)
    task_ids = [str(row.get("task_id") or "") for row in rows]
    expected_ids = [str(value) for value in plan["benchmark"]["task_ids"]]
    if len(rows) != 10 or len(set(task_ids)) != 10 or set(task_ids) != set(expected_ids):
        reasons.append("results do not contain the exact ten frozen tasks")
    if {row.get("group") for row in rows} != {"G1"}:
        reasons.append("results are not exactly G1")
    if manifest.get("status") != "complete" or manifest.get("execution_pass") is not True:
        reasons.append("manifest is not execution-complete")
    if manifest.get("result_count") != 10 or manifest.get("task_count") != 10:
        reasons.append("manifest row/task counts differ from ten")
    execution_status = "pass" if (
        manifest.get("execution_pass") is True
        and audit.get("execution_pass") is True
        and proof.get("execution_pass") is True
    ) else "fail"
    policy_status = "pass" if (
        manifest.get("policy_pass") is True
        and audit.get("policy_pass") is True
        and proof.get("policy_pass") is True
    ) else "warning"
    audit_status = (
        "pass"
        if manifest.get("audit_pass") is True and audit.get("pass") is True
        else "warning"
    )
    result.update(
        {
            "formal": True,
            "formal_evidence_valid": not reasons,
            "formal_evidence_reasons": reasons,
            "rows": rows,
            "metrics": common.summarize_rows(rows),
            "statuses": {
                "execution": execution_status,
                "policy": policy_status,
                "audit": audit_status,
            },
            "account": common.account_evidence(manifest, proof),
            "manifest": {
                "manifest_sha256": manifest.get("manifest_sha256"),
                "status": manifest.get("status"),
                "execution_pass": manifest.get("execution_pass"),
                "policy_pass": manifest.get("policy_pass"),
                "audit_pass": manifest.get("audit_pass"),
                "warnings": manifest.get("warnings") or [],
            },
            "audit": {
                "audit_sha256": audit.get("audit_sha256"),
                "pass": audit.get("pass"),
                "warnings": audit.get("warnings") or [],
            },
            "proof": {
                "proof_sha256": proof.get("proof_sha256"),
                "pass": proof.get("pass"),
                "warnings": proof.get("warnings") or [],
            },
        }
    )
    return result


def metric_row(arm: Mapping[str, Any]) -> str:
    metric = arm["metrics"]
    exact = f"{metric['selected_generation_cost_exact_task_count']}/{metric['row_count']}"
    return "| " + " | ".join(
        [
            str(arm["arm_id"]),
            str(metric["row_count"]),
            str(metric["done_count"]),
            fmt(metric["avg_quality_total"]),
            pct(metric["avg_pass_rate"]),
            str(metric["judge_error_count"]),
            fmt(metric["avg_selected_generation_cost_usd"], 6),
            fmt(metric["selected_generation_cost_counted_usd"], 6),
            exact,
            fmt(metric["avg_input_tokens"], 1),
            fmt(metric["avg_output_tokens"], 1),
            fmt(metric["avg_reasoning_tokens"], 1),
            fmt(metric["avg_cached_tokens"], 1),
            fmt(metric["avg_visible_tokens"], 1),
            fmt(metric["avg_total_tokens"], 1),
            fmt(metric["avg_tool_calls"], 2),
            pct(metric["tool_task_rate"]),
            fmt(metric["avg_trajectory_steps"], 2),
            fmt(metric["avg_llm_requests"], 2),
            fmt(metric["latency_p50_ms"], 0),
            fmt(metric["latency_p95_ms"], 0),
        ]
    ) + " |"


TABLE_HEADER = (
    "| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$† | Total Gen$† | Gen exact | "
    "Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | "
    "Tool% | Avg Steps | Avg LLMReq | p50 ms | p95 ms |\n"
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
    "---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
)


def compact_for_json(arm: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in arm.items() if key != "rows"}


def build_group_markdown(
    group: str,
    group_arms: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    *,
    plan: Mapping[str, Any],
    hit_decisions: Mapping[str, Any],
) -> str:
    lines = [
        f"# {group} — DRACO mini P1 条件切片实验",
        "",
        f"- Run：`{plan['run_id']}`",
        "- 冻结 commit/tree："
        f"`{plan['freeze']['snapshot_commit']}` / `{plan['freeze']['snapshot_tree']}`",
        "- 样本：G1 10 题 mini，仅用于诊断/淘汰，不等同正式定稿。",
        "- DRACO mini 未提供独立 SafetyGate；"
        "不得把 execution/policy/audit 状态当作 SafetyGate。",
        "",
        TABLE_HEADER,
    ]
    lines.extend(metric_row(arm) for arm in group_arms if arm["state"] == "succeeded")
    if not any(arm["state"] == "succeeded" for arm in group_arms):
        lines.append(
            "| — | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | "
            "— | — | — | — | — | — | — |"
        )
    lines.extend(
        [
            "",
            "† generation 费用只统计最终成功且 selected 的 generation 调用："
            "actual 美元优先；缺美元时按冻结价格和 input/output/cache-read/"
            "cache-write tokens 做 cache-aware 估算；"
            "金额和 token 都缺失则忽略并披露。"
            "Judge、失败/被替换 retry 不计入该列。",
            "",
            "## 命中门与运行状态",
            "",
            "命中门只决定是否启动候选臂：P1-17 的 Aggregator 时长为 "
            "`agent_call_duration - max(proposer_elapsed)` 的保守估算；"
            "P1-36 只认 trace 中明确的 cooperative soft-deadline replacement，"
            "普通最终回答不会解锁该臂。",
            "",
        ]
    )
    for arm in group_arms:
        decision = hit_decisions.get(arm["arm_id"], {})
        lines.append(
            f"- `{arm['arm_id']}`：state=`{arm['state']}`；"
            f"hit=`{decision.get('decision', 'n/a')}`；"
            f"matched={decision.get('matched_task_count', 0)}；execution/policy/audit="
            f"`{arm['statuses']['execution']}/{arm['statuses']['policy']}/"
            f"{arm['statuses']['audit']}`。"
        )
        if arm["state"] == "succeeded":
            metric = arm["metrics"]
            lines.append(
                "  - 诊断："
                f"N={json.dumps(metric['n_distribution'], ensure_ascii=False)}；"
                f"fallback={metric['fallback_task_count']}；"
                f"outer retry={metric['outer_retry_count']}；"
                f"partial proposer={metric['partial_proposer_task_count']}；"
                f"degraded={metric['degraded_task_count']}；"
                f"truncated={metric['assembly_truncated_task_count']}。"
            )
            lines.append(
                "  - generation 费用证据："
                f"actual requests={metric['selected_generation_cost_actual_request_count']}，"
                "estimated requests="
                f"{metric['selected_generation_cost_estimated_request_count']}，"
                f"ignored requests={metric['selected_generation_cost_ignored_request_count']}。"
            )
        for reason in arm.get("formal_evidence_reasons") or []:
            lines.append(f"  - 证据警告：{reason}")
    lines.extend(["", "## 配对结果", ""])
    if not comparisons:
        lines.append("无完整、同 Analyzer 模式的 10 题配对结果。")
    for comparison in comparisons:
        ci = comparison.get("bootstrap_ci95") or [None, None]
        lines.append(
            f"- `{comparison['variant_arm_id']}` vs `{comparison['control_arm_id']}`："
            f"n={comparison['pair_count']}，Mean ΔQ={fmt(comparison['mean_delta_quality'])}，"
            f"95% CI=[{fmt(ci[0])}, {fmt(ci[1])}]，W/T/L="
            f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']}。"
        )
    lines.extend(
        [
            "",
            "## 账户与理论费用口径",
            "",
            "selected generation 理论费用与账户 reconciliation 实扣严格分列；"
            "token 补价不叠加到账户实扣。BYOK 或逐请求费用缺失本身不等于 "
            "execution failure，但会如实影响 policy/audit 与费用覆盖率。",
            "",
        ]
    )
    for arm in group_arms:
        if arm["state"] != "succeeded":
            continue
        account = arm.get("account") or {}
        lines.append(
            f"- `{arm['arm_id']}` 账户实扣（含 Judge）="
            f"{fmt(account.get('account_delta_usd'), 6)} USD；"
            f"reconciliation stable={account.get('reconciliation_stable')}；"
            f"BYOK delta={fmt(account.get('byok_delta_usd'), 6)} USD。"
        )
    lines.append("")
    return "\n".join(lines)


def build_root_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# P1 DRACO mini 条件切片实验结果",
        "",
        f"- Run：`{report['run_id']}`",
        f"- Controller phase：`{report['phase']}`",
        f"- 正式完整臂：{report['formal_valid_arm_count']}/{report['arm_count']}",
        "- 本报告先用 E0 取证命中切片；no-hit 臂未调用模型。"
        "P1-35 优先，P1-15 仅在其降本不足或证据不确定时解锁。",
        "- DRACO mini 未提供独立 SafetyGate；10 题结果只用于诊断/淘汰。",
        "",
        TABLE_HEADER,
    ]
    lines.extend(metric_row(arm) for arm in report["arms"] if arm["state"] == "succeeded")
    lines.extend(
        [
            "",
            "† generation 费用口径：selected 成功 attempt；actual 优先，"
            "缺失时 cache-aware token 估算；排除 Judge 与失败/被替换 retry。"
            "账户实际支出只取 reconciliation delta，不与理论估算相加。",
            "",
            "## 支持/跳过矩阵",
            "",
        ]
    )
    for item in report["excluded"]:
        lines.append(f"- `{item['id']}`：`{item['kind']}` — {item['reason']}")
    lines.extend(["", "## 各组", ""])
    for group in report["groups"]:
        lines.append(
            f"- `{group['experiment_id']}`：arms={group['arm_count']}，"
            f"succeeded={group['succeeded_count']}，no-hit/progression "
            f"skipped={group['skipped_count']}，failed/blocked={group['failed_count']}。"
        )
    lines.extend(["", "## 结论边界", ""])
    lines.append(
        "只有完整 10 题配对、execution 通过且费用证据充分的结果"
        "可用于 mini 淘汰；"
        "mini 通过后仍需 n≥30 选参与独立 n≥50 留出集正式非劣检验。"
    )
    lines.append("")
    return "\n".join(lines)


def generate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    plan = load_json(args.plan)
    if plan.get("schema") != PLAN_SCHEMA:
        raise ReportError("campaign plan schema differs")
    controller, common = load_frozen_modules(plan)
    try:
        arms = controller.validate_plan(plan, allow_placeholders=False)
        snapshot, snapshot_identity = controller.validate_snapshot(plan)
        controller.validate_runtime_freeze(
            plan, snapshot=snapshot, expected_snapshot_identity=snapshot_identity
        )
    except Exception as exc:
        raise ReportError(f"frozen controller validation failed: {exc}") from exc
    status_path = args.status or Path(str(plan["paths"]["run_root"])) / "status.json"
    status = load_json(status_path)
    if (
        status.get("schema") != STATUS_SCHEMA
        or status.get("campaign_plan_sha256") != canonical_sha256(plan)
    ):
        raise ReportError("controller status identity differs")
    phase = str(status.get("phase") or "")
    if phase not in TERMINAL_PHASES and not args.allow_nonterminal:
        raise ReportError(f"controller is not terminal: {phase}")
    try:
        derived, artifact = controller.load_derived(plan)
    except Exception as exc:  # noqa: BLE001
        derived, artifact = {}, None
        derived_error = str(exc)
    else:
        derived_error = None
    registry_path = snapshot / str(plan["freeze"]["model_registry"]["path"])
    prices, price_metadata = common.load_prices(registry_path, plan["freeze"]["model_registry"])
    loaded: dict[str, dict[str, Any]] = {}
    for arm in arms:
        state = status.get("arms", {}).get(arm.arm_id)
        if not isinstance(state, Mapping):
            raise ReportError(f"controller status lacks arm {arm.arm_id}")
        loaded[arm.arm_id] = load_arm(
            plan,
            state,
            arm,
            controller=controller,
            common=common,
            snapshot=snapshot,
            snapshot_identity=snapshot_identity,
            artifact=artifact,
            prices=prices,
        )
    comparisons: list[dict[str, Any]] = []
    for arm in arms:
        candidate = loaded[arm.arm_id]
        control_id = arm.control_arm_id
        if not control_id or candidate["state"] != "succeeded":
            continue
        control = loaded.get(control_id)
        if not control or control["state"] != "succeeded":
            continue
        try:
            paired = common.paired(
                {
                    **control,
                    "spec": {
                        "arm_id": control_id,
                        "analyzer_mode": control["analyzer_mode"],
                    },
                },
                {
                    **candidate,
                    "spec": {
                        "arm_id": arm.arm_id,
                        "analyzer_mode": candidate["analyzer_mode"],
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            candidate["formal_evidence_reasons"].append(f"paired comparison unavailable: {exc}")
            continue
        comparisons.append(paired)
    hit_decisions = derived.get("hit_decisions") if isinstance(derived, Mapping) else {}
    output_root = args.output_root or Path(str(plan["paths"]["report_root"]))
    groups: list[dict[str, Any]] = []
    experiment_ids = sorted(
        {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"},
        key=lambda value: tuple(int(part) if part.isdigit() else part for part in re_split(value)),
    )
    for group in experiment_ids:
        group_arms = [loaded[arm.arm_id] for arm in arms if arm.experiment_id == group]
        group_arm_ids = {arm["arm_id"] for arm in group_arms}
        group_comparisons = [
            row for row in comparisons if row["variant_arm_id"] in group_arm_ids
        ]
        markdown = build_group_markdown(
            group,
            group_arms,
            group_comparisons,
            plan=plan,
            hit_decisions=hit_decisions if isinstance(hit_decisions, Mapping) else {},
        )
        group_root = output_root / group
        atomic_write(group_root / "EXPERIMENT_RESULTS.md", markdown)
        group_receipt = {
            "schema": "opensquilla.draco-p1-group-report/v1",
            "campaign_plan_sha256": canonical_sha256(plan),
            "experiment_id": group,
            "arms": [compact_for_json(arm) for arm in group_arms],
            "comparisons": group_comparisons,
        }
        group_receipt["report_sha256"] = canonical_sha256(group_receipt)
        atomic_write_json(group_root / "EXPERIMENT_RESULTS.json", group_receipt)
        groups.append(
            {
                "experiment_id": group,
                "arm_count": len(group_arms),
                "succeeded_count": sum(arm["state"] == "succeeded" for arm in group_arms),
                "skipped_count": sum(
                    arm["state"] in {"no_hit_skipped", "progression_skipped"}
                    for arm in group_arms
                ),
                "failed_count": sum(
                    arm["state"] in {"failed", "blocked_prerequisite"}
                    for arm in group_arms
                ),
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run_id": plan["run_id"],
        "campaign_plan_sha256": canonical_sha256(plan),
        "phase": phase,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "source_plan": copy.deepcopy(plan["source_plan"]),
        "arm_count": len(arms),
        "formal_valid_arm_count": sum(
            arm["formal_evidence_valid"] is True for arm in loaded.values()
        ),
        "derived_error": derived_error,
        "hit_receipt_sha256": (
            derived.get("hit_receipt_sha256")
            if isinstance(derived, Mapping)
            else None
        ),
        "price_registry": price_metadata,
        "independent_safety_gate_available": False,
        "arms": [compact_for_json(loaded[arm.arm_id]) for arm in arms],
        "comparisons": comparisons,
        "excluded": copy.deepcopy(plan["excluded"]),
        "groups": groups,
        "cost_scope": {
            "selected_generation": (
                "actual USD first; cache-aware token estimate second; "
                "missing money+tokens ignored and disclosed"
            ),
            "judge_excluded": True,
            "failed_or_replaced_generation_retries_excluded": True,
            "account_actual": "reconciliation delta only; never add theoretical estimates",
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    atomic_write_json(output_root / "EXPERIMENT_RESULTS.json", report)
    atomic_write(output_root / "EXPERIMENT_RESULTS.md", build_root_markdown(report))
    complete = (
        phase in TERMINAL_PHASES
        and all(
            arm["state"] in controller.TERMINAL_ARM_STATES for arm in loaded.values()
        )
        and not any(
            arm["state"] == "succeeded" and arm["formal_evidence_valid"] is not True
            for arm in loaded.values()
        )
    )
    return report, 0 if complete else 2


def re_split(value: str) -> list[str]:
    parts: list[str] = []
    current = ""
    digit = None
    for char in value:
        is_digit = char.isdigit()
        if digit is None or digit == is_digit:
            current += char
        else:
            parts.append(current)
            current = char
        digit = is_digit
    if current:
        parts.append(current)
    return parts


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--allow-nonterminal", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _, returncode = generate(parse_args(argv))
    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"report error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
