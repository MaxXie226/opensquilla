#!/usr/bin/env python3
"""Fail-closed, read-only OpenRouter endpoint preflight for formal DRACO runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.provider.compat_policy import (
    compat_policy_for_kind,
    model_matches_policy_prefix,
)

API_ORIGIN = "https://openrouter.ai"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPERIMENT_CONFIG_PATH = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
B2_EXPECTED_ROUTES = {
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    "google/gemini-3.1-pro-preview": "google-ai-studio",
}


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def formal_registry_snapshot(contract: Any) -> dict[str, Any]:
    from opensquilla.provider.ranking_router import (
        _legacy_registry_snapshot_projection,
        load_model_registry_snapshot,
    )

    raw_snapshot = load_model_registry_snapshot()
    candidates = (raw_snapshot, _legacy_registry_snapshot_projection(raw_snapshot))
    version_matches: list[dict[str, Any]] = []
    for snapshot in candidates:
        if str(snapshot.get("snapshot_version") or "") != contract.source_registry_snapshot_version:
            continue
        version_matches.append(snapshot)
        if canonical_sha256(snapshot) == contract.expected_source_registry_snapshot_sha256:
            return snapshot
    if not version_matches:
        raise ValueError("formal route preflight registry version differs")
    raise ValueError("formal route preflight registry hash differs")


def resolved_g1_contract(experiment_config: Path) -> tuple[Any, dict[str, Any]]:
    config = load_draco_experiment_config(experiment_config).config
    contract = config.g1_routing
    if contract is None:
        raise ValueError("formal route preflight requires g1_routing.expected_routes")
    if contract.candidate_scope == "exact_routes":
        assert contract.expected_routes is not None
        assert contract.expected_candidate_count is not None
        assert contract.expected_routes_sha256 is not None
        routes = dict(contract.expected_routes)
        policy = "exact_openrouter_routes"
    else:
        snapshot = formal_registry_snapshot(contract)
        rows = snapshot.get("models")
        if not isinstance(rows, list):
            raise ValueError("formal route preflight registry is malformed")
        models: set[str] = set()
        for row in rows:
            facts = row.get("registry_facts") if isinstance(row, dict) else None
            if not isinstance(facts, dict):
                raise ValueError("formal route preflight registry row is malformed")
            if str(facts.get("provider") or "").strip().lower() != "openrouter":
                continue
            model = str(facts.get("model_id") or "").strip().lower()
            if not model or model in models:
                raise ValueError("formal route preflight registry identity is malformed")
            models.add(model)
        routes = {model: "auto" for model in sorted(models)}
        policy = "all_registry_models"
    return config, {
        **contract.model_dump(mode="json", exclude_none=True),
        "candidate_scope": contract.candidate_scope,
        "policy": policy,
        "expected_candidate_count": len(routes),
        "expected_routes": routes,
        "expected_routes_sha256": canonical_sha256(routes),
    }


def formal_expected_routes(experiment_config: Path) -> dict[str, str]:
    _, contract = resolved_g1_contract(experiment_config)
    return dict(contract["expected_routes"])


FORMAL_EXPECTED_ROUTES = formal_expected_routes(DEFAULT_EXPERIMENT_CONFIG_PATH)
_FORMAL_CONFIG = load_draco_experiment_config(DEFAULT_EXPERIMENT_CONFIG_PATH).config
assert _FORMAL_CONFIG.g1_routing is not None
_FORMAL_REGISTRY_SNAPSHOT = formal_registry_snapshot(_FORMAL_CONFIG.g1_routing)
FORMAL_REASONING_INELIGIBLE_MODELS = frozenset(
    str(facts.get("model_id") or "").strip().lower()
    for row in _FORMAL_REGISTRY_SNAPSHOT.get("models") or []
    if isinstance(row, dict)
    and isinstance((facts := row.get("registry_facts")), dict)
    and str(facts.get("provider") or "").strip().lower() == "openrouter"
    and facts.get("supports_reasoning") is not True
)
EXPECTED_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "z-ai": "Z.AI",
    "moonshotai": "Moonshot AI",
    "alibaba": "Alibaba",
    "google-ai-studio": "Google AI Studio",
    "openai": "OpenAI",
    "xai": "xAI",
    "streamlake": "StreamLake",
    "groq": "Groq",
    "minimax": "Minimax",
    "mistral": "Mistral",
    "poolside": "Poolside",
    "tencent": "Tencent",
}
# Match the actual frozen request surface.  B2 proposers do not receive tool
# definitions; only the GLM aggregator can call the local tool surface.  The
# Gemini Judge is also text-only.  Over-requiring tool support on every
# proposer would reject an otherwise valid formal route before the canary.
B2_REQUIRED_PARAMETERS = {model: {"max_tokens", "reasoning"} for model in B2_EXPECTED_ROUTES}
B2_REQUIRED_PARAMETERS["deepseek/deepseek-v4-pro"].add("temperature")
B2_REQUIRED_PARAMETERS["z-ai/glm-5.2"] |= {
    "temperature",
    "tools",
}
B2_REQUIRED_PARAMETERS["qwen/qwen3.7-max"].add("temperature")
B2_REQUIRED_PARAMETERS["google/gemini-3.1-pro-preview"].add("temperature")
OPENROUTER_COMPAT_POLICY = compat_policy_for_kind("openrouter")
FORMAL_UNSUPPORTED_TEMPERATURE_MODELS = frozenset(
    model
    for model in FORMAL_EXPECTED_ROUTES
    if model_matches_policy_prefix(
        model,
        OPENROUTER_COMPAT_POLICY.unsupported_temperature_model_prefixes,
    )
)
FORMAL_MAX_COMPLETION_TOKENS_MODELS = frozenset(
    model
    for model in FORMAL_EXPECTED_ROUTES
    if model_matches_policy_prefix(
        model,
        OPENROUTER_COMPAT_POLICY.max_completion_tokens_model_prefixes,
    )
)


def formal_required_parameters(expected_routes: dict[str, str]) -> dict[str, set[str]]:
    required = {
        model: {
            (
                "max_completion_tokens"
                if model_matches_policy_prefix(
                    model,
                    OPENROUTER_COMPAT_POLICY.max_completion_tokens_model_prefixes,
                )
                else "max_tokens"
            ),
            "tools",
        }
        for model in expected_routes
    }
    for model in set(expected_routes) - FORMAL_REASONING_INELIGIBLE_MODELS:
        required[model].add("reasoning")
    for model in expected_routes:
        if not model_matches_policy_prefix(
            model,
            OPENROUTER_COMPAT_POLICY.unsupported_temperature_model_prefixes,
        ):
            required[model].add("temperature")
    return required


FORMAL_REQUIRED_PARAMETERS = formal_required_parameters(FORMAL_EXPECTED_ROUTES)


def get_json(client: httpx.Client, path: str) -> tuple[Any, str]:
    url = f"{API_ORIGIN}{path}"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.get(url)
            if response.is_redirect:
                raise RuntimeError(f"redirect refused for {path}")
            response.raise_for_status()
            payload = response.json()
            return payload, canonical_sha256(payload)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(float(attempt))
    raise RuntimeError(f"OpenRouter metadata request failed for {path}: {last_error}")


def tag_matches(tag: str, expected: str) -> bool:
    return tag == expected or tag.startswith(f"{expected}/")


def recompute_model_endpoint_compatibility(
    *,
    model: str,
    expected_provider: str,
    required_parameters: set[str],
    evidence: dict[str, Any],
) -> tuple[int, int]:
    """Recompute route compatibility from the endpoint rows being persisted."""

    if evidence.get("expected_provider") != expected_provider:
        raise ValueError(f"endpoint evidence provider differs for {model}")
    if evidence.get("response_model_id") != model:
        raise ValueError(f"endpoint response model differs for {model}")
    endpoints = evidence.get("matching_endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError(f"endpoint evidence is missing for {model}")
    provider_is_auto = expected_provider == "auto"
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not provider_is_auto and not expected_provider_name:
        raise ValueError(f"provider display-name contract is missing for {expected_provider}")

    operational_count = 0
    compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"endpoint evidence row is invalid for {model}")
        if not provider_is_auto and not tag_matches(
            str(endpoint.get("tag") or ""),
            expected_provider,
        ):
            raise ValueError(f"endpoint tag differs for {model} -> {expected_provider}")
        status = endpoint.get("status")
        operational = isinstance(status, int) and not isinstance(status, bool) and status == 0
        if not operational:
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"endpoint parameters are invalid for {model}")
        supported_parameters = (
            {str(item) for item in supported} if isinstance(supported, list) else set()
        )
        if (
            (provider_is_auto or endpoint.get("provider_name") == expected_provider_name)
            and endpoint.get("model_id") == model
            and required_parameters <= supported_parameters
        ):
            compatible_count += 1

    if operational_count <= 0:
        raise ValueError(f"no operational endpoint remains for {model}")
    if compatible_count <= 0:
        raise ValueError(f"no saved endpoint supports the frozen request surface for {model}")
    if evidence.get("operational_match_count") != operational_count:
        raise ValueError(f"saved operational endpoint count differs for {model}")
    if evidence.get("compatible_operational_match_count") != compatible_count:
        raise ValueError(f"saved compatible endpoint count differs for {model}")
    if evidence.get("required_parameters") != sorted(required_parameters):
        raise ValueError(f"saved required parameters differ for {model}")
    return operational_count, compatible_count


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--scope", choices=("b2", "formal"), default="formal")
    parser.add_argument(
        "--experiment-config",
        type=Path,
        default=DEFAULT_EXPERIMENT_CONFIG_PATH,
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite route preflight evidence: {args.output}")
    return args


def main() -> int:
    args = parse_args()

    experiment = load_draco_experiment_config(args.experiment_config).config
    resolved_contract = (
        resolved_g1_contract(args.experiment_config)[1] if args.scope == "formal" else None
    )
    expected_routes = (
        dict(resolved_contract["expected_routes"])
        if resolved_contract is not None
        else B2_EXPECTED_ROUTES
    )
    if args.scope == "formal" and experiment.g1_routing is None:
        raise SystemExit("formal route preflight requires g1_routing.expected_routes")
    candidate_scope = (
        str(resolved_contract["candidate_scope"])
        if resolved_contract is not None
        else "exact_routes"
    )
    candidate_policy = (
        str(resolved_contract["policy"])
        if resolved_contract is not None
        else "exact_openrouter_routes"
    )
    required_parameters = (
        formal_required_parameters(expected_routes)
        if args.scope == "formal"
        else B2_REQUIRED_PARAMETERS
    )

    with httpx.Client(
        timeout=httpx.Timeout(20.0),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/json"},
    ) as client:
        providers_payload, providers_sha256 = get_json(client, "/api/v1/providers")
        provider_rows = (
            providers_payload.get("data") if isinstance(providers_payload, dict) else None
        )
        if not isinstance(provider_rows, list):
            raise SystemExit("OpenRouter providers response has an invalid schema")
        provider_slugs = {
            str(row.get("slug"))
            for row in provider_rows
            if isinstance(row, dict) and row.get("slug")
        }
        missing_slugs = sorted(set(expected_routes.values()) - {"auto"} - provider_slugs)
        if missing_slugs:
            raise SystemExit(f"OpenRouter provider slug(s) unavailable: {missing_slugs}")

        model_evidence: dict[str, Any] = {}
        for model, expected_provider in expected_routes.items():
            encoded_model = "/".join(quote(part, safe="") for part in model.split("/"))
            payload, response_sha256 = get_json(
                client,
                f"/api/v1/models/{encoded_model}/endpoints",
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            endpoints = data.get("endpoints") if isinstance(data, dict) else None
            if not isinstance(endpoints, list):
                raise SystemExit(f"OpenRouter endpoint schema invalid for {model}")
            if data.get("id") != model:
                raise SystemExit(f"OpenRouter endpoint response model differs for {model}")
            provider_is_auto = expected_provider == "auto"
            matches = [
                row
                for row in endpoints
                if isinstance(row, dict)
                and (provider_is_auto or tag_matches(str(row.get("tag") or ""), expected_provider))
            ]
            operational = [row for row in matches if row.get("status") == 0]
            compatible = [
                row
                for row in operational
                if required_parameters[model]
                <= {str(item) for item in (row.get("supported_parameters") or [])}
                and (
                    provider_is_auto
                    or row.get("provider_name") == EXPECTED_PROVIDER_NAMES[expected_provider]
                )
                and row.get("model_id") == model
            ]
            if not matches:
                raise SystemExit(f"No OpenRouter endpoint matches {model} -> {expected_provider}")
            if not operational:
                raise SystemExit(
                    f"No operational OpenRouter endpoint for {model} -> {expected_provider}"
                )
            if not compatible:
                raise SystemExit(
                    f"No operational endpoint supports the frozen request surface for {model}"
                )
            saved_evidence = {
                "expected_provider": expected_provider,
                "response_model_id": data.get("id"),
                "response_sha256": response_sha256,
                "matching_endpoints": [
                    {
                        "tag": row.get("tag"),
                        "provider_name": row.get("provider_name"),
                        "model_id": row.get("model_id"),
                        "status": row.get("status"),
                        "supported_parameters": sorted(
                            str(item) for item in (row.get("supported_parameters") or [])
                        ),
                        "pricing": row.get("pricing"),
                        "max_completion_tokens": row.get("max_completion_tokens"),
                    }
                    for row in matches
                ],
                "operational_match_count": len(operational),
                "compatible_operational_match_count": len(compatible),
                "required_parameters": sorted(required_parameters[model]),
            }
            recompute_model_endpoint_compatibility(
                model=model,
                expected_provider=expected_provider,
                required_parameters=required_parameters[model],
                evidence=saved_evidence,
            )
            model_evidence[model] = saved_evidence

    evidence = {
        "schema": "opensquilla.openrouter-route-preflight/v2",
        "captured_at": datetime.now(UTC).isoformat(),
        "api_origin": API_ORIGIN,
        "scope": args.scope,
        "trust_env": False,
        "providers_response_sha256": providers_sha256,
        "candidate_scope": candidate_scope,
        "candidate_policy": candidate_policy,
        "expected_routes": expected_routes,
        "expected_routes_sha256": canonical_sha256(expected_routes),
        "experiment_config": {
            "path": str(args.experiment_config.expanduser().resolve()),
            "sha256": hashlib.sha256(
                args.experiment_config.expanduser().resolve().read_bytes()
            ).hexdigest(),
            "g1_routing_profile_id": (
                experiment.g1_routing.profile_id if experiment.g1_routing is not None else None
            ),
            "source_registry_snapshot_version": (
                experiment.g1_routing.source_registry_snapshot_version
                if experiment.g1_routing is not None
                else None
            ),
        },
        "required_parameters_sha256": canonical_sha256(
            {model: sorted(parameters) for model, parameters in required_parameters.items()}
        ),
        "models": model_evidence,
        "route_metadata_pass": True,
        "non_byok_verified": None,
        "billing_verified": None,
        "reasoning_ineligible_models": (
            sorted(FORMAL_REASONING_INELIGIBLE_MODELS) if args.scope == "formal" else []
        ),
        "scope_note": (
            "Public metadata availability only; per-request router metadata, non-BYOK "
            "usage evidence, canary, and account reconciliation remain mandatory."
        ),
    }
    atomic_write_json(args.output, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
