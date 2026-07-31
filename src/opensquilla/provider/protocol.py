"""LLMProvider Protocol and provider-plugin extension contract."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .types import (
    ChatConfig,
    ErrorEvent,
    Message,
    ModelInfo,
    ProviderMessageCountProjection,
    QuotaStatus,
    StreamEvent,
    ToolDefinition,
)

if TYPE_CHECKING:
    from .selector import ProviderConfig, SelectorConfig


@dataclass(frozen=True)
class ProviderMetadata:
    """Read-only non-secret identity metadata exposed by provider implementations."""

    provider_name: str = ""
    provider_kind: str = ""
    model: str = ""
    base_url: str = ""
    # Configured registry identity (for example ``dashscope`` or
    # ``minimax_global``).  This is deliberately separate from
    # ``provider_name``, which identifies the adapter family, and
    # ``provider_kind``, which selects a wire-compatibility policy.
    provider_id: str = ""


@dataclass(frozen=True)
class ProviderConnectionConfig:
    """Provider connection fields for internal runtime calls."""

    provider_kind: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""


@runtime_checkable
class ProviderMetadataProvider(Protocol):
    def provider_metadata(self) -> ProviderMetadata:
        """Return read-only provider metadata without exposing secrets."""
        ...


@runtime_checkable
class ProviderConnectionConfigProvider(Protocol):
    def provider_connection_config(self) -> ProviderConnectionConfig:
        """Return internal connection fields for provider-owned runtime calls."""
        ...


@runtime_checkable
class ProviderMessageCountProjector(Protocol):
    """Optional, side-effect-free wire-message cardinality projection."""

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        """Project the adapter's final wire count without issuing a request."""
        ...


def _string_value(value: object) -> str:
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        value = get_secret_value()
    return str(value).strip()


def provider_metadata(provider: object | None) -> ProviderMetadata:
    """Return provider identity metadata, preferring the public protocol."""
    if provider is None:
        return ProviderMetadata()
    metadata_fn = getattr(provider, "provider_metadata", None)
    if callable(metadata_fn):
        metadata = metadata_fn()
        if isinstance(metadata, ProviderMetadata):
            return metadata

    provider_name = _string_value(getattr(provider, "provider_name", ""))
    provider_id = _string_value(getattr(provider, "provider_id", ""))
    provider_kind = _string_value(getattr(provider, "provider_kind", ""))
    model = _string_value(getattr(provider, "model", ""))
    base_url = _string_value(getattr(provider, "base_url", ""))

    # Metadata-provider migration path: new code should expose provider_metadata().
    provider_kind = provider_kind or _string_value(getattr(provider, "_provider_kind", ""))
    model = model or _string_value(getattr(provider, "_model", ""))
    base_url = base_url or _string_value(getattr(provider, "_base_url", ""))
    return ProviderMetadata(
        provider_name=provider_name,
        provider_kind=provider_kind,
        model=model,
        base_url=base_url,
        provider_id=provider_id or provider_name,
    )


def configured_provider_id(provider: object | None) -> str:
    """Return the operator-facing registry identity for a provider instance.

    Generic adapters intentionally keep their family ``provider_name`` (for
    example ``openai`` or ``anthropic``) because compatibility, error
    classification, and catalog logic rely on it.  Runtime telemetry must use
    the configured deployment identity instead, when one was supplied by the
    selector factory.
    """

    metadata = provider_metadata(provider)
    return metadata.provider_id or metadata.provider_name


def provider_connection_config(provider: object | None) -> ProviderConnectionConfig:
    """Return internal provider connection fields without broadening metadata."""
    if provider is None:
        return ProviderConnectionConfig()
    config_fn = getattr(provider, "provider_connection_config", None)
    if callable(config_fn):
        config = config_fn()
        if isinstance(config, ProviderConnectionConfig):
            return config

    metadata = provider_metadata(provider)
    api_key = _string_value(getattr(provider, "api_key", ""))
    api_key = api_key or _string_value(getattr(provider, "_api_key", ""))
    return ProviderConnectionConfig(
        provider_kind=metadata.provider_kind,
        model=metadata.model,
        api_key=api_key,
        base_url=metadata.base_url,
    )


