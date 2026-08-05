#!/usr/bin/env python3
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPORT_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "generate_draco_p0_p05_reports.py"
)
if not REPORT_MODULE_PATH.is_file():
    REPORT_MODULE_PATH = Path(__file__).with_name("generate_reports.py")
REPORT_MODULE_NAME = "_generate_draco_p0_p05_reports_under_test"
REPORT_SPEC = importlib.util.spec_from_file_location(
    REPORT_MODULE_NAME,
    REPORT_MODULE_PATH,
)
if REPORT_SPEC is None or REPORT_SPEC.loader is None:
    raise RuntimeError(f"cannot import report generator: {REPORT_MODULE_PATH}")
report = importlib.util.module_from_spec(REPORT_SPEC)
sys.modules[REPORT_MODULE_NAME] = report
REPORT_SPEC.loader.exec_module(report)

FROZEN_CONTROLLER_FIXTURE = r"""#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Arm:
    arm_id: str
    experiment_id: str
    directory_name: str
    output_name: str
    analyzer_mode: str
    override: dict
    dynamic: dict | None


def validate_plan(plan, *, allow_placeholders):
    assert allow_placeholders is False
    result = []
    run_id = plan["run_id"]
    for row in plan.get("common_e0", []):
        result.append(Arm(row["arm_id"], "common-E0", "common", f'{row["arm_id"]}-{run_id}', row["analyzer_mode"], copy.deepcopy(row.get("override") or {}), None))
    for experiment in plan.get("experiments", []):
        for variant in experiment.get("variants", []):
            arm_id = f'{experiment["id"]}-{variant["id"]}'
            result.append(Arm(arm_id, experiment["id"], experiment["directory_name"], f"{arm_id}-{run_id}", variant.get("analyzer_mode", "frozen_replay"), copy.deepcopy(variant.get("override") or {}), copy.deepcopy(variant.get("dynamic"))))
    return result


def validate_snapshot(plan):
    return Path(plan["paths"]["snapshot"]).resolve(), {
        "commit": plan["freeze"]["snapshot_commit"],
        "tree": plan["freeze"]["snapshot_tree"],
        "status": "",
    }


def validate_runtime_freeze(plan, *, snapshot, expected_snapshot_identity):
    assert snapshot == Path(plan["paths"]["snapshot"]).resolve()
    assert expected_snapshot_identity["commit"] == plan["freeze"]["snapshot_commit"]
    return {"sources": copy.deepcopy(plan["freeze"]["sources"])}


def output_dir(plan, arm):
    return Path(plan["paths"]["report_root"]) / arm.directory_name / arm.output_name


def load_derived(plan, plan_sha256):
    path = Path(plan["paths"]["run_root"]) / "derived-plan.json"
    derived = json.loads(path.read_text())
    detached = dict(derived)
    claimed = detached.pop("derived_plan_sha256", None)
    if derived.get("campaign_plan_sha256") != plan_sha256 or claimed != canonical_sha256(detached):
        raise RuntimeError("derived plan authentication failed")
    descriptor = derived["frozen_analyzer_artifact"]
    artifact_path = Path(descriptor["path"])
    artifact = json.loads(artifact_path.read_text())
    artifact_detached = dict(artifact)
    artifact_claimed = artifact_detached.pop("artifact_sha256", None)
    if descriptor["file_sha256"] != file_sha256(artifact_path) or artifact_claimed != canonical_sha256(artifact_detached):
        raise RuntimeError("artifact authentication failed")
    return derived, artifact


def resolve_arm_override(plan, arm, *, artifact, p99_receipt):
    override = copy.deepcopy(arm.override)
    if arm.dynamic is not None:
        if p99_receipt is None:
            raise RuntimeError("dynamic arm lacks p99 receipt")
        override["dynamic_value"] = p99_receipt["derived_max_output_tokens"][arm.arm_id]
    if arm.analyzer_mode == "frozen_replay":
        if artifact is None:
            raise RuntimeError("frozen replay arm lacks artifact")
        override["frozen_artifact_sha256"] = artifact["artifact_sha256"]
    return override


def arm_completion_identity(plan, arm, *, snapshot, snapshot_identity, override):
    main = snapshot / "scripts/run_draco_routing_experiment.py"
    resume = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    return {
        "arm_id": arm.arm_id,
        "output_name": arm.output_name,
        "run_id": plan["run_id"],
        "output_dir": str(output_dir(plan, arm).resolve()),
        "snapshot": str(snapshot.resolve()),
        "snapshot_commit": snapshot_identity["commit"],
        "runner_identities": {
            str(main.resolve()): file_sha256(main),
            str(resume.resolve()): file_sha256(resume),
        },
        "override_sha256": canonical_sha256(override),
        "effective_config_sha256": canonical_sha256({"override": override}),
    }


def inspect_complete_arm(directory, *, expected_task_ids, expected_task_concurrency, expected_identity=None):
    directory = Path(directory)
    if not directory.exists():
        return False, {"reason": "output_absent"}
    try:
        if expected_identity is None:
            raise RuntimeError("expected arm publication identity is unavailable")
        publication = json.loads((directory / "controller-publication-identity.json").read_text())
        if not isinstance(publication.get("runner_identities"), dict):
            raise RuntimeError("expected runner identity set is missing")
        for field in ("arm_id", "output_name", "run_id", "runner_identities", "override_sha256", "effective_config_sha256"):
            if publication.get(field) != expected_identity.get(field):
                raise RuntimeError(f"source wave {field} identity differs")
        with (directory / "results.jsonl").open(encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        if {row.get("task_id") for row in rows} != set(expected_task_ids):
            raise RuntimeError("task identities differ")
        required = {
            "manifest": directory / "manifest.json",
            "results": directory / "results.jsonl",
            "trace": directory / "trace.jsonl",
            "audit": directory / "audit.json",
            "proof": directory / "openrouter-non-byok-campaign-proof.json",
        }
        checks = {"synthetic_contract": len(rows) == 10 and expected_task_concurrency == 6}
        arm_identity = {
            key: publication[key]
            for key in ("arm_id", "output_name", "run_id", "override_sha256", "effective_config_sha256")
        }
        arm_identity["source_manifests"] = [{"runner_kind": "main", "manifest_sha256": file_sha256(required["manifest"])}]
        evidence = {
            "reason": "complete" if all(checks.values()) else "completion_contract_failed",
            "checks": checks,
            "artifact_sha256": {key: file_sha256(path) for key, path in required.items()},
            "arm_identity": arm_identity,
        }
        return all(checks.values()), evidence
    except (OSError, ValueError, RuntimeError) as exc:
        return False, {"reason": "artifact_validation_failed", "detail": str(exc)}
"""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seal(document: dict, field: str) -> dict:
    document[field] = "sha256:" + report.canonical_sha256(document)
    return document


class CostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = {"vendor/model": (2.0, 10.0, 0.5, 3.0)}

    def test_actual_usd_wins_over_tokens(self) -> None:
        unit = {
            "model": "vendor/model",
            "input_tokens": 999_999,
            "output_tokens": 999_999,
            "billed_cost": 0.123,
            "provider_usage": {"is_byok": False},
        }
        priced = report.price_unit(unit, self.prices)
        self.assertEqual(priced["mode"], "actual")
        self.assertAlmostEqual(priced["usd"], 0.123)

    def test_cache_aware_estimate(self) -> None:
        unit = {
            "model": "vendor/model",
            "input_tokens": 1000,
            "output_tokens": 100,
            "cached_tokens": 400,
            "cache_write_tokens": 100,
        }
        priced = report.price_unit(unit, self.prices)
        # fresh=500: 500*2 + read=400*.5 + write=100*3 + output=100*10
        self.assertEqual(priced["mode"], "estimated_cache_aware")
        self.assertAlmostEqual(priced["usd"], 0.0025)

    def test_missing_cache_rate_is_disclosed_fallback(self) -> None:
        prices = {"vendor/model": (2.0, 10.0, None, None)}
        priced = report.price_unit(
            {
                "model": "vendor/model",
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached_tokens": 400,
            },
            prices,
        )
        self.assertEqual(priced["mode"], "estimated_cache_price_fallback")
        self.assertAlmostEqual(priced["usd"], 0.003)

    def test_no_money_no_tokens_is_ignored(self) -> None:
        priced = report.price_unit({"model": "vendor/model"}, self.prices)
        self.assertEqual(priced["mode"], "ignored")
        self.assertIsNone(priced["usd"])

    def test_actual_cost_evidence_and_zero_placeholder_fallbacks(self) -> None:
        token_usage = {
            "model": "vendor/model",
            "input_tokens": 1000,
            "output_tokens": 100,
            "cached_tokens": 400,
        }
        cases = [
            (
                "placeholder zero with tokens",
                {**token_usage, "billed_cost": 0.0, "cost_source": "unavailable"},
                "estimated_cache_aware",
                0.0024,
            ),
            (
                "provider billed zero",
                {**token_usage, "billed_cost": 0.0, "cost_source": "provider_billed"},
                "actual",
                0.0,
            ),
            (
                "nested provider billed zero",
                {
                    **token_usage,
                    "billed_cost": 0.0,
                    "cost_source": "unavailable",
                    "provider_usage": {
                        "cost_source": "provider_billed",
                        "provider_reported_cost": 0.0,
                    },
                },
                "actual",
                0.0,
            ),
            (
                "legacy positive",
                {"model": "vendor/model", "billed_cost": 0.125},
                "actual",
                0.125,
            ),
            (
                "nested positive wins over outer zero",
                {
                    "model": "vendor/model",
                    "billed_cost": 0.0,
                    "provider_usage": {"provider_reported_cost": 0.375},
                },
                "actual",
                0.375,
            ),
            (
                "BYOK zero",
                {
                    **token_usage,
                    "billed_cost": 0.0,
                    "cost_source": "openrouter_usage",
                    "provider_usage": {"is_byok": True, "provider_reported_cost": 0.0},
                },
                "estimated_cache_aware",
                0.0024,
            ),
            (
                "no money or tokens",
                {"model": "vendor/model", "billed_cost": 0.0, "cost_source": "unknown"},
                "ignored",
                None,
            ),
            (
                "OpenSquilla estimate is not actual",
                {
                    **token_usage,
                    "billed_cost": 0.5,
                    "cost_source": "opensquilla_model_registry",
                },
                "estimated_cache_aware",
                0.0024,
            ),
            (
                "confirmed billing receipt",
                {
                    "model": "vendor/model",
                    "billed_cost": 0.0,
                    "cost_source": "unknown",
                    "billing_receipt": {
                        "status": "confirmed",
                        "usd_equivalent_nanos": 250_000_000,
                    },
                },
                "actual",
                0.25,
            ),
            (
                "pending billing receipt is not actual",
                {
                    **token_usage,
                    "billed_cost": 0.5,
                    "billing_receipt": {
                        "status": "pending",
                        "usd_equivalent_nanos": None,
                    },
                },
                "estimated_cache_aware",
                0.0024,
            ),
        ]
        for name, unit, expected_mode, expected_usd in cases:
            with self.subTest(name=name):
                priced = report.price_unit(unit, self.prices)
                self.assertEqual(priced["mode"], expected_mode)
                if expected_usd is None:
                    self.assertIsNone(priced["usd"])
                else:
                    self.assertAlmostEqual(priced["usd"], expected_usd)

    def test_selected_scope_does_not_use_whole_generation_retry_spend(self) -> None:
        row = {
            "cost_accounting": {
                "selected_generation_attempt": {
                    "scope": "selected_generation_attempt",
                    "request_count": 1,
                    "recorded_cost_usd": 0.2,
                    "cost_complete": True,
                    "cost_exact": True,
                    "unknown_request_count": 0,
                },
                "actual_generation_spend": {
                    "recorded_cost_usd": 999.0,
                    "request_count": 4,
                },
            },
            "generation_attempt_count": 3,
            "usage": {
                "model_usage_breakdown": [
                    {
                        "model": "vendor/model",
                        "role": "proposer",
                        "billed_cost": 0.2,
                        "provider_usage": {"is_byok": False},
                    }
                ]
            },
        }
        selected_id = "1" * 32
        row["selected_generation_succeeded"] = True
        row["campaign_finalization"] = {
            "selection": {"selected_generation_attempt_id": selected_id}
        }
        row["execution"] = {
            "generation_attempts": [
                {
                    "attempt_id": selected_id,
                    "attempt_kind": "generation",
                    "run": {"usage": json.loads(json.dumps(row["usage"]))},
                }
            ]
        }
        selected = report.selected_generation_cost(row, self.prices)
        self.assertEqual(selected["source"], "selected_scope_exact_aggregate")
        self.assertAlmostEqual(selected["usd"], 0.2)

    def test_unbound_selected_usage_is_ignored_not_guessed(self) -> None:
        row = {
            "selected_generation_succeeded": True,
            "cost_accounting": {
                "selected_generation_attempt": {
                    "scope": "selected_generation_attempt",
                    "request_count": 1,
                    "recorded_cost_usd": 0.2,
                    "cost_complete": True,
                    "cost_exact": True,
                }
            },
            "usage": {
                "model_usage_breakdown": [
                    {
                        "role": "proposer",
                        "model": "vendor/model",
                        "billed_cost": 0.2,
                    }
                ]
            },
        }
        selected = report.selected_generation_cost(row, self.prices)
        self.assertEqual(selected["source"], "selected_attempt_identity_unverified_ignored")
        self.assertIsNone(selected["usd"])
        self.assertEqual(selected["ignored_requests"], 1)
        self.assertEqual(report.selected_model_usage(row, self.prices), {})


class AnalyzerOriginTests(unittest.TestCase):
    def test_v2_replay_fallback_is_explicit_and_preserves_reason(self) -> None:
        evidence = report.task_analyzer_origin_evidence(
            {
                "source": "frozen_replay",
                "schema_valid": False,
                "fallback_reason": "TimeoutError",
                "replay": {
                    "schema": "opensquilla.draco.frozen-task-analysis/v2",
                    "origin_outcome": "deterministic_router_fallback",
                },
            }
        )
        self.assertEqual(evidence["origin_outcome"], "deterministic_router_fallback")
        self.assertTrue(evidence["origin_outcome_explicit"])
        self.assertTrue(evidence["is_explicit_router_fallback"])
        self.assertEqual(evidence["fallback_reason"], "TimeoutError")

    def test_v1_success_is_compatible_but_legacy_reason_does_not_invent_fallback(self) -> None:
        v1 = report.task_analyzer_origin_evidence(
            {
                "source": "frozen_replay",
                "schema_valid": True,
                "fallback_reason": "",
                "replay": {"schema": "opensquilla.draco.frozen-task-analysis/v1"},
            }
        )
        self.assertEqual(v1["origin_outcome"], "live_success")
        self.assertFalse(v1["origin_outcome_explicit"])
        self.assertFalse(v1["is_explicit_router_fallback"])

        ambiguous = report.task_analyzer_origin_evidence(
            {
                "source": "frozen_replay",
                "schema_valid": False,
                "fallback_reason": "TimeoutError",
                "replay": {"schema": "opensquilla.draco.frozen-task-analysis/v1"},
            }
        )
        self.assertEqual(ambiguous["origin_outcome"], "unknown")
        self.assertFalse(ambiguous["is_explicit_router_fallback"])
        self.assertEqual(ambiguous["fallback_reason"], "TimeoutError")

    def test_summary_reports_distributions_and_only_explicit_fallback_task_ids(self) -> None:
        def row(
            task_id: str,
            outcome: str,
            *,
            reason: str = "",
            explicit_fallback: bool = False,
        ) -> dict:
            return {
                "task_id": task_id,
                "generation_cost": {
                    "usd": 0.1,
                    "complete": True,
                    "request_count": 1,
                    "actual_requests": 1,
                },
                "judge_cost": {
                    "usd": 0.01,
                    "complete": True,
                    "request_count": 1,
                    "actual_requests": 1,
                },
                "model_generation": {},
                "analyzer_origin_outcome": outcome,
                "analyzer_fallback_reason": reason,
                "analyzer_origin_is_fallback": explicit_fallback,
            }

        summary = report.summarize_rows(
            [
                row("live", "live_success"),
                row(
                    "fallback",
                    "deterministic_router_fallback",
                    reason="TimeoutError",
                    explicit_fallback=True,
                ),
                row("legacy-ambiguous", "unknown", reason="ConnectionError"),
            ]
        )
        self.assertEqual(
            summary["analyzer_origin_outcome_distribution"],
            {
                "deterministic_router_fallback": 1,
                "live_success": 1,
                "unknown": 1,
            },
        )
        self.assertEqual(
            summary["analyzer_fallback_reason_distribution"],
            {"ConnectionError": 1, "TimeoutError": 1},
        )
        self.assertEqual(summary["analyzer_fallback_task_ids"], ["fallback"])
        self.assertEqual(summary["analyzer_unknown_origin_task_ids"], ["legacy-ambiguous"])


