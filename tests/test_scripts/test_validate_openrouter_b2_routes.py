from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.gateway.llm_runtime import OPENROUTER_DEFAULT_PROVIDER_ROUTING

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "experiments" / "validate_openrouter_b2_routes.py"
REGISTRY_PATH = ROOT / "src" / "opensquilla" / "provider" / "router_dynamic_model_profiles.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_openrouter_b2_routes_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def test_formal_scope_is_default(tmp_path: Path) -> None:
    args = validator.parse_args([str(tmp_path / "evidence.json")])

    assert args.scope == "formal"
    assert args.experiment_config == validator.DEFAULT_EXPERIMENT_CONFIG_PATH


def test_formal_routes_are_a_valid_subset_of_router_dynamic_registry() -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    registry_models = {str(row["registry_facts"]["model_id"]) for row in payload["models"]}

    assert len(registry_models) == 80
    assert set(validator.FORMAL_EXPECTED_ROUTES) <= registry_models
    assert set(validator.B2_EXPECTED_ROUTES) <= set(validator.FORMAL_EXPECTED_ROUTES)
    experiment = load_draco_experiment_config(validator.DEFAULT_EXPERIMENT_CONFIG_PATH).config
    assert experiment.g1_routing is not None
    _, resolved = validator.resolved_g1_contract(validator.DEFAULT_EXPERIMENT_CONFIG_PATH)
    assert validator.FORMAL_EXPECTED_ROUTES == resolved["expected_routes"]
    assert (
        validator.canonical_sha256(validator.FORMAL_EXPECTED_ROUTES)
        == (resolved["expected_routes_sha256"])
    )


def test_formal_routes_match_runtime_pins_and_capability_contract() -> None:
    experiment = load_draco_experiment_config(validator.DEFAULT_EXPERIMENT_CONFIG_PATH).config
    assert experiment.g1_routing is not None
    candidate_scope = getattr(experiment.g1_routing, "candidate_scope", "exact_routes")
    for model, provider in validator.FORMAL_EXPECTED_ROUTES.items():
        if candidate_scope == "registry_all":
            assert provider == "auto"
        else:
            assert OPENROUTER_DEFAULT_PROVIDER_ROUTING[model] == provider
        required = validator.FORMAL_REQUIRED_PARAMETERS[model]
        assert {"max_tokens", "tools"} <= required
        assert ("reasoning" in required) is (
            model not in validator.FORMAL_REASONING_INELIGIBLE_MODELS
        )
        assert ("temperature" in required) is (
            model not in validator.FORMAL_UNSUPPORTED_TEMPERATURE_MODELS
        )
    if candidate_scope == "registry_all":
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        registry_models = {
            str(row["registry_facts"]["model_id"])
            for row in payload["models"]
            if row["registry_facts"]["provider"] == "openrouter"
        }
        assert validator.FORMAL_EXPECTED_ROUTES == dict.fromkeys(registry_models, "auto")
        assert validator.FORMAL_REASONING_INELIGIBLE_MODELS == {
            str(row["registry_facts"]["model_id"])
            for row in payload["models"]
            if row["registry_facts"]["provider"] == "openrouter"
            and row["registry_facts"]["supports_reasoning"] is not True
        }
        assert len(validator.FORMAL_REASONING_INELIGIBLE_MODELS) == 15
    else:
        assert validator.FORMAL_EXPECTED_ROUTES["google/gemini-3.5-flash"] == ("google-ai-studio")
        assert "openai/gpt-5.6-luna" not in validator.FORMAL_EXPECTED_ROUTES
    assert validator.FORMAL_REQUIRED_PARAMETERS["google/gemini-3.5-flash"] == {
        "max_tokens",
        "reasoning",
        "temperature",
        "tools",
    }


def _saved_endpoint_evidence(
    *,
    model: str,
    provider: str,
    required_parameters: set[str],
) -> dict[str, object]:
    return {
        "expected_provider": provider,
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": provider,
                "provider_name": validator.EXPECTED_PROVIDER_NAMES[provider],
                "model_id": model,
                "status": 0,
                "supported_parameters": sorted(required_parameters),
            },
            {
                "tag": f"{provider}/backup",
                "provider_name": validator.EXPECTED_PROVIDER_NAMES[provider],
                "model_id": model,
                "status": 1,
                "supported_parameters": sorted(required_parameters),
            },
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 1,
        "required_parameters": sorted(required_parameters),
    }


def test_validator_recomputes_saved_endpoint_compatibility() -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = "deepseek"
    evidence = _saved_endpoint_evidence(
        model=model,
        provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
    )

    assert validator.recompute_model_endpoint_compatibility(
        model=model,
        expected_provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
        evidence=evidence,
    ) == (1, 1)


def test_validator_rejects_tampered_precomputed_compatible_count() -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = "deepseek"
    evidence = _saved_endpoint_evidence(
        model=model,
        provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
    )
    evidence["compatible_operational_match_count"] = 2

    try:
        validator.recompute_model_endpoint_compatibility(
            model=model,
            expected_provider=provider,
            required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
            evidence=evidence,
        )
    except ValueError as exc:
        assert "compatible endpoint count differs" in str(exc)
    else:
        raise AssertionError("tampered compatible count must fail closed")


def test_validator_auto_provider_accepts_matching_operational_endpoint() -> None:
    model = "vendor/model"
    required = {"max_tokens", "tools"}
    evidence = {
        "expected_provider": "auto",
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": "any-upstream",
                "provider_name": "Any Upstream",
                "model_id": model,
                "status": 0,
                "supported_parameters": sorted(required),
            }
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 1,
        "required_parameters": sorted(required),
    }

    assert validator.recompute_model_endpoint_compatibility(
        model=model,
        expected_provider="auto",
        required_parameters=required,
        evidence=evidence,
    ) == (1, 1)


def test_validator_auto_provider_rejects_wrong_serving_model() -> None:
    model = "vendor/model"
    required = {"max_tokens", "tools"}
    evidence = {
        "expected_provider": "auto",
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": "any-upstream",
                "provider_name": "Any Upstream",
                "model_id": "vendor/other",
                "status": 0,
                "supported_parameters": sorted(required),
            }
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 0,
        "required_parameters": sorted(required),
    }

    try:
        validator.recompute_model_endpoint_compatibility(
            model=model,
            expected_provider="auto",
            required_parameters=required,
            evidence=evidence,
        )
    except ValueError as exc:
        assert "no saved endpoint supports" in str(exc)
    else:
        raise AssertionError("auto provider must still bind the serving model")
