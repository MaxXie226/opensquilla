from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from opensquilla.provider.aggregator_prompt import (
    AGGREGATOR_PROMPT_VERSION_CURRENT,
    AGGREGATOR_PROMPT_VERSIONS,
    aggregator_prompt_version_evidence,
)
from opensquilla.provider.ensemble import (
    EnsembleMemberConfig,
    EnsembleProvider,
    _CandidateResult,
)
from opensquilla.provider.ranking_router import (
    DynamicRankingError,
    ranking_config_resolution,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import Message

ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "scripts" / "experiments" / "finalize_draco_campaign.py"


def _member(model: str) -> EnsembleMemberConfig:
    return EnsembleMemberConfig(
        provider_config=ProviderConfig(provider="fake", model=model),
        label=model,
    )


def _provider(
    version: str = AGGREGATOR_PROMPT_VERSION_CURRENT,
    *,
    routed: bool = True,
) -> EnsembleProvider:
    evidence = aggregator_prompt_version_evidence(version)
    return EnsembleProvider(
        profile_name="prompt-version-test",
        proposers=[_member("proposer")],
        aggregator=_member("aggregator"),
        min_successful_proposers=1,
        all_failed_policy="error",
        shuffle_candidates=False,
        aggregator_prompt_version=version,
        selection_plan=(
            {
                "strategy": "router_dynamic",
                "aggregator_prompt": evidence,
            }
            if routed
            else {}
        ),
    )


def _candidate() -> _CandidateResult:
    return _CandidateResult(
        index=0,
        sample_index=0,
        label="proposer",
        provider="fake",
        model="proposer",
        text="A supported draft with figure 42.",
    )


def _prompt(provider: EnsembleProvider) -> str:
    messages = provider._build_aggregator_messages(  # noqa: SLF001
        [Message(role="user", content="question")],
        [_candidate()],
    )
    return str(messages[-1].content)


def _load_finalizer():
    spec = importlib.util.spec_from_file_location(
        "finalize_draco_campaign_prompt_test",
        FINALIZER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_prompt_evidence_contains_complete_contract_and_canonical_sha() -> None:
    assert AGGREGATOR_PROMPT_VERSIONS == {
        "aggregator-v1-current",
        "aggregator-v2-verify-first",
        "aggregator-v3-preserve-best",
    }
    for version in AGGREGATOR_PROMPT_VERSIONS:
        evidence = aggregator_prompt_version_evidence(version)
        digest = evidence.pop("sha256")
        assert evidence["version"] == version
        assert evidence["description"]
        assert isinstance(evidence["additional_instructions"], list)
        assert (
            digest
            == hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )


def test_v1_explicit_version_is_byte_equivalent_to_default_prompt() -> None:
    default_provider = EnsembleProvider(
        profile_name="default",
        proposers=[_member("proposer")],
        aggregator=_member("aggregator"),
        shuffle_candidates=False,
    )
    explicit_provider = _provider(routed=False)

    assert _prompt(explicit_provider) == _prompt(default_provider)


def test_v2_and_v3_insert_only_their_declared_policy_lines() -> None:
    base = _prompt(_provider("aggregator-v1-current"))
    verify = _prompt(_provider("aggregator-v2-verify-first"))
    preserve = _prompt(_provider("aggregator-v3-preserve-best"))

    assert "Verification mode:" not in base
    assert "Preserve-best mode:" not in base
    assert "Verification mode:" in verify
    assert "Preserve-best mode:" not in verify
    assert "Preserve-best mode:" in preserve
    assert "Verification mode:" not in preserve


def test_prompt_evidence_is_copied_to_call_trace() -> None:
    provider = _provider("aggregator-v2-verify-first")
    trace = provider._trace_payload(  # noqa: SLF001
        [_candidate()],
        successful_count=1,
        fallback_used=False,
        fallback_reason="",
        final_request_role="aggregator",
    )

    expected = aggregator_prompt_version_evidence("aggregator-v2-verify-first")
    assert trace["aggregator_prompt"] == expected
    assert trace["selection_plan"]["aggregator_prompt"] == expected


def test_ranking_override_accepts_only_known_prompt_versions() -> None:
    resolution = ranking_config_resolution(
        override={
            "aggregator": {
                "prompt_version": "aggregator-v3-preserve-best",
            }
        }
    )
    assert (
        resolution["effective_config"]["aggregator"]["prompt_version"]
        == "aggregator-v3-preserve-best"
    )

    with pytest.raises(DynamicRankingError, match="aggregator.prompt_version"):
        ranking_config_resolution(override={"aggregator": {"prompt_version": "aggregator-v99"}})


def test_finalizer_authenticates_prompt_version_and_full_evidence() -> None:
    module = _load_finalizer()
    version = "aggregator-v2-verify-first"
    plan = {
        "ranking_parameters": {"aggregator": {"prompt_version": version}},
        "aggregator_prompt": aggregator_prompt_version_evidence(version),
    }
    assert module.g1_aggregator_prompt_plan_reason(plan) == ""

    tampered = deepcopy(plan)
    tampered["aggregator_prompt"]["description"] = "tampered"
    assert module.g1_aggregator_prompt_plan_reason(tampered) == "wrong_g1_aggregator_prompt"
    missing = deepcopy(plan)
    missing.pop("aggregator_prompt")
    assert module.g1_aggregator_prompt_plan_reason(missing) == "wrong_g1_aggregator_prompt"
    assert module.g1_aggregator_prompt_plan_reason({"ranking_parameters": {"aggregator": {}}}) == ""
