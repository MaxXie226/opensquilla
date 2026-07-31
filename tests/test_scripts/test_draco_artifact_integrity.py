from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.provider import ranking_router

ROOT = Path(__file__).resolve().parents[2]
PREPARE_CANARY = ROOT / "scripts" / "experiments" / "prepare_draco_b2_canary.py"
SEAL_ARTIFACTS = ROOT / "scripts" / "experiments" / "seal_draco_b2_artifacts.py"
CAPTURE_RUNTIME = ROOT / "scripts" / "experiments" / "capture_draco_runtime_environment.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sealer_registry_snapshot_selects_by_version_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_registry_select_test")
    raw = {
        "schema_version": "raw",
        "snapshot_version": "same-version",
        "models": [{"raw": True}],
    }
    legacy = {
        "schema_version": "legacy",
        "snapshot_version": "same-version",
        "models": [{"raw": False}],
    }
    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", lambda: raw)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )
    raw_contract = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(raw),
    )
    legacy_contract = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(legacy),
    )

    assert module._formal_registry_snapshot(raw_contract) is raw
    assert module._formal_registry_snapshot(legacy_contract) is legacy


def test_sealer_registry_snapshot_fails_closed_without_exact_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_registry_reject_test")
    raw = {"snapshot_version": "same-version", "models": [{"raw": True}]}
    legacy = {"snapshot_version": "same-version", "models": [{"raw": False}]}
    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", lambda: raw)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )
    wrong_hash = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256="0" * 64,
    )
    wrong_version = SimpleNamespace(
        source_registry_snapshot_version="other-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(raw),
    )

    with pytest.raises(ValueError, match="hash differs"):
        module._formal_registry_snapshot(wrong_hash)
    with pytest.raises(ValueError, match="version differs"):
        module._formal_registry_snapshot(wrong_version)


def test_full_result_hash_and_exact_trace_projection_fail_on_mutation() -> None:
    row = seal_result_row(
        {
            "row_index": 1,
            "group": "B2",
            "task_id": "task-1",
            "final_text": "answer",
            "execution": {"generation_attempts": [{"attempt": 1}]},
            "usage": {"billed_cost": 0.25},
        }
    )
    assert verify_result_row_evidence(row) is True
    trace = trace_row_from_result(row)
    assert trace == trace_row_from_result(row)

    changed_result = {**row, "final_text": "different"}
    assert verify_result_row_evidence(changed_result) is False
    changed_trace = json.loads(json.dumps(trace))
    changed_trace["execution"]["generation_attempts"][0]["attempt"] = 2
    assert changed_trace != trace_row_from_result(row)


def test_canary_is_disjoint_and_only_changes_scheduling_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(PREPARE_CANARY, "prepare_draco_canary_test")
    benchmark = tmp_path / "mini.jsonl"
    benchmark.write_text(
        json.dumps({"id": "formal-task", "prompt": "formal prompt"}) + "\n",
        encoding="utf-8",
    )
    base = {
        "profile_id": "frozen",
        "benchmark_input": {"task_count": 1},
        "runner": {"mode": "agent_loop", "concurrency": 5},
        "judge": {"model": "judge", "repeats": 3, "concurrency": 6},
        "ensemble": {"members": ["unchanged"]},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    output_input = tmp_path / "canary.jsonl"
    output_config = tmp_path / "canary-config.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PREPARE_CANARY),
            "--base-config",
            str(base_path),
            "--benchmark-input",
            str(benchmark),
            "--output-input",
            str(output_input),
            "--output-config",
            str(output_config),
        ],
    )

    assert module.main() == 0
    canary = json.loads(output_input.read_text(encoding="utf-8"))
    config = json.loads(output_config.read_text(encoding="utf-8"))
    assert canary["id"] != "formal-task"
    assert canary["prompt"] != "formal prompt"
    assert "web_search" in canary["prompt"] and "web_fetch" in canary["prompt"]
    assert config["runner"] == {"mode": "agent_loop", "concurrency": 1}
    assert config["judge"] == {
        "model": "judge",
        "repeats": 3,
        "concurrency": 1,
    }
    assert config["ensemble"] == base["ensemble"]
    assert os.stat(output_input).st_mode & 0o777 == 0o600
    assert os.stat(output_config).st_mode & 0o777 == 0o600


