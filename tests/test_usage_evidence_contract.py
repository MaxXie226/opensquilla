from __future__ import annotations

from copy import deepcopy

import pytest

from opensquilla.usage_evidence import (
    USAGE_EVIDENCE_SCHEMA,
    UsageEvidenceError,
    canonical_run_usage_units,
    canonicalize_run_usage,
    derive_physical_request_count,
    is_missing_usage_placeholder,
)


@pytest.mark.parametrize(
    "run",
    [
        {"llm_request_count": 1, "usage": {}},
        {"usage": {"usage_missing_count": 1}},
        {
            "llm_request_count": 1,
            "usage_unknown_count": 1,
            "usage": {},
        },
    ],
)
def test_empty_or_counter_only_usage_materializes_one_placeholder(
    run: dict[str, object],
) -> None:
    original = deepcopy(run)

    units = canonical_run_usage_units(
        run,
        identity_seed="judge-attempt-1",
        requested_provider="openrouter",
        requested_model="openai/gpt-5.5",
        role="agent_llm_request_unknown",
    )

    assert run == original
    assert derive_physical_request_count(run) == 1
    assert len(units) == 1
    placeholder = units[0]
    assert placeholder == {
        "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
        "usage_evidence_id": placeholder["usage_evidence_id"],
        "usage_evidence_source": "physical_request_counter_deficit",
        "role": "agent_llm_request_unknown",
        "physical_request_ordinal": 1,
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "openai/gpt-5.5",
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "usage_unknown": True,
        "provider_usage": {
            "usage_unknown": True,
            "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
            "usage_evidence_id": placeholder["usage_evidence_id"],
        },
    }
    assert placeholder["usage_evidence_id"].startswith("sha256:")
    assert len(placeholder["usage_evidence_id"]) == len("sha256:") + 64
    assert is_missing_usage_placeholder(placeholder)


@pytest.mark.parametrize(
    "run",
    [
        {"llm_request_count": 0, "usage": {}},
        {"physical_request_count": 0, "usage": {}},
        {"request_started": False, "usage": {}},
    ],
)
def test_explicit_zero_request_count_stays_empty(run: dict[str, object]) -> None:
    assert derive_physical_request_count(run, default_request_count=7) == 0
    assert (
        canonical_run_usage_units(
            run,
            identity_seed="explicit-zero",
            default_request_count=7,
        )
        == []
    )
    assert (
        canonicalize_run_usage(
            run,
            identity_seed="explicit-zero",
            default_request_count=7,
        )
        == {}
    )


def test_declared_request_count_materializes_exactly_n_placeholders() -> None:
    run = {
        "physical_request_count": 3,
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-sonnet-5",
        "usage": {},
    }

    units = canonical_run_usage_units(run, identity_seed="three-requests")

    assert len(units) == 3
    assert [unit["physical_request_ordinal"] for unit in units] == [1, 2, 3]
    assert len({unit["usage_evidence_id"] for unit in units}) == 3
    assert all(unit["usage_unknown"] is True for unit in units)
    assert all(unit["requested_provider"] == "openrouter" for unit in units)
    assert all(unit["requested_model"] == "anthropic/claude-sonnet-5" for unit in units)


def test_real_receipt_is_preserved_and_only_the_deficit_is_materialized() -> None:
    receipt = {
        "role": "judge",
        "provider": "openrouter",
        "model": "openai/gpt-5.5",
        "input_tokens": 11,
        "output_tokens": 7,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
    }
    run = {
        "llm_request_count": 2,
        "usage": {
            "usage_missing_count": 1,
            "model_usage_breakdown": [receipt],
        },
    }

    canonical = canonicalize_run_usage(
        run,
        identity_seed="receipt-plus-missing",
        requested_provider="openrouter",
        requested_model="openai/gpt-5.5",
    )

    units = canonical["model_usage_breakdown"]
    assert units[0] == receipt
    assert len(units) == 2
    assert is_missing_usage_placeholder(units[1])
    assert units[1]["physical_request_ordinal"] == 2
    assert canonical["usage_missing_count"] == 1
    assert canonical["usage_evidence_schema"] == USAGE_EVIDENCE_SCHEMA


