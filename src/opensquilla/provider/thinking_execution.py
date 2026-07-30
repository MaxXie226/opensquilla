"""Replay validation for routed thinking-level execution receipts.

The router decision is immutable.  A provider may consume the frozen
provider-rejection chain, or choose a strictly lower remaining level after a
reasoning-only length completion.  This module validates those runtime
receipts without treating a secondary aggregator as the selected primary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

EXECUTION_MUTABLE_SELECTION_PLAN_FIELDS = frozenset(
    {
        "executed_thinking_assignment",
        "thinking_execution_fallbacks",
    }
)
THINKING_FALLBACK_FIELDS = frozenset(
    {
        "trigger_stage",
        "fallback_type",
        "reason",
        "identity",
        "requested_thinking_level",
        "rejected_unified_level",
        "rejected_provider_level",
        "effective_thinking_level",
        "effective_provider_level",
        "thinking_policy_version",
        "fallback_result",
    }
)
TERMINAL_THINKING_FALLBACK_RESULTS = frozenset({"succeeded", "failed"})
THINKING_FALLBACK_REASONS = frozenset({"provider_rejected_thinking_level", "reasoning_only_length"})
THINKING_LEVEL_ORDER = ("low", "medium", "high", "highest")
THINKING_HISTORY_PROJECTION_SCHEMA = (
    "opensquilla.router-dynamic-thinking-history-projection/v1"
)
THINKING_PHYSICAL_EVIDENCE_SCHEMA = (
    "opensquilla.router-dynamic-thinking-physical-evidence/v1"
)
PHYSICAL_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PHYSICAL_ATTEMPT_OUTCOMES = frozenset(
    {
        "succeeded",
        "failed",
        "provider_rejected_thinking_level",
        "reasoning_only_length",
        "interrupted",
    }
)


def immutable_selection_plan_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Project a plan without provider-execution state and receipts."""

    return {
        str(key): deepcopy(value)
        for key, value in plan.items()
        if str(key) not in EXECUTION_MUTABLE_SELECTION_PLAN_FIELDS
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _schedule_signature(
    schedule: Mapping[str, Any],
    *,
    policy_version: str,
) -> dict[str, Any]:
    return {
        "requested_thinking_level": str(schedule.get("requested") or ""),
        "initial_unified_level": str((schedule.get("current") or ("", ""))[0]),
        "initial_provider_level": str((schedule.get("current") or ("", ""))[1]),
        "fallback_chain": [
            {
                "unified_level": str(unified),
                "provider_level": str(provider),
            }
            for unified, provider in schedule.get("remaining") or []
        ],
        "thinking_policy_version": policy_version,
    }


def _receipt_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("trigger_stage") or ""),
        str(row.get("identity") or ""),
    )


def _key_label(key: tuple[str, str]) -> str:
    return f"{key[0]}:{key[1]}"


def _detail_schedule(
    detail: Mapping[str, Any],
    *,
    expected_role: str,
) -> tuple[dict[str, Any], str]:
    identity = str(detail.get("identity") or "")
    role = str(detail.get("role") or "")
    requested = str(detail.get("requested_level") or "")
    unified = str(detail.get("effective_level") or "")
    provider = str(detail.get("provider_level") or "")
    if not identity or role != expected_role or not requested or not unified or not provider:
        return {}, "invalid_frozen_thinking_assignment_detail"
    raw_fallbacks = detail.get("provider_rejection_fallbacks")
    if not isinstance(raw_fallbacks, list):
        return {}, "invalid_frozen_thinking_fallback_chain"
    fallbacks: list[tuple[str, str]] = []
    for fallback in raw_fallbacks:
        if (
            not isinstance(fallback, Mapping)
            or set(fallback) != {"unified_level", "provider_level", "reason"}
            or fallback.get("reason") != "provider_rejection_fallback"
        ):
            return {}, "invalid_frozen_thinking_fallback_chain"
        fallback_unified = str(fallback.get("unified_level") or "")
        fallback_provider = str(fallback.get("provider_level") or "")
        if not fallback_unified or not fallback_provider:
            return {}, "invalid_frozen_thinking_fallback_chain"
        fallbacks.append((fallback_unified, fallback_provider))
    return {
        "identity": identity,
        "requested": requested,
        "current": (unified, provider),
        "remaining": fallbacks,
    }, ""


