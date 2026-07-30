from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from opensquilla.provider.thinking_execution import (
    THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    immutable_selection_plan_payload,
    project_thinking_execution_history,
    restore_projected_thinking_execution,
    validate_thinking_execution_call,
    validate_thinking_execution_history_closure,
    validate_thinking_execution_plan_mutation,
)


def _detail(
    identity: str,
    *,
    role: str,
    initial: str = "high",
) -> dict[str, object]:
    return {
        "identity": identity,
        "model_id": identity.partition(":")[2],
        "role": role,
        "requested_level": "high",
        "effective_level": initial,
        "provider_level": initial,
        "fallback_reason": "",
        "reasons": [],
        "provider_rejection_fallbacks": [
            {
                "unified_level": "medium",
                "provider_level": "medium",
                "reason": "provider_rejection_fallback",
            },
            {
                "unified_level": "low",
                "provider_level": "low",
                "reason": "provider_rejection_fallback",
            },
        ],
    }


def _receipt(
    stage: str,
    identity: str,
    *,
    result: str = "succeeded",
    rejected: str = "high",
    effective: str = "medium",
    policy: str = "thinking-policy-v1",
) -> dict[str, object]:
    return {
        "trigger_stage": stage,
        "fallback_type": "thinking_level_neighbor",
        "reason": "provider_rejected_thinking_level",
        "identity": identity,
        "requested_thinking_level": "high",
        "rejected_unified_level": rejected,
        "rejected_provider_level": rejected,
        "effective_thinking_level": effective,
        "effective_provider_level": effective,
        "thinking_policy_version": policy,
        "fallback_result": result,
    }


def test_immutable_plan_ignores_only_recursive_analyzer_billing_receipts() -> None:
    routing_plan = {
        "decision_id": "decision-a",
        "task_analyzer": {
            "source": "remote",
            "usage": {
                "input_tokens": 11,
                "physical_attempt_id": "a" * 32,
                "billing_receipt": {"status": "confirmed"},
                "physical_attempts": [
                    {
                        "input_tokens": 11,
                        "physical_attempt_id": "a" * 32,
                        "billing_receipt": {"status": "confirmed"},
                    }
                ],
            },
        },
    }
    physical_plan = deepcopy(routing_plan)
    physical_plan["task_analyzer"]["usage"]["billing_receipt"] = (
        "ProviderBillingReceipt(status='confirmed')"
    )
    physical_plan["task_analyzer"]["usage"]["physical_attempts"][0][
        "billing_receipt"
    ] = "ProviderBillingReceipt(status='confirmed')"

    assert immutable_selection_plan_payload(
        routing_plan
    ) == immutable_selection_plan_payload(physical_plan)
    assert validate_thinking_execution_plan_mutation(
        routing_plan,
        physical_plan,
    ) == ""

    physical_plan["task_analyzer"]["usage"]["input_tokens"] += 1
    assert immutable_selection_plan_payload(
        routing_plan
    ) != immutable_selection_plan_payload(physical_plan)
    assert validate_thinking_execution_plan_mutation(
        routing_plan,
        physical_plan,
    ) == "thinking_execution_immutable_plan_mismatch"


def _plan(
    decision_id: str,
    *,
    proposers: list[str],
    aggregators: list[str],
    receipts: list[dict[str, object]] | None = None,
    policy: str = "thinking-policy-v1",
    initial_by_identity: dict[str, str] | None = None,
) -> dict[str, object]:
    initial_by_identity = initial_by_identity or {}
    rows = list(receipts or [])
    proposer_levels = {
        identity: initial_by_identity.get(identity, "high")
        for identity in proposers
    }
    aggregator_levels = {
        identity: initial_by_identity.get(identity, "high")
        for identity in aggregators
    }
    for row in rows:
        identity = str(row["identity"])
        if row["trigger_stage"] == "proposer_execution":
            proposer_levels[identity] = str(row["effective_thinking_level"])
        else:
            aggregator_levels[identity] = str(row["effective_thinking_level"])
    return {
        "decision_id": decision_id,
        "selected_P": proposers,
        "selected_A": aggregators[0],
        "aggregator_candidates": aggregators,
        "ranking_thinking_assignment_enabled": True,
        "thinking_assignment_details": {
            "proposers": [
                _detail(
                    identity,
                    role="proposer",
                    initial=initial_by_identity.get(identity, "high"),
                )
                for identity in proposers
            ],
            "aggregator": _detail(
                aggregators[0],
                role="aggregator",
                initial=initial_by_identity.get(aggregators[0], "high"),
            ),
            "aggregator_candidates": [
                _detail(
                    identity,
                    role="aggregator" if index == 0 else "aggregator_fallback",
                    initial=initial_by_identity.get(identity, "high"),
                )
                for index, identity in enumerate(aggregators)
            ],
        },
        "executed_thinking_assignment": {
            "proposers": proposer_levels,
            "aggregator": aggregator_levels[aggregators[0]],
            "thinking_policy_version": policy,
        },
        "thinking_execution_fallbacks": deepcopy(rows),
    }