def test_existing_placeholder_is_not_duplicated() -> None:
    receipt = {
        "role": "judge",
        "provider": "openrouter",
        "model": "openai/gpt-5.5",
        "input_tokens": 1,
        "output_tokens": 1,
    }
    existing_placeholder = {
        "role": "usage_missing",
        "usage_unknown": True,
        "usage_evidence_id": "sha256:existing",
        "physical_request_ordinal": 2,
    }
    run = {
        "llm_request_count": 2,
        "usage": {
            "usage_missing_count": 1,
            "model_usage_breakdown": [receipt, existing_placeholder],
        },
    }

    first = canonicalize_run_usage(run, identity_seed="idempotent")
    replay = canonicalize_run_usage(
        {"llm_request_count": 2, "usage": first},
        identity_seed="idempotent",
    )

    assert first["model_usage_breakdown"] == [receipt, existing_placeholder]
    assert replay["model_usage_breakdown"] == [receipt, existing_placeholder]
    assert replay["usage_missing_count"] == 1


def test_placeholder_identity_is_deterministic_and_seed_scoped() -> None:
    run = {"llm_request_count": 2, "usage": {}}

    first = canonical_run_usage_units(run, identity_seed="stable-seed")
    second = canonical_run_usage_units(run, identity_seed="stable-seed")
    different_seed = canonical_run_usage_units(run, identity_seed="other-seed")

    assert first == second
    assert [unit["usage_evidence_id"] for unit in first] != [
        unit["usage_evidence_id"] for unit in different_seed
    ]


def test_more_usage_units_than_declared_requests_fails_closed() -> None:
    run = {
        "llm_request_count": 1,
        "usage": {
            "model_usage_breakdown": [
                {"role": "judge", "input_tokens": 1},
                {"role": "judge", "input_tokens": 2},
            ]
        },
    }

    with pytest.raises(
        UsageEvidenceError,
        match=r"more physical requests than declared \(2 > 1\)",
    ):
        derive_physical_request_count(run)

    with pytest.raises(UsageEvidenceError):
        canonical_run_usage_units(run, identity_seed="contradiction")


@pytest.mark.parametrize(
    "declared_count",
    [-1, -1.0, 1.5, True, "not-an-integer"],
)
def test_invalid_declared_request_count_fails_closed(declared_count: object) -> None:
    with pytest.raises(
        UsageEvidenceError,
        match="must be a nonnegative integer",
    ):
        derive_physical_request_count({"llm_request_count": declared_count, "usage": {}})


def test_none_request_count_is_absent_not_an_invalid_declaration() -> None:
    assert (
        derive_physical_request_count(
            {"llm_request_count": None, "usage": {}},
            default_request_count=2,
        )
        == 2
    )


def test_conflicting_physical_and_llm_request_counts_fail_closed() -> None:
    with pytest.raises(
        UsageEvidenceError,
        match="conflicting physical request count declarations",
    ):
        derive_physical_request_count(
            {
                "physical_request_count": 1,
                "llm_request_count": 2,
                "usage": {},
            }
        )


@pytest.mark.parametrize(
    "breakdown",
    [
        {"role": "judge"},
        ("not", "a", "list"),
        "not-a-list",
        1,
    ],
)
def test_non_list_usage_breakdown_fails_closed(breakdown: object) -> None:
    with pytest.raises(
        UsageEvidenceError,
        match="model_usage_breakdown must be a list",
    ):
        canonical_run_usage_units(
            {
                "llm_request_count": 1,
                "usage": {"model_usage_breakdown": breakdown},
            },
            identity_seed="invalid-breakdown",
        )


def test_usage_breakdown_with_non_mapping_unit_fails_closed() -> None:
    with pytest.raises(
        UsageEvidenceError,
        match="contains a non-mapping usage unit",
    ):
        canonical_run_usage_units(
            {
                "llm_request_count": 2,
                "usage": {
                    "model_usage_breakdown": [
                        {"role": "judge"},
                        None,
                    ]
                },
            },
            identity_seed="invalid-breakdown-unit",
        )