def project_provider_message_count(
    provider: object | None,
    messages: list[Message],
    config: ChatConfig | None = None,
    *,
    additional_messages: int = 0,
) -> ProviderMessageCountProjection | None:
    """Return an optional provider projection without broadening ``LLMProvider``.

    Projection is a recovery aid, never a prerequisite for a normal provider
    call.  A missing, invalid, or failing optional implementation therefore
    resolves to ``None`` rather than changing the established chat contract.
    """

    if provider is None:
        return None
    projection_fn = getattr(provider, "project_message_count", None)
    if not callable(projection_fn):
        return None
    try:
        projection = projection_fn(
            messages,
            config,
            additional_messages=additional_messages,
        )
    except Exception:  # noqa: BLE001 - optional capability must stay best-effort
        return None
    return projection if isinstance(projection, ProviderMessageCountProjection) else None


def validate_provider_chat_request(
    provider: object | None,
    messages: list[Message],
) -> ErrorEvent | None:
    """Run an optional, side-effect-free provider request preflight.

    Validation is deliberately duck-typed instead of widening
    :class:`LLMProvider`: ordinary providers keep the established chat
    contract, while composite providers may reject requests before any
    physical model call or usage-accounting envelope starts.

    A missing, raising, or invalid optional implementation is ignored.  The
    provider remains the authoritative fallback boundary and must repeat its
    own validation immediately before starting work.
    """

    if provider is None:
        return None
    validation_fn = getattr(provider, "validate_chat_request", None)
    if not callable(validation_fn):
        return None
    try:
        validation_error = validation_fn(messages)
    except Exception:  # noqa: BLE001 - optional preflight must stay best-effort
        return None
    return validation_error if isinstance(validation_error, ErrorEvent) else None


@runtime_checkable
class LLMProvider(Protocol):
    """Unified async streaming interface for any LLM backend.

    Implementors must provide:
    - chat(): streams events for a conversation turn
    - list_models(): returns available models for this provider
    - provider_name: str identifier (e.g. "anthropic", "openai", "ollama")
    """

    provider_name: str

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a conversation turn.

        Yields StreamEvent instances in order:
        - TextDeltaEvent for text chunks
        - ToolUseStartEvent / ToolUseDeltaEvent / ToolUseEndEvent for tool calls
        - DoneEvent when the turn completes
        - ErrorEvent on failure (instead of raising)
        """
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Return all models available from this provider."""
        ...


@dataclass(frozen=True)
class ProviderRetryTransition:
    """A zero-request transition to a materially different provider roster.

    The failed provider remains the owner of its terminal usage evidence.  A
    transition only describes a newly constructed provider; it never carries
    usage rows forward or authorizes replay of the failed provider instance.
    """

    replacement_provider: LLMProvider = field(repr=False, compare=False)
    reason: str
    source_roster_fingerprint: str
    target_roster_fingerprint: str
    excluded_identities: tuple[str, ...] = ()
    source_plan: dict[str, Any] = field(default_factory=dict, repr=False)
    target_plan: dict[str, Any] = field(default_factory=dict, repr=False)
    setup_physical_request_count: int = 0


class ProviderRetryScopeError(RuntimeError):
    """An optional provider retry-scope hook failed closed."""


def _validated_provider_retry_scope_id(scope_id: object) -> str:
    if (
        not isinstance(scope_id, str)
        or not scope_id
        or scope_id != scope_id.strip()
    ):
        raise ValueError("provider retry scope_id must be a non-empty stripped string")
    return scope_id


def begin_provider_retry_scope(
    provider: object | None,
    scope_id: str,
    *,
    max_additional_physical_requests: int = 3,
) -> bool:
    """Begin an optional run-turn retry ledger on ``provider``.

    Providers without the hook remain compatible. Once a hook exists, an
    explicit refusal or exception is a correctness failure: silently falling
    back to a per-chat budget would reset a run-turn physical-call cap.
    """

    validated_scope_id = _validated_provider_retry_scope_id(scope_id)
    if (
        isinstance(max_additional_physical_requests, bool)
        or not isinstance(max_additional_physical_requests, int)
        or max_additional_physical_requests < 0
    ):
        raise ValueError(
            "max_additional_physical_requests must be a non-negative integer"
        )
    if provider is None:
        return False
    begin = getattr(provider, "begin_provider_retry_scope", None)
    if not callable(begin):
        return False
    try:
        result = begin(
            validated_scope_id,
            max_additional_physical_requests=max_additional_physical_requests,
        )
    except Exception as exc:  # noqa: BLE001 - optional hook must fail closed
        raise ProviderRetryScopeError(
            "provider begin_provider_retry_scope hook failed"
        ) from exc
    if result is False:
        raise ProviderRetryScopeError(
            "provider begin_provider_retry_scope hook refused the scope"
        )
    if result not in {None, True}:
        raise ProviderRetryScopeError(
            "provider begin_provider_retry_scope hook returned an invalid result"
        )
    return True