def test_artifact_snapshot_detects_post_audit_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_test")
    artifact = tmp_path / "result.jsonl"
    artifact.write_text("sealed\n", encoding="utf-8")
    artifact.chmod(0o600)
    snapshot = tmp_path / "snapshot.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SEAL_ARTIFACTS),
            "snapshot",
            str(snapshot),
            "--root",
            str(tmp_path),
            "--file",
            str(artifact),
        ],
    )
    assert module.main() == 0
    module.verify_snapshot(snapshot)

    artifact.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after audit"):
        module.verify_snapshot(snapshot)


def test_recursive_snapshot_is_closed_and_portable_after_archiving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_recursive_test")
    source = tmp_path / "source"
    nested = source / "strict-structure-audit"
    nested.mkdir(parents=True)
    result = source / "result.jsonl"
    report = nested / "report.json"
    result.write_text("sealed\n", encoding="utf-8")
    report.write_text("{}\n", encoding="utf-8")
    result.chmod(0o600)
    report.chmod(0o600)
    snapshot = source / "artifact-snapshot.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SEAL_ARTIFACTS),
            "snapshot",
            str(snapshot),
            "--root",
            str(source),
            "--recursive",
            "--allow-after",
            "FORMAL_RUN_SUCCESS.json",
        ],
    )
    assert module.main() == 0
    module.verify_snapshot(snapshot)

    extra = source / "unexpected.jsonl"
    extra.write_text("pollution\n", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(ValueError, match="artifact set changed"):
        module.verify_snapshot(snapshot)
    extra.unlink()

    archived = tmp_path / "archived"
    shutil.copytree(source, archived)
    archived_snapshot = archived / snapshot.name
    module.verify_snapshot(archived_snapshot)
    (archived / "result.jsonl").write_text("mutated copy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after audit"):
        module.verify_snapshot(archived_snapshot)


def test_runtime_environment_capture_is_verifiable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(CAPTURE_RUNTIME, "capture_draco_runtime_test")
    evidence = tmp_path / "runtime-environment.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_RUNTIME),
            "capture",
            str(evidence),
            "--repo",
            str(ROOT),
        ],
    )
    assert module.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_RUNTIME),
            "verify",
            str(evidence),
            "--repo",
            str(ROOT),
        ],
    )
    assert module.main() == 0

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["environment_sha256"] = "0" * 64
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime changed"):
        module.main()


def _owner_only_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _formal_g1_config(module, tmp_path: Path) -> tuple[Path, dict[str, object], str]:
    source = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    path = tmp_path / "experiment-config.json"
    _owner_only_json(path, config)
    return path, config, module.file_sha256(path)


def _v2_route_preflight(
    module,
    *,
    config_path: Path,
    config: dict[str, object],
    config_sha256: str,
) -> dict[str, object]:
    raw_g1 = config["g1_routing"]
    assert isinstance(raw_g1, dict)
    resolved, resolved_contract = module._resolved_g1_contract(config_path)
    routes = dict(resolved_contract["expected_routes"])
    candidate_scope = str(resolved_contract["candidate_scope"])
    candidate_policy = str(resolved_contract["policy"])
    required_parameters = module._formal_required_parameters(routes)
    return {
        "schema": module.ROUTE_PREFLIGHT_V2_SCHEMA,
        "api_origin": "https://openrouter.ai",
        "scope": "formal",
        "trust_env": False,
        "providers_response_sha256": "1" * 64,
        "candidate_scope": candidate_scope,
        "candidate_policy": candidate_policy,
        "expected_routes": routes,
        "expected_routes_sha256": module.canonical_sha256(routes),
        "experiment_config": {
            "path": str(config_path.resolve()),
            "sha256": config_sha256,
            "g1_routing_profile_id": resolved.profile_id,
            "source_registry_snapshot_version": resolved.source_registry_snapshot_version,
        },
        "required_parameters_sha256": module.canonical_sha256(required_parameters),
        "models": {
            model: {
                "expected_provider": provider,
                "response_model_id": model,
                "response_sha256": "2" * 64,
                "matching_endpoints": [
                    {
                        "tag": "any-upstream" if provider == "auto" else provider,
                        "provider_name": (
                            "Any Upstream"
                            if provider == "auto"
                            else module.EXPECTED_PROVIDER_NAMES[provider]
                        ),
                        "model_id": model,
                        "status": 0,
                        "supported_parameters": list(required_parameters[model]),
                    }
                ],
                "operational_match_count": 1,
                "compatible_operational_match_count": 1,
                "required_parameters": list(required_parameters[model]),
            }
            for model, provider in routes.items()
        },
        "route_metadata_pass": True,
    }