def _aggregator_execution(
    identity: str,
    *,
    level: str,
) -> dict[str, object]:
    provider, _, model = identity.partition(":")
    return {
        "role": "aggregator",
        "requested_provider": provider,
        "provider": provider,
        "requested_model": model,
        "model": model,
        "assigned_thinking_level": level,
        "effective_thinking_level": level,
        "provider_thinking_level": level,
        "thinking_override": level,
        "effective_thinking": level != "off",
        "effective_provider_thinking_level": level,
        "thinking_policy_managed": True,
    }


def _strict_aggregator_transition_call() -> tuple[
    dict[str, object],
    dict[str, object],
]:
    identity = "openrouter:model-a"
    receipt = _receipt("aggregator_execution", identity)
    previous = _plan(
        "decision-a",
        proposers=["openrouter:model-p"],
        aggregators=[identity],
    )
    observed = _plan(
        "decision-a",
        proposers=["openrouter:model-p"],
        aggregators=[identity],
        receipts=[receipt],
    )
    previous["thinking_physical_evidence_schema"] = (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA
    )
    observed["thinking_physical_evidence_schema"] = (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA
    )
    attempts = [
        {
            "attempt": 1,
            "kind": "primary",
            "request_started": True,
            "physical_request_count": 1,
            "physical_attempt_id": "1" * 32,
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "outcome": "failed",
            "code": "transient_error",
            "execution": _aggregator_execution(identity, level="high"),
        },
        {
            "attempt": 2,
            "kind": "primary",
            "request_started": True,
            "physical_request_count": 1,
            "physical_attempt_id": "2" * 32,
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "outcome": "abandoned",
            "code": "invalid_reasoning_effort",
            "thinking_fallback_rejection_reason": (
                "provider_rejected_thinking_level"
            ),
            "thinking_fallback_binding": {
                "receipt": deepcopy(receipt),
                "rejected_physical_attempt_id": "2" * 32,
            },
            "execution": _aggregator_execution(identity, level="high"),
        },
        {
            "attempt": 3,
            "kind": "primary",
            "request_started": True,
            "physical_request_count": 1,
            "physical_attempt_id": "3" * 32,
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "outcome": "succeeded",
            "execution": _aggregator_execution(
                identity,
                level="medium",
            ),
        },
    ]
    call = {
        "selection_plan": observed,
        "candidates": [],
        "aggregator_recovery": {
            "attempts": attempts,
            "selected_attempt": 3,
        },
        "llm_request_count": 3,
        "physical_request_count": 3,
        "final_request": {
            "usage": {
                "physical_attempt_id": "3" * 32,
                "provider_usage": {
                    "physical_attempt_id": "3" * 32,
                },
            }
        },
    }
    return previous, call


def test_strict_aggregator_transition_binds_rejected_request_to_immediate_retry(
) -> None:
    previous, call = _strict_aggregator_transition_call()

    _, reason = validate_thinking_execution_call(previous, call)

    assert reason == ""


def test_strict_aggregator_transition_rejects_binding_moved_to_earlier_retry(
) -> None:
    previous, call = _strict_aggregator_transition_call()
    attempts = call["aggregator_recovery"]["attempts"]
    binding = attempts[1].pop("thinking_fallback_binding")
    rejection_reason = attempts[1].pop(
        "thinking_fallback_rejection_reason"
    )
    binding["rejected_physical_attempt_id"] = "1" * 32
    attempts[0]["thinking_fallback_binding"] = binding
    attempts[0]["thinking_fallback_rejection_reason"] = (
        rejection_reason
    )

    _, reason = validate_thinking_execution_call(previous, call)

    assert reason == (
        "aggregator_thinking_fallback_not_immediate_successor"
    )