def reserve_provider_retry_physical_request(
    provider: object | None,
    scope_id: str,
    *,
    physical_request_count: int = 1,
) -> bool:
    """Reserve physical-call budget from an optional run-turn retry ledger.

    A provider without this capability remains compatible and returns
    ``False``. Once the hook exists, every refusal, invalid result, or
    exception fails closed so a caller cannot issue an unaccounted request
    after the shared retry budget is exhausted.
    """

    validated_scope_id = _validated_provider_retry_scope_id(scope_id)
    if (
        isinstance(physical_request_count, bool)
        or not isinstance(physical_request_count, int)
        or physical_request_count <= 0
    ):
        raise ValueError("physical_request_count must be a positive integer")
    if provider is None:
        return False
    reserve = getattr(
        provider,
        "reserve_provider_retry_physical_request",
        None,
    )
    if not callable(reserve):
        return False
    try:
        result = reserve(
            validated_scope_id,
            physical_request_count=physical_request_count,
        )
    except Exception as exc:  # noqa: BLE001 - optional hook must fail closed
        raise ProviderRetryScopeError(
            "provider reserve_provider_retry_physical_request hook failed"
        ) from exc
    if result is False:
        raise ProviderRetryScopeError(
            "provider retry physical-request budget is exhausted"
        )
    if result is not True:
        raise ProviderRetryScopeError(
            "provider reserve_provider_retry_physical_request hook returned "
            "an invalid result"
        )
    return True


def end_provider_retry_scope(
    provider: object | None,
    scope_id: str,
) -> bool:
    """End an optional run-turn retry ledger on ``provider``."""

    validated_scope_id = _validated_provider_retry_scope_id(scope_id)
    if provider is None:
        return False
    end = getattr(provider, "end_provider_retry_scope", None)
    if not callable(end):
        return False
    try:
        result = end(validated_scope_id)
    except Exception as exc:  # noqa: BLE001 - optional hook must fail closed
        raise ProviderRetryScopeError(
            "provider end_provider_retry_scope hook failed"
        ) from exc
    if result is False:
        raise ProviderRetryScopeError(
            "provider end_provider_retry_scope hook refused the scope"
        )
    if result not in {None, True}:
        raise ProviderRetryScopeError(
            "provider end_provider_retry_scope hook returned an invalid result"
        )
    return True


def _canonical_provider_model_identity(
    value: object,
) -> tuple[str, str] | None:
    """Return the two canonical identity components or reject the value."""

    if (
        not isinstance(value, str)
        or value != value.strip()
        or ":" not in value
        or any(character.isspace() for character in value)
    ):
        return None
    provider, model = value.split(":", 1)
    if (
        not provider
        or provider != provider.casefold()
        or not model
        or any(not segment for segment in model.split(":"))
    ):
        return None
    return provider, model