def _run_formal_success(
    module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    payloads: list[dict[str, object]],
    experiment_config_sha256: str,
) -> dict[str, object]:
    snapshots: list[Path] = []
    for index in range(3):
        artifact = tmp_path / f"artifact-{index}.jsonl"
        artifact.write_text(f"sealed-{index}\n", encoding="utf-8")
        artifact.chmod(0o600)
        snapshot = tmp_path / f"snapshot-{index}.json"
        module.atomic_write_json(
            snapshot,
            {
                "schema": module.SNAPSHOT_SCHEMA,
                "created_at": "2026-07-25T00:00:00+00:00",
                "root": ".",
                "closed_world": False,
                "allowed_after_snapshot": [],
                "artifacts": [module.relative_file_record(tmp_path, artifact)],
            },
        )
        snapshots.append(snapshot)
    evidence_paths: list[Path] = []
    for index, payload in enumerate(payloads):
        path = tmp_path / f"route-preflight-{index}.json"
        module.atomic_write_json(path, payload)
        evidence_paths.append(path)
    output = tmp_path / "FORMAL_RUN_SUCCESS.json"
    argv = [
        str(SEAL_ARTIFACTS),
        "success",
        str(output),
        "--source-git-head",
        "a" * 40,
        "--input-sha256",
        "b" * 64,
        "--gateway-config-sha256",
        "c" * 64,
        "--experiment-config-sha256",
        experiment_config_sha256,
    ]
    for snapshot in snapshots:
        argv.extend(("--snapshot", str(snapshot)))
    for evidence in evidence_paths:
        argv.extend(("--evidence", str(evidence)))
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_formal_success_accepts_recomputed_v2_route_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        "seal_draco_artifacts_v2_success_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    success = _run_formal_success(
        module,
        monkeypatch,
        tmp_path,
        payloads=[payload, json.loads(json.dumps(payload))],
        experiment_config_sha256=config_sha256,
    )
    evidence = success["route_preflight_evidence"]
    assert isinstance(evidence, list)
    assert {row["route_preflight_schema"] for row in evidence} == {payload["schema"]}
    contract = evidence[0]["formal_g1_contract"]
    _, resolved_contract = module._resolved_g1_contract(config_path)
    assert contract["candidate_scope"] == resolved_contract["candidate_scope"]
    assert contract["candidate_policy"] == resolved_contract["policy"]
    assert contract["expected_candidate_count"] == resolved_contract["expected_candidate_count"]
    assert contract["expected_routes_sha256"] == resolved_contract["expected_routes_sha256"]


def test_formal_success_rejects_v1_without_endpoint_details(tmp_path: Path) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v1_rejected_test")

    with pytest.raises(ValueError, match="lacks endpoint details"):
        module.validate_route_preflight_payload(
            {"schema": module.ROUTE_PREFLIGHT_V1_SCHEMA, "pass": True},
            experiment_config_sha256="a" * 64,
            label="legacy evidence",
        )


def test_route_preflight_auto_provider_still_binds_model_and_parameters() -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_auto_provider_test")
    parameters = ["max_tokens", "tools"]
    endpoints = [
        {
            "tag": "unfrozen-upstream",
            "provider_name": "Unfrozen Upstream",
            "model_id": "vendor/model",
            "status": 0,
            "supported_parameters": parameters,
        }
    ]

    assert module._recompute_endpoint_counts(
        model="vendor/model",
        expected_provider="auto",
        required_parameters=parameters,
        endpoints=endpoints,
        label="test",
    ) == (1, 1)

    endpoints[0]["model_id"] = "vendor/other"
    with pytest.raises(ValueError, match="no compatible route"):
        module._recompute_endpoint_counts(
            model="vendor/model",
            expected_provider="auto",
            required_parameters=parameters,
            endpoints=endpoints,
            label="test",
        )