class PairingTests(unittest.TestCase):
    @staticmethod
    def arm(arm_id: str, offset: float) -> dict:
        return {
            "spec": {"arm_id": arm_id, "analyzer_mode": "frozen_replay"},
            "rows": [
                {
                    "task_id": f"task-{index}",
                    "domain": "test",
                    "quality": float(index) + offset,
                    "request_context_hash": "context",
                    "task_profile_hash": "profile",
                    "selected_p": ["p"],
                    "selected_a": "a",
                    "n": 1,
                }
                for index in range(10)
            ],
        }

    def test_task_id_pairing_bootstrap_and_wtl(self) -> None:
        comparison = report.paired(self.arm("E0", 0), self.arm("E1", 1))
        self.assertEqual(comparison["pair_count"], 10)
        self.assertEqual(comparison["wins"], 10)
        self.assertEqual(comparison["ties"], 0)
        self.assertEqual(comparison["losses"], 0)
        self.assertAlmostEqual(comparison["mean_delta_quality"], 1.0)
        self.assertEqual(comparison["bootstrap_samples"], 20_000)
        self.assertEqual(comparison["bootstrap_ci95"], [1.0, 1.0])

    def test_repeated_pairing_averages_by_task_before_bootstrap(self) -> None:
        comparisons = [
            report.paired(self.arm(f"E0-R{rep}", 0), self.arm(f"E1-R{rep}", float(rep)))
            for rep in (1, 2, 3)
        ]
        repeated = report.repeated_pairing(comparisons)
        assert repeated is not None
        self.assertEqual(repeated["replicate_count"], 3)
        self.assertEqual(repeated["task_count"], 10)
        self.assertAlmostEqual(repeated["mean_delta_quality"], 2.0)
        self.assertEqual(repeated["bootstrap_ci95"], [2.0, 2.0])

    def test_scoped_repeat_uses_task_intersection(self) -> None:
        allowed = {f"task-{index}" for index in range(5)}
        comparisons = [
            report.paired(
                self.arm(f"E0-R{rep}", 0),
                self.arm(f"E1-R{rep}", float(rep)),
                scope="ap_non_overlap",
                allowed_task_ids=allowed,
            )
            for rep in (1, 2, 3)
        ]
        repeated = report.repeated_pairing(comparisons)
        assert repeated is not None
        self.assertEqual(repeated["replicate_count"], 3)
        self.assertEqual(repeated["task_count"], 5)
        self.assertFalse(repeated["complete_task_id_pairing"])
        self.assertAlmostEqual(repeated["mean_delta_quality"], 2.0)
        self.assertEqual(repeated["bootstrap_ci95"], [2.0, 2.0])

    def test_ap_non_overlap_scope_filters_route_counters_too(self) -> None:
        control = self.arm("E0", 0)
        variant = self.arm("E1", 1)
        control["rows"][0]["ap_overlap"] = True
        variant["rows"][1]["ap_overlap"] = True
        comparison = report.paired(control, variant, scope="ap_non_overlap")
        self.assertEqual(comparison["pair_count"], 8)
        self.assertEqual(comparison["request_context_match_count"], 8)
        self.assertEqual(comparison["task_profile_match_count"], 8)

    def test_pairing_rejects_mixed_analyzer_modes(self) -> None:
        control = self.arm("E0", 0)
        variant = self.arm("E1", 1)
        variant["spec"]["analyzer_mode"] = "live"
        with self.assertRaisesRegex(report.ReportError, "identical non-empty analyzer_mode"):
            report.paired(control, variant)