def _frozen_schedules(
    plan: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    if plan.get("ranking_thinking_assignment_enabled") is not True:
        return {}, "thinking_execution_receipts_without_enabled_policy"
    details = plan.get("thinking_assignment_details")
    selected_p = plan.get("selected_P")
    selected_a = str(plan.get("selected_A") or "")
    aggregator_candidates = plan.get("aggregator_candidates")
    if (
        not isinstance(details, Mapping)
        or not isinstance(selected_p, list)
        or any(not isinstance(identity, str) or not identity for identity in selected_p)
        or not selected_a
        or not isinstance(aggregator_candidates, list)
        or not aggregator_candidates
        or aggregator_candidates[0] != selected_a
    ):
        return {}, "invalid_frozen_thinking_assignment_details"

    schedules: dict[tuple[str, str], dict[str, Any]] = {}
    raw_proposers = details.get("proposers")
    if not isinstance(raw_proposers, list):
        return {}, "invalid_frozen_proposer_thinking_details"
    for raw_detail in raw_proposers:
        if not isinstance(raw_detail, Mapping):
            return {}, "invalid_frozen_proposer_thinking_details"
        schedule, reason = _detail_schedule(raw_detail, expected_role="proposer")
        if reason:
            return {}, reason
        key = ("proposer_execution", str(schedule["identity"]))
        if key in schedules:
            return {}, "duplicate_frozen_thinking_assignment_identity"
        schedules[key] = schedule
    if {identity for stage, identity in schedules if stage == "proposer_execution"} != set(
        selected_p
    ):
        return {}, "frozen_proposer_thinking_details_mismatch"

    raw_aggregators = details.get("aggregator_candidates")
    if raw_aggregators is None:
        # Historical routed plans froze only the primary detail. They remain
        # readable when no secondary execution receipt claims a transition.
        raw_primary = details.get("aggregator")
        raw_aggregators = [raw_primary] if isinstance(raw_primary, Mapping) else []
    if not isinstance(raw_aggregators, list) or not raw_aggregators:
        return {}, "invalid_frozen_aggregator_thinking_details"
    aggregator_detail_ids: list[str] = []
    for index, raw_detail in enumerate(raw_aggregators):
        if not isinstance(raw_detail, Mapping):
            return {}, "invalid_frozen_aggregator_thinking_details"
        expected_role = "aggregator" if index == 0 else "aggregator_fallback"
        schedule, reason = _detail_schedule(
            raw_detail,
            expected_role=expected_role,
        )
        if reason:
            return {}, reason
        identity = str(schedule["identity"])
        key = ("aggregator_execution", identity)
        if key in schedules:
            return {}, "duplicate_frozen_thinking_assignment_identity"
        schedules[key] = schedule
        aggregator_detail_ids.append(identity)
    if aggregator_detail_ids[0] != selected_a:
        return {}, "frozen_primary_aggregator_thinking_detail_mismatch"
    if "aggregator_candidates" in details and aggregator_detail_ids != aggregator_candidates:
        return {}, "frozen_aggregator_thinking_details_mismatch"
    return schedules, ""


def replay_thinking_execution_plan(
    plan: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """Replay all receipts and return each member's current/remaining state."""

    schedules, reason = _frozen_schedules(plan)
    if reason:
        return {}, reason
    assignment = plan.get("executed_thinking_assignment")
    raw_receipts = plan.get("thinking_execution_fallbacks", [])
    if (
        not isinstance(assignment, Mapping)
        or set(assignment) != {"proposers", "aggregator", "thinking_policy_version"}
        or not isinstance(raw_receipts, list)
        or any(not isinstance(row, Mapping) for row in raw_receipts)
    ):
        return {}, "invalid_thinking_execution_provenance"
    policy_version = assignment.get("thinking_policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        return {}, "invalid_thinking_execution_policy_version"

    for row in raw_receipts:
        if (
            set(row) != THINKING_FALLBACK_FIELDS
            or row.get("trigger_stage") not in {"proposer_execution", "aggregator_execution"}
            or row.get("fallback_type") != "thinking_level_neighbor"
            or row.get("reason") not in THINKING_FALLBACK_REASONS
            or row.get("fallback_result") not in TERMINAL_THINKING_FALLBACK_RESULTS
            or row.get("thinking_policy_version") != policy_version
        ):
            return {}, "invalid_thinking_execution_receipt"
        key = (str(row["trigger_stage"]), str(row.get("identity") or ""))
        schedule = schedules.get(key)
        if schedule is None:
            return {}, "unknown_thinking_execution_receipt_identity"
        current_unified, current_provider = schedule["current"]
        if (
            row.get("requested_thinking_level") != schedule["requested"]
            or row.get("rejected_unified_level") != current_unified
            or row.get("rejected_provider_level") != current_provider
        ):
            return {}, "thinking_execution_receipt_rejected_state_mismatch"
        remaining = list(schedule["remaining"])
        if row.get("reason") == "provider_rejected_thinking_level":
            if not remaining:
                return {}, "thinking_execution_provider_fallback_chain_exhausted"
            target = remaining[0]
            next_remaining = remaining[1:]
        else:
            if current_unified not in THINKING_LEVEL_ORDER:
                return {}, "invalid_reasoning_only_thinking_level"
            current_index = THINKING_LEVEL_ORDER.index(current_unified)
            lower = [
                fallback
                for fallback in remaining
                if fallback[0] in THINKING_LEVEL_ORDER
                and THINKING_LEVEL_ORDER.index(fallback[0]) < current_index
            ]
            if not lower:
                return {}, "reasoning_only_thinking_fallback_unavailable"
            target = max(
                lower,
                key=lambda fallback: THINKING_LEVEL_ORDER.index(fallback[0]),
            )
            target_index = THINKING_LEVEL_ORDER.index(target[0])
            next_remaining = sorted(
                (
                    fallback
                    for fallback in lower
                    if THINKING_LEVEL_ORDER.index(fallback[0]) < target_index
                ),
                key=lambda fallback: THINKING_LEVEL_ORDER.index(fallback[0]),
                reverse=True,
            )
        if (
            row.get("effective_thinking_level"),
            row.get("effective_provider_level"),
        ) != target:
            return {}, "thinking_execution_receipt_effective_state_mismatch"
        schedule["current"] = target
        schedule["remaining"] = next_remaining

    proposer_assignment = assignment.get("proposers")
    if not isinstance(proposer_assignment, Mapping):
        return {}, "invalid_executed_proposer_thinking_assignment"
    expected_proposers = {
        identity: state["current"][0]
        for (stage, identity), state in schedules.items()
        if stage == "proposer_execution"
    }
    if dict(proposer_assignment) != expected_proposers:
        return {}, "executed_proposer_thinking_assignment_mismatch"
    primary_key = ("aggregator_execution", str(plan.get("selected_A") or ""))
    primary = schedules.get(primary_key)
    if primary is None or assignment.get("aggregator") != primary["current"][0]:
        return {}, "executed_primary_aggregator_thinking_assignment_mismatch"
    return schedules, ""


def pristine_thinking_execution_plan(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Return the frozen decision at its zero-receipt execution state."""

    schedules, reason = _frozen_schedules(plan)
    if reason:
        return {}, reason
    assignment = plan.get("executed_thinking_assignment")
    policy_version = (
        str(assignment.get("thinking_policy_version") or "")
        if isinstance(assignment, Mapping)
        else ""
    )
    if not policy_version:
        return {}, "invalid_thinking_execution_policy_version"
    pristine = deepcopy(dict(plan))
    pristine["thinking_execution_fallbacks"] = []
    pristine["executed_thinking_assignment"] = {
        "proposers": {
            identity: schedule["current"][0]
            for (stage, identity), schedule in schedules.items()
            if stage == "proposer_execution"
        },
        "aggregator": schedules[
            ("aggregator_execution", str(plan.get("selected_A") or ""))
        ]["current"][0],
        "thinking_policy_version": policy_version,
    }
    _, replay_reason = replay_thinking_execution_plan(pristine)
    return (pristine, replay_reason)


def project_thinking_execution_history(
    history_plans: Sequence[Mapping[str, Any]],
    target_plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Project the physical receipt ledger onto one newly ranked roster.

    Receipts belong to ``(trigger_stage, identity)`` rather than to a ranker
    decision.  This lets an aggregator move between primary and secondary
    positions while preventing a proposer receipt from crossing into an
    aggregator role.  Every overlapping identity must retain the exact
    requested/native schedule and policy version, and every per-decision and
    per-identity receipt stream must be monotonic.
    """

    if target_plan.get("ranking_thinking_assignment_enabled") is not True:
        return {}, {}, "thinking_history_projection_without_enabled_policy"
    pristine_target, pristine_reason = pristine_thinking_execution_plan(
        target_plan
    )
    if pristine_reason:
        return {}, {}, pristine_reason
    if (
        target_plan.get("executed_thinking_assignment")
        != pristine_target.get("executed_thinking_assignment")
        or target_plan.get("thinking_execution_fallbacks", [])
        != []
    ):
        return {}, {}, "thinking_history_projection_target_not_pristine"
    target_decision_id = str(target_plan.get("decision_id") or "")
    if not target_decision_id:
        return {}, {}, "missing_thinking_execution_target_decision_id"
    target_immutable_hash = _canonical_sha256(
        immutable_selection_plan_payload(target_plan)
    )
    target_schedules, target_reason = _frozen_schedules(target_plan)
    if target_reason:
        return {}, {}, target_reason
    target_assignment = target_plan.get("executed_thinking_assignment")
    target_policy_version = (
        str(target_assignment.get("thinking_policy_version") or "")
        if isinstance(target_assignment, Mapping)
        else ""
    )
    if not target_policy_version:
        return {}, {}, "invalid_thinking_execution_policy_version"

    signatures: dict[tuple[str, str], dict[str, Any]] = {}
    receipts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    latest_states: dict[tuple[str, str], tuple[str, str]] = {}
    decision_prefixes: dict[str, list[dict[str, Any]]] = {}
    decision_immutable_hashes: dict[str, str] = {}
    ledger: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for history_index, raw_plan in enumerate(history_plans):
        if not isinstance(raw_plan, Mapping):
            return {}, {}, "invalid_thinking_execution_history_plan"
        decision_id = str(raw_plan.get("decision_id") or "")
        if not decision_id:
            return {}, {}, "missing_thinking_execution_history_decision_id"
        immutable_hash = _canonical_sha256(
            immutable_selection_plan_payload(raw_plan)
        )
        prior_immutable_hash = decision_immutable_hashes.get(decision_id)
        if (
            prior_immutable_hash is not None
            and prior_immutable_hash != immutable_hash
        ):
            return {}, {}, "thinking_execution_decision_immutable_plan_drift"
        decision_immutable_hashes[decision_id] = immutable_hash
        schedules, schedule_reason = _frozen_schedules(raw_plan)
        if schedule_reason:
            return {}, {}, schedule_reason
        assignment = raw_plan.get("executed_thinking_assignment")
        policy_version = (
            str(assignment.get("thinking_policy_version") or "")
            if isinstance(assignment, Mapping)
            else ""
        )
        states, replay_reason = replay_thinking_execution_plan(raw_plan)
        if replay_reason:
            return {}, {}, replay_reason
        raw_receipts = raw_plan.get("thinking_execution_fallbacks", [])
        if not isinstance(raw_receipts, list) or any(
            not isinstance(row, Mapping) for row in raw_receipts
        ):
            return {}, {}, "invalid_thinking_execution_history_receipts"
        receipts = [deepcopy(dict(row)) for row in raw_receipts]
        prior_decision_prefix = decision_prefixes.get(decision_id)
        if prior_decision_prefix is not None and (
            len(receipts) < len(prior_decision_prefix)
            or receipts[: len(prior_decision_prefix)] != prior_decision_prefix
        ):
            return {}, {}, "thinking_execution_decision_receipt_prefix_drift"
        decision_prefixes[decision_id] = receipts

        for key, schedule in schedules.items():
            signature = _schedule_signature(
                schedule,
                policy_version=policy_version,
            )
            prior_signature = signatures.get(key)
            if prior_signature is not None and prior_signature != signature:
                return {}, {}, "thinking_execution_schedule_signature_drift"
            signatures[key] = signature

        local_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in receipts:
            local_by_key.setdefault(_receipt_key(row), []).append(row)
        # A member that remains in the roster must not erase its prior
        # physical receipts by merely omitting the key from this plan's list.
        # A temporarily absent member is allowed; its ledger is retained for
        # a later same-role reappearance.
        for key in schedules:
            local_by_key.setdefault(key, [])
        prior_lengths: dict[tuple[str, str], int] = {}
        for key, local_receipts in local_by_key.items():
            prior_receipts = receipts_by_key.get(key, [])
            if (
                len(local_receipts) < len(prior_receipts)
                or local_receipts[: len(prior_receipts)] != prior_receipts
            ):
                return {}, {}, "thinking_execution_identity_receipt_prefix_drift"
            prior_lengths[key] = len(prior_receipts)
        seen_ordinals: dict[tuple[str, str], int] = {}
        for row in receipts:
            key = _receipt_key(row)
            ordinal = seen_ordinals.get(key, 0)
            if ordinal >= prior_lengths.get(key, 0):
                ledger.append(deepcopy(row))
            seen_ordinals[key] = ordinal + 1
        for key, local_receipts in local_by_key.items():
            if len(local_receipts) > prior_lengths[key]:
                receipts_by_key[key] = [deepcopy(row) for row in local_receipts]
                latest_states[key] = tuple(states[key]["current"])
            elif key not in receipts_by_key:
                receipts_by_key[key] = []
                latest_states[key] = tuple(states[key]["current"])
        decisions.append(
            {
                "history_index": history_index,
                "decision_id": decision_id,
                "immutable_plan_sha256": immutable_hash,
                "receipt_count": len(receipts),
            }
        )

    prior_target_hash = decision_immutable_hashes.get(target_decision_id)
    if (
        prior_target_hash is not None
        and prior_target_hash != target_immutable_hash
    ):
        return {}, {}, "thinking_execution_target_decision_immutable_plan_drift"
    target_keys = set(target_schedules)
    for key, schedule in target_schedules.items():
        target_signature = _schedule_signature(
            schedule,
            policy_version=target_policy_version,
        )
        prior_signature = signatures.get(key)
        if prior_signature is not None and prior_signature != target_signature:
            return {}, {}, "thinking_execution_target_schedule_signature_drift"

    projected_receipts = [
        deepcopy(row) for row in ledger if _receipt_key(row) in target_keys
    ]
    projected = deepcopy(dict(target_plan))
    projected["thinking_execution_fallbacks"] = projected_receipts
    proposer_levels: dict[str, str] = {}
    for key, schedule in target_schedules.items():
        current = latest_states.get(key, tuple(schedule["current"]))
        if key[0] == "proposer_execution":
            proposer_levels[key[1]] = current[0]
    primary_key = (
        "aggregator_execution",
        str(target_plan.get("selected_A") or ""),
    )
    primary_schedule = target_schedules.get(primary_key)
    if primary_schedule is None:
        return {}, {}, "missing_projected_primary_aggregator_schedule"
    primary_current = latest_states.get(
        primary_key,
        tuple(primary_schedule["current"]),
    )
    projected["executed_thinking_assignment"] = {
        "proposers": proposer_levels,
        "aggregator": primary_current[0],
        "thinking_policy_version": target_policy_version,
    }
    _, projected_reason = replay_thinking_execution_plan(projected)
    if projected_reason:
        return {}, {}, projected_reason

    carried_keys = sorted(
        {_key_label(_receipt_key(row)) for row in projected_receipts}
    )
    dropped_rows = [row for row in ledger if _receipt_key(row) not in target_keys]
    dropped_keys = sorted({_key_label(_receipt_key(row)) for row in dropped_rows})
    audit = {
        "schema": THINKING_HISTORY_PROJECTION_SCHEMA,
        "policy": "same_stage_identity_exact_schedule",
        "thinking_policy_version": target_policy_version,
        "source_decision_ids": [row["decision_id"] for row in decisions],
        "target_decision_id": target_decision_id,
        "source_decisions": decisions,
        "target_immutable_plan_sha256": target_immutable_hash,
        "ledger_sha256": _canonical_sha256(ledger),
        "projected_plan_sha256": _canonical_sha256(projected),
        "ledger_receipt_count": len(ledger),
        "carried_receipt_count": len(projected_receipts),
        "dropped_receipt_count": len(dropped_rows),
        "carried_keys": carried_keys,
        "dropped_keys": dropped_keys,
    }
    audit["audit_sha256"] = _canonical_sha256(audit)
    return projected, audit, ""


def validate_thinking_execution_history_closure(
    history_plans: Sequence[Mapping[str, Any]],
    target_plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Recompute a target prefix without ever shortening its mutable state."""

    _, replay_reason = replay_thinking_execution_plan(target_plan)
    if replay_reason:
        return {}, {}, replay_reason
    pristine, pristine_reason = pristine_thinking_execution_plan(target_plan)
    if pristine_reason:
        return {}, {}, pristine_reason
    projected, audit, projection_reason = project_thinking_execution_history(
        history_plans,
        pristine,
    )
    if projection_reason:
        return {}, {}, projection_reason
    if (
        target_plan.get("executed_thinking_assignment")
        != projected.get("executed_thinking_assignment")
        or target_plan.get("thinking_execution_fallbacks", [])
        != projected.get("thinking_execution_fallbacks", [])
    ):
        return {}, {}, "thinking_execution_target_prefix_not_closed"
    return projected, audit, ""


def restore_projected_thinking_execution(
    provider: Any,
    *,
    target_plan: Mapping[str, Any],
    projected_plan: Mapping[str, Any],
) -> None:
    """Restore a validated projected receipt prefix into a fresh provider."""

    snapshot = getattr(provider, "selection_plan_execution_snapshot", None)
    record_fallback = getattr(provider, "_record_thinking_fallback", None)
    if not callable(snapshot) or not callable(record_fallback):
        raise ValueError("thinking execution provider cannot restore projected state")
    base_plan = snapshot()
    mutation_reason = (
        validate_thinking_execution_plan_mutation(base_plan, projected_plan)
        if isinstance(base_plan, Mapping)
        else "invalid_base_plan"
    )
    if (
        not isinstance(base_plan, Mapping)
        or immutable_selection_plan_payload(base_plan)
        != immutable_selection_plan_payload(target_plan)
        or immutable_selection_plan_payload(target_plan)
        != immutable_selection_plan_payload(projected_plan)
        or mutation_reason
    ):
        raise ValueError("projected thinking execution prefix is incompatible")
    if (
        base_plan.get("executed_thinking_assignment")
        == projected_plan.get("executed_thinking_assignment")
        and base_plan.get("thinking_execution_fallbacks", [])
        == projected_plan.get("thinking_execution_fallbacks", [])
    ):
        return
    receipts = projected_plan.get("thinking_execution_fallbacks", [])
    base_receipts = base_plan.get("thinking_execution_fallbacks", [])
    if not isinstance(receipts, list) or any(
        not isinstance(row, Mapping) for row in receipts
    ) or not isinstance(base_receipts, list):
        raise ValueError("projected thinking execution receipts are malformed")
    if receipts[: len(base_receipts)] != base_receipts:
        raise ValueError("projected thinking execution prefix is incompatible")

    base_states, base_replay_reason = replay_thinking_execution_plan(base_plan)
    if base_replay_reason:
        raise ValueError("projected thinking execution prefix is incompatible")
    def current_members_by_key() -> dict[tuple[str, str], Any]:
        resolved: dict[tuple[str, str], Any] = {}
        for stage, members in (
            ("proposer_execution", list(getattr(provider, "proposers", []) or [])),
            (
                "aggregator_execution",
                [
                    getattr(provider, "aggregator", None),
                    *(list(getattr(provider, "aggregator_fallbacks", []) or [])),
                ],
            ),
        ):
            for member in members:
                if member is None:
                    continue
                identity = (
                    f"{getattr(getattr(member, 'provider_config', None), 'provider', '')}:"
                    f"{getattr(getattr(member, 'provider_config', None), 'model', '')}"
                ).strip().lower()
                key = (stage, identity)
                if not identity.partition(":")[0] or not identity.partition(":")[2]:
                    raise ValueError(
                        "projected thinking receipt references an unknown member"
                    )
                if key in resolved:
                    raise ValueError(
                        "projected thinking receipt references an ambiguous member"
                    )
                resolved[key] = member
        return resolved

    members_by_key = current_members_by_key()

    # Validate the entire suffix against a virtual copy of provider state
    # before invoking the mutating receipt recorder.  A malformed later row
    # therefore cannot leave a partially restored provider behind.
    simulated_states = {
        key: tuple(state["current"])
        for key, state in base_states.items()
    }
    suffix: list[tuple[tuple[str, str], str, dict[str, Any]]] = []
    checked_members: set[tuple[str, str]] = set()
    for raw_row in receipts[len(base_receipts) :]:
        row = dict(raw_row)
        identity = str(row.get("identity") or "").strip().lower()
        stage = str(row.get("trigger_stage") or "")
        key = (stage, identity)
        member = members_by_key.get(key)
        if member is None:
            raise ValueError("projected thinking receipt references an unknown member")
        simulated = simulated_states.get(key)
        if simulated is None:
            raise ValueError("projected thinking receipt references an unknown member")
        if key not in checked_members:
            if (
                str(getattr(member, "effective_thinking_level", "") or "")
                != str(simulated[0])
                or str(getattr(member, "thinking", "") or "")
                != str(simulated[1])
            ):
                raise ValueError("projected thinking receipt resets member state")
            checked_members.add(key)
        if (
            str(row.get("rejected_unified_level") or "")
            != str(simulated[0])
            or str(row.get("rejected_provider_level") or "")
            != str(simulated[1])
        ):
            raise ValueError("projected thinking receipt resets member state")
        simulated_states[key] = (
            str(row.get("effective_thinking_level") or ""),
            str(row.get("effective_provider_level") or ""),
        )
        suffix.append(
            (
                key,
                "proposer" if stage == "proposer_execution" else "aggregator",
                row,
            )
        )

    rollback_attributes = {
        attribute: deepcopy(getattr(provider, attribute))
        for attribute in (
            "proposers",
            "aggregator",
            "aggregator_fallbacks",
            "_thinking_execution_fallbacks",
            "selection_plan",
            "_plan",
        )
        if hasattr(provider, attribute)
    }
    try:
        for key, role, row in suffix:
            member = current_members_by_key().get(key)
            if member is None:
                raise ValueError(
                    "projected thinking receipt references an unknown member"
                )
            if (
                str(getattr(member, "effective_thinking_level", "") or "")
                != str(row.get("rejected_unified_level") or "")
                or str(getattr(member, "thinking", "") or "")
                != str(row.get("rejected_provider_level") or "")
            ):
                raise ValueError("projected thinking receipt resets member state")
            restored = record_fallback(
                member=member,
                role=role,
                rejected_unified_level=row.get("rejected_unified_level"),
                rejected_provider_level=row.get("rejected_provider_level"),
                effective_unified_level=str(
                    row.get("effective_thinking_level") or ""
                ),
                effective_provider_level=str(
                    row.get("effective_provider_level") or ""
                ),
                fallback_result=str(row.get("fallback_result") or ""),
                reason=str(row.get("reason") or ""),
            )
            if restored != dict(row):
                raise ValueError(
                    "projected thinking receipt changed during restore"
                )
        restored_plan = snapshot()
        if (
            not isinstance(restored_plan, Mapping)
            or restored_plan.get("executed_thinking_assignment")
            != projected_plan.get("executed_thinking_assignment")
            or restored_plan.get("thinking_execution_fallbacks", [])
            != projected_plan.get("thinking_execution_fallbacks", [])
        ):
            raise ValueError(
                "projected thinking execution state did not restore exactly"
            )
    except Exception:
        for attribute, value in rollback_attributes.items():
            setattr(provider, attribute, value)
        raise


def validate_thinking_execution_plan_mutation(
    expected_plan: Mapping[str, Any],
    observed_plan: Mapping[str, Any],
) -> str:
    """Validate that observed execution state extends one frozen receipt prefix."""

    if immutable_selection_plan_payload(expected_plan) != (
        immutable_selection_plan_payload(observed_plan)
    ):
        return "thinking_execution_immutable_plan_mismatch"
    has_execution_fields = any(
        field in plan
        for plan in (expected_plan, observed_plan)
        for field in EXECUTION_MUTABLE_SELECTION_PLAN_FIELDS
    )
    if not has_execution_fields:
        return ""
    expected_receipts = expected_plan.get("thinking_execution_fallbacks", [])
    observed_receipts = observed_plan.get("thinking_execution_fallbacks", [])
    if (
        not isinstance(expected_receipts, list)
        or not isinstance(observed_receipts, list)
        or observed_receipts[: len(expected_receipts)] != expected_receipts
    ):
        return "thinking_execution_receipt_prefix_mismatch"
    _, expected_reason = replay_thinking_execution_plan(expected_plan)
    if expected_reason:
        return expected_reason
    _, observed_reason = replay_thinking_execution_plan(observed_plan)
    return observed_reason


def final_request_thinking_execution_reason(
    plan: Mapping[str, Any],
    call: Mapping[str, Any],
) -> str:
    """Bind the physical final request identity/native level to replay state."""

    if plan.get("ranking_thinking_assignment_enabled") is not True:
        return ""
    final_request = call.get("final_request")
    if not isinstance(final_request, Mapping) or final_request.get("request_started") is not True:
        return ""
    if str(final_request.get("role") or "") != "aggregator":
        return ""
    execution = final_request.get("execution")
    if not isinstance(execution, Mapping):
        return "missing_final_request_thinking_execution"
    requested_provider = str(execution.get("requested_provider") or "")
    requested_model = str(execution.get("requested_model") or "")
    provider = str(execution.get("provider") or "")
    model = str(execution.get("model") or "")
    if (
        not requested_provider
        or not requested_model
        or provider != requested_provider
        or model != requested_model
    ):
        return "final_request_thinking_execution_identity_mismatch"
    identity = f"{requested_provider}:{requested_model}"
    schedules, reason = replay_thinking_execution_plan(plan)
    if reason:
        return reason
    state = schedules.get(("aggregator_execution", identity))
    if state is None:
        return "final_request_thinking_execution_identity_not_frozen"
    unified, native = state["current"]
    thinking_enabled = native.strip().lower() not in {"off", "none", "false"}
    if (
        execution.get("effective_thinking_level") != unified
        or execution.get("assigned_thinking_level") != unified
        or execution.get("provider_thinking_level") != native
        or execution.get("thinking_override") != native
        or execution.get("effective_thinking") is not thinking_enabled
        or execution.get("effective_provider_thinking_level") != native
    ):
        return "final_request_thinking_execution_level_mismatch"
    return ""


def _execution_identity_state(
    execution: Mapping[str, Any],
    *,
    expected_role: str,
) -> tuple[tuple[str, str, str], str]:
    if str(execution.get("role") or "") != expected_role:
        return ("", "", ""), "thinking_execution_role_mismatch"
    requested_provider = str(execution.get("requested_provider") or "")
    requested_model = str(execution.get("requested_model") or "")
    provider = str(execution.get("provider") or "")
    model = str(execution.get("model") or "")
    if (
        not requested_provider
        or not requested_model
        or provider != requested_provider
        or model != requested_model
    ):
        return ("", "", ""), "thinking_execution_identity_mismatch"
    unified = str(execution.get("effective_thinking_level") or "")
    assigned = str(execution.get("assigned_thinking_level") or "")
    native = str(execution.get("provider_thinking_level") or "")
    override = str(execution.get("thinking_override") or "")
    thinking_enabled = native.strip().lower() not in {"off", "none", "false"}
    if (
        not unified
        or assigned != unified
        or not native
        or override != native
        or execution.get("effective_thinking") is not thinking_enabled
        or execution.get("effective_provider_thinking_level") != native
        or execution.get("thinking_policy_managed") is not True
    ):
        return ("", "", ""), "thinking_execution_level_snapshot_invalid"
    return (f"{requested_provider}:{requested_model}", unified, native), ""


def _validate_thinking_execution_call_legacy(
    previous_plan: Mapping[str, Any],
    call: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Validate one physical call and extend the global receipt prefix."""

    observed_plan = call.get("selection_plan")
    if not isinstance(observed_plan, Mapping):
        return {}, "missing_physical_thinking_selection_plan"
    mutation_reason = validate_thinking_execution_plan_mutation(
        previous_plan,
        observed_plan,
    )
    if mutation_reason:
        return {}, mutation_reason
    if observed_plan.get("ranking_thinking_assignment_enabled") is not True:
        return dict(observed_plan), ""

    previous_receipts = previous_plan.get("thinking_execution_fallbacks", [])
    observed_receipts = observed_plan.get("thinking_execution_fallbacks", [])
    if not isinstance(previous_receipts, list) or not isinstance(
        observed_receipts,
        list,
    ):
        return {}, "invalid_physical_thinking_receipt_prefix"
    new_receipts = observed_receipts[len(previous_receipts) :]
    previous_schedules, reason = replay_thinking_execution_plan(previous_plan)
    if reason:
        return {}, reason
    schedules, reason = replay_thinking_execution_plan(observed_plan)
    if reason:
        return {}, reason
    proposer_receipt_evidence: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    candidates = call.get("candidates")
    if not isinstance(candidates, list):
        return {}, "missing_physical_proposer_candidates"
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("request_started") is not True:
            continue
        execution = candidate.get("execution")
        if not isinstance(execution, Mapping):
            return {}, "missing_physical_proposer_thinking_execution"
        (identity, unified, native), execution_reason = _execution_identity_state(
            execution,
            expected_role="proposer",
        )
        if execution_reason:
            return {}, execution_reason
        key = ("proposer_execution", identity)
        final_state = schedules.get(key)
        if (
            final_state is None
            or (unified, native) != tuple(final_state["current"])
            or candidate.get("effective_thinking_level") != unified
            or candidate.get("provider_thinking_level") != native
            or str(candidate.get("requested_provider") or "")
            + ":"
            + str(candidate.get("requested_model") or "")
            != identity
        ):
            return {}, "physical_proposer_thinking_execution_mismatch"
        raw_attempts = execution.get("thinking_fallback_attempts", [])
        if not isinstance(raw_attempts, list) or any(
            not isinstance(row, Mapping) for row in raw_attempts
        ):
            return {}, "invalid_physical_proposer_thinking_fallback_attempts"
        proposer_receipt_evidence.extend((fallback, candidate) for fallback in raw_attempts)

    for receipt in (
        row for row in new_receipts if row.get("trigger_stage") == "proposer_execution"
    ):
        matching_evidence = [
            candidate
            for evidence, candidate in proposer_receipt_evidence
            if dict(evidence) == dict(receipt)
        ]
        if len(matching_evidence) != 1:
            return {}, "unbound_physical_proposer_thinking_receipt"
        evidence_candidate = matching_evidence[0]
        if (
            receipt.get("fallback_result") == "succeeded"
            and evidence_candidate.get("ok") is not True
            or receipt.get("fallback_result") == "failed"
            and evidence_candidate.get("ok") is True
            and evidence_candidate.get("effective_thinking_level")
            == receipt.get("effective_thinking_level")
        ):
            return {}, "proposer_thinking_receipt_outcome_mismatch"
        identity = str(receipt.get("identity") or "")
        matching_candidates = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and candidate.get("request_started") is True
            and (
                f"{candidate.get('requested_provider') or ''}:"
                f"{candidate.get('requested_model') or ''}"
            )
            == identity
        ]
        receipt_count = sum(
            1
            for row in new_receipts
            if row.get("trigger_stage") == "proposer_execution" and row.get("identity") == identity
        )
        if (
            not matching_candidates
            or max(
                int(candidate.get("physical_request_count") or 0)
                for candidate in matching_candidates
            )
            < receipt_count + 1
        ):
            return {}, "proposer_thinking_receipt_request_count_mismatch"

    recovery = call.get("aggregator_recovery")
    recovery_attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    aggregator_receipts_by_identity: dict[str, list[Mapping[str, Any]]] = {}
    for receipt in new_receipts:
        if receipt.get("trigger_stage") == "aggregator_execution":
            aggregator_receipts_by_identity.setdefault(
                str(receipt.get("identity") or ""),
                [],
            ).append(receipt)
    aggregator_receipt_positions = dict.fromkeys(
        aggregator_receipts_by_identity,
        0,
    )
    aggregator_current = {
        identity: tuple(state["current"])
        for (stage, identity), state in previous_schedules.items()
        if stage == "aggregator_execution"
    }
    aggregator_current_seen: set[str] = set()
    if recovery_attempts is not None:
        if not isinstance(recovery_attempts, list):
            return {}, "invalid_physical_aggregator_recovery_attempts"
        for attempt in recovery_attempts:
            if not isinstance(attempt, Mapping) or attempt.get("request_started") is not True:
                continue
            execution = attempt.get("execution")
            if not isinstance(execution, Mapping):
                return {}, "missing_physical_aggregator_thinking_execution"
            state, execution_reason = _execution_identity_state(
                execution,
                expected_role="aggregator",
            )
            if execution_reason:
                return {}, execution_reason
            identity, unified, native = state
            current = aggregator_current.get(identity)
            if current is None:
                return {}, "physical_aggregator_thinking_execution_not_frozen"
            if (unified, native) == current:
                aggregator_current_seen.add(identity)
                continue
            position = aggregator_receipt_positions.get(identity, 0)
            receipts = aggregator_receipts_by_identity.get(identity, [])
            receipt = receipts[position] if position < len(receipts) else None
            if (
                receipt is None
                or identity not in aggregator_current_seen
                or (
                    receipt.get("rejected_unified_level"),
                    receipt.get("rejected_provider_level"),
                )
                != current
                or (
                    receipt.get("effective_thinking_level"),
                    receipt.get("effective_provider_level"),
                )
                != (unified, native)
            ):
                return {}, "unbound_physical_aggregator_thinking_receipt"
            outcome = str(attempt.get("outcome") or "")
            if (
                receipt.get("fallback_result") == "succeeded"
                and outcome != "succeeded"
                or receipt.get("fallback_result") == "failed"
                and outcome not in {"failed", "abandoned"}
            ):
                return {}, "aggregator_thinking_receipt_outcome_mismatch"
            aggregator_current[identity] = (unified, native)
            aggregator_current_seen.add(identity)
            aggregator_receipt_positions[identity] = position + 1

    if any(
        aggregator_receipt_positions.get(identity, 0) != len(receipts)
        for identity, receipts in aggregator_receipts_by_identity.items()
    ):
        return {}, "unbound_physical_aggregator_thinking_receipt"
    if any(
        tuple(schedules[("aggregator_execution", identity)]["current"]) != current
        for identity, current in aggregator_current.items()
    ):
        return {}, "physical_aggregator_thinking_execution_state_mismatch"

    final_reason = final_request_thinking_execution_reason(observed_plan, call)
    if final_reason:
        return {}, final_reason
    return dict(observed_plan), ""


def _physical_attempt_id(value: Mapping[str, Any]) -> tuple[str, str]:
    """Read one top-level/nested physical id and reject mirror drift."""

    direct = str(value.get("physical_attempt_id") or "").strip()
    provider_usage = value.get("provider_usage")
    nested = (
        str(provider_usage.get("physical_attempt_id") or "").strip()
        if isinstance(provider_usage, Mapping)
        else ""
    )
    if direct and nested and direct != nested:
        return "", "physical_attempt_id_mirror_mismatch"
    attempt_id = direct or nested
    if PHYSICAL_ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        return "", "invalid_physical_attempt_id"
    return attempt_id, ""


def _receipt_from_binding(binding: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    receipt = binding.get("receipt")
    if (
        set(binding) != {"receipt", "rejected_physical_attempt_id"}
        or not isinstance(receipt, Mapping)
        or set(receipt) != THINKING_FALLBACK_FIELDS
        or PHYSICAL_ATTEMPT_ID_RE.fullmatch(
            str(binding.get("rejected_physical_attempt_id") or "")
        )
        is None
    ):
        return {}, "invalid_thinking_fallback_physical_binding"
    return dict(receipt), ""


def _validate_thinking_physical_evidence_v1(
    previous_plan: Mapping[str, Any],
    call: Mapping[str, Any],
) -> str:
    """Validate exact one-request/one-id managed-thinking evidence."""

    observed_plan = call.get("selection_plan")
    if not isinstance(observed_plan, Mapping):
        return "missing_physical_thinking_selection_plan"
    if (
        previous_plan.get("thinking_physical_evidence_schema")
        != THINKING_PHYSICAL_EVIDENCE_SCHEMA
        or observed_plan.get("thinking_physical_evidence_schema")
        != THINKING_PHYSICAL_EVIDENCE_SCHEMA
    ):
        return "thinking_physical_evidence_schema_drift"

    previous_receipts = previous_plan.get("thinking_execution_fallbacks", [])
    observed_receipts = observed_plan.get("thinking_execution_fallbacks", [])
    if not isinstance(previous_receipts, list) or not isinstance(
        observed_receipts,
        list,
    ):
        return "invalid_physical_thinking_receipt_prefix"
    new_receipts = observed_receipts[len(previous_receipts) :]
    proposer_receipts: dict[str, list[dict[str, Any]]] = {}
    aggregator_receipts: list[dict[str, Any]] = []
    for receipt in new_receipts:
        if not isinstance(receipt, Mapping):
            return "invalid_physical_thinking_receipt"
        stage = str(receipt.get("trigger_stage") or "")
        if stage == "proposer_execution":
            proposer_receipts.setdefault(
                str(receipt.get("identity") or ""),
                [],
            ).append(dict(receipt))
        elif stage == "aggregator_execution":
            aggregator_receipts.append(dict(receipt))

    physical_ids: list[str] = []
    candidates = call.get("candidates")
    if not isinstance(candidates, list):
        return "missing_physical_proposer_candidates"
    proposer_bindings: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return "invalid_physical_proposer_candidate"
        execution = candidate.get("execution")
        if not isinstance(execution, Mapping):
            if candidate.get("request_started") is True:
                return "missing_physical_proposer_thinking_execution"
            continue
        identity = (
            f"{str(candidate.get('requested_provider') or '').strip()}:"
            f"{str(candidate.get('requested_model') or '').strip()}"
        )
        attempts = execution.get("physical_attempts", [])
        if not isinstance(attempts, list) or any(
            not isinstance(row, Mapping) for row in attempts
        ):
            return "invalid_physical_proposer_attempt_ledger"
        if bool(attempts) != (candidate.get("request_started") is True):
            return "physical_proposer_started_ledger_mismatch"
        raw_count = candidate.get("physical_request_count")
        if (
            not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count != len(attempts)
        ):
            return "physical_proposer_exact_request_count_mismatch"
        attempt_by_id: dict[str, Mapping[str, Any]] = {}
        for index, attempt in enumerate(attempts, start=1):
            if (
                attempt.get("attempt") != index
                or attempt.get("request_started") is not True
                or str(attempt.get("identity") or "") != identity
                or str(attempt.get("outcome") or "") not in PHYSICAL_ATTEMPT_OUTCOMES
                or not str(attempt.get("effective_thinking_level") or "")
                or not str(attempt.get("provider_thinking_level") or "")
            ):
                return "invalid_physical_proposer_attempt"
            attempt_id, attempt_reason = _physical_attempt_id(attempt)
            if attempt_reason:
                return attempt_reason
            physical_ids.append(attempt_id)
            attempt_by_id[attempt_id] = attempt

        bindings = execution.get("thinking_fallback_bindings", [])
        if not isinstance(bindings, list) or any(
            not isinstance(binding, Mapping) for binding in bindings
        ):
            return "invalid_physical_proposer_thinking_fallback_bindings"
        for binding in bindings:
            receipt, binding_reason = _receipt_from_binding(binding)
            if binding_reason:
                return binding_reason
            rejected_id = str(binding.get("rejected_physical_attempt_id") or "")
            rejected = attempt_by_id.get(rejected_id)
            if rejected is None:
                return "unbound_physical_proposer_thinking_receipt"
            expected_outcome = str(receipt.get("reason") or "")
            if (
                str(rejected.get("outcome") or "") != expected_outcome
                or str(rejected.get("effective_thinking_level") or "")
                != str(receipt.get("rejected_unified_level") or "")
                or str(rejected.get("provider_thinking_level") or "")
                != str(receipt.get("rejected_provider_level") or "")
            ):
                return "proposer_thinking_receipt_rejected_attempt_mismatch"
            proposer_bindings.setdefault(identity, []).append(receipt)
    if proposer_bindings != proposer_receipts:
        return "unbound_physical_proposer_thinking_receipt"

    recovery = call.get("aggregator_recovery")
    recovery_attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    aggregator_binding_receipts: list[dict[str, Any]] = []
    selected_attempt_id = ""
    if not isinstance(recovery_attempts, list):
        return "invalid_physical_aggregator_recovery_attempts"
    selected_attempt = recovery.get("selected_attempt") if isinstance(recovery, Mapping) else None
    for recovery_index, attempt in enumerate(recovery_attempts):
        if not isinstance(attempt, Mapping):
            return "invalid_physical_aggregator_recovery_attempt"
        started = attempt.get("request_started") is True
        raw_count = attempt.get("physical_request_count")
        if started:
            if raw_count != 1:
                return "physical_aggregator_exact_request_count_mismatch"
            attempt_id, attempt_reason = _physical_attempt_id(attempt)
            if attempt_reason:
                return attempt_reason
            physical_ids.append(attempt_id)
            if attempt.get("attempt") == selected_attempt:
                selected_attempt_id = attempt_id
        else:
            if raw_count not in {None, 0} or attempt.get("physical_attempt_id"):
                return "unstarted_aggregator_attempt_has_physical_identity"
        binding = attempt.get("thinking_fallback_binding")
        if binding is None:
            continue
        if not started or not isinstance(binding, Mapping):
            return "invalid_physical_aggregator_thinking_fallback_binding"
        receipt, binding_reason = _receipt_from_binding(binding)
        if binding_reason:
            return binding_reason
        if binding.get("rejected_physical_attempt_id") != attempt.get(
            "physical_attempt_id"
        ):
            return "aggregator_thinking_receipt_rejected_attempt_mismatch"
        if (
            attempt.get("thinking_fallback_rejection_reason")
            != receipt.get("reason")
        ):
            return "aggregator_thinking_rejection_reason_mismatch"
        execution = attempt.get("execution")
        if not isinstance(execution, Mapping):
            return "missing_physical_aggregator_thinking_execution"
        if (
            str(receipt.get("identity") or "")
            != (
                f"{str(attempt.get('requested_provider') or '').strip()}:"
                f"{str(attempt.get('requested_model') or '').strip()}"
            )
            or str(execution.get("effective_thinking_level") or "")
            != str(receipt.get("rejected_unified_level") or "")
            or str(execution.get("provider_thinking_level") or "")
            != str(receipt.get("rejected_provider_level") or "")
        ):
            return "aggregator_thinking_receipt_rejected_attempt_mismatch"
        next_started = next(
            (
                candidate
                for candidate in recovery_attempts[
                    recovery_index + 1 :
                ]
                if isinstance(candidate, Mapping)
                and candidate.get("request_started") is True
            ),
            None,
        )
        next_execution = (
            next_started.get("execution")
            if isinstance(next_started, Mapping)
            else None
        )
        if (
            not isinstance(next_started, Mapping)
            or not isinstance(next_execution, Mapping)
            or str(next_started.get("requested_provider") or "")
            != str(attempt.get("requested_provider") or "")
            or str(next_started.get("requested_model") or "")
            != str(attempt.get("requested_model") or "")
            or str(
                next_execution.get("effective_thinking_level") or ""
            )
            != str(receipt.get("effective_thinking_level") or "")
            or str(next_execution.get("provider_thinking_level") or "")
            != str(receipt.get("effective_provider_level") or "")
        ):
            return (
                "aggregator_thinking_fallback_not_immediate_successor"
            )
        if receipt.get("reason") == "reasoning_only_length" and (
            attempt.get("code")
            != "ensemble_aggregator_reasoning_only_length"
            or attempt.get("trigger") != "reasoning_only_length"
            or str(attempt.get("stop_reason") or "").strip().casefold()
            not in {"length", "max_tokens"}
        ):
            return "aggregator_reasoning_only_rejection_evidence_mismatch"
        aggregator_binding_receipts.append(receipt)
    if aggregator_binding_receipts != aggregator_receipts:
        return "unbound_physical_aggregator_thinking_receipt"

    if len(physical_ids) != len(set(physical_ids)):
        return "duplicate_physical_attempt_id"
    raw_call_count = call.get("physical_request_count")
    if (
        not isinstance(raw_call_count, int)
        or isinstance(raw_call_count, bool)
        or raw_call_count != len(physical_ids)
        or call.get("llm_request_count") != raw_call_count
    ):
        return "thinking_physical_call_exact_request_count_mismatch"

    final_request = call.get("final_request")
    usage = final_request.get("usage") if isinstance(final_request, Mapping) else None
    if selected_attempt_id:
        if not isinstance(usage, Mapping):
            return "missing_final_request_physical_usage"
        usage_id, usage_reason = _physical_attempt_id(usage)
        if usage_reason or usage_id != selected_attempt_id:
            return "final_request_physical_attempt_id_mismatch"
    return ""


def validate_thinking_execution_call(
    previous_plan: Mapping[str, Any],
    call: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Validate one call, using exact v1 evidence only for v4 routed plans."""

    validated_plan, reason = _validate_thinking_execution_call_legacy(
        previous_plan,
        call,
    )
    if reason:
        return {}, reason
    observed_plan = call.get("selection_plan")
    if not isinstance(observed_plan, Mapping):
        return {}, "missing_physical_thinking_selection_plan"
    schema = observed_plan.get("thinking_physical_evidence_schema")
    if schema is None:
        return validated_plan, ""
    if schema != THINKING_PHYSICAL_EVIDENCE_SCHEMA:
        return {}, "unknown_thinking_physical_evidence_schema"
    strict_reason = _validate_thinking_physical_evidence_v1(
        previous_plan,
        call,
    )
    if strict_reason:
        return {}, strict_reason
    return validated_plan, ""