def test_route_preflight_reasoning_requirements_follow_frozen_registry(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_reasoning_registry_test")
    config_path, _, _ = _formal_g1_config(module, tmp_path)
    _, resolved_contract = module._resolved_g1_contract(config_path)
    ineligible = set(resolved_contract["reasoning_ineligible_models"])
    routes = dict(resolved_contract["expected_routes"])
    required = module._formal_required_parameters(
        routes,
        reasoning_ineligible_models=ineligible,
    )

    assert len(ineligible) == 15
    assert "qwen/qwen3-coder-next" in ineligible
    assert "reasoning" not in required["qwen/qwen3-coder-next"]
    assert "reasoning" in required["deepseek/deepseek-v4-pro"]
    assert required["anthropic/claude-fable-5"] == [
        "max_tokens",
        "reasoning",
        "tools",
    ]
    assert required["openai/gpt-5.3-codex"] == [
        "max_tokens",
        "reasoning",
        "tools",
    ]
    assert required["openai/gpt-5.6-terra"] == [
        "max_completion_tokens",
        "reasoning",
        "tools",
    ]


@pytest.mark.parametrize(
    ("bad_field", "error_match"),
    [
        ("route_metadata_pass", "metadata did not pass"),
        ("scope", "scope must be formal"),
        ("expected_routes_sha256", "expected-routes hash differs"),
        ("g1_routing_profile_id", "G1 profile differs"),
        ("models", "model evidence set differs"),
        ("required_parameters_sha256", "required-parameters hash differs"),
        ("experiment_config_sha256", "experiment config hash differs"),
    ],
)
def test_v2_route_preflight_rejects_bad_contract_fields(
    bad_field: str,
    error_match: str,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        f"seal_draco_artifacts_v2_bad_{bad_field}_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    if bad_field == "route_metadata_pass":
        payload["route_metadata_pass"] = False
    elif bad_field == "scope":
        payload["scope"] = "b2"
    elif bad_field == "expected_routes_sha256":
        payload["expected_routes_sha256"] = "0" * 64
    elif bad_field == "g1_routing_profile_id":
        payload["experiment_config"]["g1_routing_profile_id"] = "wrong-profile"
    elif bad_field == "models":
        payload["models"].pop(next(iter(payload["models"])))
    elif bad_field == "required_parameters_sha256":
        payload["required_parameters_sha256"] = "0" * 64
    else:
        payload["experiment_config"]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match=error_match):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="test evidence",
        )


def test_route_preflight_set_rejects_mixed_v1_v2_schemas(tmp_path: Path) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_mixed_schema_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    v2_payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    with pytest.raises(ValueError, match="lacks endpoint details"):
        module.validate_route_preflight_set(
            [
                {"schema": module.ROUTE_PREFLIGHT_V1_SCHEMA, "pass": True},
                v2_payload,
            ],
            experiment_config_sha256=config_sha256,
            labels=["before canary", "before full"],
        )


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    [
        ("inflated_count", "precomputed endpoint counts differ"),
        ("wrong_status", "no compatible route"),
        ("wrong_provider_name", "no compatible route"),
        ("wrong_model_id", "no compatible route"),
        ("missing_parameter", "no compatible route"),
        ("wrong_tag", "endpoint provider tag differs"),
        ("weakened_required_parameters", "frozen parameters differ"),
    ],
)
def test_v2_route_preflight_recomputes_saved_endpoint_compatibility(
    mutation: str,
    error_match: str,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        f"seal_draco_artifacts_v2_endpoint_{mutation}_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    model = next(iter(payload["models"]))
    model_row = payload["models"][model]
    endpoint = model_row["matching_endpoints"][0]
    if mutation == "inflated_count":
        model_row["compatible_operational_match_count"] = 2
    elif mutation == "wrong_status":
        endpoint["status"] = 1
    elif mutation == "wrong_provider_name":
        endpoint["provider_name"] = "Wrong Provider"
    elif mutation == "wrong_model_id":
        endpoint["model_id"] = "wrong/model"
    elif mutation == "missing_parameter":
        endpoint["supported_parameters"].pop()
    elif mutation == "wrong_tag":
        endpoint["tag"] = "wrong-provider"
    else:
        model_row["required_parameters"] = model_row["required_parameters"][:-1]
        payload["required_parameters_sha256"] = module.canonical_sha256(
            {
                route_model: payload["models"][route_model]["required_parameters"]
                for route_model in payload["expected_routes"]
            }
        )

    if model_row["expected_provider"] == "auto" and mutation in {
        "wrong_provider_name",
        "wrong_tag",
    }:
        # ``auto`` authenticates an operational, model-matching, compatible
        # endpoint without freezing its upstream provider identity.
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="unfrozen provider evidence",
        )
        return

    with pytest.raises(ValueError, match=error_match):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="tampered evidence",
        )