def test_projection_carries_failed_proposer_and_secondary_aggregator_receipts() -> None:
    proposer = "openrouter:model-p"
    secondary = "openrouter:model-a2"
    source = _plan(
        "decision-a",
        proposers=[proposer, "openrouter:model-old"],
        aggregators=["openrouter:model-a1", secondary],
        receipts=[
            _receipt("proposer_execution", proposer, result="failed"),
            _receipt("aggregator_execution", secondary),
        ],
    )
    target = _plan(
        "decision-b",
        proposers=[proposer, "openrouter:model-new"],
        aggregators=[secondary, "openrouter:model-a1"],
    )

    projected, audit, reason = project_thinking_execution_history(
        [source],
        target,
    )

    assert reason == ""
    assert projected["executed_thinking_assignment"]["proposers"][proposer] == "medium"
    assert projected["executed_thinking_assignment"]["aggregator"] == "medium"
    assert [row["fallback_result"] for row in projected["thinking_execution_fallbacks"]] == [
        "failed",
        "succeeded",
    ]
    assert audit["carried_receipt_count"] == 2
    assert audit["dropped_receipt_count"] == 0


def test_projection_does_not_carry_receipt_across_role_switch() -> None:
    identity = "openrouter:model-shared"
    source = _plan(
        "decision-a",
        proposers=["openrouter:model-p"],
        aggregators=["openrouter:model-a1", identity],
        receipts=[_receipt("aggregator_execution", identity)],
    )
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a1"],
    )

    projected, audit, reason = project_thinking_execution_history(
        [source],
        target,
    )

    assert reason == ""
    assert projected["thinking_execution_fallbacks"] == []
    assert projected["executed_thinking_assignment"]["proposers"][identity] == "high"
    assert audit["dropped_receipt_count"] == 1


def test_projection_keeps_full_ledger_across_temporary_roster_disappearance() -> None:
    identity = "openrouter:model-returning"
    first = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[_receipt("proposer_execution", identity)],
    )
    middle = _plan(
        "decision-b",
        proposers=["openrouter:model-other"],
        aggregators=["openrouter:model-a"],
    )
    target = _plan(
        "decision-c",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )

    projected, audit, reason = project_thinking_execution_history(
        [first, middle],
        target,
    )

    assert reason == ""
    assert projected["executed_thinking_assignment"]["proposers"][identity] == "medium"
    assert len(projected["thinking_execution_fallbacks"]) == 1
    assert audit["source_decision_ids"] == ["decision-a", "decision-b"]


def test_projection_rejects_same_roster_receipt_reset_to_empty() -> None:
    identity = "openrouter:model-p"
    first = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[_receipt("proposer_execution", identity)],
    )
    reset = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    target = _plan(
        "decision-c",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )

    _, _, reason = project_thinking_execution_history(
        [first, reset],
        target,
    )

    assert reason == "thinking_execution_identity_receipt_prefix_drift"


def test_projection_rejects_schedule_or_policy_drift_for_overlapping_key() -> None:
    identity = "openrouter:model-p"
    source = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[_receipt("proposer_execution", identity)],
    )
    schedule_drift = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        initial_by_identity={identity: "medium"},
    )
    policy_drift = _plan(
        "decision-c",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        policy="thinking-policy-v2",
    )

    assert project_thinking_execution_history(
        [source],
        schedule_drift,
    )[2] == "thinking_execution_target_schedule_signature_drift"
    assert project_thinking_execution_history(
        [source],
        policy_drift,
    )[2] == "thinking_execution_target_schedule_signature_drift"


def test_projection_rejects_reused_decision_id_with_immutable_plan_drift() -> None:
    first = _plan(
        "decision-a",
        proposers=["openrouter:model-p"],
        aggregators=["openrouter:model-a"],
    )
    drifted = deepcopy(first)
    drifted["unrelated_immutable_field"] = "tampered"
    target = _plan(
        "decision-b",
        proposers=["openrouter:model-p"],
        aggregators=["openrouter:model-a"],
    )

    assert project_thinking_execution_history(
        [first, drifted],
        target,
    )[2] == "thinking_execution_decision_immutable_plan_drift"


def test_projection_requires_nonempty_target_decision_id() -> None:
    target = _plan(
        "decision-b",
        proposers=["openrouter:model-p"],
        aggregators=["openrouter:model-a"],
    )
    target["decision_id"] = ""

    assert project_thinking_execution_history([], target)[2] == (
        "missing_thinking_execution_target_decision_id"
    )


def test_projection_rejects_target_reusing_decision_id_with_different_roster() -> None:
    history = _plan(
        "decision-a",
        proposers=["openrouter:model-old"],
        aggregators=["openrouter:aggregator-old"],
    )
    target = _plan(
        "decision-a",
        proposers=["openrouter:model-new"],
        aggregators=["openrouter:aggregator-new"],
    )

    assert project_thinking_execution_history([history], target)[2] == (
        "thinking_execution_target_decision_immutable_plan_drift"
    )


