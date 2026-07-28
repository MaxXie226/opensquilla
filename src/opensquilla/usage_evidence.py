"""Pure helpers for representing physical LLM request usage evidence.

The accounting contract distinguishes a physical request from a provider
receipt.  A request may have no receipt (for example, an upstream HTTP 503),
but it must still occupy exactly one usage unit.  Such units are represented
by explicit, zero-valued placeholders whose cost remains unknown.

This module intentionally has no provider or runner dependencies so runtime
adapters, experiment runners, and offline finalizers can share one contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

USAGE_EVIDENCE_SCHEMA = "opensquilla.usage-evidence/v1"
MISSING_USAGE_PLACEHOLDER_ROLE = "usage_missing"
MISSING_USAGE_PLACEHOLDER_ROLES = frozenset(
    {
        "abandoned_stream",
        "usage_missing",
        "unknown_call",
        "abandoned_stream_request",
        "agent_llm_request_unknown",
        "abandoned_provider_request",
        "unknown_request",
        "incomplete_stream",
    }
)

_USAGE_ENVELOPE_ONLY_KEYS = frozenset(
    {
        "ensemble_trace",
        "llm_request_count",
        "model_usage_breakdown",
        "physical_request_count",
        "request_started",
        "usage_evidence_schema",
        "usage_missing_count",
        "usage_unknown_count",
    }
)


class UsageEvidenceError(ValueError):
    """Usage evidence contradicts its declared physical-request count."""


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not value.is_integer() or value < 0:
            return None
        return int(value)
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = int(text)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def is_missing_usage_placeholder(row: Mapping[str, Any] | Any) -> bool:
    """Return whether ``row`` explicitly represents a request without usage."""

    return isinstance(row, Mapping) and (
        str(row.get("role") or "").strip().casefold()
        in MISSING_USAGE_PLACEHOLDER_ROLES
    )


def usage_units(usage: Any) -> list[dict[str, Any]]:
    """Return physical receipt/placeholder rows from a usage envelope.

    Counter-only envelopes are not receipts.  This is the important
    distinction for records such as ``{"usage_missing_count": 1}``: the
    counter drives canonical placeholder creation but must not itself count as
    an already represented physical request.
    """

    if not isinstance(usage, Mapping) or not usage:
        return []

    def envelope_unit() -> dict[str, Any]:
        return {
            str(key): value
            for key, value in usage.items()
            if key not in _USAGE_ENVELOPE_ONLY_KEYS
        }

    breakdown = usage.get("model_usage_breakdown")
    if breakdown is not None and not isinstance(breakdown, list):
        raise UsageEvidenceError("model_usage_breakdown must be a list when present")
    if isinstance(breakdown, list) and breakdown:
        if any(not isinstance(item, Mapping) for item in breakdown):
            raise UsageEvidenceError(
                "model_usage_breakdown contains a non-mapping usage unit"
            )
        return [dict(item) for item in breakdown if isinstance(item, Mapping)]
    if set(usage).issubset(_USAGE_ENVELOPE_ONLY_KEYS):
        return []
    if is_missing_usage_placeholder(usage):
        return [envelope_unit()]
    if any(
        str(usage.get(key) or "").strip()
        for key in ("role", "provider", "model")
    ):
        return [envelope_unit()]
    if any(
        (_nonnegative_int(usage.get(key)) or 0) > 0
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_write_tokens",
        )
    ):
        return [envelope_unit()]
    try:
        billed_cost = float(usage.get("billed_cost") or 0.0)
    except (TypeError, ValueError, OverflowError):
        billed_cost = 0.0
    if billed_cost != 0.0 or usage.get("billing_receipt") is not None:
        return [envelope_unit()]
    if str(usage.get("cost_source") or "").strip().casefold() not in {
        "",
        "none",
        "unavailable",
    }:
        return [envelope_unit()]
    provider_usage = usage.get("provider_usage")
    if isinstance(provider_usage, Mapping) and any(
        provider_usage.get(key) not in (None, "", [], {})
        for key in (
            "response_id",
            "response_ids",
            "provider_reported_cost",
            "router_metadata",
        )
    ):
        return [envelope_unit()]
    return []


def _usage_from_run(run: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = run.get("usage")
    return usage if isinstance(usage, Mapping) else {}


def _declared_request_count(run: Mapping[str, Any]) -> int | None:
    """Read the highest-level available physical-request declaration.

    A run-level count is the aggregate for the whole run.  Nested ensemble
    traces and individual error trace events can describe a smaller call
    within that run (for example, a paid setup request followed by an outer
    request that never started), so declarations are compared only within the
    same accounting layer and lower layers are used strictly as fallbacks.
    """

    def source_declaration(source: Mapping[str, Any], *, label: str) -> int | None:
        declarations: list[tuple[str, int]] = []
        for key in ("physical_request_count", "llm_request_count"):
            if key not in source or source.get(key) is None:
                continue
            value = _nonnegative_int(source.get(key))
            if value is None:
                raise UsageEvidenceError(
                    f"{label}.{key} must be a nonnegative integer"
                )
            declarations.append((f"{label}.{key}", value))
        if not declarations:
            return None
        values = {value for _, value in declarations}
        if len(values) != 1:
            detail = ", ".join(f"{name}={value}" for name, value in declarations)
            raise UsageEvidenceError(
                f"conflicting physical request count declarations: {detail}"
            )
        return declarations[0][1]

    direct = source_declaration(run, label="run")
    if direct is not None:
        return direct

    usage = _usage_from_run(run)
    for label, source in (
        ("usage", usage),
        ("run.ensemble_trace", run.get("ensemble_trace")),
        ("usage.ensemble_trace", usage.get("ensemble_trace")),
    ):
        if isinstance(source, Mapping):
            value = source_declaration(source, label=label)
            if value is not None:
                return value

    trace_events = run.get("trace_events")
    if isinstance(trace_events, Sequence) and not isinstance(
        trace_events,
        (str, bytes, bytearray),
    ):
        for event in reversed(trace_events):
            if not isinstance(event, Mapping):
                continue
            value = source_declaration(event, label="trace_event")
            if value is not None:
                return value
            if event.get("request_started") is False:
                return 0
    return None


def _missing_request_count(
    run: Mapping[str, Any],
    *,
    units: Sequence[Mapping[str, Any]],
) -> int:
    usage = _usage_from_run(run)
    counts: list[int] = []

    def add_count(source: Mapping[str, Any], key: str, *, label: str) -> None:
        if key not in source or source.get(key) is None:
            return
        value = _nonnegative_int(source.get(key))
        if value is None:
            raise UsageEvidenceError(f"{label}.{key} must be a nonnegative integer")
        counts.append(value)

    add_count(run, "usage_missing_count", label="run")
    add_count(usage, "usage_missing_count", label="usage")
    for source in (run.get("ensemble_trace"), usage.get("ensemble_trace")):
        if isinstance(source, Mapping):
            add_count(source, "usage_missing_count", label="ensemble_trace")

    # ``usage_unknown_count`` can also describe an inexact receipt.  It is
    # safe to interpret it as a missing physical unit only when no unit exists.
    if not units:
        add_count(run, "usage_unknown_count", label="run")
        add_count(usage, "usage_unknown_count", label="usage")
    return max(counts, default=0)


def derive_physical_request_count(
    run: Mapping[str, Any],
    *,
    default_request_count: int = 0,
) -> int:
    """Derive one authoritative physical-request count for ``run``.

    Placeholder rows already materialize missing requests, so scalar missing
    counters add only their unrepresented deficit.  An explicit declaration
    is authoritative and contradictions fail closed.
    """

    if not isinstance(run, Mapping):
        raise TypeError("run must be a mapping")
    units = usage_units(run.get("usage"))
    placeholder_count = sum(is_missing_usage_placeholder(unit) for unit in units)
    missing_count = _missing_request_count(run, units=units)
    represented_count = len(units) + max(0, missing_count - placeholder_count)
    declared_count = _declared_request_count(run)

    if declared_count is not None:
        if represented_count > declared_count:
            raise UsageEvidenceError(
                "usage evidence represents more physical requests than declared "
                f"({represented_count} > {declared_count})"
            )
        if run.get("request_started") is False and declared_count != 0:
            raise UsageEvidenceError(
                "request_started=false contradicts a nonzero physical request count"
            )
        return declared_count

    default_count = _nonnegative_int(default_request_count)
    if default_count is None:
        raise UsageEvidenceError("default_request_count must be a nonnegative integer")
    if run.get("request_started") is False:
        if represented_count:
            raise UsageEvidenceError(
                "request_started=false contradicts persisted usage evidence"
            )
        return 0
    return max(
        represented_count,
        default_count,
        1 if run.get("request_started") is True else 0,
    )


def _requested_identity(
    run: Mapping[str, Any],
    *,
    requested_provider: str | None,
    requested_model: str | None,
) -> tuple[str, str]:
    usage = _usage_from_run(run)
    provider = (
        requested_provider
        if requested_provider is not None
        else run.get("requested_provider", usage.get("requested_provider", ""))
    )
    model = (
        requested_model
        if requested_model is not None
        else run.get("requested_model", usage.get("requested_model", ""))
    )
    return str(provider or "").strip(), str(model or "").strip()


def _placeholder_id(*, identity_seed: str, ordinal: int, role: str) -> str:
    canonical = json.dumps(
        {
            "identity_seed": str(identity_seed),
            "ordinal": ordinal,
            "role": role,
            "schema": USAGE_EVIDENCE_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _missing_usage_placeholder(
    *,
    identity_seed: str,
    ordinal: int,
    requested_provider: str,
    requested_model: str,
    role: str,
) -> dict[str, Any]:
    evidence_id = _placeholder_id(
        identity_seed=identity_seed,
        ordinal=ordinal,
        role=role,
    )
    return {
        "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
        "usage_evidence_id": evidence_id,
        "usage_evidence_source": "physical_request_counter_deficit",
        "role": role,
        "physical_request_ordinal": ordinal,
        # Requested identity is configuration evidence.  A failed request has
        # no successful response from which to claim actual identity.
        "provider": "",
        "model": "",
        "requested_provider": requested_provider,
        "requested_model": requested_model,
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
            "usage_evidence_id": evidence_id,
        },
    }


def canonical_run_usage_units(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    role: str | None = None,
    default_request_count: int = 0,
) -> list[dict[str, Any]]:
    """Return exactly one usage unit per physical request in ``run``."""

    units = usage_units(run.get("usage"))
    physical_count = derive_physical_request_count(
        run,
        default_request_count=default_request_count,
    )
    if len(units) > physical_count:
        raise UsageEvidenceError(
            "usage has more units than the physical request count "
            f"({len(units)} > {physical_count})"
        )
    requested_provider_value, requested_model_value = _requested_identity(
        run,
        requested_provider=requested_provider,
        requested_model=requested_model,
    )
    placeholder_role = (
        str(role).strip().casefold()
        if role is not None and str(role).strip()
        else MISSING_USAGE_PLACEHOLDER_ROLE
    )
    result = [dict(unit) for unit in units]
    for ordinal in range(len(result) + 1, physical_count + 1):
        result.append(
            _missing_usage_placeholder(
                identity_seed=identity_seed,
                ordinal=ordinal,
                requested_provider=requested_provider_value,
                requested_model=requested_model_value,
                role=placeholder_role,
            )
        )
    return result


def canonicalize_run_usage(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    role: str | None = None,
    default_request_count: int = 0,
) -> dict[str, Any]:
    """Return a canonical usage envelope without mutating ``run``."""

    raw_usage = run.get("usage")
    canonical = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
    units = canonical_run_usage_units(
        run,
        identity_seed=identity_seed,
        requested_provider=requested_provider,
        requested_model=requested_model,
        role=role,
        default_request_count=default_request_count,
    )
    if not units:
        canonical.pop("model_usage_breakdown", None)
        return canonical
    canonical["usage_evidence_schema"] = USAGE_EVIDENCE_SCHEMA
    canonical["model_usage_breakdown"] = units
    placeholder_count = sum(is_missing_usage_placeholder(unit) for unit in units)
    canonical["usage_missing_count"] = max(
        _nonnegative_int(canonical.get("usage_missing_count")) or 0,
        placeholder_count,
    )
    return canonical