def provider_retry_expanded_proposer_identities(
    plan: object,
) -> tuple[str, ...]:
    """Bind every k-expanded proposer sample to one exact provider identity.

    ``selected_P`` names distinct ranked members, while ``proposer_models``
    records their physical samples after each member's ``k`` expansion. The
    latter omits provider ids. A one-sample-per-member roster is still
    unambiguous by position even when two providers expose the same model id.
    For k-expanded rosters, selected members must expose distinct model ids and
    each model must occupy one non-empty contiguous block in ranking order.
    An empty tuple is the fail-closed invalid result.
    """

    if not isinstance(plan, Mapping):
        try:
            plan = dict(plan)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ()
    selected_p = plan.get("selected_P")
    proposer_models = plan.get("proposer_models")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or not isinstance(proposer_models, list)
        or not proposer_models
    ):
        return ()
    selected_parts = [
        _canonical_provider_model_identity(identity)
        for identity in selected_p
    ]
    selected_identities = [
        identity for identity in selected_p if isinstance(identity, str)
    ]
    if (
        any(parts is None for parts in selected_parts)
        or len(selected_identities) != len(selected_p)
        or len(set(selected_identities)) != len(selected_identities)
    ):
        return ()
    selected_models = [
        parts[1]
        for parts in selected_parts
        if parts is not None
    ]
    if any(
        not isinstance(model, str)
        or not model
        or model != model.strip()
        or any(character.isspace() for character in model)
        or any(not segment for segment in model.split(":"))
        for model in proposer_models
    ):
        return ()

    if len(proposer_models) == len(selected_models):
        if proposer_models != selected_models:
            return ()
        return tuple(selected_identities)
    if len(set(selected_models)) != len(selected_models):
        return ()

    expanded: list[str] = []
    model_index = 0
    for identity, model in zip(
        selected_identities,
        selected_models,
        strict=True,
    ):
        block_start = model_index
        while (
            model_index < len(proposer_models)
            and proposer_models[model_index] == model
        ):
            expanded.append(identity)
            model_index += 1
        if model_index == block_start:
            return ()
    if model_index != len(proposer_models):
        return ()
    return tuple(expanded)