def test_projection_accepts_multiple_strict_extensions_for_one_decision() -> None:
    identity = "openrouter:model-p"
    first_receipt = _receipt("proposer_execution", identity)
    second_receipt = _receipt(
        "proposer_execution",
        identity,
        rejected="medium",
        effective="low",
    )
    first = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[first_receipt],
    )
    extended = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[first_receipt, second_receipt],
    )
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )

    projected, audit, reason = project_thinking_execution_history(
        [first, extended],
        target,
    )

    assert reason == ""
    assert projected["executed_thinking_assignment"]["proposers"][identity] == "low"
    assert projected["thinking_execution_fallbacks"] == [
        first_receipt,
        second_receipt,
    ]
    assert audit["ledger_receipt_count"] == 2


class _RestorableProvider:
    def __init__(self, plan: dict[str, object]) -> None:
        self._plan = deepcopy(plan)
        self.proposers = [
            SimpleNamespace(
                provider_config=SimpleNamespace(
                    provider=identity.partition(":")[0],
                    model=identity.partition(":")[2],
                ),
                effective_thinking_level=detail["effective_level"],
                thinking=detail["provider_level"],
            )
            for identity, detail in (
                (
                    str(row["identity"]),
                    row,
                )
                for row in plan["thinking_assignment_details"]["proposers"]
            )
        ]
        aggregator_rows = plan["thinking_assignment_details"]["aggregator_candidates"]
        aggregator_members = [
            SimpleNamespace(
                provider_config=SimpleNamespace(
                    provider=str(row["identity"]).partition(":")[0],
                    model=str(row["identity"]).partition(":")[2],
                ),
                effective_thinking_level=row["effective_level"],
                thinking=row["provider_level"],
            )
            for row in aggregator_rows
        ]
        self.aggregator = aggregator_members[0]
        self.aggregator_fallbacks = aggregator_members[1:]

    def selection_plan_execution_snapshot(self) -> dict[str, object]:
        return deepcopy(self._plan)

    def _record_thinking_fallback(self, **kwargs: object) -> dict[str, object]:
        member = kwargs["member"]
        member.effective_thinking_level = kwargs["effective_unified_level"]
        member.thinking = kwargs["effective_provider_level"]
        row = {
            "trigger_stage": (
                "proposer_execution"
                if kwargs["role"] == "proposer"
                else "aggregator_execution"
            ),
            "fallback_type": "thinking_level_neighbor",
            "reason": kwargs["reason"],
            "identity": (
                f"{member.provider_config.provider}:{member.provider_config.model}"
            ),
            "requested_thinking_level": "high",
            "rejected_unified_level": kwargs["rejected_unified_level"],
            "rejected_provider_level": kwargs["rejected_provider_level"],
            "effective_thinking_level": kwargs["effective_unified_level"],
            "effective_provider_level": kwargs["effective_provider_level"],
            "thinking_policy_version": "thinking-policy-v1",
            "fallback_result": kwargs["fallback_result"],
        }
        self._plan["thinking_execution_fallbacks"].append(deepcopy(row))
        if kwargs["role"] == "proposer":
            self._plan["executed_thinking_assignment"]["proposers"][
                row["identity"]
            ] = kwargs["effective_unified_level"]
        elif row["identity"] == self._plan["selected_A"]:
            self._plan["executed_thinking_assignment"][
                "aggregator"
            ] = kwargs["effective_unified_level"]
        return row


def test_restore_projected_prefix_succeeds_exactly_and_rejects_incompatible_base() -> None:
    identity = "openrouter:model-p"
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    history = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[_receipt("proposer_execution", identity)],
    )
    projected, _, reason = project_thinking_execution_history([history], target)
    assert reason == ""
    provider = _RestorableProvider(target)

    restore_projected_thinking_execution(
        provider,
        target_plan=target,
        projected_plan=projected,
    )

    assert provider.selection_plan_execution_snapshot()[
        "thinking_execution_fallbacks"
    ] == projected["thinking_execution_fallbacks"]
    restore_projected_thinking_execution(
        provider,
        target_plan=target,
        projected_plan=projected,
    )
    assert provider.selection_plan_execution_snapshot() == projected
    incompatible = _RestorableProvider(target)
    incompatible._plan["selected_P"] = ["openrouter:other"]
    with pytest.raises(
        ValueError,
        match="projected thinking execution prefix is incompatible",
    ):
        restore_projected_thinking_execution(
            incompatible,
            target_plan=target,
            projected_plan=projected,
        )