class ScheduleAndSeedTests(unittest.TestCase):
    @staticmethod
    def plan() -> dict:
        candidate_ids = [f"P0.5-36-E1-R{replicate}" for replicate in (1, 2, 3)]
        anchors = {
            "common-E0-source": "common-E0-source",
            "common-E0-R1": "common-E0-R1",
            candidate_ids[0]: "common-E0-R1",
            "common-E0-R2": "common-E0-R2",
            candidate_ids[1]: "common-E0-R2",
            "common-E0-R3": "common-E0-R3",
            candidate_ids[2]: "common-E0-R3",
        }
        order = list(anchors)
        return {
            "run_id": "fixture",
            "execution": {
                "schedule": {
                    "mode": "anchored_serial",
                    "strict_task_interleaving": False,
                    "arm_order": order,
                    "anchor_by_arm_id": anchors,
                }
            },
            "comparison_controls": {
                "source_arm_id": "common-E0-source",
                "live_control_arm_id": "common-E0-source",
                "default_control_arm_id": "common-E0-R1",
                "replay_control_arm_ids": [
                    "common-E0-R1",
                    "common-E0-R2",
                    "common-E0-R3",
                ],
                "require_same_analyzer_mode": True,
                "arm_control_overrides": dict(
                    zip(
                        candidate_ids,
                        ("common-E0-R1", "common-E0-R2", "common-E0-R3"),
                        strict=True,
                    )
                ),
            },
            "common_e0": [
                {
                    "arm_id": "common-E0-source",
                    "variant": "E0-live",
                    "replicate": 1,
                    "analyzer_mode": "live",
                    "override": {},
                },
                *[
                    {
                        "arm_id": f"common-E0-R{replicate}",
                        "variant": "E0-replay",
                        "replicate": replicate,
                        "analyzer_mode": "frozen_replay",
                        "override": {},
                    }
                    for replicate in (1, 2, 3)
                ],
            ],
            "experiments": [
                {
                    "id": "P0.5-36",
                    "directory_name": "P0-5-36",
                    "title": "Candidate order",
                    "variants": [
                        {
                            "id": "E1",
                            "replicates": 3,
                            "override": {"ensemble": {"shuffle_candidates": True}},
                            "replicate_overrides": [
                                {"ensemble": {"candidate_order_seed": seed}}
                                for seed in (0, 1, 4)
                            ],
                        }
                    ],
                }
            ],
        }

    def test_dynamic_controls_and_replicate_seeds_expand_exactly(self) -> None:
        specs = report.expand_arms(self.plan())
        variants = [spec for spec in specs if spec.experiment_id == "P0.5-36"]
        self.assertEqual(
            [spec.control_arm_id for spec in variants],
            ["common-E0-R1", "common-E0-R2", "common-E0-R3"],
        )
        self.assertEqual(
            [spec.override["ensemble"]["candidate_order_seed"] for spec in variants],
            [0, 1, 4],
        )
        self.assertTrue(all(spec.override["ensemble"]["shuffle_candidates"] for spec in variants))

    def test_schedule_binds_sha_ordinals_anchors_and_non_interleaving(self) -> None:
        plan = self.plan()
        specs = report.expand_arms(plan)
        schedule = plan["execution"]["schedule"]
        status = {
            "schedule_sha256": report.canonical_sha256(schedule),
            "schedule_mode": "anchored_serial",
            "strict_task_interleaving": False,
            "arms": {
                arm_id: {
                    "schedule_ordinal": ordinal,
                    "anchor_arm_id": schedule["anchor_by_arm_id"][arm_id],
                }
                for ordinal, arm_id in enumerate(schedule["arm_order"], start=1)
            },
        }
        evidence = report.validate_schedule_evidence(plan, status, specs)
        self.assertTrue(evidence["valid"], evidence["reasons"])
        self.assertFalse(evidence["strict_task_interleaving"])
        self.assertEqual(evidence["design_label"], "anchored_serial_not_task_interleaved")
        status["arms"]["P0.5-36-E1-R2"]["schedule_ordinal"] = 99
        evidence = report.validate_schedule_evidence(plan, status, specs)
        self.assertFalse(evidence["valid"])
        self.assertIn(
            "status schedule ordinal differs: P0.5-36-E1-R2",
            evidence["reasons"],
        )

    def test_seed_gate_only_applies_after_candidate_aggregation_starts(self) -> None:
        spec = next(
            spec
            for spec in report.expand_arms(self.plan())
            if spec.arm_id == "P0.5-36-E1-R1"
        )
        evidence = report.p0_5_36_seed_evidence(
            spec,
            {
                "valid": [
                    {
                        "applicable": True,
                        "configured_candidate_order_seed": 0,
                        "candidate_order_seed": 0,
                    }
                ],
                "pre-aggregation": [
                    {
                        "applicable": False,
                        "configured_candidate_order_seed": 0,
                        "candidate_order_seed": None,
                    }
                ],
                "mismatch": [
                    {
                        "applicable": True,
                        "configured_candidate_order_seed": 0,
                        "candidate_order_seed": 1,
                    }
                ],
            },
            expected_task_ids=["valid", "pre-aggregation", "mismatch"],
        )
        assert evidence is not None
        self.assertEqual(evidence["valid_task_ids"], ["valid"])
        self.assertEqual(evidence["not_applicable_task_ids"], ["pre-aggregation"])
        self.assertEqual(evidence["invalid_task_ids"], ["mismatch"])
        self.assertEqual(evidence["per_task"]["pre-aggregation"]["state"], "not_applicable")
        self.assertTrue(evidence["comparison_slice_available"])

    def test_seed_gate_all_pre_aggregation_has_no_comparison_slice(self) -> None:
        spec = next(
            spec
            for spec in report.expand_arms(self.plan())
            if spec.arm_id == "P0.5-36-E1-R1"
        )
        evidence = report.p0_5_36_seed_evidence(
            spec,
            {
                "pre-aggregation": [
                    {
                        "applicable": False,
                        "configured_candidate_order_seed": 0,
                        "candidate_order_seed": None,
                    }
                ]
            },
            expected_task_ids=["pre-aggregation"],
        )
        assert evidence is not None
        self.assertEqual(evidence["not_applicable_task_ids"], ["pre-aggregation"])
        self.assertFalse(evidence["comparison_slice_available"])

    def test_p0_20_e3_is_not_c3_promotion_evidence_without_interleaving(self) -> None:
        evidence = report.p0_20_e3_promotion_evidence(
            {
                "strict_task_interleaving": False,
                "arm_timing": {
                    "common-E0-R1": {"schedule_ordinal": 6},
                    "P0-20-E3": {
                        "schedule_ordinal": 13,
                        "anchor_arm_id": "common-E0-R1",
                    },
                },
            }
        )
        self.assertEqual(evidence["status"], "mini_diagnostic_only")
        self.assertFalse(evidence["eligible_as_c3_promotion_evidence"])
        self.assertTrue(evidence["scheduled_after_r1_anchor"])
        self.assertEqual(evidence["schedule_ordinal_gap"], 7)