def provider_retry_roster_fingerprint(plan: object) -> str:
    """Hash the physical ensemble roster and quorum encoded by a route plan.

    Decision IDs are deliberately excluded: changing an ID does not prove that
    a retry will invoke different physical models.
    """

    if not isinstance(plan, dict):
        try:
            plan = dict(plan)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ""
    strategy = _string_value(plan.get("strategy")).casefold()
    selection_mode = _string_value(plan.get("selection_mode")).casefold()
    selected_p = plan.get("selected_P")
    backup_p = plan.get("backup_P")
    proposer_recovery_policy = plan.get("proposer_recovery_policy")
    proposer_models = plan.get("proposer_models")
    selected_a = plan.get("selected_A")
    aggregator_candidates = plan.get("aggregator_candidates")
    quorum = plan.get("effective_min_successful_proposers")
    sample_count = plan.get("proposer_sample_count")
    selected_p_parts = (
        [
            _canonical_provider_model_identity(identity)
            for identity in selected_p
        ]
        if isinstance(selected_p, list)
        else []
    )
    selected_a_parts = _canonical_provider_model_identity(selected_a)
    backup_p_parts = (
        [
            _canonical_provider_model_identity(identity)
            for identity in backup_p
        ]
        if isinstance(backup_p, list)
        else []
    )
    aggregator_parts = (
        [
            _canonical_provider_model_identity(identity)
            for identity in aggregator_candidates
        ]
        if isinstance(aggregator_candidates, list)
        else []
    )
    normalized_proposer_models = (
        list(proposer_models)
        if isinstance(proposer_models, list)
        and all(
            isinstance(model, str)
            and model
            and model == model.strip()
            and not any(character.isspace() for character in model)
            and not any(
                not segment for segment in model.split(":")
            )
            for model in proposer_models
        )
        else []
    )
    selected_p_strings = (
        [identity for identity in selected_p if isinstance(identity, str)]
        if isinstance(selected_p, list)
        else []
    )
    backup_p_strings = (
        [identity for identity in backup_p if isinstance(identity, str)]
        if isinstance(backup_p, list)
        else []
    )
    aggregator_strings = (
        [
            identity
            for identity in aggregator_candidates
            if isinstance(identity, str)
        ]
        if isinstance(aggregator_candidates, list)
        else []
    )
    selected_model_counts = Counter(
        parts[1]
        for parts in selected_p_parts
        if parts is not None
    )
    proposer_model_counts = Counter(normalized_proposer_models)
    backup_aggregator_overlap = bool(
        isinstance(aggregator_candidates, list)
        and isinstance(backup_p, list)
        and any(candidate in backup_p for candidate in aggregator_candidates)
    )
    expanded_proposer_identities = (
        provider_retry_expanded_proposer_identities(plan)
    )
    valid_recovery_policy = False
    normalized_recovery_policy: dict[str, object] = {}
    if isinstance(proposer_recovery_policy, Mapping):
        configured_backup_count = proposer_recovery_policy.get(
            "configured_backup_count"
        )
        effective_backup_count = proposer_recovery_policy.get(
            "effective_backup_count"
        )
        max_additional = proposer_recovery_policy.get(
            "max_additional_physical_requests"
        )
        policy_quorum = proposer_recovery_policy.get("quorum_required")
        proposer_cap = proposer_recovery_policy.get("max_tokens_cap")
        proposer_reserve = proposer_recovery_policy.get(
            "visible_answer_reserve_tokens"
        )
        downgrade_order = proposer_recovery_policy.get(
            "thinking_downgrade_order"
        )
        transient_retries = proposer_recovery_policy.get(
            "transient_same_model_retries"
        )
        backup_downgrades = proposer_recovery_policy.get(
            "backup_reasoning_downgrades"
        )
        integer_values = (
            configured_backup_count,
            effective_backup_count,
            max_additional,
            policy_quorum,
            proposer_cap,
            proposer_reserve,
            transient_retries,
            backup_downgrades,
        )
        valid_recovery_policy = bool(
            proposer_recovery_policy.get("schema")
            == "opensquilla.router-dynamic-proposer-recovery/v1"
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in integer_values
            )
            and configured_backup_count >= 0
            and effective_backup_count == len(backup_p_parts)
            and effective_backup_count <= configured_backup_count
            and 0 <= max_additional <= 3
            and policy_quorum == quorum
            and proposer_cap >= 2
            and 1 <= proposer_reserve < proposer_cap
            and downgrade_order == ["one_strictly_lower"]
            and transient_retries == 1
            and backup_downgrades == 1
        )
        if valid_recovery_policy:
            # Fingerprint only the versioned execution contract. Unknown
            # metadata is deliberately excluded: it cannot change execution,
            # and including arbitrary extension values would make hashing
            # raise for otherwise harmless non-JSON objects or cycles.
            normalized_recovery_policy = {
                "schema": proposer_recovery_policy.get("schema"),
                "configured_backup_count": configured_backup_count,
                "effective_backup_count": effective_backup_count,
                "max_additional_physical_requests": max_additional,
                "quorum_required": policy_quorum,
                "max_tokens_cap": proposer_cap,
                "visible_answer_reserve_tokens": proposer_reserve,
                "thinking_downgrade_order": list(downgrade_order),
                "transient_same_model_retries": transient_retries,
                "backup_reasoning_downgrades": backup_downgrades,
            }
    if (
        plan.get("strategy") != "router_dynamic"
        or plan.get("selection_mode") != "router_dynamic"
        or strategy != "router_dynamic"
        or selection_mode != "router_dynamic"
        or not isinstance(selected_p, list)
        or not selected_p
        or any(parts is None for parts in selected_p_parts)
        or len(selected_p_strings) != len(selected_p)
        or len(set(selected_p_strings)) != len(selected_p_strings)
        or not expanded_proposer_identities
        or not isinstance(backup_p, list)
        or any(parts is None for parts in backup_p_parts)
        or len(backup_p_strings) != len(backup_p)
        or len(set(backup_p_strings)) != len(backup_p_strings)
        or bool(set(selected_p_strings).intersection(backup_p_strings))
        or backup_aggregator_overlap
        or not valid_recovery_policy
        or not isinstance(proposer_models, list)
        or not proposer_models
        or not normalized_proposer_models
        or set(proposer_model_counts) != set(selected_model_counts)
        or any(
            proposer_model_counts[model] < selected_count
            for model, selected_count in selected_model_counts.items()
        )
        or selected_a_parts is None
        or not isinstance(aggregator_candidates, list)
        or not aggregator_candidates
        or any(parts is None for parts in aggregator_parts)
        or len(aggregator_strings) != len(aggregator_candidates)
        or len(set(aggregator_strings)) != len(aggregator_strings)
        or aggregator_candidates[0] != selected_a
        or isinstance(quorum, bool)
        or not isinstance(quorum, int)
        or quorum <= 0
        or isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count != len(proposer_models)
        or quorum > sample_count
    ):
        return ""
    payload = {
        "strategy": strategy,
        "selection_mode": selection_mode,
        "selected_P": selected_p,
        "backup_P": backup_p,
        "proposer_recovery_policy": normalized_recovery_policy,
        "proposer_models": normalized_proposer_models,
        "selected_A": selected_a,
        "aggregator_candidates": aggregator_candidates,
        "effective_min_successful_proposers": quorum,
        "proposer_sample_count": sample_count,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def prepare_provider_retry_after_failure(
    provider: object | None,
    event: ErrorEvent,
) -> ProviderRetryTransition | None:
    """Invoke an optional typed replacement hook and validate it fail-closed."""

    if provider is None or not isinstance(event, ErrorEvent):
        return None
    prepare = getattr(provider, "prepare_retry_after_failure", None)
    if not callable(prepare):
        return None
    try:
        transition = prepare(event)
    except Exception:  # noqa: BLE001 - optional replacement must fail closed
        return None
    if not isinstance(transition, ProviderRetryTransition):
        return None
    source_fingerprint = provider_retry_roster_fingerprint(
        transition.source_plan
    )
    target_fingerprint = provider_retry_roster_fingerprint(
        transition.target_plan
    )
    exclusions = transition.excluded_identities
    source_proposers = transition.source_plan.get("selected_P")
    target_proposers = transition.target_plan.get("selected_P")
    canonical_exclusions = (
        isinstance(exclusions, tuple)
        and bool(exclusions)
        and all(
            _canonical_provider_model_identity(identity) is not None
            for identity in exclusions
        )
        and len(set(exclusions)) == len(exclusions)
    )
    if (
        transition.replacement_provider is None
        or transition.replacement_provider is provider
        or not _string_value(transition.reason)
        or transition.setup_physical_request_count != 0
        or not source_fingerprint
        or not target_fingerprint
        or source_fingerprint == target_fingerprint
        or transition.source_roster_fingerprint != source_fingerprint
        or transition.target_roster_fingerprint != target_fingerprint
        or not canonical_exclusions
        or not isinstance(source_proposers, list)
        or not set(exclusions).issubset(source_proposers)
        or not isinstance(target_proposers, list)
        or bool(set(exclusions).intersection(target_proposers))
    ):
        return None
    return transition


class ProviderFailure(Exception):  # noqa: N818 - public compatibility name
    """Raised / wrapped when a primary provider turn fails.

    The selector passes instances of this exception (or any ``Exception``
    subclass) to ``failover_hook`` so plugin authors can inspect the
    underlying cause and decide which fallback chain to return.
    """


@runtime_checkable
class ProviderPlugin(Protocol):
    """Extension contract for provider-adjacent plugins.

    Plugins may implement any subset of these hooks; ``ModelSelector``
    consults them through ``resolve_failover_chain`` /
    ``resolve_quota_status``, which return the documented defaults when
    no hook is registered.
    """

    def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
        """Return the ordered fallback chain for a primary failure.

        The returned list excludes the primary. An empty list signals
        "no fallback available" and forces the caller to surface the
        original failure to the user.
        """
        ...

    def quota_hook(self, session_id: str) -> QuotaStatus:
        """Return the remaining quota for ``session_id``.

        Unlimited / not-enforced is signaled via the default
        ``QuotaStatus`` (sentinel ``-1`` on both counters, ``None`` abort
        reason). A non-None ``abort_reason`` is surfaced verbatim in the
        user-facing graceful-abort payload.
        """
        ...


def resolve_failover_chain(
    primary_failure: Exception,
    config: SelectorConfig,
    plugin: ProviderPlugin | None = None,
) -> list[ProviderConfig]:
    """Return the fallback chain honoring a plugin ``failover_hook`` if set.

    Default (no plugin, or plugin raising) returns the static
    ``config.fallbacks`` chain declared on ``SelectorConfig``.
    """
    if plugin is not None and hasattr(plugin, "failover_hook"):
        try:
            chain = plugin.failover_hook(primary_failure)
        except Exception:
            chain = None
        if chain is not None:
            return list(chain)
    return list(config.fallbacks)


def resolve_quota_status(
    session_id: str,
    plugin: ProviderPlugin | None = None,
) -> QuotaStatus:
    """Return the quota status honoring a plugin ``quota_hook`` if set.

    Default (no plugin, or plugin raising) returns an unlimited sentinel
    ``QuotaStatus`` with ``abort_reason=None``.
    """
    if plugin is not None and hasattr(plugin, "quota_hook"):
        try:
            status = plugin.quota_hook(session_id)
        except Exception:
            return QuotaStatus()
        if status is not None:
            return status
    return QuotaStatus()