def test_restore_projected_prefix_resumes_from_an_exact_partial_prefix() -> None:
    identity = "openrouter:model-p"
    first_receipt = _receipt("proposer_execution", identity)
    second_receipt = _receipt(
        "proposer_execution",
        identity,
        rejected="medium",
        effective="low",
    )
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    partial = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[first_receipt],
    )
    projected = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[first_receipt, second_receipt],
    )
    provider = _RestorableProvider(target)

    restore_projected_thinking_execution(
        provider,
        target_plan=target,
        projected_plan=partial,
    )
    restore_projected_thinking_execution(
        provider,
        target_plan=target,
        projected_plan=projected,
    )

    assert provider.selection_plan_execution_snapshot() == projected


def test_restore_re_resolves_members_replaced_by_each_recorded_transition() -> None:
    identity = "openrouter:model-p"
    first_receipt = _receipt("proposer_execution", identity)
    second_receipt = _receipt(
        "proposer_execution",
        identity,
        rejected="medium",
        effective="low",
    )
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    projected = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[first_receipt, second_receipt],
    )

    class ReplacingProvider(_RestorableProvider):
        def _record_thinking_fallback(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            member = kwargs["member"]
            row = super()._record_thinking_fallback(**kwargs)
            index = self.proposers.index(member)
            self.proposers[index] = SimpleNamespace(
                provider_config=member.provider_config,
                effective_thinking_level=kwargs["effective_unified_level"],
                thinking=kwargs["effective_provider_level"],
            )
            return row

    provider = ReplacingProvider(target)
    restore_projected_thinking_execution(
        provider,
        target_plan=target,
        projected_plan=projected,
    )

    assert provider.selection_plan_execution_snapshot() == projected
    assert provider.proposers[0].effective_thinking_level == "low"


def test_restore_preflights_full_suffix_before_mutating_provider() -> None:
    first_identity = "openrouter:model-p1"
    second_identity = "openrouter:model-p2"
    target = _plan(
        "decision-b",
        proposers=[first_identity, second_identity],
        aggregators=["openrouter:model-a"],
    )
    projected = _plan(
        "decision-b",
        proposers=[first_identity, second_identity],
        aggregators=["openrouter:model-a"],
        receipts=[
            _receipt("proposer_execution", first_identity),
            _receipt("proposer_execution", second_identity),
        ],
    )
    provider = _RestorableProvider(target)
    provider.proposers[1].effective_thinking_level = "low"
    provider.proposers[1].thinking = "low"
    before = provider.selection_plan_execution_snapshot()

    with pytest.raises(
        ValueError,
        match="projected thinking receipt resets member state",
    ):
        restore_projected_thinking_execution(
            provider,
            target_plan=target,
            projected_plan=projected,
        )

    assert provider.selection_plan_execution_snapshot() == before
    assert provider.proposers[0].effective_thinking_level == "high"


def test_restore_rolls_back_when_a_later_recorder_callback_raises() -> None:
    identity = "openrouter:model-p"
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    projected = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[
            _receipt("proposer_execution", identity),
            _receipt(
                "proposer_execution",
                identity,
                rejected="medium",
                effective="low",
            ),
        ],
    )

    class RaisingProvider(_RestorableProvider):
        callback_count = 0

        def _record_thinking_fallback(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            self.callback_count += 1
            row = super()._record_thinking_fallback(**kwargs)
            if self.callback_count == 2:
                raise RuntimeError("injected recorder failure")
            return row

    provider = RaisingProvider(target)
    before = provider.selection_plan_execution_snapshot()

    with pytest.raises(RuntimeError, match="injected recorder failure"):
        restore_projected_thinking_execution(
            provider,
            target_plan=target,
            projected_plan=projected,
        )

    assert provider.selection_plan_execution_snapshot() == before
    assert provider.proposers[0].effective_thinking_level == "high"


def test_history_closure_validates_existing_prefix_without_shortening_it() -> None:
    identity = "openrouter:model-p"
    target = _plan(
        "decision-b",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
    )
    history = _plan(
        "decision-a",
        proposers=[identity],
        aggregators=["openrouter:model-a"],
        receipts=[_receipt("proposer_execution", identity)],
    )
    projected, _, reason = project_thinking_execution_history([history], target)
    assert reason == ""

    closed, _, closure_reason = validate_thinking_execution_history_closure(
        [history],
        projected,
    )

    assert closure_reason == ""
    assert closed == projected
    assert project_thinking_execution_history([history], projected)[2] == (
        "thinking_history_projection_target_not_pristine"
    )