class ReceiptTests(unittest.TestCase):
    def test_wire_receipt_hash_allows_derived_path_annotation(self) -> None:
        receipt = {"schema": "receipt/v1", "decision": "deleted_no_live_run"}
        receipt["receipt_sha256"] = report.canonical_sha256(receipt)
        receipt["path"] = "/evidence/receipt.json"
        self.assertTrue(report.receipt_hash_valid(receipt))
        receipt["decision"] = "run"
        self.assertFalse(report.receipt_hash_valid(receipt))

    def test_predeclared_noop_requires_controller_receipt_binding(self) -> None:
        receipt = {
            "schema": "receipt/v1",
            "decision": "deleted_no_live_run",
            "reason": "temperature omitted",
        }
        receipt["receipt_sha256"] = report.canonical_sha256(receipt)
        plan = {
            "run_id": "fixture",
            "comparison_controls": {
                "source_arm_id": "common-E0-source",
                "default_control_arm_id": "common-E0-R1",
                "replay_control_arm_ids": [
                    "common-E0-R1",
                    "common-E0-R2",
                    "common-E0-R3",
                ],
                "require_same_analyzer_mode": True,
                "arm_control_overrides": {},
            },
            "common_e0": [
                {
                    "arm_id": "common-E0-source",
                    "variant": "E0-live",
                    "replicate": 1,
                    "analyzer_mode": "live",
                    "override": {},
                },
                *[
                    {
                        "arm_id": f"common-E0-R{replicate}",
                        "variant": "E0-replay",
                        "replicate": replicate,
                        "analyzer_mode": "frozen_replay",
                        "override": {},
                    }
                    for replicate in (1, 2, 3)
                ],
            ],
            "experiments": [],
            "no_op_experiments": [{"id": "P0.5-07", "title": "Analyzer temperature"}],
        }
        status = {
            "no_op_experiments": {"P0.5-07": {"state": "no_op_deleted", "receipt": dict(receipt)}}
        }
        inventory = report.build_experiment_inventory(
            plan,
            status,
            {},
            {"p0_5_07": receipt},
            {},
        )
        self.assertEqual(inventory["P0.5-07"]["state"], "no_op_deleted")
        status["no_op_experiments"]["P0.5-07"]["receipt"]["receipt_sha256"] = "wrong"
        inventory = report.build_experiment_inventory(
            plan,
            status,
            {},
            {"p0_5_07": receipt},
            {},
        )
        self.assertEqual(inventory["P0.5-07"]["state"], "incomplete_no_op_evidence")

    def test_temperature_scope_uses_exact_task_members_and_sent_usage_only(self) -> None:
        arm_id = "P0.5-11-E1-R1"
        receipt = {
            "arm_ids": [arm_id],
            "temperature_analysis_scope": {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "members": [
                            {
                                "role": "proposer",
                                "model": "vendor/p",
                                "temperature_parameter_sent": True,
                                "wire_temperature": 0.3,
                            },
                            {
                                "role": "aggregator",
                                "model": "vendor/a",
                                "temperature_parameter_sent": True,
                                "wire_temperature": 0.3,
                            },
                        ],
                    },
                    {
                        "task_id": "task-2",
                        "members": [
                            {
                                "role": "proposer",
                                "model": "vendor/p",
                                "temperature_parameter_sent": True,
                                "wire_temperature": 0.3,
                            },
                            {
                                "role": "aggregator",
                                "model": "vendor/a",
                                "temperature_parameter_sent": False,
                                "wire_temperature": None,
                            },
                        ],
                    },
                ]
            },
        }
        scope = report.parse_temperature_wire_receipt(
            receipt,
            task_ids={"task-1", "task-2"},
            arm_ids={arm_id},
        )
        self.assertTrue(scope["scope_verifiable"])

        def usage(model: str) -> dict:
            return {
                "model": model,
                "roles": ["proposer" if model.endswith("/p") else "aggregator"],
                "request_count": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 5,
                "cache_write_tokens": 0,
                "cost_counted_usd": 0.01,
                "actual_requests": 1,
                "estimated_requests": 0,
                "ignored_requests": 0,
            }

        rows = [
            {
                "task_id": task_id,
                "selected_p": ["openrouter:vendor/p"],
                "selected_a": "openrouter:vendor/a",
                "model_generation": {
                    "vendor/p": usage("vendor/p"),
                    "vendor/a": usage("vendor/a"),
                },
            }
            for task_id in ("task-1", "task-2")
        ]
        arm = {"spec": {"arm_id": arm_id}, "rows": rows, "metrics": {}}
        analysis = report.temperature_model_subanalysis(scope, arm)
        self.assertEqual(analysis["all_selected_pa_temperature_sent_task_ids"], ["task-1"])
        self.assertEqual(analysis["models"]["vendor/p"]["request_count"], 2)
        self.assertEqual(analysis["models"]["vendor/a"]["request_count"], 1)
        self.assertNotIn("quality", analysis["models"]["vendor/a"])
        self.assertFalse(analysis["models"]["vendor/a"]["quality_score_fields_present"])


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p0-p05-report-test-")
        self.root = Path(self.temporary.name)
        self.run_root = self.root / "run"
        self.snapshot = self.root / "snapshot"
        self.report_root = self.root / "reports"
        self.task_ids = [f"task-{index}" for index in range(10)]
        controller_path = self.snapshot / report.FROZEN_CONTROLLER_RELATIVE
        controller_path.parent.mkdir(parents=True, exist_ok=True)
        controller_path.write_text(FROZEN_CONTROLLER_FIXTURE, encoding="utf-8")
        for relative in (
            "scripts/run_draco_routing_experiment.py",
            "scripts/run_draco_routing_experiment_resume.py",
        ):
            runner = self.snapshot / relative
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_text(f"# frozen fixture: {relative}\n", encoding="utf-8")
        registry = {
            "schema_version": "test",
            "snapshot_version": "frozen79-test",
            "models": [
                {
                    "registry_facts": {
                        "model_id": "vendor/model" if index == 0 else f"vendor/model-{index}",
                        "provider": "openrouter",
                        "identity": (
                            "openrouter:vendor/model"
                            if index == 0
                            else f"openrouter:vendor/model-{index}"
                        ),
                        "price": {
                            "input_per_million": 2.0,
                            "output_per_million": 10.0,
                            "cache_read_per_million": 0.5,
                            "cache_write_per_million": 3.0,
                        },
                    }
                }
                for index in range(79)
            ],
        }
        registry_path = self.snapshot / report.PRICE_REGISTRY_RELATIVE
        write_json(registry_path, registry)
        formal_registry = report.formal_registry_projection(registry)
        full_ids = report.registry_identities(registry["models"], label="full")
        formal_ids = report.registry_identities(formal_registry["models"], label="formal")
        registry_contract = {
            "path": str(report.PRICE_REGISTRY_RELATIVE),
            "raw_sha256": report.file_sha256(registry_path),
            "full_snapshot_version": registry["snapshot_version"],
            "full_canonical_sha256": report.canonical_sha256(registry),
            "formal_snapshot_version": formal_registry["snapshot_version"],
            "formal_canonical_sha256": report.canonical_sha256(formal_registry),
            "model_count": 79,
            "full_model_count": 79,
            "formal_model_count": 79,
            "full_identities_sha256": report.canonical_sha256(full_ids),
            "formal_identities_sha256": report.canonical_sha256(formal_ids),
        }
        self.plan = {
            "schema": report.PLAN_SCHEMA,
            "run_id": "synthetic",
            "freeze": {
                "snapshot_commit": "a" * 40,
                "snapshot_tree": "b" * 40,
                "model_registry": registry_contract,
                "sources": {
                    "controller_raw_sha256": report.file_sha256(controller_path),
                },
            },
            "paths": {
                "run_root": str(self.run_root),
                "snapshot": str(self.snapshot),
                "report_root": str(self.report_root),
            },
            "benchmark": {"task_count": 10, "task_ids": self.task_ids, "groups": ["G1"]},
            "execution": {
                "task_concurrency": 6,
                "schedule": {
                    "mode": "anchored_serial",
                    "strict_task_interleaving": False,
                    "arm_order": [
                        "common-E0-source",
                        "common-E0-R1",
                        "P0-12-E1",
                        "common-E0-R2",
                        "common-E0-R3",
                    ],
                    "anchor_by_arm_id": {
                        "common-E0-source": "common-E0-source",
                        "common-E0-R1": "common-E0-R1",
                        "P0-12-E1": "common-E0-R1",
                        "common-E0-R2": "common-E0-R2",
                        "common-E0-R3": "common-E0-R3",
                    },
                },
            },
            "comparison_controls": {
                "source_arm_id": "common-E0-source",
                "live_control_arm_id": "common-E0-source",
                "default_control_arm_id": "common-E0-R1",
                "replay_control_arm_ids": [
                    "common-E0-R1",
                    "common-E0-R2",
                    "common-E0-R3",
                ],
                "require_same_analyzer_mode": True,
                "arm_control_overrides": {"P0-12-E1": "common-E0-R1"},
            },
            "common_e0": [
                {
                    "arm_id": "common-E0-source",
                    "variant": "E0-live",
                    "replicate": 1,
                    "analyzer_mode": "live",
                    "override": {},
                },
                *[
                    {
                        "arm_id": f"common-E0-R{replicate}",
                        "variant": "E0-replay",
                        "replicate": replicate,
                        "analyzer_mode": "frozen_replay",
                        "override": {},
                    }
                    for replicate in (1, 2, 3)
                ],
            ],
            "experiments": [
                {
                    "id": "P0-12",
                    "directory_name": "P0-12",
                    "title": "Synthetic variable",
                    "variants": [{"id": "E1", "override": {"x": 1}}],
                }
            ],
            "no_op_experiments": [],
            "excluded": [],
            "reporting": {
                "mini_is_diagnostic_only": True,
                "automatic_winner_promotion": False,
                "independent_safety_gate_available": False,
            },
        }
        self.plan_path = self.run_root / "campaign-plan.json"
        write_json(self.plan_path, self.plan)
        self.arm_dirs = {
            "common-E0-source": self.report_root
            / "common"
            / "common-E0-source-synthetic",
            "common-E0-R1": self.report_root / "common" / "common-E0-R1-synthetic",
            "common-E0-R2": self.report_root / "common" / "common-E0-R2-synthetic",
            "common-E0-R3": self.report_root / "common" / "common-E0-R3-synthetic",
            "P0-12-E1": self.report_root / "P0-12" / "P0-12-E1-synthetic",
        }
        self._formal_arm(self.arm_dirs["common-E0-source"], 50.0, "common-E0-source")
        self._formal_arm(self.arm_dirs["common-E0-R1"], 50.0, "common-E0-R1")
        self._formal_arm(self.arm_dirs["common-E0-R2"], 50.5, "common-E0-R2")
        self._formal_arm(self.arm_dirs["common-E0-R3"], 49.5, "common-E0-R3")
        self._formal_arm(self.arm_dirs["P0-12-E1"], 51.0, "P0-12-E1")
        artifact = {"schema": "test-artifact", "task_count": 10}
        artifact["artifact_sha256"] = report.canonical_sha256(artifact)
        artifact_path = self.run_root / "frozen-analyzer-profiles.json"
        write_json(artifact_path, artifact)
        overlay_sha = "c" * 64
        offline_receipt = {
            "schema": "opensquilla.draco.offline-effect-receipt/v1",
            "kind": "offline-main-runner-behavior-effect",
            "arm_ids": ["P0-12-E1"],
            "experiment_ids": ["P0-12"],
            "campaign_plan_sha256": report.canonical_sha256(self.plan),
            "source_artifact_sha256": artifact["artifact_sha256"],
            "overlay_sha256": overlay_sha,
            "decision": "run",
            "comparison_by_proposer_cap_explicitness": {
                "explicit": {
                    "changed_task_count": 10,
                    "changed_tasks": [{"task_id": task_id} for task_id in self.task_ids],
                },
                "implicit": {
                    "changed_task_count": 10,
                    "changed_tasks": [{"task_id": task_id} for task_id in self.task_ids],
                },
            },
        }
        offline_receipt["receipt_sha256"] = report.canonical_sha256(offline_receipt)
        offline_receipt_path = self.run_root / "offline-P0-12-E1.json"
        write_json(offline_receipt_path, offline_receipt)
        replay_overlay_sha = "d" * 64
        replay_receipt = {
            "schema": "opensquilla.draco.offline-effect-receipt/v1",
            "kind": "offline-main-runner-behavior-effect",
            "arm_ids": ["common-E0-R1", "common-E0-R2", "common-E0-R3"],
            "experiment_ids": ["common-E0"],
            "campaign_plan_sha256": report.canonical_sha256(self.plan),
            "source_artifact_sha256": artifact["artifact_sha256"],
            "overlay_sha256": replay_overlay_sha,
            "decision": "run",
            "comparison_by_proposer_cap_explicitness": {},
        }
        replay_receipt["receipt_sha256"] = report.canonical_sha256(replay_receipt)
        replay_receipt_path = self.run_root / "offline-common-E0-replays.json"
        write_json(replay_receipt_path, replay_receipt)
        derived = {
            "schema": report.DERIVED_SCHEMA,
            "campaign_plan_sha256": report.canonical_sha256(self.plan),
            "source_arm_id": "common-E0-source",
            "source_output_dir": str(self.arm_dirs["common-E0-source"]),
            "frozen_analyzer_artifact": {
                "path": str(artifact_path),
                "file_sha256": report.file_sha256(artifact_path),
                "artifact_sha256": artifact["artifact_sha256"],
            },
            "offline_effect": {
                **{
                    f"common-E0-R{replicate}": {
                        "decision": "run",
                        "overlay_sha256": replay_overlay_sha,
                        "receipt_path": str(replay_receipt_path),
                        "receipt_sha256": replay_receipt["receipt_sha256"],
                    }
                    for replicate in (1, 2, 3)
                },
                "P0-12-E1": {
                    "decision": "run",
                    "overlay_sha256": overlay_sha,
                    "receipt_path": str(offline_receipt_path),
                    "receipt_sha256": offline_receipt["receipt_sha256"],
                }
            },
            "offline_unique_overlays": {
                replay_overlay_sha: {
                    **replay_receipt,
                    "path": str(replay_receipt_path),
                },
                overlay_sha: {
                    **offline_receipt,
                    "path": str(offline_receipt_path),
                }
            },
        }
        derived["derived_plan_sha256"] = report.canonical_sha256(derived)
        self.derived_path = self.run_root / "derived-plan.json"
        write_json(self.derived_path, derived)
        verifier = report.load_frozen_controller_verifier(
            self.plan,
            plan_sha256=report.canonical_sha256(self.plan),
        )
        arm_evidence = {
            arm_id: self._controller_evidence(verifier, arm_id) for arm_id in self.arm_dirs
        }
        status = {
            "schema": report.STATUS_SCHEMA,
            "run_id": "synthetic",
            "campaign_plan_sha256": report.canonical_sha256(self.plan),
            "schedule_sha256": report.canonical_sha256(
                self.plan["execution"]["schedule"]
            ),
            "schedule_mode": "anchored_serial",
            "strict_task_interleaving": False,
            "phase": "succeeded",
            "derived_plan": {
                "path": str(self.derived_path),
                "sha256": derived["derived_plan_sha256"],
            },
            "arms": {
                arm_id: {
                    "state": "succeeded",
                    "output_dir": str(path),
                    "completion_evidence": arm_evidence[arm_id],
                    "schedule_ordinal": self.plan["execution"]["schedule"][
                        "arm_order"
                    ].index(arm_id)
                    + 1,
                    "anchor_arm_id": self.plan["execution"]["schedule"][
                        "anchor_by_arm_id"
                    ][arm_id],
                    "started_at": (
                        "2026-08-05T00:00:00+00:00"
                        if arm_id == "common-E0-source"
                        else "2026-08-05T00:10:00+00:00"
                        if arm_id == "common-E0-R1"
                        else "2026-08-05T00:20:00+00:00"
                        if arm_id == "P0-12-E1"
                        else "2026-08-05T00:30:00+00:00"
                        if arm_id == "common-E0-R2"
                        else "2026-08-05T00:40:00+00:00"
                    ),
                    "completed_at": (
                        "2026-08-05T00:09:00+00:00"
                        if arm_id == "common-E0-source"
                        else "2026-08-05T00:19:00+00:00"
                        if arm_id == "common-E0-R1"
                        else "2026-08-05T00:29:00+00:00"
                        if arm_id == "P0-12-E1"
                        else "2026-08-05T00:39:00+00:00"
                        if arm_id == "common-E0-R2"
                        else "2026-08-05T00:49:00+00:00"
                    ),
                }
                for arm_id, path in self.arm_dirs.items()
            },
            "no_op_experiments": {},
        }
        self.mutable_status_path = self.run_root / "status.json"
        write_json(self.mutable_status_path, status)
        status["terminal_freeze"] = {
            "schema": report.TERMINAL_STATUS_INPUT_SCHEMA,
            "campaign_plan_sha256": report.canonical_sha256(self.plan),
            "run_id": "synthetic",
            "phase": "succeeded",
        }
        status["terminal_status_input_sha256"] = report.canonical_sha256(status)
        self.status_path = self.run_root / "terminal-status-input.json"
        write_json(self.status_path, status)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _row(self, task_id: str, quality: float) -> dict:
        row = {
            "task_id": task_id,
            "group": "G1",
            "domain": "Synthetic",
            "quality_total": quality,
            "final_text": "answer",
            "generation_attempt_count": 2,
            "selected_generation_succeeded": True,
            "completion_status": {"execution_pass": True, "judge_complete": True},
            "execution_status": {"success": True, "status": "success"},
            "judge": {
                "score_status": "complete",
                "valid_pass_rate": 75.0,
                "judge_error_count": 0,
                "criterion_judgments": [],
            },
            "selected_attempt_metrics": {
                "latency_ms": 1000,
                "llm_request_count": 1,
                "total_tool_call_count": 1,
                "trajectory_steps": 2,
            },
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "reasoning_tokens": 20,
                "cached_tokens": 400,
                "model_usage_breakdown": [
                    {
                        "role": "proposer",
                        "model": "vendor/model",
                        "input_tokens": 1000,
                        "output_tokens": 100,
                        "cached_tokens": 400,
                        "billed_cost": 0.2,
                        "provider_usage": {"is_byok": False},
                    }
                ],
            },
            "cost_accounting": {
                "selected_generation_attempt": {
                    "scope": "selected_generation_attempt",
                    "request_count": 1,
                    "recorded_cost_usd": 0.2,
                    "cost_complete": True,
                    "cost_exact": True,
                    "unknown_request_count": 0,
                },
                "judge": {
                    "request_count": 1,
                    "recorded_cost_usd": 0.05,
                    "cost_complete": True,
                    "cost_exact": True,
                    "unknown_request_count": 0,
                },
                "actual_generation_spend": {"recorded_cost_usd": 999.0},
            },
            "routing_trace": {
                "selection_plan": {
                    "selected_P": ["openrouter:vendor/model"],
                    "selected_A": "openrouter:vendor/model-1",
                    "proposer_count": 1,
                    "request_context_hash": "context",
                    "task_profile_hash": "profile",
                    "task_analyzer": {"source": "frozen_replay"},
                }
            },
            "ensemble_trace": {},
            "openrouter_non_byok_audit": {"pass": True},
            "result_evidence_schema": "opensquilla.draco.result-evidence/v1",
        }
        selected_id = hashlib.sha256(task_id.encode()).hexdigest()[:32]
        row["campaign_finalization"] = {
            "selection": {"selected_generation_attempt_id": selected_id}
        }
        row["execution"] = {
            "generation_attempts": [
                {
                    "attempt_id": selected_id,
                    "attempt_kind": "generation",
                    "run": {"usage": json.loads(json.dumps(row["usage"]))},
                }
            ]
        }
        payload = {
            "schema": "opensquilla.draco.result-evidence/v1",
            "result": dict(row),
        }
        row["result_evidence_sha256"] = "sha256:" + report.canonical_sha256(payload)
        return row

    def _formal_arm(self, root: Path, quality: float, arm_id: str) -> dict:
        root.mkdir(parents=True)
        results_path = root / "results.jsonl"
        trace_path = root / "trace.jsonl"
        rows = [self._row(task_id, quality) for task_id in self.task_ids]
        # A literal U+2028 is valid inside a JSON string and must not become a
        # JSONL boundary.  Both readers are intentionally handle-iterated.
        rows[0]["final_text"] = "left\u2028right"
        payload = {
            "schema": rows[0]["result_evidence_schema"],
            "result": {
                key: value for key, value in rows[0].items() if key != "result_evidence_sha256"
            },
        }
        rows[0]["result_evidence_sha256"] = "sha256:" + report.canonical_sha256(payload)
        with results_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        with trace_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        audit = seal(
            {
                "schema": "opensquilla.draco.campaign-final-audit/v1",
                "status": "complete",
                "pass": True,
                "execution_pass": True,
                "policy_pass": True,
            },
            "audit_sha256",
        )
        proof = seal(
            {
                "schema": "opensquilla.draco.openrouter-non-byok-campaign-proof/v1",
                "status": "complete",
                "pass": True,
                "execution_pass": True,
                "policy_pass": True,
                "account": {
                    "campaign_usage_delta_usd": 3.0,
                    "campaign_byok_usage_delta_usd": 0.0,
                },
            },
            "proof_sha256",
        )
        write_json(root / "audit.json", audit)
        write_json(root / "openrouter-non-byok-campaign-proof.json", proof)
        artifacts = {}
        for name in (
            "results.jsonl",
            "trace.jsonl",
            "audit.json",
            "openrouter-non-byok-campaign-proof.json",
        ):
            path = root / name
            artifacts[name] = {
                "path": name,
                "size_bytes": path.stat().st_size,
                "sha256": report.file_sha256(path),
            }
        manifest = seal(
            {
                "schema": "opensquilla.draco.campaign-final-manifest/v1",
                "status": "complete",
                "execution_pass": True,
                "policy_pass": True,
                "audit_pass": True,
                "result_count": 10,
                "task_count": 10,
                "groups": ["G1"],
                "selected_generation_attempt_bindings": {
                    f"G1/{row['task_id']}": row["campaign_finalization"]["selection"][
                        "selected_generation_attempt_id"
                    ]
                    for row in rows
                },
                "audit_sha256": audit["audit_sha256"],
                "openrouter_non_byok_campaign_proof_sha256": proof["proof_sha256"],
                "source_manifests": [{"execution_scheduling": {"task_concurrency": 6}}],
                "artifacts": artifacts,
                "cost_attribution": {
                    "campaign_bound_account_window_total_usd": 3.0,
                    "account_windows": [{"stable_poll_count": 6, "required_stable_poll_count": 6}],
                },
                "reconciliation": {"status": "stable", "stable": True},
            },
            "manifest_sha256",
        )
        write_json(root / "manifest.json", manifest)
        files = {
            "manifest": root / "manifest.json",
            "results": results_path,
            "trace": trace_path,
            "audit": root / "audit.json",
            "proof": root / "openrouter-non-byok-campaign-proof.json",
        }
        return {
            "reason": "complete",
            "checks": {"synthetic_contract": True},
            "arm_identity": {
                "arm_id": arm_id,
                "output_name": root.name,
                "run_id": "synthetic",
                "source_manifests": [{"manifest_sha256": "synthetic"}],
            },
            "artifact_sha256": {key: report.file_sha256(path) for key, path in files.items()},
        }

    def _controller_evidence(
        self,
        verifier: report.FrozenControllerVerifier,
        arm_id: str,
    ) -> dict:
        arm = verifier.arms[arm_id]
        override = verifier.module.resolve_arm_override(
            self.plan,
            arm,
            artifact=verifier.artifact,
            p99_receipt=(verifier.derived.get("p0_5_06") if verifier.derived is not None else None),
        )
        identity = verifier.module.arm_completion_identity(
            self.plan,
            arm,
            snapshot=verifier.snapshot,
            snapshot_identity=verifier.snapshot_identity,
            override=override,
        )
        root = Path(verifier.module.output_dir(self.plan, arm))
        write_json(root / "controller-publication-identity.json", identity)
        complete, evidence = verifier.module.inspect_complete_arm(
            root,
            expected_task_ids=set(self.task_ids),
            expected_task_concurrency=6,
            expected_identity=identity,
        )
        self.assertTrue(complete, evidence)
        return evidence

    def test_end_to_end_writes_group_root_markdown_and_json(self) -> None:
        output = self.root / "generated"
        args = argparse.Namespace(
            plan=self.plan_path,
            status=self.status_path,
            derived_plan=self.derived_path,
            price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
            output_root=output,
            allow_nonterminal=False,
            strict=True,
        )
        result, exit_code = report.generate(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["completion"]["status"], "complete")
        self.assertTrue((output / "P0-12" / "EXPERIMENT_RESULTS.md").is_file())
        self.assertTrue((output / "P0-12" / "EXPERIMENT_RESULTS.json").is_file())
        self.assertTrue((output / "EXPERIMENT_RESULTS.md").is_file())
        self.assertTrue((output / "EXPERIMENT_RESULTS.json").is_file())
        comparison = result["experiments"]["P0-12"]["comparisons"][0]
        self.assertEqual(comparison["pair_count"], 10)
        self.assertAlmostEqual(comparison["mean_delta_quality"], 1.0)
        self.assertAlmostEqual(
            result["arms"]["P0-12-E1"]["metrics"]["selected_generation_cost_counted_usd"], 2.0
        )
        self.assertEqual(
            len(result["derived"]["offline_effect"]["P0-12-E1"]["effective_changed_task_ids"]),
            10,
        )
        markdown = (output / "P0-12" / "EXPERIMENT_RESULTS.md").read_text()
        self.assertIn("Avg Gen$", markdown)
        self.assertIn("Judge$", markdown)
        self.assertIn("没有独立 SafetyGate", markdown)
        self.assertIn("失败/被替换 retry", markdown)
        self.assertIn("anchored-serial", markdown)
        self.assertIn("不是逐题 AB/BA", markdown)
        self.assertIn("Analyzer origin / fallback 取证", markdown)
        self.assertIn("Origin outcome distribution", markdown)
        self.assertTrue(result["schedule_evidence"]["valid"])
        self.assertFalse(result["schedule_evidence"]["strict_task_interleaving"])
        self.assertEqual(result["replay_control_drift"]["comparison_count"], 2)
        self.assertEqual(result["unique_arm_costs"]["unique_formal_arm_count"], 5)
        root_json = json.loads((output / "EXPERIMENT_RESULTS.json").read_text())
        self.assertTrue(report.validate_embedded_hash(root_json, "report_sha256"))
        group_json = json.loads((output / "P0-12" / "EXPERIMENT_RESULTS.json").read_text())
        self.assertTrue(report.validate_embedded_hash(group_json, "group_report_sha256"))
        self.assertEqual(
            report.METRIC_HEADER.splitlines()[0],
            "| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | Total Gen$ | Gen exact | Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | p50 ms | p95 ms |",
        )

    def test_controller_manifest_hash_binding_fails_closed(self) -> None:
        status = json.loads(self.status_path.read_text())
        status["arms"]["P0-12-E1"]["completion_evidence"]["artifact_sha256"]["manifest"] = "0" * 64
        status.pop("terminal_status_input_sha256")
        status["terminal_status_input_sha256"] = report.canonical_sha256(status)
        write_json(self.status_path, status)
        args = argparse.Namespace(
            plan=self.plan_path,
            status=self.status_path,
            derived_plan=self.derived_path,
            price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
            output_root=self.root / "tampered-output",
            allow_nonterminal=False,
            strict=True,
        )
        result, exit_code = report.generate(args)
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["completion"]["status"], "partial_or_failed")
        reasons = result["arms"]["P0-12-E1"]["formal_evidence_reasons"]
        self.assertIn(
            "terminal status completion evidence differs from frozen controller reinspection",
            reasons,
        )
        self.assertEqual(result["experiments"]["P0-12"]["comparisons"], [])
        self.assertEqual(result["unique_arm_costs"]["unique_formal_arm_count"], 4)

    def test_invalid_active_comparison_evidence_is_partial_and_strict_exit_two(self) -> None:
        output = self.root / "invalid-comparison-output"
        args = argparse.Namespace(
            plan=self.plan_path,
            status=self.status_path,
            derived_plan=self.derived_path,
            price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
            output_root=output,
            allow_nonterminal=False,
            strict=True,
        )
        original = report.build_experiment_inventory

        def invalidate_comparison(*call_args, **call_kwargs):
            experiments = original(*call_args, **call_kwargs)
            experiments["P0-12"]["comparison_evidence_valid"] = False
            experiments["P0-12"]["comparison_invalid_reasons"] = [
                "no candidate-order comparison slice is available"
            ]
            return experiments

        with mock.patch.object(
            report,
            "build_experiment_inventory",
            side_effect=invalidate_comparison,
        ):
            result, exit_code = report.generate(args)

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["completion"]["status"], "partial_or_failed")
        self.assertFalse(result["completion"]["comparison_evidence_valid"])
        self.assertEqual(
            result["completion"]["comparison_evidence_invalid_experiment_ids"],
            ["P0-12"],
        )

    def test_unfrozen_79_model_price_registry_is_rejected(self) -> None:
        original_path = self.snapshot / report.PRICE_REGISTRY_RELATIVE
        registry = json.loads(original_path.read_text())
        registry["models"][0]["registry_facts"]["price"]["input_per_million"] = 999.0
        replacement = self.root / "replacement-79.json"
        write_json(replacement, registry)
        args = argparse.Namespace(
            plan=self.plan_path,
            status=self.status_path,
            derived_plan=self.derived_path,
            price_registry=replacement,
            output_root=self.root / "replacement-output",
            allow_nonterminal=False,
            strict=True,
        )
        with self.assertRaisesRegex(
            report.ReportError,
            "frozen price registry differs from plan.freeze.model_registry",
        ):
            report.generate(args)

    def test_donor_relabelled_terminal_arm_is_rejected(self) -> None:
        status = json.loads(self.status_path.read_text())
        donor = json.loads(json.dumps(status["arms"]["common-E0-R1"]))
        status["arms"]["P0-12-E1"] = donor
        status.pop("terminal_status_input_sha256")
        status["terminal_status_input_sha256"] = report.canonical_sha256(status)
        write_json(self.status_path, status)
        result, exit_code = report.generate(
            argparse.Namespace(
                plan=self.plan_path,
                status=self.status_path,
                derived_plan=self.derived_path,
                price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
                output_root=self.root / "donor-output",
                allow_nonterminal=False,
                strict=True,
            )
        )
        attacked = result["arms"]["P0-12-E1"]
        self.assertEqual(exit_code, 2)
        self.assertEqual(attacked["state"], "controller_identity_mismatch")
        self.assertEqual(
            Path(attacked["output_dir"]).resolve(),
            self.arm_dirs["P0-12-E1"].resolve(),
        )
        self.assertFalse(attacked["controller_reinspection"]["terminal_evidence_matches"])

    def test_old_single_runner_identity_schema_is_rejected(self) -> None:
        publication_path = self.arm_dirs["P0-12-E1"] / "controller-publication-identity.json"
        publication = json.loads(publication_path.read_text())
        runners = publication.pop("runner_identities")
        runner_path, runner_sha = next(iter(runners.items()))
        publication["runner_path"] = runner_path
        publication["runner_sha256"] = runner_sha
        write_json(publication_path, publication)
        result, exit_code = report.generate(
            argparse.Namespace(
                plan=self.plan_path,
                status=self.status_path,
                derived_plan=self.derived_path,
                price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
                output_root=self.root / "old-runner-schema-output",
                allow_nonterminal=False,
                strict=True,
            )
        )
        attacked = result["arms"]["P0-12-E1"]
        self.assertEqual(exit_code, 2)
        self.assertEqual(attacked["state"], "controller_identity_mismatch")
        self.assertFalse(attacked["controller_reinspection"]["complete"])
        self.assertEqual(
            attacked["controller_reinspection"]["evidence"]["detail"],
            "expected runner identity set is missing",
        )

    def test_completed_with_failures_without_derived_plan_still_reports(self) -> None:
        self.derived_path.unlink()
        status = json.loads(self.status_path.read_text())
        status["phase"] = "completed_with_failures"
        status["arms"]["P0-12-E1"] = {
            "state": "failed",
            "output_dir": str(self.arm_dirs["P0-12-E1"]),
            "failure": {"reason": "derived_prerequisite_failed"},
        }
        status["terminal_freeze"]["phase"] = "completed_with_failures"
        status.pop("terminal_status_input_sha256")
        status["terminal_status_input_sha256"] = report.canonical_sha256(status)
        write_json(self.status_path, status)
        result, exit_code = report.generate(
            argparse.Namespace(
                plan=self.plan_path,
                status=self.status_path,
                derived_plan=self.derived_path,
                price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
                output_root=self.root / "failed-no-derived-output",
                allow_nonterminal=False,
                strict=True,
            )
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["completion"]["status"], "partial_or_failed")
        self.assertEqual(result["arms"]["P0-12-E1"]["state"], "failed")
        self.assertFalse(result["derived"]["available"])

    def test_frozen_controller_raw_hash_mismatch_is_rejected(self) -> None:
        controller_path = self.snapshot / report.FROZEN_CONTROLLER_RELATIVE
        controller_path.write_text(
            controller_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            report.ReportError,
            "frozen controller raw hash differs",
        ):
            report.generate(
                argparse.Namespace(
                    plan=self.plan_path,
                    status=self.status_path,
                    derived_plan=self.derived_path,
                    price_registry=self.snapshot / report.PRICE_REGISTRY_RELATIVE,
                    output_root=self.root / "controller-hash-output",
                    allow_nonterminal=False,
                    strict=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
