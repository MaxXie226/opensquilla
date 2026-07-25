#!/usr/bin/env python3
"""Finalize a complete DRACO Mini campaign using only sealed local artifacts.

This command deliberately performs no networking or model-provider calls.  It
validates immutable source shards, replays the pure frozen G1 ranker, selects
one completed result per expected group/task pair, rebuilds whole-campaign
spend from physical request receipts, binds incomplete receipt metadata to a
stable account-level non-BYOK proof, reseals results, rebuilds traces, and
publishes one directory atomically.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import hmac
import json
import math
import os
import random
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

GROUPS = ("B0", "B1", "B2", "B4", "G1")
GROUP_SET = frozenset(GROUPS)
RESULT_EVIDENCE_SCHEMA = "opensquilla.draco.result-evidence/v1"
RESULT_EVIDENCE_SHA256_FIELD = "result_evidence_sha256"
RUNTIME_SCHEMA = "opensquilla.draco-runtime-environment/v1"
RECONCILIATION_SCHEMA = "opensquilla.openrouter-account-reconciliation/v1"
PROOF_SCHEMA = "opensquilla.draco.openrouter-non-byok-campaign-proof/v1"
LEDGER_SCHEMA = "opensquilla.draco.actual-spend-ledger/v1"
AUDIT_SCHEMA = "opensquilla.draco.campaign-final-audit/v1"
MANIFEST_SCHEMA = "opensquilla.draco.campaign-final-manifest/v1"
RESOLUTION_SCHEMA = "opensquilla.draco.openrouter-non-byok-resolution/v1"
GENERATION_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-generation-attempt/v1"
JUDGE_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-judge-attempt/v1"
JUDGE_ATTEMPT_BUDGET_SCOPE = "criterion_repeat_campaign"
JUDGE_ATTEMPT_BUDGET_LIMIT = 3
JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR = "judge_attempt_budget_exhausted"
FINALIZER_VERSION = 2
FROZEN_DRACO_MINI_TASK_COUNT = 10
FROZEN_DRACO_MINI_SHA256 = "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
FORMAL_REQUIRED_STABLE_POLL_COUNT = 6
FORMAL_POLL_INTERVAL_SECONDS = 15
FORMAL_MINIMUM_SETTLEMENT_SECONDS = 180
FORMAL_MINIMUM_STABLE_TAIL_SECONDS = 75
FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256 = (
    "420a338072f865cae99f0bcdf4e34f2345e46884389e5f7241cccdffe913c4b1"
)
FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION = "step2-ranking-config-v3"
FORMAL_G1_RANKING_CONFIG_VERSION = "step2-ranking-2026-07-22.1"
FORMAL_G1_RANKING_CONFIG_SHA256 = "a8addcdefa04349209c20e97ca5851ed0f5ca55646c9d0c5badc5d32dd7ef10c"
FORMAL_G1_PROPOSER_COUNT_MAX = 5
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")

B2_PROPOSERS = (
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "qwen/qwen3.7-max",
)
B2_AGGREGATOR = "z-ai/glm-5.2"
B0_MODEL = "anthropic/claude-opus-4.8"
B4_MODEL = "openai/gpt-5.5"
B1_TEXT_TIER_MODELS = {
    "c0": "deepseek/deepseek-v4-flash",
    "c1": "deepseek/deepseek-v4-pro",
    "c2": "z-ai/glm-5.2",
    "c3": "anthropic/claude-opus-4.8",
}
B1_TIER_MODELS = {
    **B1_TEXT_TIER_MODELS,
    "image_model": "moonshotai/kimi-k2.6",
}
FORMAL_UPSTREAM_PINS = {
    B0_MODEL: "anthropic",
    B4_MODEL: "openai",
    "deepseek/deepseek-v4-flash": "deepseek",
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.6": "moonshotai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    "google/gemini-3.1-pro-preview": "google-ai-studio",
}
JUDGE_MODEL = "google/gemini-3.1-pro-preview"
JUDGE_REPEATS = 3
ALLOWED_NON_GENERATION_ERRORS = frozenset(
    {
        "",
        "cost_metadata_incomplete",
        "judge_incomplete",
        "openrouter_non_byok_metadata_incomplete",
        "openrouter_non_byok_verification_failed",
    }
)
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
POLICY_VIOLATION_ERRORS = frozenset(
    {
        "openrouter_non_byok_policy_violation",
        "openrouter_byok_detected",
    }
)
USAGE_CONTRACT_KEYS = (
    "provider",
    "model",
    "requested_provider",
    "requested_model",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
TRACE_FIELDS = (
    "row_index",
    "task_id",
    "group",
    "domain",
    "runner_mode",
    "tools_enabled",
    "tool_policy",
    "generation_policy",
    "generation_config",
    "routing_trace",
    "started_at",
    "completed_at",
    "prompt_sha256",
    "task_input_sha256",
    "run_compatibility_fingerprint",
    "final_text_sha256",
    "final_text_chars",
    "error",
    "stream_tool_call_count",
    "server_tool_call_count",
    "server_tool_use",
    "total_tool_call_count",
    "trajectory_steps",
    "llm_request_count",
    "generation_attempt_count",
    "generation_max_attempts",
    "generation_retry_backoff_s",
    "generation_attempt_total_billed_cost",
    "generation_retry_reasons",
    "execution",
    "usage",
    "cost_accounting",
    "openrouter_non_byok_audit",
    "run_trace",
    "ensemble_trace",
    "fusion_delta",
)


class FinalizationError(ValueError):
    """A deterministic finalization gate failed."""


@dataclass(frozen=True)
class SourceRecord:
    path: Path
    source_index: int
    line: int
    row: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (
            str(self.row.get("group") or ""),
            str(self.row.get("task_id") or ""),
        )

    @property
    def reference(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source_index": self.source_index,
            "line": self.line,
        }


@dataclass
class LedgerEntry:
    identity: str
    scopes: set[str] = field(default_factory=set)
    units: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    response_ids: set[str] = field(default_factory=set)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any, *, prefix: bool = False) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"sha256:{digest}" if prefix else digest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def required_decimal(value: Any, *, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinalizationError(f"{label} is missing or non-numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinalizationError(f"{label} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise FinalizationError(f"{label} must be finite and non-negative")
    return parsed


def finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def normalize_key_fingerprint(value: Any, *, label: str) -> str:
    fingerprint = str(value or "").strip().casefold()
    if fingerprint.startswith("sha256:"):
        fingerprint = fingerprint[7:]
    if not HEX64.fullmatch(fingerprint):
        raise FinalizationError(f"{label} is not a SHA-256 key fingerprint")
    return fingerprint


def parse_iso(value: Any, *, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise FinalizationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalizationError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise FinalizationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def normalize_groups(value: str) -> tuple[str, ...]:
    groups = tuple(item.strip() for item in value.split(",") if item.strip())
    if groups != GROUPS:
        raise FinalizationError(
            f"formal finalization requires exactly {','.join(GROUPS)} in that order"
        )
    return groups


def require_regular_file(path: Path, *, owner_only: bool = True) -> Path:
    if path.is_symlink() or not path.is_file():
        raise FinalizationError(f"source is not a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    mode = resolved.stat().st_mode & 0o777
    if owner_only and mode & 0o077:
        raise FinalizationError(f"sensitive source is not owner-only: {path}")
    return resolved


def load_json(path: Path, *, owner_only: bool = True) -> dict[str, Any]:
    resolved = require_regular_file(path, owner_only=owner_only)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"invalid JSON source {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"JSON source is not an object: {resolved}")
    return value


def load_jsonl_rows(
    path: Path,
    *,
    owner_only: bool,
    source_label: str,
) -> list[tuple[int, Any]]:
    """Load JSONL records without treating Unicode line separators as row boundaries."""
    resolved = require_regular_file(path, owner_only=owner_only)
    rows: list[tuple[int, Any]] = []
    try:
        with resolved.open("r", encoding="utf-8", newline="\n") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise FinalizationError(
                        f"invalid {source_label} at {resolved}:{line_number}"
                    ) from exc
                rows.append((line_number, value))
    except (OSError, UnicodeError) as exc:
        raise FinalizationError(f"unable to read {source_label} {resolved}: {exc}") from exc
    return rows


def read_tasks(path: Path) -> list[dict[str, Any]]:
    resolved = require_regular_file(path, owner_only=False)
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, value in load_jsonl_rows(
        resolved,
        owner_only=False,
        source_label="benchmark JSONL",
    ):
        if not isinstance(value, dict):
            raise FinalizationError(f"benchmark row is not an object at {resolved}:{line_number}")
        task_id = str(value.get("id") or value.get("task_id") or "").strip()
        prompt = str(value.get("prompt") or value.get("problem") or "").strip()
        if not task_id or not prompt or task_id in seen:
            raise FinalizationError(
                f"benchmark row requires a unique id and prompt at {resolved}:{line_number}"
            )
        seen.add(task_id)
        value["id"] = task_id
        value["prompt"] = prompt
        if "rubric" in value:
            value["rubric"] = parse_maybe_json(value["rubric"])
        elif "answer" in value:
            value["rubric"] = parse_maybe_json(value["answer"])
        tasks.append(value)
    if not tasks:
        raise FinalizationError("benchmark contains no tasks")
    return tasks


def validate_frozen_draco_input(path: Path, tasks: Sequence[Mapping[str, Any]]) -> str:
    digest = file_sha256(path)
    if digest != FROZEN_DRACO_MINI_SHA256:
        raise FinalizationError("DRACO Mini input SHA256 differs from the frozen set")
    if len(tasks) != FROZEN_DRACO_MINI_TASK_COUNT:
        raise FinalizationError(
            f"DRACO Mini input must contain exactly {FROZEN_DRACO_MINI_TASK_COUNT} tasks"
        )
    task_ids = [str(task.get("id") or "") for task in tasks]
    if len(set(task_ids)) != FROZEN_DRACO_MINI_TASK_COUNT:
        raise FinalizationError("DRACO Mini input task IDs are not unique")
    return digest


def result_evidence_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESULT_EVIDENCE_SCHEMA,
        "result": {key: value for key, value in row.items() if key != RESULT_EVIDENCE_SHA256_FIELD},
    }


def verify_result_row_evidence(row: Mapping[str, Any]) -> bool:
    if row.get("result_evidence_schema") != RESULT_EVIDENCE_SCHEMA:
        return False
    actual = row.get(RESULT_EVIDENCE_SHA256_FIELD)
    if not isinstance(actual, str):
        return False
    try:
        expected = canonical_sha256(result_evidence_payload(row), prefix=True)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def seal_result_row(row: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(row))
    sealed["result_evidence_schema"] = RESULT_EVIDENCE_SCHEMA
    sealed.pop(RESULT_EVIDENCE_SHA256_FIELD, None)
    sealed[RESULT_EVIDENCE_SHA256_FIELD] = canonical_sha256(
        result_evidence_payload(sealed), prefix=True
    )
    return sealed


def read_source_rows(paths: Sequence[Path]) -> tuple[list[SourceRecord], dict[str, str]]:
    if not paths:
        raise FinalizationError("at least one --result source is required")
    records: list[SourceRecord] = []
    snapshots: dict[str, str] = {}
    for source_index, raw_path in enumerate(paths):
        path = require_regular_file(raw_path, owner_only=True)
        key = str(path)
        if key in snapshots:
            raise FinalizationError(f"duplicate result source: {path}")
        snapshots[key] = file_sha256(path)
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise FinalizationError(
                        f"invalid result JSONL at {path}:{line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise FinalizationError(f"result row is not an object at {path}:{line_number}")
                if not verify_result_row_evidence(row):
                    raise FinalizationError(
                        f"result row is not sealed or was mutated: {path}:{line_number}"
                    )
                records.append(SourceRecord(path, source_index, line_number, row))
    if not records:
        raise FinalizationError("result sources contain no sealed rows")
    return records, snapshots


def verify_source_snapshots(snapshots: Mapping[str, str]) -> None:
    for raw_path, expected in snapshots.items():
        path = require_regular_file(Path(raw_path), owner_only=False)
        actual = file_sha256(path)
        if not hmac.compare_digest(actual, expected):
            raise FinalizationError(f"source shard changed during finalization: {path}")


def validate_source_policy_history(records: Sequence[SourceRecord]) -> None:
    """Make explicit BYOK/provider failures campaign-fatal across every wave."""

    failures: list[dict[str, Any]] = []
    for record in records:
        row = record.row
        error = str(row.get("error") or "")
        audit = row.get("openrouter_non_byok_audit")
        audit_status = str(audit.get("status") or "") if isinstance(audit, Mapping) else ""
        unsafe = isinstance(audit, Mapping) and audit.get("policy_safe_to_continue") is False
        if (
            error in POLICY_VIOLATION_ERRORS
            or unsafe
            or audit_status
            in {
                "policy_violation",
                "explicit_byok",
                "provider_conflict",
                "receipt_conflict",
            }
        ):
            failures.append(
                record.reference
                | {
                    "group": record.key[0],
                    "task_id": record.key[1],
                    "error": error,
                    "audit_status": audit_status,
                }
            )
    if failures:
        raise FinalizationError(
            f"campaign source history contains explicit BYOK/provider failures: {failures[:5]}"
        )


def load_manifest_contracts(
    paths: Sequence[Path],
    *,
    result_paths: Sequence[Path],
    groups: Sequence[str],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], str, list[dict[str, Any]]]:
    if not paths:
        raise FinalizationError("at least one --manifest source is required")
    if len(paths) != len(result_paths):
        raise FinalizationError(
            "each result shard must have exactly one manifest in matching order"
        )
    allowed_statuses = {
        "complete",
        "metadata_incomplete",
        "judge_incomplete",
        "result_incomplete",
        "resume_repair_incomplete",
    }
    forbidden_failure_markers = {
        "openrouter_non_byok_policy_violation",
        "openrouter_byok_detected",
        "cost_audit_failed",
        "preflight_failed",
    }
    authoritative_fingerprints: dict[str, str] | None = None
    authoritative_contracts: dict[str, dict[str, Any]] | None = None
    key_fingerprint = ""
    source_evidence: list[dict[str, Any]] = []
    prior_manifest_attempt_ids: set[str] = set()
    for raw_path, raw_result_path in zip(paths, result_paths, strict=True):
        path = require_regular_file(raw_path, owner_only=True)
        result_path = require_regular_file(raw_result_path, owner_only=True)
        payload = load_json(path)
        status = str(payload.get("status") or "")
        if status not in allowed_statuses:
            raise FinalizationError(
                f"source manifest status is not allowed for finalization: {status!r} at {path}"
            )
        failure_text = json.dumps(payload.get("failure"), ensure_ascii=False, sort_keys=True)
        if any(marker in failure_text for marker in forbidden_failure_markers):
            raise FinalizationError(f"source manifest contains a fatal policy/cost failure: {path}")
        tool_policy = payload.get("tool_policy")
        local_tools = (
            tool_policy.get("local_web_tools") if isinstance(tool_policy, Mapping) else None
        )
        preflight = local_tools.get("preflight") if isinstance(local_tools, Mapping) else None
        preflight_calls = (
            preflight.get("preflight_calls") if isinstance(preflight, Mapping) else None
        )
        preflight_status = (
            str(preflight.get("status") or "") if isinstance(preflight, Mapping) else ""
        )
        if (
            preflight_status not in {"passed", "skipped_not_required"}
            or not isinstance(preflight_calls, Mapping)
            or any(
                isinstance(preflight_calls.get(tool_name), bool)
                or not isinstance(preflight_calls.get(tool_name), int)
                or int(preflight_calls[tool_name]) < 0
                for tool_name in ("web_search", "web_fetch")
            )
        ):
            raise FinalizationError(
                f"live source manifest lacks a passed Web preflight ledger: {path}"
            )
        artifacts = payload.get("artifacts")
        declared_result = artifacts.get("results_jsonl") if isinstance(artifacts, Mapping) else None
        if (
            not isinstance(declared_result, str)
            or Path(declared_result).resolve(strict=False) != result_path
        ):
            raise FinalizationError(
                f"manifest results_jsonl is not bound to its result shard: {path}"
            )
        result_rows = [
            value
            for _, value in load_jsonl_rows(
                result_path,
                owner_only=True,
                source_label="result JSONL",
            )
        ]
        shard_attempt_ids = {
            str(attempt.get("attempt_id") or "")
            for row in result_rows
            if isinstance(row, Mapping)
            for attempt in (
                row.get("execution", {}).get("generation_attempts", [])
                if isinstance(row.get("execution"), Mapping)
                else []
            )
            if isinstance(attempt, Mapping)
            and HEX32.fullmatch(str(attempt.get("attempt_id") or ""))
        }
        new_attempt_ids = shard_attempt_ids - prior_manifest_attempt_ids
        if preflight_status == "skipped_not_required":
            resume_selection = payload.get("resume_selection")
            if (
                any(
                    int(preflight_calls[tool_name]) != 0
                    for tool_name in (
                        "web_search",
                        "web_fetch",
                    )
                )
                or not isinstance(resume_selection, Mapping)
                or resume_selection.get("model_regenerate_pair_count") != 0
                or bool(new_attempt_ids)
                or not prior_manifest_attempt_ids
                or any(
                    not isinstance(row.get("execution"), Mapping)
                    or row["execution"].get("generation_reused") is not True
                    or str(row["execution"].get("resume_action") or "")
                    not in {"judge_only", "metadata_only"}
                    or (
                        str(row["execution"].get("resume_action") or "") == "metadata_only"
                        and row["execution"].get("judge_reran") is True
                    )
                    for row in result_rows
                    if isinstance(row, Mapping)
                )
            ):
                raise FinalizationError(
                    f"skipped Web preflight is not bound to a no-generation repair shard: {path}"
                )
        prior_manifest_attempt_ids.update(shard_attempt_ids)
        if nonnegative_int(payload.get("rows_written")) != len(result_rows):
            raise FinalizationError(f"manifest rows_written differs from its result shard: {path}")
        manifest_groups = payload.get("groups")
        manifest_task_ids = payload.get("task_ids")
        if (
            not isinstance(manifest_groups, list)
            or not isinstance(manifest_task_ids, list)
            or any(
                str(row.get("group") or "") not in manifest_groups
                or str(row.get("task_id") or "") not in manifest_task_ids
                for row in result_rows
                if isinstance(row, Mapping)
            )
        ):
            raise FinalizationError(
                f"manifest groups/task_ids do not cover its result shard: {path}"
            )
        compatibility = payload.get("run_compatibility")
        if not isinstance(compatibility, dict):
            raise FinalizationError(f"manifest lacks run compatibility: {path}")
        raw_fingerprints = compatibility.get("fingerprints")
        raw_contracts = compatibility.get("contracts")
        if not isinstance(raw_fingerprints, dict) or not isinstance(raw_contracts, dict):
            raise FinalizationError(f"manifest compatibility contract is incomplete: {path}")
        fingerprints: dict[str, str] = {}
        contracts: dict[str, dict[str, Any]] = {}
        for group in groups:
            fingerprint = str(raw_fingerprints.get(group) or "")
            contract = raw_contracts.get(group)
            if not SHA256_VALUE.fullmatch(fingerprint) or not isinstance(contract, dict):
                raise FinalizationError(
                    f"manifest lacks the {group} compatibility contract: {path}"
                )
            if canonical_sha256(contract, prefix=True) != fingerprint:
                raise FinalizationError(
                    f"manifest {group} compatibility fingerprint differs: {path}"
                )
            fingerprints[group] = fingerprint
            contracts[group] = contract
            runtime = contract.get("resolved_llm_runtime")
            candidate_key = (
                normalize_key_fingerprint(
                    runtime.get("api_key_sha256"),
                    label=f"{path} {group} runtime key",
                )
                if isinstance(runtime, dict)
                else ""
            )
            if not candidate_key:
                raise FinalizationError(f"manifest lacks a runtime key binding: {path}")
            if key_fingerprint and candidate_key != key_fingerprint:
                raise FinalizationError("source manifests use different OpenRouter keys")
            key_fingerprint = candidate_key
        if authoritative_fingerprints is None:
            authoritative_fingerprints = fingerprints
            authoritative_contracts = contracts
        elif authoritative_fingerprints != fingerprints or authoritative_contracts != contracts:
            raise FinalizationError("source manifests use different run contracts")
        source_evidence.append(
            {
                "path": str(path),
                "sha256": file_sha256(path),
                "status": status,
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at"),
                "result_path": str(result_path),
                "result_sha256": file_sha256(result_path),
                "rows_written": len(result_rows),
                "live_web_preflight": {
                    "status": preflight_status,
                    "preflight_calls": {
                        tool_name: int(preflight_calls[tool_name])
                        for tool_name in ("web_search", "web_fetch")
                    },
                },
            }
        )
    assert authoritative_fingerprints is not None
    assert authoritative_contracts is not None
    return (
        authoritative_fingerprints,
        authoritative_contracts,
        key_fingerprint,
        source_evidence,
    )


def validate_formal_campaign_contracts(
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Pin the formal group definitions independently of source manifests."""

    for group, expected_model in (("B0", B0_MODEL), ("B4", B4_MODEL)):
        contract = contracts.get(group)
        spec = contract.get("group_spec") if isinstance(contract, Mapping) else None
        if (
            not isinstance(spec, Mapping)
            or spec.get("kind") != "single"
            or spec.get("model") != expected_model
        ):
            raise FinalizationError(f"{group} formal contract must use openrouter/{expected_model}")
    b1 = contracts.get("B1")
    b1_spec = b1.get("group_spec") if isinstance(b1, Mapping) else None
    gateway = b1.get("gateway_execution") if isinstance(b1, Mapping) else None
    router = gateway.get("squilla_router") if isinstance(gateway, Mapping) else None
    tiers = router.get("tiers") if isinstance(router, Mapping) else None
    if not isinstance(b1_spec, Mapping) or b1_spec.get("kind") != "router_single":
        raise FinalizationError("B1 formal contract must be router_single")
    if not isinstance(tiers, Mapping) or set(tiers) != set(B1_TIER_MODELS):
        raise FinalizationError(
            "B1 formal tier set differs from c0/c1/c2/c3/image_model"
        )
    for tier, expected_model in B1_TIER_MODELS.items():
        value = tiers.get(tier)
        if (
            not isinstance(value, Mapping)
            or str(value.get("provider") or "").casefold() != "openrouter"
            or value.get("model") != expected_model
        ):
            raise FinalizationError(f"B1 {tier} must use openrouter/{expected_model}")
    b2 = contracts.get("B2")
    b2_spec = b2.get("group_spec") if isinstance(b2, Mapping) else None
    if (
        not isinstance(b2_spec, Mapping)
        or b2_spec.get("kind") != "selection_mode"
        or b2_spec.get("selection_mode") != "static_openrouter_b5"
    ):
        raise FinalizationError("B2 formal contract must use static_openrouter_b5")
    g1 = contracts.get("G1")
    g1_spec = g1.get("group_spec") if isinstance(g1, Mapping) else None
    registry = g1.get("g1_registry_contract") if isinstance(g1, Mapping) else None
    routes = registry.get("expected_routes") if isinstance(registry, Mapping) else None
    if (
        not isinstance(g1_spec, Mapping)
        or g1_spec.get("kind") != "selection_mode"
        or g1_spec.get("selection_mode") != "router_dynamic"
        or not isinstance(routes, Mapping)
        or not routes
        or nonnegative_int(registry.get("expected_candidate_count")) != len(routes)
        or not str(registry.get("profile_id") or "").strip()
        or not str(registry.get("source_registry_snapshot_version") or "").strip()
        or not HEX64.fullmatch(str(registry.get("expected_routes_sha256") or ""))
        or canonical_sha256(routes) != str(registry.get("expected_routes_sha256") or "")
        or registry.get("expected_source_registry_snapshot_sha256")
        != FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        or registry.get("expected_ranking_config_schema_version")
        != FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
        or registry.get("expected_ranking_config_version") != FORMAL_G1_RANKING_CONFIG_VERSION
        or registry.get("expected_ranking_config_sha256") != FORMAL_G1_RANKING_CONFIG_SHA256
        or registry.get("expected_proposer_count_max") != FORMAL_G1_PROPOSER_COUNT_MAX
        or registry.get("user_profile_enabled") is not False
    ):
        raise FinalizationError("G1 formal contract must use router_dynamic with frozen routes")
    for group, contract in contracts.items():
        runtime = contract.get("resolved_llm_runtime")
        pins = runtime.get("provider_routing") if isinstance(runtime, Mapping) else None
        if not isinstance(pins, Mapping):
            raise FinalizationError(f"{group} formal contract lacks provider_routing pins")
        required_models = (
            {B0_MODEL}
            if group == "B0"
            else {B4_MODEL}
            if group == "B4"
            else set(B1_TIER_MODELS.values())
            if group == "B1"
            else {*B2_PROPOSERS, B2_AGGREGATOR, B0_MODEL}
            if group == "B2"
            else {*routes, B0_MODEL}
        )
        for model in required_models:
            expected_pin = (
                str(routes[model])
                if group == "G1" and model in routes
                else FORMAL_UPSTREAM_PINS.get(model)
            )
            if not expected_pin or str(pins.get(model) or "").strip().casefold() != expected_pin:
                raise FinalizationError(f"{group} upstream provider pin differs for {model}")


def usage_generation_contract(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    contract = {key: value.get(key) for key in USAGE_CONTRACT_KEYS if key in value}
    breakdown = value.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        contract["model_usage_breakdown"] = [
            usage_generation_contract(item) for item in breakdown if isinstance(item, Mapping)
        ]
    return contract


def usage_generation_identity_contract(value: Any) -> Any:
    """Project only immutable generation data used to link repair waves.

    Requested/actual provider identity and cost receipt fields are deliberately
    excluded: a metadata-only repair is allowed to backfill those fields
    without becoming a new generation.  Token counts and breakdown shape stay
    bound to the accepted generation.
    """

    if not isinstance(value, Mapping):
        return None
    token_keys = USAGE_CONTRACT_KEYS[4:]
    contract = {key: value.get(key) for key in token_keys if key in value}
    breakdown = value.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        contract["model_usage_breakdown"] = [
            usage_generation_identity_contract(item)
            for item in breakdown
            if isinstance(item, Mapping)
        ]
    return contract


def generation_identity(row: Mapping[str, Any]) -> str:
    # Formal repair rows may either carry no attempt items (the current
    # contract) or repeat immutable prior items (legacy-compatible input).
    # Therefore attempt-list shape cannot be part of the cross-wave identity.
    # Physical attempts are independently authenticated and budgeted by
    # validate_generation_attempt_evidence().
    return canonical_sha256(
        {
            "group": row.get("group"),
            "task_id": row.get("task_id"),
            "prompt_sha256": row.get("prompt_sha256"),
            "started_at": row.get("started_at"),
            "generation_completed_at": row.get("generation_completed_at"),
            "final_text_sha256": row.get("final_text_sha256"),
            "llm_request_count": row.get("llm_request_count"),
            "usage": usage_generation_identity_contract(row.get("usage")),
        }
    )


def repair_evidence(row: Mapping[str, Any], execution: Mapping[str, Any]) -> bool:
    """Return whether a no-new-generation row is explicitly a repair."""

    if execution.get("generation_reused") is not True:
        return False
    action = str(execution.get("resume_action") or "")
    if action not in {"judge_only", "metadata_only"}:
        return False
    completion = row.get("resume_completion")
    if isinstance(completion, Mapping):
        if completion.get("generation_reused") is not True:
            return False
        completion_action = str(completion.get("action") or "")
        if completion_action and completion_action != action:
            return False
    # The action itself records the reason a repair wave was emitted.  The
    # booleans describe its outcome and may legitimately both be false when a
    # repair was attempted but could not fill the missing metadata.
    return True


def immutable_attempt_payload(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Project attempt fields a later receipt repair may not change."""

    run = attempt.get("run")
    immutable_run = (
        {
            "error": run.get("error"),
            "final_text_sha256": run.get("final_text_sha256"),
            "llm_request_count": run.get("llm_request_count"),
            "usage": usage_generation_identity_contract(run.get("usage")),
        }
        if isinstance(run, Mapping)
        else None
    )
    return {
        "attempt_id": attempt.get("attempt_id"),
        "attempt_kind": attempt.get("attempt_kind"),
        "attempt": attempt.get("attempt"),
        "started_at": attempt.get("started_at"),
        "completed_at": attempt.get("completed_at"),
        "retryable": attempt.get("retryable"),
        "retry_reason": attempt.get("retry_reason"),
        "retry_suppressed_reason": attempt.get("retry_suppressed_reason"),
        "will_retry": attempt.get("will_retry"),
        "retry_backoff_s": attempt.get("retry_backoff_s"),
        "run": immutable_run,
    }


def validate_generation_attempt_evidence(
    records: Sequence[SourceRecord],
    *,
    max_attempts: int,
) -> dict[str, Any]:
    """Validate immutable, cumulative v1 attempt declarations across waves."""

    seen_payloads: dict[str, str] = {}
    attempt_owner: dict[str, tuple[str, str]] = {}
    state: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "seen_ids": set(),
            "attempt_ordinals": {},
            "budget_used": 0,
            "row_count": 0,
            "repair_row_count": 0,
        }
    )
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        row = record.row
        if row.get("generation_attempt_evidence_schema") != (GENERATION_ATTEMPT_EVIDENCE_SCHEMA):
            raise FinalizationError(
                "formal campaign contains legacy or missing generation attempt "
                f"evidence at {record.path}:{record.line}"
            )
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        if not isinstance(attempts, list):
            raise FinalizationError(
                f"formal result lacks a generation attempt list: {record.path}:{record.line}"
            )
        declared_count = nonnegative_int(row.get("generation_attempt_count"))
        if declared_count != len(attempts):
            raise FinalizationError(
                "generation_attempt_count differs from v1 attempt evidence at "
                f"{record.path}:{record.line}"
            )
        actual_spend_metrics = row.get("actual_spend_metrics")
        if not isinstance(actual_spend_metrics, Mapping) or nonnegative_int(
            actual_spend_metrics.get("generation_attempt_count")
        ) != len(attempts):
            raise FinalizationError(
                "actual-spend generation attempt count differs from v1 evidence "
                f"at {record.path}:{record.line}"
            )
        if nonnegative_int(row.get("generation_attempt_budget_limit")) != max_attempts:
            raise FinalizationError(
                f"generation attempt budget limit differs at {record.path}:{record.line}"
            )
        key_state = state[record.key]
        new_ids: list[str] = []
        row_ids: set[str] = set()
        prior_budget = nonnegative_int(
            execution.get("prior_generation_attempts_used") if isinstance(execution, Mapping) else 0
        )
        declared_budget = nonnegative_int(row.get("generation_attempt_budget_used"))
        if prior_budget != key_state["budget_used"]:
            raise FinalizationError(
                f"{record.key} prior generation attempt declaration is non-monotonic"
            )
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise FinalizationError("generation attempt evidence is not an object")
            attempt_id = str(attempt.get("attempt_id") or "")
            if not HEX32.fullmatch(attempt_id) or attempt_id in row_ids:
                raise FinalizationError("generation attempt id is invalid or duplicated")
            row_ids.add(attempt_id)
            attempt_ordinal = nonnegative_int(attempt.get("attempt"))
            if not 1 <= attempt_ordinal <= max_attempts:
                raise FinalizationError("generation attempt ordinal is invalid")
            if attempt.get("attempt_kind") not in {
                "generation",
                "provider_build_after_paid_setup",
            }:
                raise FinalizationError("generation attempt kind is unsupported")
            started = attempt.get("started_at")
            completed = attempt.get("completed_at")
            if (
                not finite_number(started)
                or not finite_number(completed)
                or float(completed) < float(started)
            ):
                raise FinalizationError("generation attempt timestamps are invalid")
            run = attempt.get("run")
            if not isinstance(run, Mapping):
                raise FinalizationError("generation attempt lacks a run payload")
            if attempt.get("attempt_kind") == "provider_build_after_paid_setup" and (
                run_expected_request_count(run) < 1 or not usage_units(run.get("usage"))
            ):
                raise FinalizationError("paid provider-build attempt lacks physical setup evidence")
            payload_sha = canonical_sha256(immutable_attempt_payload(attempt))
            prior_sha = seen_payloads.get(attempt_id)
            if prior_sha is not None and prior_sha != payload_sha:
                raise FinalizationError(
                    f"generation attempt id {attempt_id} has conflicting payloads"
                )
            owner = attempt_owner.get(attempt_id)
            if owner is not None and owner != record.key:
                raise FinalizationError(
                    f"generation attempt id {attempt_id} is reused by another pair"
                )
            seen_payloads[attempt_id] = payload_sha
            attempt_owner[attempt_id] = record.key
            if attempt_id not in key_state["seen_ids"]:
                expected_ordinal = prior_budget + len(new_ids) + 1
                if attempt_ordinal != expected_ordinal:
                    raise FinalizationError(
                        "new generation attempt ordinal is not contiguous with "
                        "the cumulative cross-wave budget"
                    )
                new_ids.append(attempt_id)
                key_state["attempt_ordinals"][attempt_id] = attempt_ordinal
            elif key_state["attempt_ordinals"].get(attempt_id) != attempt_ordinal:
                raise FinalizationError(f"generation attempt id {attempt_id} changed ordinal")
        expected_budget = key_state["budget_used"] + len(new_ids)
        if declared_budget != expected_budget or declared_budget > max_attempts:
            raise FinalizationError(
                f"{record.key} cumulative generation attempt declaration differs "
                f"or exceeds {max_attempts}"
            )
        if not new_ids:
            if not key_state["seen_ids"] or not repair_evidence(row, execution):
                raise FinalizationError(
                    f"{record.key} no-new-attempt row lacks explicit repair evidence"
                )
            key_state["repair_row_count"] += 1
        elif len(new_ids) != len(attempts):
            raise FinalizationError(
                f"{record.key} generation row mixes prior and new attempt evidence"
            )
        elif execution.get("generation_reused") is True:
            raise FinalizationError(
                f"{record.key} new attempt row incorrectly claims generation reuse"
            )
        key_state["seen_ids"].update(new_ids)
        key_state["budget_used"] = declared_budget
        key_state["row_count"] += 1
    return {
        f"{key[0]}/{key[1]}": {
            "generation_attempt_budget_used": value["budget_used"],
            "unique_attempt_count": len(value["seen_ids"]),
            "source_row_count": value["row_count"],
            "repair_row_count": value["repair_row_count"],
        }
        for key, value in sorted(state.items())
    }


def generation_sort_key(record: SourceRecord) -> tuple[int, float, int, int]:
    row = record.row
    raw = row.get("generation_completed_at")
    if not finite_number(raw):
        raw = row.get("completed_at")
    if not finite_number(raw):
        raw = row.get("started_at")
    timestamp = float(raw) if finite_number(raw) else 0.0
    return (
        int(timestamp > 0.0),
        timestamp,
        record.source_index,
        record.line,
    )


def generation_attempt_count(row: Mapping[str, Any]) -> int:
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    count = max(nonnegative_int(row.get("generation_attempt_count")), len(attempts))
    if count == 0 and str(row.get("final_text") or "").strip():
        count = 1
    return count


def usage_units(usage: Any) -> list[dict[str, Any]]:
    if not isinstance(usage, Mapping) or not usage:
        return []
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list) and breakdown:
        return [dict(item) for item in breakdown if isinstance(item, Mapping)]
    return [dict(usage)]


def selected_usage_models(row: Mapping[str, Any]) -> set[str]:
    return {
        str(unit.get("model") or "").strip()
        for unit in usage_units(row.get("usage"))
        if str(unit.get("model") or "").strip()
    }


def contract_provider_pins(contract: Mapping[str, Any]) -> dict[str, str]:
    runtime = contract.get("resolved_llm_runtime")
    raw = runtime.get("provider_routing") if isinstance(runtime, Mapping) else None
    return (
        {str(model): str(provider).strip().casefold() for model, provider in raw.items()}
        if isinstance(raw, Mapping)
        else {}
    )


def _is_unknown_task_analyzer_placeholder(unit: Mapping[str, Any]) -> bool:
    provider_usage = unit.get("provider_usage")
    return (
        str(unit.get("role") or "").strip().casefold() == "unknown_request"
        and str(unit.get("label") or "").strip().casefold() == "task_analyzer"
        and str(unit.get("provider") or "").strip() == ""
        and str(unit.get("model") or "").strip() == ""
        and str(unit.get("requested_provider") or "").strip().casefold() == "openrouter"
        and str(unit.get("requested_model") or "").strip() == B0_MODEL
        and isinstance(unit.get("attempt"), int)
        and not isinstance(unit.get("attempt"), bool)
        and int(unit["attempt"]) >= 1
        and HEX32.fullmatch(str(unit.get("physical_attempt_id") or "")) is not None
        and nonnegative_int(unit.get("input_tokens")) == 0
        and nonnegative_int(unit.get("output_tokens")) == 0
        and nonnegative_int(unit.get("reasoning_tokens")) == 0
        and nonnegative_int(unit.get("cached_tokens")) == 0
        and nonnegative_int(unit.get("cache_write_tokens")) == 0
        and finite_number(unit.get("billed_cost"))
        and float(unit["billed_cost"]) == 0.0
        and str(unit.get("cost_source") or "none").casefold() in {"none", "unavailable"}
        and isinstance(provider_usage, Mapping)
        and provider_usage.get("usage_unknown") is True
        and provider_usage.get("physical_attempt_id") == unit.get("physical_attempt_id")
    )


def usage_route_reasons(
    usage: Any,
    *,
    allowed_models: set[str],
    provider_pins: Mapping[str, str] | None = None,
    role_model_pins: Mapping[str, str] | None = None,
    allow_unknown_task_analyzer_attempts: bool = False,
) -> list[str]:
    reasons: list[str] = []
    units = usage_units(usage)
    if not units:
        return ["missing_generation_usage_route_evidence"]
    represented = 0
    for unit in units:
        role = str(unit.get("role") or "").strip().casefold()
        label = str(unit.get("label") or "").strip().casefold()
        route_role = (
            label
            if role in MISSING_USAGE_PLACEHOLDER_ROLES
            and role_model_pins is not None
            and label in role_model_pins
            else role
        )
        effective_allowed_models = (
            {str(role_model_pins[route_role])}
            if role_model_pins is not None and route_role in role_model_pins
            else allowed_models
        )
        if role in MISSING_USAGE_PLACEHOLDER_ROLES:
            unknown_analyzer_allowed = (
                allow_unknown_task_analyzer_attempts
                and _is_unknown_task_analyzer_placeholder(unit)
            )
            provider = str(unit.get("provider") or "").strip().casefold()
            requested_provider = str(
                unit.get("requested_provider") or ""
            ).strip().casefold()
            model = str(unit.get("model") or "").strip()
            requested_model = str(unit.get("requested_model") or "").strip()
            provider_usage = unit.get("provider_usage")
            nested_requested_provider = (
                str(provider_usage.get("requested_provider") or "").strip().casefold()
                if isinstance(provider_usage, Mapping)
                else ""
            )
            nested_requested_model = (
                str(provider_usage.get("requested_model") or "").strip()
                if isinstance(provider_usage, Mapping)
                else ""
            )
            router_metadata = (
                provider_usage.get("router_metadata")
                if isinstance(provider_usage, Mapping)
                else None
            )
            router_requested_provider = (
                str(router_metadata.get("requested_provider") or "").strip().casefold()
                if isinstance(router_metadata, Mapping)
                else ""
            )
            router_requested_model = (
                str(router_metadata.get("requested") or "").strip()
                if isinstance(router_metadata, Mapping)
                else ""
            )
            known_providers = {
                value
                for value in (
                    provider,
                    requested_provider,
                    nested_requested_provider,
                    router_requested_provider,
                )
                if value
            }
            known_models = [
                value
                for value in (
                    model,
                    requested_model,
                    nested_requested_model,
                    router_requested_model,
                )
                if value
            ]
            if any(value != "openrouter" for value in known_providers):
                reasons.append("wrong_generation_provider_route")
            model_outside_contract = any(
                not any(
                    _formal_openrouter_models_equivalent(value, allowed_model)
                    for allowed_model in effective_allowed_models
                )
                for value in known_models
            )
            conflicting_known_models = bool(known_models) and any(
                not _formal_openrouter_models_equivalent(known_models[0], value)
                for value in known_models[1:]
            )
            if model_outside_contract or conflicting_known_models:
                reasons.append("wrong_generation_model_route")
            successful = _successful_router_bindings(unit)
            known_model = known_models[0] if known_models else ""
            if successful and (
                not known_model
                or any(
                    any(
                        not _formal_openrouter_models_equivalent(
                            upstream_model,
                            known_value,
                        )
                        for known_value in known_models
                    )
                    for _, upstream_model in successful
                )
            ):
                reasons.append("conflicting_successful_router_receipt")
            if provider_pins is not None and known_model:
                pin_model = next(
                    (
                        allowed_model
                        for allowed_model in effective_allowed_models
                        if _formal_openrouter_models_equivalent(
                            known_model,
                            allowed_model,
                        )
                    ),
                    "",
                )
                expected_pin = str(
                    provider_pins.get(pin_model) or ""
                ).strip().casefold()
                if not expected_pin:
                    reasons.append("missing_formal_upstream_provider_pin")
                elif successful and any(
                    upstream_provider
                    != _normalize_openrouter_provider_identity(expected_pin)
                    for upstream_provider, _ in successful
                ):
                    reasons.append("router_receipt_provider_not_bound_to_formal_route")
            if unknown_analyzer_allowed:
                represented += 1
            # The runner emits an explicit placeholder for a physical request whose
            # response usage could not be recovered.  Preserve it for account-level
            # reconciliation.  Missing fields remain unknown, while every field and
            # router binding that is present must still satisfy the frozen route.
            continue
        represented += 1
        provider = str(unit.get("provider") or "").strip().casefold()
        requested_provider = str(unit.get("requested_provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        requested_model = str(unit.get("requested_model") or "").strip()
        if provider != "openrouter" or requested_provider != "openrouter":
            reasons.append("wrong_generation_provider_route")
        if (
            not model
            or model not in effective_allowed_models
            or not requested_model
            or requested_model not in effective_allowed_models
            or not _formal_openrouter_models_equivalent(model, requested_model)
        ):
            reasons.append("wrong_generation_model_route")
        provider_usage = unit.get("provider_usage")
        nested_requested_provider = (
            str(provider_usage.get("requested_provider") or "").strip().casefold()
            if isinstance(provider_usage, Mapping)
            else ""
        )
        nested_requested_model = (
            str(provider_usage.get("requested_model") or "").strip()
            if isinstance(provider_usage, Mapping)
            else ""
        )
        router_metadata = (
            provider_usage.get("router_metadata") if isinstance(provider_usage, Mapping) else None
        )
        successful = _successful_router_bindings(unit)
        model_bound = {
            (upstream_provider, upstream_model)
            for upstream_provider, upstream_model in successful
            if _formal_openrouter_models_equivalent(upstream_model, model)
            and _formal_openrouter_models_equivalent(upstream_model, requested_model)
        }
        router_requested = (
            str(router_metadata.get("requested") or "").strip()
            if isinstance(router_metadata, Mapping)
            else ""
        )
        router_requested_provider = (
            str(router_metadata.get("requested_provider") or "").strip().casefold()
            if isinstance(router_metadata, Mapping)
            else ""
        )
        if any(
            value and value != "openrouter"
            for value in (nested_requested_provider, router_requested_provider)
        ):
            reasons.append("wrong_generation_provider_route")
        if nested_requested_model and not _formal_openrouter_models_equivalent(
            nested_requested_model,
            requested_model,
        ):
            reasons.append("wrong_generation_model_route")
        if not isinstance(router_metadata, Mapping) or not successful:
            reasons.append("missing_successful_router_receipt")
        elif not model_bound:
            reasons.append("router_receipt_model_not_bound_to_formal_route")
        elif model_bound != successful:
            reasons.append("conflicting_successful_router_receipt")
        if not _formal_openrouter_models_equivalent(router_requested, requested_model):
            reasons.append("router_receipt_request_not_bound_to_formal_route")
        expected_pin = (
            str(provider_pins.get(requested_model) or "").strip().casefold()
            if provider_pins is not None
            else ""
        )
        if provider_pins is not None and not expected_pin:
            reasons.append("missing_formal_upstream_provider_pin")
        elif expected_pin and any(
            upstream_provider != _normalize_openrouter_provider_identity(expected_pin)
            for upstream_provider, _ in successful
        ):
            reasons.append("router_receipt_provider_not_bound_to_formal_route")
    if represented <= 0:
        reasons.append("missing_generation_usage_route_evidence")
    return list(dict.fromkeys(reasons))


def _unit_present_value(unit: Mapping[str, Any], field: str) -> Any | None:
    value = unit.get(field)
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if field in {"provider", "requested_provider"}:
        return str(value).strip().casefold()
    if field in {"model", "requested_model", "role"}:
        return str(value).strip()
    return value


def _normalize_openrouter_provider_identity(value: Any) -> str:
    """Normalize OpenRouter provider slugs and display names for comparison."""
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


@cache
def _formal_openrouter_model_aliases() -> dict[str, frozenset[str]]:
    """Bind requested model ids to the frozen registry's serving-model aliases."""
    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    aliases: dict[str, set[str]] = {
        str(model).strip().casefold(): {str(model).strip().casefold()}
        for model in FORMAL_UPSTREAM_PINS
        if str(model).strip()
    }
    snapshot = load_model_registry_snapshot()
    if canonical_sha256(snapshot) != FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256:
        raise FinalizationError("formal OpenRouter model registry snapshot changed")
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        raise FinalizationError("formal OpenRouter model registry snapshot is malformed")
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        model = str(facts.get("model_id") or "").strip().casefold()
        version = str(facts.get("version") or "").strip().casefold()
        provider = str(facts.get("provider") or "").strip().casefold()
        if not model or provider != "openrouter":
            continue
        aliases.setdefault(model, {model})
        if version:
            aliases[model].add(version)
    return {model: frozenset(values) for model, values in aliases.items()}


def _formal_openrouter_models_equivalent(left: Any, right: Any) -> bool:
    """Treat a frozen requested model and its serving version as one identity."""
    left_model = str(left or "").strip().casefold()
    right_model = str(right or "").strip().casefold()
    if not left_model or not right_model:
        return False
    if left_model == right_model:
        return True
    for requested_model, aliases in _formal_openrouter_model_aliases().items():
        equivalence_class = {requested_model, *aliases}
        if left_model in equivalence_class and right_model in equivalence_class:
            return True
    return False


def _successful_router_bindings(unit: Mapping[str, Any]) -> set[tuple[str, str]]:
    provider_usage = unit.get("provider_usage")
    router_metadata = (
        provider_usage.get("router_metadata") if isinstance(provider_usage, Mapping) else None
    )
    attempts = router_metadata.get("attempts") if isinstance(router_metadata, Mapping) else None
    bindings = {
        (
            _normalize_openrouter_provider_identity(attempt.get("provider")),
            str(attempt.get("model") or "").strip(),
        )
        for attempt in attempts or []
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("status"), int)
        and not isinstance(attempt.get("status"), bool)
        and 200 <= int(attempt["status"]) < 300
        and str(attempt.get("provider") or "").strip()
        and str(attempt.get("model") or "").strip()
    }
    endpoints = router_metadata.get("endpoints") if isinstance(router_metadata, Mapping) else None
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    bindings.update(
        (
            _normalize_openrouter_provider_identity(endpoint.get("provider")),
            str(endpoint.get("model") or "").strip(),
        )
        for endpoint in available or []
        if isinstance(endpoint, Mapping)
        and endpoint.get("selected") is True
        and str(endpoint.get("provider") or "").strip()
        and str(endpoint.get("model") or "").strip()
    )
    return bindings


def run_receipt_enrichment(
    run: Any,
    *,
    source_index: int,
    line: int,
) -> tuple[int, int, int]:
    units = usage_units(run.get("usage")) if isinstance(run, Mapping) else []
    score = 0
    for unit in units:
        score += len(response_ids(unit)) * 20
        score += sum(
            bool(_unit_present_value(unit, field)) * weight
            for field, weight in (
                ("provider", 2),
                ("model", 2),
                ("requested_provider", 2),
                ("requested_model", 2),
            )
        )
        score += int(unit.get("billed_cost") is not None) * 5
        provider_usage = unit.get("provider_usage")
        if isinstance(provider_usage, Mapping):
            score += int(provider_usage.get("provider_reported_cost") is not None) * 8
            score += int(provider_usage.get("is_byok") in {True, False}) * 8
            router = provider_usage.get("router_metadata")
            if isinstance(router, Mapping):
                score += int(router.get("is_byok") in {True, False}) * 8
                score += int(router_provider_metadata_complete(router)) * 8
    return score, source_index, line


def validate_and_select_monotonic_run_version(
    versions: Sequence[tuple[SourceRecord, Mapping[str, Any]]],
    *,
    label: str,
) -> tuple[SourceRecord, Mapping[str, Any]]:
    """Allow receipt backfill only; reject conflicting or regressive copies."""

    if not versions:
        raise FinalizationError(f"{label} has no physical run versions")
    ordered = sorted(
        versions,
        key=lambda value: (value[0].source_index, value[0].line),
    )
    expected_request_count: int | None = None
    prior_units: list[dict[str, Any]] | None = None
    exact_costs_by_unit: dict[int, set[Decimal]] = defaultdict(set)
    for record, run in ordered:
        request_count = run_expected_request_count(run)
        if expected_request_count is None:
            expected_request_count = request_count
        elif request_count != expected_request_count:
            raise FinalizationError(
                f"{label} changed its physical request count across receipt repairs"
            )
        units = usage_units(run.get("usage"))
        if len(units) != expected_request_count:
            raise FinalizationError(f"{label} does not represent every physical request in usage")
        if prior_units is not None and len(units) != len(prior_units):
            raise FinalizationError(f"{label} changed its physical usage-unit shape")
        for index, unit in enumerate(units):
            prior = prior_units[index] if prior_units is not None else None
            if prior is not None:
                for field in (
                    "role",
                    "label",
                    "attempt",
                    "physical_attempt_id",
                    "provider",
                    "model",
                    "requested_provider",
                    "requested_model",
                    *USAGE_CONTRACT_KEYS[4:],
                ):
                    old = _unit_present_value(prior, field)
                    new = _unit_present_value(unit, field)
                    if old is not None and new != old:
                        raise FinalizationError(f"{label} receipt repair conflicts on {field}")
                old_ids = response_ids(prior)
                new_ids = response_ids(unit)
                if old_ids and new_ids != old_ids:
                    raise FinalizationError(f"{label} receipt repair conflicts on response_id")
                old_usage_flags, old_router_flags = unit_non_byok_flags(prior)
                new_usage_flags, new_router_flags = unit_non_byok_flags(unit)
                for name, old_flags, new_flags in (
                    ("usage_is_byok", old_usage_flags, new_usage_flags),
                    ("router_is_byok", old_router_flags, new_router_flags),
                ):
                    if old_flags and new_flags != old_flags:
                        raise FinalizationError(f"{label} receipt repair conflicts on {name}")
                old_routes = _successful_router_bindings(prior)
                new_routes = _successful_router_bindings(unit)
                if old_routes and new_routes != old_routes:
                    raise FinalizationError(f"{label} receipt repair conflicts on router route")
                if unit_cost_is_exact(prior) and not unit_cost_is_exact(unit):
                    raise FinalizationError(f"{label} receipt repair regressed an exact cost")
            exact_cost: Decimal | None = None
            if unit_cost_is_exact(unit):
                provider_usage = unit.get("provider_usage")
                assert isinstance(provider_usage, Mapping)
                exact_cost = required_decimal(
                    provider_usage.get("provider_reported_cost"),
                    label=f"{label} exact receipt cost",
                ).quantize(Decimal("0.000000001"))
            if exact_cost is not None:
                exact_costs_by_unit[index].add(exact_cost)
        prior_units = units
    if any(len(values) > 1 for values in exact_costs_by_unit.values()):
        raise FinalizationError(f"{label} receipt repair conflicts on exact cost")
    selected_record, selected_run = max(
        ordered,
        key=lambda version: run_receipt_enrichment(
            version[1],
            source_index=version[0].source_index,
            line=version[0].line,
        ),
    )
    return selected_record, selected_run


def immutable_judge_attempt_payload(attempt: Mapping[str, Any]) -> dict[str, Any]:
    run = attempt.get("run")
    immutable_run = (
        {
            "error": run.get("error"),
            "final_text_sha256": run.get("final_text_sha256"),
            "llm_request_count": run.get("llm_request_count"),
            "usage": usage_generation_identity_contract(run.get("usage")),
        }
        if isinstance(run, Mapping)
        else None
    )
    return {
        "attempt_id": attempt.get("attempt_id"),
        "attempt": attempt.get("attempt"),
        "verdict": attempt.get("verdict"),
        "met": attempt.get("met"),
        "retry_suppressed_reason": attempt.get("retry_suppressed_reason"),
        "run": immutable_run,
    }


def _judge_run_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "error": value.get("error"),
        "final_text_sha256": value.get("final_text_sha256"),
        "llm_request_count": value.get("llm_request_count"),
        "usage": usage_generation_identity_contract(value.get("usage")),
    }


def validate_judge_attempt_evidence(
    records: Sequence[SourceRecord],
) -> dict[str, Any]:
    """Validate cumulative Judge budgets and every physical Judge attempt."""

    payloads: dict[str, str] = {}
    owners: dict[str, tuple[str, str, str, str, int]] = {}
    run_versions: dict[str, list[tuple[SourceRecord, Mapping[str, Any]]]] = defaultdict(list)
    state: dict[tuple[str, str, str, str, int], dict[str, Any]] = defaultdict(
        lambda: {
            "attempt_ids": (),
            "successful": False,
            "exhausted": False,
            "source_row_count": 0,
            "declaration_prior": None,
            "declaration_new": None,
        }
    )
    scope_count = 0
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        scopes: list[tuple[str, Any]] = [("judge", record.row.get("judge"))]
        candidate_judges = record.row.get("candidate_judges")
        if candidate_judges is not None:
            if not isinstance(candidate_judges, list):
                raise FinalizationError("candidate_judges is not a list")
            scopes.extend(
                (f"candidate_judge/{index}", judge) for index, judge in enumerate(candidate_judges)
            )
        for scope_name, judge in scopes:
            if judge is None:
                continue
            if not isinstance(judge, Mapping):
                raise FinalizationError(f"{record.key} {scope_name} is not an object")
            scope_count += 1
            if (
                judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
                or judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
                or judge.get("judge_attempt_budget_limit_per_unit") != JUDGE_ATTEMPT_BUDGET_LIMIT
                or judge.get("prior_judge_attempts")
            ):
                raise FinalizationError(
                    f"{record.key} {scope_name} lacks the formal Judge attempt contract"
                )
            judgments = judge.get("criterion_judgments")
            if not isinstance(judgments, list):
                raise FinalizationError(f"{record.key} {scope_name} lacks criterion Judge evidence")
            seen_units: set[tuple[str, int]] = set()
            top_attempt_count = 0
            top_new_count = 0
            top_exhausted_count = 0
            for judgment in judgments:
                if not isinstance(judgment, Mapping):
                    raise FinalizationError("Judge criterion evidence is not an object")
                criterion_id = str(judgment.get("id") or "")
                repeat_index = judgment.get("repeat_index")
                if (
                    not criterion_id
                    or isinstance(repeat_index, bool)
                    or not isinstance(repeat_index, int)
                    or repeat_index < 0
                    or (criterion_id, repeat_index) in seen_units
                ):
                    raise FinalizationError(f"{record.key} {scope_name} has an invalid Judge unit")
                seen_units.add((criterion_id, repeat_index))
                owner = (
                    record.key[0],
                    record.key[1],
                    scope_name,
                    criterion_id,
                    repeat_index,
                )
                unit_state = state[owner]
                attempts = judgment.get("judge_attempts")
                if not isinstance(attempts, list):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "lacks cumulative Judge attempts"
                    )
                if len(attempts) > JUDGE_ATTEMPT_BUDGET_LIMIT:
                    raise FinalizationError("Judge attempt budget exceeds 3")
                attempt_ids: list[str] = []
                for ordinal, attempt in enumerate(attempts, start=1):
                    if not isinstance(attempt, Mapping):
                        raise FinalizationError("Judge attempt is not an object")
                    attempt_id = str(attempt.get("attempt_id") or "")
                    if not HEX32.fullmatch(attempt_id) or attempt.get("attempt") != ordinal:
                        raise FinalizationError("Judge attempt identity or ordinal is invalid")
                    prior_owner = owners.get(attempt_id)
                    if prior_owner is not None and prior_owner != owner:
                        raise FinalizationError(
                            f"Judge attempt id {attempt_id} is reused by another unit"
                        )
                    payload = canonical_sha256(immutable_judge_attempt_payload(attempt))
                    if attempt_id in payloads and payloads[attempt_id] != payload:
                        raise FinalizationError(
                            f"Judge attempt id {attempt_id} has conflicting payloads"
                        )
                    run = attempt.get("run")
                    if not isinstance(run, Mapping):
                        raise FinalizationError("Judge attempt lacks a physical run")
                    owners[attempt_id] = owner
                    payloads[attempt_id] = payload
                    run_versions[attempt_id].append((record, run))
                    attempt_ids.append(attempt_id)
                previous_ids = tuple(unit_state["attempt_ids"])
                current_ids = tuple(attempt_ids)
                if current_ids[: len(previous_ids)] != previous_ids:
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge attempts are not cumulative"
                    )
                new_count = len(current_ids) - len(previous_ids)
                declared_prior = judgment.get("prior_judge_attempts_used")
                declared_new = judgment.get("judge_new_attempt_count")
                canonical_delta_declaration = (
                    declared_prior == len(previous_ids) and declared_new == new_count
                )
                replayed_snapshot_declaration = (
                    new_count == 0
                    and current_ids == previous_ids
                    and declared_prior == unit_state["declaration_prior"]
                    and declared_new == unit_state["declaration_new"]
                )
                if (
                    not (canonical_delta_declaration or replayed_snapshot_declaration)
                    or judgment.get("judge_attempt_count") != len(current_ids)
                    or judgment.get("judge_attempt_budget_used") != len(current_ids)
                    or judgment.get("judge_attempt_budget_remaining")
                    != JUDGE_ATTEMPT_BUDGET_LIMIT - len(current_ids)
                    or judgment.get("judge_attempt_budget_limit") != JUDGE_ATTEMPT_BUDGET_LIMIT
                    or judgment.get("judge_attempt_evidence_schema")
                    != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
                    or judgment.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
                ):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge budget declarations differ"
                    )
                exhausted = judgment.get("judge_attempt_budget_exhausted") is True
                if (
                    exhausted != (judgment.get("error") == JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR)
                    or exhausted
                    and len(current_ids) != JUDGE_ATTEMPT_BUDGET_LIMIT
                ):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge exhaustion declaration differs"
                    )
                if unit_state["successful"] and new_count or unit_state["exhausted"] and new_count:
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "spent a new Judge attempt after terminal state"
                    )
                successful = isinstance(judgment.get("met"), bool) and not judgment.get("error")
                if successful:
                    if not attempts:
                        raise FinalizationError("successful Judge unit lacks a physical attempt")
                    final_attempt = attempts[-1]
                    final_run = final_attempt.get("run")
                    final_met = final_attempt.get("met")
                    final_verdict = str(final_attempt.get("verdict") or "").strip().upper()
                    expected_verdict = "MET" if judgment.get("met") is True else "UNMET"
                    judge_run = judgment.get("judge_run")
                    if (
                        not isinstance(final_run, Mapping)
                        or str(final_run.get("error") or "")
                        or final_attempt.get("retry_suppressed_reason")
                        or final_met is not judgment.get("met")
                        or final_verdict != expected_verdict
                        or str(judgment.get("verdict") or "").strip().upper() != expected_verdict
                        or not isinstance(judge_run, Mapping)
                        or _judge_run_binding(judge_run) != _judge_run_binding(final_run)
                    ):
                        raise FinalizationError(
                            f"{record.key} {scope_name}/{criterion_id}/"
                            f"{repeat_index} result is not bound to its final "
                            "successful Judge attempt"
                        )
                unit_state["attempt_ids"] = current_ids
                unit_state["successful"] = successful
                unit_state["exhausted"] = exhausted
                unit_state["source_row_count"] += 1
                unit_state["declaration_prior"] = declared_prior
                unit_state["declaration_new"] = declared_new
                top_attempt_count += len(current_ids)
                top_new_count += int(declared_new)
                top_exhausted_count += int(exhausted)
            if (
                judge.get("judge_attempt_count") != top_attempt_count
                or judge.get("judge_new_attempt_count") != top_new_count
                or judge.get("judge_attempt_budget_exhausted_count") != top_exhausted_count
                or judge.get("judge_attempt_budget_exhausted") is not bool(top_exhausted_count)
            ):
                raise FinalizationError(f"{record.key} {scope_name} aggregate Judge budget differs")
    for attempt_id, versions in run_versions.items():
        _, run = validate_and_select_monotonic_run_version(
            versions,
            label=f"Judge attempt {attempt_id}",
        )
        route_failures = usage_route_reasons(
            run.get("usage"),
            allowed_models={JUDGE_MODEL},
            provider_pins={JUDGE_MODEL: FORMAL_UPSTREAM_PINS[JUDGE_MODEL]},
        )
        if route_failures:
            raise FinalizationError(
                f"Judge attempt {attempt_id} violates the frozen route: {route_failures}"
            )
    return {
        "schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
        "budget_limit_per_unit": JUDGE_ATTEMPT_BUDGET_LIMIT,
        "judge_scope_source_count": scope_count,
        "criterion_repeat_unit_count": len(state),
        "unique_physical_judge_attempt_count": len(payloads),
        "units": {
            "/".join((group, task, scope, criterion, str(repeat))): {
                "judge_attempt_budget_used": len(value["attempt_ids"]),
                "successful": value["successful"],
                "exhausted": value["exhausted"],
                "source_row_count": value["source_row_count"],
            }
            for (
                group,
                task,
                scope,
                criterion,
                repeat,
            ), value in sorted(state.items())
        },
    }


def ensemble_call_trace_sequence(
    trace: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return physical ensemble calls from direct and Agent-loop traces."""

    calls = trace.get("calls")
    if calls is None and str(trace.get("mode") or "") != "agent_loop":
        return [trace], []
    if not isinstance(calls, list) or not calls:
        return [], ["missing_ensemble_call_trace"]
    if any(not isinstance(item, Mapping) for item in calls):
        return [], ["invalid_ensemble_call_trace"]
    call_traces = [item for item in calls if isinstance(item, Mapping)]
    reasons: list[str] = []
    raw_count = trace.get("agent_llm_call_count")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count != len(call_traces)
    ):
        reasons.append("wrong_agent_llm_call_count")
    if nonnegative_int(trace.get("untraced_agent_llm_call_count")) != 0:
        reasons.append("untraced_agent_llm_calls")
    indices = [item.get("agent_call_index") for item in call_traces]
    if indices != list(range(1, len(call_traces) + 1)):
        reasons.append("invalid_agent_call_index_sequence")
    return call_traces, reasons


def successful_candidate(candidate: Any) -> bool:
    content = candidate.get("content") if isinstance(candidate, Mapping) else None
    return bool(
        isinstance(candidate, Mapping)
        and candidate.get("ok") is True
        and candidate.get("request_started") is True
        and isinstance(candidate.get("physical_request_count"), int)
        and not isinstance(candidate.get("physical_request_count"), bool)
        and candidate.get("physical_request_count") > 0
        and not candidate.get("error")
        and isinstance(content, Mapping)
        and nonnegative_int(content.get("chars")) > 0
        and bool(str(content.get("text") or "").strip())
    )


def aggregator_output_reasons(
    final_request: Mapping[str, Any],
    *,
    final_text: str,
) -> list[str]:
    output = final_request.get("output")
    if not isinstance(output, Mapping):
        return ["missing_aggregator_output_binding"]
    output_text = output.get("text")
    output_chars = nonnegative_int(output.get("chars"))
    if (
        not isinstance(output_text, str)
        or not output_text.strip()
        or output_chars <= 0
        or output_chars > len(final_text)
    ):
        return ["missing_aggregator_output_binding"]
    if output.get("sha256") not in {None, "", text_sha256(output_text)}:
        return ["wrong_aggregator_output_hash"]
    final_output_tail = final_text[-output_chars:]
    if output.get("truncated") is True:
        if not final_output_tail.startswith(output_text):
            return ["wrong_aggregator_output_binding"]
    elif output_text != final_output_tail or len(output_text) != output_chars:
        return ["wrong_aggregator_output_binding"]
    return []


def ensemble_physical_call_reasons(
    call: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str],
    expected_aggregator: str,
    final_text: str,
    require_output_binding: bool,
) -> list[str]:
    """Validate one physical ensemble call from candidate evidence."""

    reasons: list[str] = []
    if str(call.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    if call.get("fallback_used") is not False:
        reasons.append("aggregator_fallback_used_or_unknown")
    if str(call.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = call.get("total_candidates")
    successful = call.get("successful_proposers")
    expected_total = len(expected_proposers)
    if not isinstance(total, int) or isinstance(total, bool) or total != expected_total:
        reasons.append("wrong_executed_proposer_count")
    if (
        not isinstance(successful, int)
        or isinstance(successful, bool)
        or not 0 <= successful <= expected_total
        or successful < math.ceil(2 * expected_total / 3)
    ):
        reasons.append("proposer_quorum_not_met")

    executed_plan = call.get("selection_plan")
    if not isinstance(executed_plan, Mapping):
        reasons.append("missing_executed_selection_plan")
    else:
        raw_models = executed_plan.get("proposer_models")
        models = tuple(str(item) for item in raw_models) if isinstance(raw_models, list) else ()
        if models != tuple(expected_proposers):
            reasons.append("wrong_proposer_models")
        if str(executed_plan.get("aggregator_model") or "") != expected_aggregator:
            reasons.append("wrong_aggregator_model")

    candidates = call.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != expected_total:
        reasons.append("missing_actual_proposer_candidates")
    else:
        proven = [successful_candidate(candidate) for candidate in candidates]
        if any(
            isinstance(candidate, Mapping) and candidate.get("ok") is True and not candidate_ok
            for candidate, candidate_ok in zip(candidates, proven, strict=True)
        ):
            reasons.append("invalid_successful_proposer_evidence")
        actual_successful = sum(proven)
        if actual_successful != successful:
            reasons.append("successful_proposer_count_mismatch")
        if actual_successful < math.ceil(2 * expected_total / 3):
            reasons.append("insufficient_actual_proposer_quorum")
        for candidate, expected_model in zip(candidates, expected_proposers, strict=True):
            if not isinstance(candidate, Mapping):
                continue
            execution = candidate.get("execution")
            actual_provider = (
                str(
                    candidate.get("provider")
                    or (execution.get("actual_provider") if isinstance(execution, Mapping) else "")
                    or ""
                )
                .strip()
                .casefold()
            )
            requested_provider = (
                str(
                    candidate.get("requested_provider")
                    or (
                        execution.get("requested_provider")
                        if isinstance(execution, Mapping)
                        else ""
                    )
                    or ""
                )
                .strip()
                .casefold()
            )
            actual_model = str(
                candidate.get("model")
                or (execution.get("actual_model") if isinstance(execution, Mapping) else "")
                or ""
            ).strip()
            requested_model = str(
                candidate.get("requested_model")
                or (execution.get("requested_model") if isinstance(execution, Mapping) else "")
                or ""
            ).strip()
            if actual_provider != "openrouter" or requested_provider != "openrouter":
                reasons.append("wrong_actual_proposer_provider")
            if actual_model != expected_model or requested_model != expected_model:
                reasons.append("wrong_actual_proposer_model")

    final_request = call.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "aggregator"
        or final_request.get("error")
        or call.get("aggregator_error")
    ):
        reasons.append("aggregator_request_incomplete")
    else:
        usage = final_request.get("usage")
        execution = final_request.get("execution")
        actual_model = (
            str(usage.get("model") or usage.get("requested_model") or "")
            if isinstance(usage, Mapping)
            else str(
                execution.get("actual_model")
                or execution.get("requested_model")
                or execution.get("model")
                or ""
            )
            if isinstance(execution, Mapping)
            else ""
        )
        if actual_model != expected_aggregator:
            reasons.append("wrong_actual_aggregator_model")
        actual_provider = (
            str(usage.get("provider") or "").strip().casefold()
            if isinstance(usage, Mapping)
            else str(execution.get("actual_provider") or "").strip().casefold()
            if isinstance(execution, Mapping)
            else ""
        )
        requested_provider = (
            str(usage.get("requested_provider") or "").strip().casefold()
            if isinstance(usage, Mapping)
            else str(execution.get("requested_provider") or "").strip().casefold()
            if isinstance(execution, Mapping)
            else ""
        )
        requested_model = (
            str(usage.get("requested_model") or "").strip()
            if isinstance(usage, Mapping)
            else str(execution.get("requested_model") or "").strip()
            if isinstance(execution, Mapping)
            else ""
        )
        if actual_provider != "openrouter" or requested_provider != "openrouter":
            reasons.append("wrong_actual_aggregator_provider")
        if requested_model != expected_aggregator:
            reasons.append("wrong_requested_aggregator_model")
        if require_output_binding:
            reasons.extend(aggregator_output_reasons(final_request, final_text=final_text))
    return list(dict.fromkeys(reasons))


_ADMISSIBLE_NONTERMINAL_FALLBACK_REASONS = frozenset(
    {
        "aggregator_fallback_used_or_unknown",
        "final_request_not_aggregator",
        "proposer_quorum_not_met",
        "insufficient_actual_proposer_quorum",
        "aggregator_request_incomplete",
    }
)


def admissible_empty_nonterminal_fallback_reasons(
    call: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str],
) -> list[str]:
    """Validate an outputless nonterminal fallback against frozen routes."""

    reasons: list[str] = []
    if str(call.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("invalid_intermediate_fallback_outcome")
    if call.get("fallback_used") is not True:
        reasons.append("invalid_intermediate_fallback_flag")
    if str(call.get("final_request_role") or "") != "fallback_single":
        reasons.append("invalid_intermediate_fallback_role")
    total = call.get("total_candidates")
    successful = call.get("successful_proposers")
    expected_total = len(expected_proposers)
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total != expected_total
        or not isinstance(successful, int)
        or isinstance(successful, bool)
        or not 0 <= successful < math.ceil(2 * expected_total / 3)
    ):
        reasons.append("invalid_intermediate_fallback_quorum")
    candidates = call.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != expected_total:
        reasons.append("invalid_intermediate_fallback_candidates")
    elif sum(successful_candidate(candidate) for candidate in candidates) != successful:
        reasons.append("intermediate_fallback_candidate_count_mismatch")
    final_request = call.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "fallback_single"
        or final_request.get("error")
        or call.get("aggregator_error")
    ):
        reasons.append("invalid_intermediate_fallback_request")
        return list(dict.fromkeys(reasons))
    output = final_request.get("output")
    output_chars = output.get("chars") if isinstance(output, Mapping) else None
    if (
        not isinstance(output, Mapping)
        or output.get("text") != ""
        or not isinstance(output_chars, int)
        or isinstance(output_chars, bool)
        or output_chars != 0
        or output.get("truncated") is not False
    ):
        reasons.append("intermediate_fallback_visible_output")
    usage = final_request.get("usage")
    execution = final_request.get("execution")
    if not isinstance(usage, Mapping):
        reasons.append("missing_intermediate_fallback_usage")
        return list(dict.fromkeys(reasons))
    if not isinstance(execution, Mapping):
        reasons.append("missing_intermediate_fallback_execution")
        return list(dict.fromkeys(reasons))
    actual_provider = str(usage.get("provider") or "").strip().casefold()
    requested_provider = str(usage.get("requested_provider") or "").strip().casefold()
    actual_model = str(usage.get("model") or "").strip()
    requested_model = str(usage.get("requested_model") or "").strip()
    execution_providers = [
        str(execution.get(field) or "").strip().casefold()
        for field in ("requested_provider", "provider", "actual_provider")
    ]
    execution_models = [
        str(execution.get(field) or "").strip()
        for field in ("requested_model", "model", "actual_model")
    ]
    if (
        actual_provider != "openrouter"
        or requested_provider != "openrouter"
        or any(provider != "openrouter" for provider in execution_providers)
    ):
        reasons.append("wrong_intermediate_fallback_provider")
    if (
        not actual_model
        or not requested_model
        or not any(
            _formal_openrouter_models_equivalent(actual_model, expected)
            and _formal_openrouter_models_equivalent(requested_model, expected)
            and all(
                _formal_openrouter_models_equivalent(execution_model, expected)
                for execution_model in execution_models
            )
            for expected in expected_proposers
        )
    ):
        reasons.append("wrong_intermediate_fallback_model")
    if str(execution.get("role") or "") != "fallback_single":
        reasons.append("wrong_intermediate_fallback_execution_role")
    if (
        not isinstance(usage, Mapping)
        or not str(usage.get("stop_reason") or "").strip()
    ):
        reasons.append("missing_intermediate_fallback_stop_reason")
    return list(dict.fromkeys(reasons))


def agent_call_output_sequence_reasons(
    calls: Sequence[Mapping[str, Any]],
    *,
    final_text: str,
) -> list[str]:
    """Bind every Agent-loop response segment to the stored final answer."""

    if len(calls) <= 1:
        return []
    reasons: list[str] = []
    offset = 0
    for call in calls:
        final_request = call.get("final_request")
        output = (
            final_request.get("output")
            if isinstance(final_request, Mapping)
            else None
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_agent_call_output_binding")
            continue
        output_text = output.get("text")
        output_chars = output.get("chars")
        if (
            not isinstance(output_text, str)
            or not isinstance(output_chars, int)
            or isinstance(output_chars, bool)
            or output_chars < 0
            or offset + output_chars > len(final_text)
        ):
            reasons.append("invalid_agent_call_output_binding")
            continue
        if output.get("sha256") not in {None, "", text_sha256(output_text)}:
            reasons.append("wrong_agent_call_output_hash")
        segment = final_text[offset : offset + output_chars]
        if output.get("truncated") is True:
            if not segment.startswith(output_text):
                reasons.append("wrong_agent_call_output_binding")
        elif len(output_text) != output_chars or output_text != segment:
            reasons.append("wrong_agent_call_output_binding")
        offset += output_chars
    if offset != len(final_text):
        reasons.append("incomplete_agent_call_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_gate(
    row: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str] | None = None,
    expected_aggregator: str | None = None,
    allowed_models: set[str] | None = None,
) -> list[str]:
    trace = row.get("ensemble_trace")
    if not isinstance(trace, Mapping):
        return ["missing_ensemble_trace"]
    routing = row.get("routing_trace")
    selection_plan = (
        routing.get("selection_plan")
        if isinstance(routing, Mapping) and isinstance(routing.get("selection_plan"), Mapping)
        else {}
    )
    raw_proposers = selection_plan.get("proposer_models")
    planned_proposers = (
        tuple(str(item) for item in raw_proposers) if isinstance(raw_proposers, list) else ()
    )
    planned_aggregator = str(selection_plan.get("aggregator_model") or "")
    proposers = tuple(expected_proposers) if expected_proposers is not None else planned_proposers
    aggregator = expected_aggregator or planned_aggregator
    reasons: list[str] = []
    if not proposers:
        reasons.append("missing_proposer_models")
    if not aggregator:
        reasons.append("missing_aggregator_model")
    if expected_proposers is not None and planned_proposers != proposers:
        reasons.append("wrong_proposer_models")
    if expected_aggregator is not None and planned_aggregator != aggregator:
        reasons.append("wrong_aggregator_model")
    if allowed_models is not None:
        if any(model not in allowed_models for model in proposers):
            reasons.append("proposer_model_outside_frozen_routes")
        if aggregator not in allowed_models:
            reasons.append("aggregator_model_outside_frozen_routes")
    calls, sequence_reasons = ensemble_call_trace_sequence(trace)
    reasons.extend(sequence_reasons)
    reasons.extend(
        agent_call_output_sequence_reasons(
            calls,
            final_text=str(row.get("final_text") or ""),
        )
    )
    if proposers and aggregator:
        for index, call in enumerate(calls):
            call_reasons = ensemble_physical_call_reasons(
                call,
                expected_proposers=proposers,
                expected_aggregator=aggregator,
                final_text=str(row.get("final_text") or ""),
                require_output_binding=index == len(calls) - 1,
            )
            if index < len(calls) - 1 and call.get("fallback_used") is True:
                fallback_reasons = admissible_empty_nonterminal_fallback_reasons(
                    call,
                    expected_proposers=proposers,
                )
                reasons.extend(fallback_reasons)
                if not fallback_reasons:
                    call_reasons = [
                        reason
                        for reason in call_reasons
                        if reason not in _ADMISSIBLE_NONTERMINAL_FALLBACK_REASONS
                    ]
            reasons.extend(call_reasons)
    return list(dict.fromkeys(reasons))


def g1_recomputed_proposer_bounds(
    plan: Mapping[str, Any],
) -> tuple[int, int, list[str]] | None:
    config = plan.get("ranking_parameters")
    task_profile = plan.get("task_profile")
    if not isinstance(config, Mapping) or not isinstance(task_profile, Mapping):
        return None
    routing_tiers = config.get("routing_tiers")
    proposer_count = config.get("proposer_count")
    if not isinstance(routing_tiers, Mapping) or not isinstance(proposer_count, Mapping):
        return None
    tier_mapping = routing_tiers.get("mapping")
    tier_dist = task_profile.get("tier_dist")
    by_tier = proposer_count.get("by_tier")
    if (
        not isinstance(tier_mapping, Mapping)
        or not isinstance(tier_dist, Mapping)
        or not isinstance(by_tier, Mapping)
    ):
        return None
    try:
        tier_values = [int(value) for value in tier_mapping.values()]
        expected_tier = sum(int(tier) * float(weight) for tier, weight in tier_dist.items())
        effective_tier = max(
            min(tier_values),
            min(
                max(tier_values),
                math.floor(expected_tier + float(proposer_count["effective_tier_rounding_offset"])),
            ),
        )
        tier_bounds = by_tier[str(effective_tier)]
        minimum = int(tier_bounds["min"])
        maximum = int(tier_bounds["max"])
    except (KeyError, TypeError, ValueError):
        return None
    constraints = task_profile.get("constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}
    reasons = [f"tier_{effective_tier}"]
    if str(constraints.get("risk") or "low") == "high":
        high_risk = proposer_count.get("high_risk")
        if not isinstance(high_risk, Mapping):
            return None
        try:
            minimum = max(minimum, int(high_risk["min"]))
            maximum = max(maximum, int(high_risk["max"]))
        except (KeyError, TypeError, ValueError):
            return None
        reasons.append("high_risk_cross_validation")
    constrained = str(constraints.get("cost")) in {
        str(value) for value in proposer_count.get("constrained_cost_values") or []
    } or str(constraints.get("latency")) in {
        str(value) for value in proposer_count.get("constrained_latency_values") or []
    }
    if constrained:
        try:
            maximum = min(maximum, int(proposer_count["constrained_max"]))
        except (KeyError, TypeError, ValueError):
            return None
        minimum = min(minimum, maximum)
        reasons.append("cost_or_latency_constrained")
    return minimum, maximum, reasons


def g1_registry_plan_reasons(
    plan: Any,
    *,
    contract: Mapping[str, Any],
) -> tuple[list[str], tuple[str, ...], str]:
    """Bind a G1 physical plan to its frozen registry and exact P/A choice."""

    reasons: list[str] = []
    if not isinstance(plan, Mapping):
        return ["missing_g1_selection_plan"], (), ""
    profile_id = str(contract.get("profile_id") or "").strip()
    source_version = str(contract.get("source_registry_snapshot_version") or "").strip()
    routes_hash = str(contract.get("expected_routes_sha256") or "").strip()
    ranking_config_hash = str(contract.get("expected_ranking_config_sha256") or "").strip()
    source_registry_snapshot_hash = str(
        contract.get("expected_source_registry_snapshot_sha256") or ""
    ).strip()
    formal_n_max = contract.get("expected_proposer_count_max")
    routes = contract.get("expected_routes")
    expected_count = nonnegative_int(contract.get("expected_candidate_count"))
    expected_identities = (
        {f"openrouter:{str(model).strip().lower()}" for model in routes}
        if isinstance(routes, Mapping)
        else set()
    )
    if (
        not profile_id
        or contract.get("selection_mode") != "router_dynamic"
        or not source_version
        or not HEX64.fullmatch(routes_hash)
        or canonical_sha256(routes) != routes_hash
        or ranking_config_hash != FORMAL_G1_RANKING_CONFIG_SHA256
        or source_registry_snapshot_hash != FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        or contract.get("expected_ranking_config_schema_version")
        != FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
        or contract.get("expected_ranking_config_version") != FORMAL_G1_RANKING_CONFIG_VERSION
        or formal_n_max != FORMAL_G1_PROPOSER_COUNT_MAX
        or contract.get("user_profile_enabled") is not False
        or expected_count <= 0
        or len(expected_identities) != expected_count
    ):
        return ["invalid_g1_registry_contract"], (), ""
    filtered_version = f"{source_version}+{profile_id}+{routes_hash[:12]}"
    allowlist = plan.get("candidate_allowlist")
    expected_allowlist = {
        "policy": "exact_openrouter_routes",
        "profile_id": profile_id,
        "source_registry_snapshot_version": source_version,
        "expected_source_registry_snapshot_sha256": (source_registry_snapshot_hash),
        "filtered_registry_snapshot_version": filtered_version,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": expected_count,
        "candidate_count": expected_count,
    }
    if not isinstance(allowlist, Mapping):
        reasons.append("missing_g1_candidate_allowlist")
    else:
        for field, expected in expected_allowlist.items():
            if allowlist.get(field) != expected:
                reasons.append(f"wrong_g1_candidate_allowlist_{field}")
        identities = allowlist.get("expected_identities")
        if (
            not isinstance(identities, list)
            or len(identities) != expected_count
            or set(str(value) for value in identities) != expected_identities
        ):
            reasons.append("wrong_g1_candidate_allowlist_identities")
    pool = plan.get("candidate_pool")
    pool_identities = (
        [str(item.get("identity") or "") for item in pool if isinstance(item, Mapping)]
        if isinstance(pool, list)
        else []
    )
    if (
        nonnegative_int(plan.get("candidate_pool_size")) != expected_count
        or len(pool_identities) != expected_count
        or len(set(pool_identities)) != expected_count
        or set(pool_identities) != expected_identities
    ):
        reasons.append("wrong_g1_candidate_pool")
    if plan.get("registry_snapshot_version") != filtered_version:
        reasons.append("wrong_g1_registry_snapshot_version")
    if not HEX64.fullmatch(str(plan.get("registry_snapshot_hash") or "")):
        reasons.append("wrong_g1_registry_snapshot_hash")
    if (
        plan.get("ranking_config_hash") != ranking_config_hash
        or plan.get("ranking_config_schema_version") != FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
        or plan.get("ranking_config_version") != FORMAL_G1_RANKING_CONFIG_VERSION
        or not isinstance(plan.get("ranking_parameters"), Mapping)
        or canonical_sha256(plan.get("ranking_parameters")) != ranking_config_hash
    ):
        reasons.append("wrong_g1_ranking_config")
    n_min = plan.get("N_min")
    n_max = plan.get("N_max")
    recomputed_bounds = g1_recomputed_proposer_bounds(plan)
    if (
        isinstance(n_min, bool)
        or not isinstance(n_min, int)
        or isinstance(n_max, bool)
        or not isinstance(n_max, int)
        or not 1 <= n_min <= n_max <= formal_n_max
    ):
        reasons.append("wrong_g1_proposer_bounds")
    if (
        recomputed_bounds is None
        or (n_min, n_max) != recomputed_bounds[:2]
        or plan.get("bound_reasons") != recomputed_bounds[2]
    ):
        reasons.append("g1_proposer_bounds_not_recomputed")
    selected_p = plan.get("selected_P")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or len(set(str(value) for value in selected_p)) != len(selected_p)
        or any(str(value) not in expected_identities for value in selected_p)
    ):
        reasons.append("wrong_g1_selected_proposers")
        proposer_models: tuple[str, ...] = ()
    else:
        proposer_models = tuple(str(identity).partition(":")[2] for identity in selected_p)
        if not (
            isinstance(n_min, int)
            and not isinstance(n_min, bool)
            and isinstance(n_max, int)
            and not isinstance(n_max, bool)
            and n_min <= len(proposer_models) <= n_max
        ):
            reasons.append("g1_selected_proposer_count_outside_bounds")
    selected_a = str(plan.get("selected_A") or "")
    aggregator_model = selected_a.partition(":")[2] if selected_a in expected_identities else ""
    if not aggregator_model:
        reasons.append("wrong_g1_selected_aggregator")
    if (
        nonnegative_int(plan.get("proposer_sample_count")) != len(proposer_models)
        or tuple(str(value) for value in plan.get("proposer_models") or []) != proposer_models
        or str(plan.get("aggregator_model") or "") != aggregator_model
    ):
        reasons.append("wrong_g1_physical_selection_plan")
    selection_steps = plan.get("selection_steps")
    if (
        not isinstance(selection_steps, list)
        or len(selection_steps) != len(proposer_models)
        or any(
            not isinstance(step, Mapping)
            or step.get("step") != index
            or str(step.get("selected") or "") != selected_p[index - 1]
            for index, step in enumerate(selection_steps, start=1)
        )
        or plan.get("proposer_count") != len(proposer_models)
    ):
        reasons.append("wrong_g1_selection_steps")
    try:
        from opensquilla.provider.ranking_router import (
            ranking_trace_replay_reasons,
        )

        reasons.extend(ranking_trace_replay_reasons(plan))
    except Exception:  # noqa: BLE001 - malformed evidence must fail closed
        reasons.append("g1_frozen_ranker_replay_failed")
    return list(dict.fromkeys(reasons)), proposer_models, aggregator_model


_G1_LIFECYCLE_PLAN_MATCH_FIELDS = (
    "decision_id",
    "registry_snapshot_hash",
    "ranking_config_hash",
    "selected_P",
    "selected_A",
    "proposer_models",
    "aggregator_model",
    "task_profile",
    "N_min",
    "N_max",
    "bound_reasons",
)


def matching_saved_generation_attempts(
    row: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    final_sha = str(row.get("final_text_sha256") or "")
    selected_usage = usage_generation_identity_contract(row.get("usage"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping)
        and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    return [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and str(attempt["run"].get("final_text_sha256") or "") == final_sha
        and usage_generation_identity_contract(attempt["run"].get("usage"))
        == selected_usage
    ]


def effective_g1_lifecycle_routing(
    row: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Resolve one G1 plan from the same row/provider lifecycle."""

    registry = contract.get("g1_registry_contract")
    if not isinstance(registry, Mapping):
        return {}, {}, ["invalid_g1_registry_contract"]
    reasons: list[str] = []
    trace = row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    reasons.extend(sequence_reasons)
    physical_plans: list[Mapping[str, Any]] = []
    for call in calls:
        plan = call.get("selection_plan")
        plan_reasons, _, _ = g1_registry_plan_reasons(plan, contract=registry)
        reasons.extend(plan_reasons)
        if isinstance(plan, Mapping):
            physical_plans.append(plan)
    if not physical_plans:
        reasons.append("missing_g1_physical_selection_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    physical = physical_plans[0]
    for plan in physical_plans[1:]:
        if any(
            plan.get(field) != physical.get(field)
            for field in _G1_LIFECYCLE_PLAN_MATCH_FIELDS
        ):
            reasons.append("conflicting_g1_physical_selection_plans")

    routing = row.get("routing_trace")
    top_routing = dict(routing) if isinstance(routing, Mapping) else {}
    top_plan = top_routing.get("selection_plan")
    if isinstance(top_plan, Mapping):
        top_reasons, _, _ = g1_registry_plan_reasons(top_plan, contract=registry)
        reasons.extend(top_reasons)
        if any(
            top_plan.get(field) != physical.get(field)
            for field in _G1_LIFECYCLE_PLAN_MATCH_FIELDS
        ):
            reasons.append("g1_routing_plan_differs_from_physical_plan")
        return top_routing, {}, list(dict.fromkeys(reasons))
    if top_routing:
        reasons.append("invalid_g1_top_routing_trace")
        return {}, {}, list(dict.fromkeys(reasons))

    matching = matching_saved_generation_attempts(row)
    if len(matching) != 1:
        reasons.append("ambiguous_g1_selected_generation_attempt")
        return {}, {}, list(dict.fromkeys(reasons))
    selected = matching[0]
    selected_ordinal = nonnegative_int(selected.get("attempt"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping)
        and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        run = attempt.get("run")
        attempt_routing = run.get("routing_trace") if isinstance(run, Mapping) else None
        plan = (
            attempt_routing.get("selection_plan")
            if isinstance(attempt_routing, Mapping)
            else None
        )
        plan_reasons, _, _ = g1_registry_plan_reasons(plan, contract=registry)
        if plan_reasons:
            if isinstance(plan, Mapping):
                reasons.extend(plan_reasons)
            continue
        if not isinstance(plan, Mapping) or any(
            plan.get(field) != physical.get(field)
            for field in _G1_LIFECYCLE_PLAN_MATCH_FIELDS
        ):
            reasons.append("g1_lifecycle_plan_differs_from_physical_plan")
            continue
        if nonnegative_int(attempt.get("attempt")) > selected_ordinal:
            continue
        candidates.append((attempt, attempt_routing))
    unique = {
        canonical_sha256(dict(candidate_routing)): (attempt, candidate_routing)
        for attempt, candidate_routing in candidates
    }
    if len(unique) != 1:
        reasons.append("ambiguous_g1_lifecycle_routing_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    attempt, recovered = next(iter(unique.values()))
    evidence = {
        "schema": "opensquilla.draco.g1-provider-lifecycle-routing-recovery/v1",
        "source_attempt_id": str(attempt.get("attempt_id") or ""),
        "source_attempt": nonnegative_int(attempt.get("attempt")),
        "selected_attempt_id": str(selected.get("attempt_id") or ""),
        "selected_attempt": selected_ordinal,
        "decision_id": str(physical.get("decision_id") or ""),
        "registry_snapshot_hash": str(physical.get("registry_snapshot_hash") or ""),
        "ranking_config_hash": str(physical.get("ranking_config_hash") or ""),
    }
    return dict(recovered), evidence, list(dict.fromkeys(reasons))


def g1_provider_lifecycle_analyzer_reasons(
    row: Mapping[str, Any],
    *,
    allow_unknown_placeholder: bool = False,
) -> list[str]:
    """Require the setup-bearing attempt to contain one frozen task analyzer."""

    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping)
        and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    request_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and run_expected_request_count(attempt["run"]) > 0
    ]
    if not request_attempts:
        return ["missing_g1_provider_lifecycle_attempt"]
    first_units = usage_units(request_attempts[0]["run"].get("usage"))
    analyzers = [
        unit
        for unit in first_units
        if str(unit.get("role") or "").strip().casefold() == "task_analyzer"
    ]
    placeholders = [
        unit
        for unit in first_units
        if allow_unknown_placeholder and _is_unknown_task_analyzer_placeholder(unit)
    ]
    if not analyzers and not placeholders:
        return ["missing_g1_task_analyzer_request"]
    if analyzers:
        for analyzer in analyzers:
            if (
                str(analyzer.get("provider") or "").strip().casefold() != "openrouter"
                or str(analyzer.get("requested_provider") or "").strip().casefold()
                != "openrouter"
                or not _formal_openrouter_models_equivalent(
                    analyzer.get("model"),
                    B0_MODEL,
                )
                or not _formal_openrouter_models_equivalent(
                    analyzer.get("requested_model"),
                    B0_MODEL,
                )
            ):
                return ["wrong_g1_task_analyzer_route"]
    for attempt in request_attempts[1:]:
        later_units = usage_units(attempt["run"].get("usage"))
        if any(
            str(unit.get("role") or "").strip().casefold() == "task_analyzer"
            or _is_unknown_task_analyzer_placeholder(unit)
            for unit in later_units
        ):
            return ["repeated_g1_task_analyzer_request"]
    return []


def route_reasons(
    row: Mapping[str, Any],
    *,
    group: str,
    contract: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    routing = row.get("routing_trace")
    routing = routing if isinstance(routing, Mapping) else {}
    models = selected_usage_models(row)
    group_spec = contract.get("group_spec")
    group_spec = group_spec if isinstance(group_spec, Mapping) else {}
    provider_pins = contract_provider_pins(contract)
    if group in {"B0", "B4"}:
        expected = str(group_spec.get("model") or "")
        if not expected or routing.get("model") != expected or models != {expected}:
            reasons.append("wrong_fixed_model")
        if expected:
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models={expected},
                    provider_pins=provider_pins,
                )
            )
    elif group == "B1":
        applied = str(routing.get("applied_model") or routing.get("routed_model") or "")
        allowed = set(B1_TEXT_TIER_MODELS.values())
        if (
            routing.get("routing_applied") is not True
            or not applied
            or applied not in allowed
            or models != {applied}
        ):
            reasons.append("wrong_router_single_model")
        if applied:
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models={applied},
                    provider_pins=provider_pins,
                )
            )
    elif group == "B2":
        b2_models = {*B2_PROPOSERS, B2_AGGREGATOR}
        reasons.extend(
            usage_route_reasons(
                row.get("usage"),
                allowed_models=b2_models,
                provider_pins=provider_pins,
            )
        )
        if any(
            str(unit.get("role") or "").strip().casefold() == "task_analyzer"
            for unit in usage_units(row.get("usage"))
        ):
            reasons.append("unexpected_b2_task_analyzer_request")
        reasons.extend(
            ensemble_gate(
                row,
                expected_proposers=B2_PROPOSERS,
                expected_aggregator=B2_AGGREGATOR,
            )
        )
    elif group == "G1":
        registry = contract.get("g1_registry_contract")
        if not isinstance(registry, Mapping):
            profile = contract.get("global_experiment_profile")
            registry = (
                profile.get("g1_routing")
                if isinstance(profile, Mapping) and isinstance(profile.get("g1_routing"), Mapping)
                else {}
            )
        routes = registry.get("expected_routes")
        allowed = set(routes) if isinstance(routes, Mapping) else set()
        if registry.get("selection_mode") != "router_dynamic" or not allowed:
            reasons.append("invalid_g1_registry_contract")
        effective_routing, _, lifecycle_routing_reasons = (
            effective_g1_lifecycle_routing(
                row,
                contract=contract,
            )
        )
        reasons.extend(lifecycle_routing_reasons)
        routing = effective_routing
        reasons.extend(g1_provider_lifecycle_analyzer_reasons(row))
        if allowed:
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models=allowed,
                    provider_pins=provider_pins,
                    role_model_pins={"task_analyzer": B0_MODEL},
                    allow_unknown_task_analyzer_attempts=True,
                )
            )
        row_plan = routing.get("selection_plan")
        plan_reasons, proposers, aggregator = g1_registry_plan_reasons(
            row_plan,
            contract=registry,
        )
        reasons.extend(plan_reasons)
        if proposers and aggregator:
            row_registry_snapshot_hash = str(row_plan.get("registry_snapshot_hash") or "")
            trace = row.get("ensemble_trace")
            calls, call_sequence_reasons = ensemble_call_trace_sequence(
                trace if isinstance(trace, Mapping) else {}
            )
            reasons.extend(call_sequence_reasons)
            for call in calls:
                physical_reasons, physical_p, physical_a = g1_registry_plan_reasons(
                    call.get("selection_plan"),
                    contract=registry,
                )
                reasons.extend(physical_reasons)
                if physical_p != proposers or physical_a != aggregator:
                    reasons.append("g1_physical_plan_differs_from_routing_trace")
                physical_plan = call.get("selection_plan")
                physical_registry_snapshot_hash = (
                    str(physical_plan.get("registry_snapshot_hash") or "")
                    if isinstance(physical_plan, Mapping)
                    else ""
                )
                if physical_registry_snapshot_hash != row_registry_snapshot_hash:
                    reasons.append("g1_physical_registry_snapshot_hash_differs_from_routing_trace")
            gate_row = dict(row)
            gate_row["routing_trace"] = routing
            reasons.extend(
                ensemble_gate(
                    gate_row,
                    expected_proposers=proposers,
                    expected_aggregator=aggregator,
                    allowed_models=allowed,
                )
            )
    return reasons


def validate_physical_generation_routes(
    records: Sequence[SourceRecord],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    violations: list[dict[str, Any]] = []
    attempt_versions: dict[
        str,
        list[
            tuple[
                SourceRecord,
                Mapping[str, Any],
                set[str],
                Mapping[str, str],
                Mapping[str, str] | None,
            ]
        ],
    ] = defaultdict(list)
    for record in records:
        group = record.key[0]
        contract = contracts.get(group) or {}
        provider_pins: Mapping[str, str] | None = contract_provider_pins(contract)
        role_model_pins: Mapping[str, str] | None = None
        routing = record.row.get("routing_trace")
        routing = routing if isinstance(routing, Mapping) else {}
        if group in {"B0", "B4"}:
            spec = contract.get("group_spec")
            allowed = {str(spec.get("model") or "")} if isinstance(spec, Mapping) else set()
        elif group == "B1":
            applied = str(routing.get("applied_model") or routing.get("routed_model") or "")
            allowed = {applied} if applied in set(B1_TEXT_TIER_MODELS.values()) else set()
        elif group == "B2":
            allowed = {*B2_PROPOSERS, B2_AGGREGATOR}
        else:
            allowed: set[str] = set()
            registry = contract.get("g1_registry_contract")
            routes = registry.get("expected_routes") if isinstance(registry, Mapping) else None
            if isinstance(routes, Mapping):
                allowed.update(str(model) for model in routes)
                role_model_pins = {"task_analyzer": B0_MODEL}
        allowed.discard("")
        execution = record.row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        if group == "G1" and any(
            isinstance(attempt, Mapping)
            and isinstance(attempt.get("run"), Mapping)
            and run_expected_request_count(attempt["run"]) > 0
            for attempt in attempts
        ):
            lifecycle_reasons = g1_provider_lifecycle_analyzer_reasons(
                record.row,
                allow_unknown_placeholder=True,
            )
            if lifecycle_reasons:
                violations.append(
                    record.reference
                    | {
                        "group": group,
                        "task_id": record.key[1],
                        "attempt_id": "provider_lifecycle",
                        "reasons": lifecycle_reasons,
                    }
                )
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            run = attempt.get("run")
            if not isinstance(run, Mapping):
                continue
            if run_expected_request_count(run) <= 0:
                continue
            attempt_versions[attempt_id].append(
                (
                    record,
                    run,
                    allowed,
                    provider_pins,
                    role_model_pins,
                )
            )
    for attempt_id, versions in attempt_versions.items():
        record, run = validate_and_select_monotonic_run_version(
            [(version[0], version[1]) for version in versions],
            label=f"generation attempt {attempt_id}",
        )
        selected_versions = [
            version for version in versions if version[0] is record and version[1] is run
        ]
        if not selected_versions:
            raise FinalizationError(
                f"generation attempt {attempt_id} selected an unknown route version"
            )
        _, _, allowed, provider_pins, role_model_pins = selected_versions[-1]
        reasons = usage_route_reasons(
            run.get("usage"),
            allowed_models=allowed,
            provider_pins=provider_pins,
            role_model_pins=role_model_pins,
            allow_unknown_task_analyzer_attempts=record.key[0] == "G1",
        )
        roles = {
            str(unit.get("role") or "").strip().casefold() for unit in usage_units(run.get("usage"))
        }
        if record.key[0] == "B2" and "task_analyzer" in roles:
            reasons.append("unexpected_b2_task_analyzer_request")
        if reasons:
            violations.append(
                record.reference
                | {
                    "group": record.key[0],
                    "task_id": record.key[1],
                    "attempt_id": attempt_id,
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
    if violations:
        raise FinalizationError(
            f"physical generation route evidence violates frozen contracts: {violations[:5]}"
        )


def generation_reasons(
    record: SourceRecord,
    *,
    task: Mapping[str, Any],
    expected_fingerprint: str,
    contract: Mapping[str, Any],
) -> list[str]:
    row = record.row
    reasons: list[str] = []
    final_text = str(row.get("final_text") or "")
    if not final_text.strip():
        reasons.append("empty_final_text")
    if row.get("final_text_sha256") != text_sha256(final_text):
        reasons.append("final_text_hash_mismatch")
    if nonnegative_int(row.get("final_text_chars")) != len(final_text):
        reasons.append("final_text_length_mismatch")
    prompt = str(task.get("prompt") or "")
    if row.get("prompt_sha256") != text_sha256(prompt):
        reasons.append("prompt_hash_mismatch")
    if row.get("task_input_sha256") != canonical_sha256(task, prefix=True):
        reasons.append("task_input_hash_mismatch")
    if row.get("run_compatibility_fingerprint") != expected_fingerprint:
        reasons.append("run_compatibility_fingerprint_mismatch")
    error = str(row.get("error") or "")
    if error in POLICY_VIOLATION_ERRORS:
        reasons.append("openrouter_policy_violation")
    elif error not in ALLOWED_NON_GENERATION_ERRORS:
        reasons.append("generation_error")
    execution = row.get("execution")
    if isinstance(execution, Mapping) and str(execution.get("run_error") or ""):
        reasons.append("generation_run_error")
    if row.get("selected_generation_succeeded") is not True:
        reasons.append("selected_generation_not_successful")
    completion = row.get("completion_status")
    if isinstance(completion, Mapping) and completion.get("generation_accepted") is False:
        reasons.append("generation_not_accepted")
    reasons.extend(
        route_reasons(
            row,
            group=str(row.get("group") or ""),
            contract=contract,
        )
    )
    return list(dict.fromkeys(reasons))


def task_rubric_criteria(task: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rubric = task.get("rubric")
    if not isinstance(rubric, Mapping):
        return "", []
    rows: list[dict[str, Any]] = []
    for section in rubric.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for criterion in section.get("criteria") or []:
            if not isinstance(criterion, Mapping):
                continue
            rows.append(
                {
                    "id": str(criterion.get("id") or ""),
                    "section_id": str(section.get("id") or "rubric"),
                    "weight": Decimal(str(criterion.get("weight") or 0)),
                }
            )
    return str(rubric.get("id") or task.get("id") or ""), rows


def recompute_judge_quality(judgments: Sequence[Mapping[str, Any]]) -> Decimal | None:
    if not judgments or any(not isinstance(item.get("met"), bool) for item in judgments):
        return None
    positive_total = sum(
        max(Decimal(0), Decimal(str(item.get("weight") or 0))) for item in judgments
    )
    if positive_total <= 0:
        return None
    raw_score = sum(
        Decimal(str(item.get("weight") or 0)) for item in judgments if item.get("met") is True
    )
    return max(Decimal(0), min(Decimal(100), raw_score / positive_total * 100))


def judge_reasons(
    row: Mapping[str, Any],
    *,
    task: Mapping[str, Any] | None = None,
) -> list[str]:
    judge = row.get("judge")
    if not isinstance(judge, Mapping):
        return ["missing_judge"]
    reasons: list[str] = []
    if judge.get("score_status") != "complete":
        reasons.append("judge_incomplete")
    if nonnegative_int(judge.get("judge_error_count")) != 0:
        reasons.append("judge_errors")
    quality = row.get("quality_total")
    if not finite_number(quality):
        reasons.append("missing_quality_total")
    if (
        judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
        or judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
        or judge.get("judge_attempt_budget_limit_per_unit") != JUDGE_ATTEMPT_BUDGET_LIMIT
        or judge.get("judge_attempt_budget_exhausted") is not False
        or nonnegative_int(judge.get("judge_attempt_budget_exhausted_count")) != 0
    ):
        reasons.append("judge_attempt_contract_mismatch")
    if task is None:
        return reasons
    rubric_id, rubric_criteria = task_rubric_criteria(task)
    judgments = judge.get("criterion_judgments")
    judgments = (
        [item for item in judgments if isinstance(item, Mapping)]
        if isinstance(judgments, list)
        else []
    )
    expected_count = len(rubric_criteria) * JUDGE_REPEATS
    if (
        judge.get("mode") != "draco_criterion_judgments"
        or str(judge.get("judge_model") or "") != JUDGE_MODEL
        or nonnegative_int(judge.get("judge_repeats")) != JUDGE_REPEATS
        or str(judge.get("rubric_id") or "") != rubric_id
        or nonnegative_int(judge.get("rubric_criteria_count")) != len(rubric_criteria)
        or nonnegative_int(judge.get("criteria_count")) != expected_count
        or len(judgments) != expected_count
        or nonnegative_int(judge.get("valid_criteria_count")) != expected_count
        or nonnegative_int(judge.get("invalid_criteria_count")) != 0
    ):
        reasons.append("judge_contract_mismatch")
    expected_occurrences = Counter(
        (criterion["id"], repeat)
        for criterion in rubric_criteria
        for repeat in range(JUDGE_REPEATS)
    )
    observed_occurrences = Counter(
        (str(item.get("id") or ""), nonnegative_int(item.get("repeat_index"))) for item in judgments
    )
    if observed_occurrences != expected_occurrences:
        reasons.append("judge_rubric_binding_mismatch")
    criteria_by_id = {item["id"]: item for item in rubric_criteria}
    for judgment in judgments:
        expected = criteria_by_id.get(str(judgment.get("id") or ""))
        if (
            expected is None
            or str(judgment.get("section_id") or "") != expected["section_id"]
            or Decimal(str(judgment.get("weight") or 0)) != expected["weight"]
            or not isinstance(judgment.get("met"), bool)
            or judgment.get("error")
        ):
            reasons.append("judge_criterion_evidence_mismatch")
        attempts = judgment.get("judge_attempts")
        if not isinstance(attempts, list) or not attempts:
            reasons.append("missing_judge_physical_attempt")
            continue
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if not isinstance(run, Mapping):
                reasons.append("missing_judge_physical_attempt")
                continue
            reasons.extend(
                "wrong_judge_model_route"
                for reason in usage_route_reasons(
                    run.get("usage"),
                    allowed_models={JUDGE_MODEL},
                    provider_pins={JUDGE_MODEL: FORMAL_UPSTREAM_PINS[JUDGE_MODEL]},
                )
                if reason
            )
        final_attempt = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
        final_run = final_attempt.get("run")
        expected_verdict = (
            "MET"
            if judgment.get("met") is True
            else "UNMET"
            if judgment.get("met") is False
            else ""
        )
        if (
            not isinstance(final_run, Mapping)
            or str(final_run.get("error") or "")
            or final_attempt.get("retry_suppressed_reason")
            or final_attempt.get("met") is not judgment.get("met")
            or str(final_attempt.get("verdict") or "").strip().upper() != expected_verdict
            or str(judgment.get("verdict") or "").strip().upper() != expected_verdict
            or not isinstance(judgment.get("judge_run"), Mapping)
            or _judge_run_binding(judgment["judge_run"]) != _judge_run_binding(final_run)
        ):
            reasons.append("judge_result_not_bound_to_successful_attempt")
    recomputed = recompute_judge_quality(judgments)
    normalized = judge.get("normalized_score")
    if (
        recomputed is None
        or not finite_number(normalized)
        or not finite_number(quality)
        or abs(Decimal(str(normalized)) - recomputed) > Decimal("0.000000001")
        or abs(Decimal(str(quality)) - recomputed) > Decimal("0.000000001")
    ):
        reasons.append("quality_total_mismatch")
    recomputed_pass_rate = row_pass_rate(row) * Decimal(100)
    if (
        not finite_number(judge.get("pass_rate"))
        or not finite_number(judge.get("valid_pass_rate"))
        or abs(Decimal(str(judge.get("pass_rate"))) - recomputed_pass_rate) > Decimal("0.000000001")
        or abs(Decimal(str(judge.get("valid_pass_rate"))) - recomputed_pass_rate)
        > Decimal("0.000000001")
    ):
        reasons.append("judge_pass_rate_mismatch")
    return reasons


def select_results(
    records: Sequence[SourceRecord],
    *,
    tasks: Sequence[dict[str, Any]],
    groups: Sequence[str],
    fingerprints: Mapping[str, str],
    contracts: Mapping[str, Mapping[str, Any]],
    max_attempts: int,
) -> tuple[list[SourceRecord], dict[str, Any]]:
    expected_keys = {(group, str(task["id"])) for task in tasks for group in groups}
    by_key: dict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    unexpected: list[dict[str, Any]] = []
    for record in records:
        if record.key not in expected_keys:
            unexpected.append(record.reference | {"key": list(record.key)})
        else:
            by_key[record.key].append(record)
    if unexpected:
        raise FinalizationError(f"result sources contain unexpected rows: {unexpected[:5]}")
    missing = sorted(expected_keys - set(by_key))
    if missing:
        raise FinalizationError(f"result sources miss expected pairs: {missing[:5]}")

    selected: list[SourceRecord] = []
    pair_audit: dict[str, Any] = {}
    for task in tasks:
        task_id = str(task["id"])
        for group in groups:
            key = (group, task_id)
            candidates = by_key[key]
            cumulative_used = max(
                (
                    nonnegative_int(record.row.get("generation_attempt_budget_used"))
                    for record in candidates
                ),
                default=0,
            )
            identities: dict[str, list[SourceRecord]] = defaultdict(list)
            identity_attempts: dict[str, int] = {}
            invalid_rows: list[dict[str, Any]] = []
            seen_pair_attempt_ids: set[str] = set()
            accepted_generation_seen = False
            for record in sorted(candidates, key=lambda item: (item.source_index, item.line)):
                execution = record.row.get("execution")
                attempts = (
                    execution.get("generation_attempts")
                    if isinstance(execution, Mapping)
                    and isinstance(execution.get("generation_attempts"), list)
                    else []
                )
                row_attempt_ids = {
                    str(attempt.get("attempt_id") or "")
                    for attempt in attempts
                    if isinstance(attempt, Mapping)
                }
                new_attempt_ids = row_attempt_ids - seen_pair_attempt_ids
                reasons = generation_reasons(
                    record,
                    task=task,
                    expected_fingerprint=fingerprints[group],
                    contract=contracts[group],
                )
                if accepted_generation_seen and new_attempt_ids:
                    raise FinalizationError(
                        f"{group}/{task_id} started a new generation attempt after "
                        "an already valid generation"
                    )
                identity = generation_identity(record.row)
                identity_attempts[identity] = max(
                    identity_attempts.get(identity, 0),
                    generation_attempt_count(record.row),
                )
                if reasons:
                    invalid_rows.append(record.reference | {"reasons": reasons})
                else:
                    identities[identity].append(record)
                    accepted_generation_seen = True
                seen_pair_attempt_ids.update(row_attempt_ids)
            legacy_used = sum(identity_attempts.values())
            budget_used = cumulative_used if cumulative_used else legacy_used
            if budget_used > max_attempts:
                raise FinalizationError(
                    f"{group}/{task_id} used {budget_used} generation attempts; "
                    f"limit is {max_attempts}"
                )
            if not identities:
                raise FinalizationError(
                    f"{group}/{task_id} has no valid generation: {invalid_rows[-3:]}"
                )
            latest_identity, identity_rows = max(
                identities.items(),
                key=lambda item: max(generation_sort_key(record) for record in item[1]),
            )
            repaired = max(
                identity_rows,
                key=lambda record: (record.source_index, record.line),
            )
            judge_failures = judge_reasons(repaired.row, task=task)
            if judge_failures:
                raise FinalizationError(
                    f"{group}/{task_id} latest repair lacks a complete Judge: {judge_failures}"
                )
            selected.append(repaired)
            pair_audit[f"{group}/{task_id}"] = {
                "source": repaired.reference,
                "generation_identity_sha256": latest_identity,
                "candidate_row_count": len(candidates),
                "valid_generation_row_count": sum(len(value) for value in identities.values()),
                "distinct_generation_count": len(identity_attempts),
                "generation_attempt_budget_used": budget_used,
                "invalid_row_count": len(invalid_rows),
            }
    return selected, pair_audit


GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA = (
    "opensquilla.draco.generation-terminal-reclassification/v1"
)
LEGACY_TERMINAL_POLICY_ERROR = "aggregator_fallback_used_or_unknown"


def selected_legacy_attempt_error_is_reclassified(
    selected_row: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    """Allow one immutable legacy policy marker after independent terminal proof."""

    if (
        str(selected_row.get("group") or "") != "B2"
        or selected_row.get("selected_generation_succeeded") is not True
        or str(selected_row.get("error") or "") not in ALLOWED_NON_GENERATION_ERRORS
    ):
        return False
    execution = selected_row.get("execution")
    provenance = (
        execution.get("generation_terminal_reclassification")
        if isinstance(execution, Mapping)
        else None
    )
    run = attempt.get("run")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("schema") != GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA
        or provenance.get("policy")
        != "terminal_aggregator_with_empty_intermediate_fallback/v1"
        or provenance.get("original_error") != LEGACY_TERMINAL_POLICY_ERROR
        or not isinstance(run, Mapping)
        or str(run.get("error") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(attempt.get("retry_reason") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(provenance.get("selected_attempt_id") or "")
        != str(attempt.get("attempt_id") or "")
        or nonnegative_int(provenance.get("selected_attempt"))
        != nonnegative_int(attempt.get("attempt"))
        or nonnegative_int(execution.get("selected_generation_attempt"))
        != nonnegative_int(attempt.get("attempt"))
        or str(execution.get("run_error") or "")
    ):
        return False
    completion = selected_row.get("completion_status")
    if (
        not isinstance(completion, Mapping)
        or completion.get("generation_accepted") is not True
    ):
        return False
    trace = selected_row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    if sequence_reasons or not calls:
        return False
    expected_intermediate = [
        nonnegative_int(call.get("agent_call_index"))
        for call in calls[:-1]
        if call.get("fallback_used") is True
    ]
    terminal = calls[-1]
    if (
        not expected_intermediate
        or provenance.get("intermediate_fallback_call_indexes")
        != expected_intermediate
        or nonnegative_int(provenance.get("terminal_call_index"))
        != nonnegative_int(terminal.get("agent_call_index"))
        or terminal.get("fallback_used") is not False
        or str(terminal.get("final_request_role") or "") != "aggregator"
    ):
        return False
    return not ensemble_gate(
        selected_row,
        expected_proposers=B2_PROPOSERS,
        expected_aggregator=B2_AGGREGATOR,
    )


def bind_selected_generation_attempts(
    records: Sequence[SourceRecord],
    selected: Sequence[SourceRecord],
) -> dict[str, str]:
    """Bind every selected answer to exactly one successful physical attempt."""

    bindings: dict[str, str] = {}
    for selected_record in selected:
        selected_identity = generation_identity(selected_record.row)
        selected_final_sha = str(selected_record.row.get("final_text_sha256") or "")
        selected_usage = usage_generation_identity_contract(selected_record.row.get("usage"))
        matching_ids: set[str] = set()
        for candidate in records:
            if (
                candidate.key != selected_record.key
                or generation_identity(candidate.row) != selected_identity
            ):
                continue
            execution = candidate.row.get("execution")
            attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, Mapping)
                and isinstance(execution.get("generation_attempts"), list)
                else []
            )
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                run = attempt.get("run")
                if (
                    not isinstance(run, Mapping)
                    or str(run.get("final_text_sha256") or "") != selected_final_sha
                    or usage_generation_identity_contract(run.get("usage")) != selected_usage
                    or run_expected_request_count(run) <= 0
                    or len(usage_units(run.get("usage"))) != run_expected_request_count(run)
                ):
                    continue
                run_error = str(run.get("error") or "")
                if run_error and not selected_legacy_attempt_error_is_reclassified(
                    selected_record.row,
                    attempt,
                ):
                    continue
                attempt_id = str(attempt.get("attempt_id") or "")
                if HEX32.fullmatch(attempt_id):
                    matching_ids.add(attempt_id)
        pair = f"{selected_record.key[0]}/{selected_record.key[1]}"
        if len(matching_ids) != 1:
            raise FinalizationError(
                f"{pair} selected final answer is not bound to exactly one "
                f"successful physical generation attempt: {sorted(matching_ids)}"
            )
        bindings[pair] = next(iter(matching_ids))
    if len(bindings) != len(selected):
        raise FinalizationError("selected generation attempt binding is incomplete")
    return bindings


def response_ids(unit: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    if unit.get("response_id") is not None:
        values.append(unit.get("response_id"))
    provider_usage = unit.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        raw_ids = provider_usage.get("response_ids")
        if isinstance(raw_ids, (list, tuple, set, frozenset)):
            values.extend(raw_ids)
        elif raw_ids is not None:
            values.append(raw_ids)
        if provider_usage.get("response_id") is not None:
            values.append(provider_usage.get("response_id"))
    return {str(value).strip() for value in values if str(value).strip()}


def run_expected_request_count(run: Mapping[str, Any]) -> int:
    usage = run.get("usage")
    units = usage_units(usage)
    declared = nonnegative_int(run.get("llm_request_count"))
    usage_missing = (
        nonnegative_int(usage.get("usage_missing_count")) if isinstance(usage, Mapping) else 0
    )
    represented_missing = sum(
        1
        for unit in units
        if str(unit.get("role") or "").strip().casefold()
        in MISSING_USAGE_PLACEHOLDER_ROLES
    )
    represented = len(units) + max(0, usage_missing - represented_missing)
    trace_events = run.get("trace_events")
    if isinstance(trace_events, list):
        for event in reversed(trace_events):
            if not isinstance(event, Mapping):
                continue
            physical = event.get("physical_request_count")
            if isinstance(physical, int) and not isinstance(physical, bool):
                if (
                    physical == 0
                    and event.get("request_started") is False
                    and not units
                    and declared == 0
                ):
                    return 0
                represented = max(represented, max(0, physical))
                break
    if units or declared:
        return max(declared, represented)
    return 0


def iter_judge_runs(judge: Any) -> Iterable[tuple[str, Mapping[str, Any]]]:
    if not isinstance(judge, Mapping):
        return
    prior = judge.get("prior_judge_attempts")
    if isinstance(prior, list):
        for index, attempt in enumerate(prior):
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, Mapping):
                yield f"prior/{index}", run
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        for criterion_index, criterion in enumerate(criteria):
            attempts = criterion.get("judge_attempts") if isinstance(criterion, Mapping) else None
            if not isinstance(attempts, list):
                continue
            for attempt_index, attempt in enumerate(attempts):
                run = attempt.get("run") if isinstance(attempt, Mapping) else None
                if isinstance(run, Mapping):
                    criterion_id = str(criterion.get("id") or criterion_index)
                    repeat_index = nonnegative_int(criterion.get("repeat_index"))
                    attempt_id = str(attempt.get("attempt_id") or attempt_index)
                    yield (
                        f"criterion/{criterion_id}/{repeat_index}/{attempt_id}",
                        run,
                    )
        return
    attempts = judge.get("judge_attempts")
    if isinstance(attempts, list):
        for index, attempt in enumerate(attempts):
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, Mapping):
                yield f"attempt/{index}", run


def unit_identity_signature(unit: Mapping[str, Any]) -> str:
    """Cost-repair-stable identity for a no-response-id physical unit."""

    return canonical_sha256(
        {
            "usage_contract": usage_generation_contract(unit),
            "role": unit.get("role"),
        }
    )


def _record_unit(
    entries: dict[str, LedgerEntry],
    response_id_bindings: dict[str, dict[str, Any]],
    *,
    identity: str,
    logical_physical_identity: str,
    unit: Mapping[str, Any],
    scope: str,
    reference: Mapping[str, Any],
) -> None:
    ids = response_ids(unit)
    reused = {
        response_id: response_id_bindings[response_id]
        for response_id in ids
        if response_id in response_id_bindings
    }
    if reused:
        raise FinalizationError(
            "provider response_id is reused across logical physical requests: "
            f"current={logical_physical_identity}/{scope}/{dict(reference)}, "
            f"first={reused}"
        )
    if ids:
        identity = f"response:{canonical_sha256(sorted(ids))}"
    entry = entries.setdefault(identity, LedgerEntry(identity))
    entry.scopes.add(scope)
    entry.units.append(copy.deepcopy(dict(unit)))
    entry.references.append(dict(reference))
    entry.response_ids.update(ids)
    for response_id in ids:
        response_id_bindings[response_id] = {
            "ledger_identity": identity,
            "logical_physical_identity": logical_physical_identity,
            "scope": scope,
            "reference": dict(reference),
        }


def _record_run(
    entries: dict[str, LedgerEntry],
    response_id_bindings: dict[str, dict[str, Any]],
    *,
    run: Mapping[str, Any],
    scope: str,
    base_identity: str,
    reference: Mapping[str, Any],
    occurrence_counter: Counter[str],
) -> None:
    units = usage_units(run.get("usage"))
    expected = run_expected_request_count(run)
    for unit in units:
        ids = response_ids(unit)
        signature = unit_identity_signature(unit)
        occurrence = occurrence_counter[signature]
        occurrence_counter[signature] += 1
        identity = (
            f"response:{canonical_sha256(sorted(ids))}"
            if ids
            else f"{base_identity}:unit:{signature}:{occurrence}"
        )
        logical_physical_identity = f"{base_identity}:unit:{signature}:{occurrence}"
        _record_unit(
            entries,
            response_id_bindings,
            identity=identity,
            logical_physical_identity=logical_physical_identity,
            unit=unit,
            scope=scope,
            reference=reference,
        )
    missing = max(0, expected - len(units))
    for missing_index in range(missing):
        placeholder = {
            "role": "missing_usage",
            "provider": "",
            "model": "",
            "cost_source": "none",
        }
        _record_unit(
            entries,
            response_id_bindings,
            identity=f"{base_identity}:missing:{missing_index}",
            logical_physical_identity=(f"{base_identity}:missing:{missing_index}"),
            unit=placeholder,
            scope=scope,
            reference=reference,
        )


def build_actual_spend_ledger(
    records: Sequence[SourceRecord],
    *,
    selected: Sequence[SourceRecord] = (),
    selected_attempt_bindings: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild physical spend from every wave; never trust a row-level total."""

    entries: dict[str, LedgerEntry] = {}
    response_id_bindings: dict[str, dict[str, Any]] = {}
    seen_generation_attempts: set[str] = set()
    generation_attempt_versions: dict[str, list[tuple[SourceRecord, Mapping[str, Any], int]]] = (
        defaultdict(list)
    )
    bindings = (
        dict(selected_attempt_bindings)
        if selected_attempt_bindings is not None
        else bind_selected_generation_attempts(records, selected)
        if selected
        else {}
    )
    if selected and len(bindings) != len(selected):
        raise FinalizationError(
            "selected generation attempt binding does not cover every selected pair"
        )
    selected_attempt_ids = set(bindings.values())
    judge_run_versions: dict[str, list[tuple[SourceRecord, str, Mapping[str, Any], str]]] = (
        defaultdict(list)
    )

    for record in records:
        row = record.row
        execution = row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        for fallback_index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            seen_generation_attempts.add(attempt_id)
            generation_attempt_versions[attempt_id].append((record, attempt, fallback_index))

        for judge_scope, judge in (
            ("judge", row.get("judge")),
            *(
                (f"candidate_judge/{index}", item)
                for index, item in enumerate(row.get("candidate_judges") or [])
            ),
        ):
            for path, run in iter_judge_runs(judge):
                identity = f"judge:{record.key[0]}:{record.key[1]}:{judge_scope}:{path}"
                judge_run_versions[identity].append((record, judge_scope, run, path))

    # A copied repair can add receipt/provider/cost metadata to the same
    # immutable attempt.  Retain exactly one physical request set, but choose
    # the most enriched monotonic copy instead of freezing the first wave.
    for attempt_id, versions in generation_attempt_versions.items():
        record, run = validate_and_select_monotonic_run_version(
            [
                (candidate_record, attempt.get("run"))
                for candidate_record, attempt, _ in versions
                if isinstance(attempt.get("run"), Mapping)
            ],
            label=f"generation attempt {attempt_id}",
        )
        matching_versions = [
            version for version in versions if version[0] is record and version[1].get("run") is run
        ]
        if not matching_versions:
            raise FinalizationError(
                f"generation attempt {attempt_id} selected an unknown receipt version"
            )
        _, attempt, fallback_index = matching_versions[-1]
        attempt_index = nonnegative_int(attempt.get("attempt")) or fallback_index
        _record_run(
            entries,
            response_id_bindings,
            run=run,
            scope="generation",
            base_identity=f"generation-attempt:{attempt_id}",
            reference=record.reference
            | {
                "group": record.key[0],
                "task_id": record.key[1],
                "phase": "generation",
                "attempt": attempt_index,
                "attempt_id": attempt_id,
                "attempt_kind": attempt.get("attempt_kind"),
                "attempt_outcome": ("failed" if str(run.get("error") or "") else "successful"),
                "selected_generation": attempt_id in selected_attempt_ids,
                "receipt_version_count": len(versions),
                "receipt_version_selected": True,
            },
            occurrence_counter=Counter(),
        )

    # Judge repair rows copy earlier logical attempt paths.  Select the most
    # enriched monotonic copy for each path; newly appended retry paths remain
    # independent physical calls.
    for identity, versions in judge_run_versions.items():
        record, run = validate_and_select_monotonic_run_version(
            [(version[0], version[2]) for version in versions],
            label=identity,
        )
        matching_versions = [
            version for version in versions if version[0] is record and version[2] is run
        ]
        if not matching_versions:
            raise FinalizationError(f"{identity} selected an unknown receipt version")
        _, judge_scope, _, path = matching_versions[-1]
        _record_run(
            entries,
            response_id_bindings,
            run=run,
            scope=judge_scope,
            base_identity=identity,
            reference=record.reference
            | {
                "group": record.key[0],
                "task_id": record.key[1],
                "phase": judge_scope,
                "judge_path": path,
                "receipt_version_count": len(versions),
                "receipt_version_selected": True,
            },
            occurrence_counter=Counter(),
        )

    ledger_rows = [ledger_entry_payload(entry) for entry in entries.values()]
    ledger_rows.sort(key=lambda value: value["ledger_id"])
    category_counts = Counter(str(row["non_byok_evidence"]) for row in ledger_rows)
    scope_counts: Counter[str] = Counter()
    scope_costs: dict[str, Decimal] = defaultdict(Decimal)
    scope_exact_counts: Counter[str] = Counter()
    scope_non_exact_counts: Counter[str] = Counter()
    scope_unknown_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    disposition_costs: dict[str, Decimal] = defaultdict(Decimal)
    recorded_cost = Decimal(0)
    exact_cost = Decimal(0)
    unknown_cost_count = 0
    non_exact_cost_count = 0
    for row in ledger_rows:
        for scope in row["scopes"]:
            scope_counts[scope] += 1
        disposition = str(row.get("generation_disposition") or "")
        if disposition:
            disposition_counts[disposition] += 1
        cost = row.get("recorded_cost_usd")
        if cost is None:
            unknown_cost_count += 1
            for scope in row["scopes"]:
                scope_unknown_counts[scope] += 1
        else:
            parsed = required_decimal(cost, label="ledger cost")
            recorded_cost += parsed
            if row.get("cost_precision") == "exact":
                exact_cost += parsed
                for scope in row["scopes"]:
                    scope_exact_counts[scope] += 1
            else:
                non_exact_cost_count += 1
                for scope in row["scopes"]:
                    scope_non_exact_counts[scope] += 1
            for scope in row["scopes"]:
                scope_costs[scope] += parsed
            if disposition:
                disposition_costs[disposition] += parsed
    summary = {
        "schema": LEDGER_SCHEMA,
        "physical_request_count": len(ledger_rows),
        "scope_request_counts": dict(sorted(scope_counts.items())),
        "scope_recorded_cost_usd": {key: str(value) for key, value in sorted(scope_costs.items())},
        "scope_cost_precision_counts": {
            scope: {
                "exact": scope_exact_counts[scope],
                "non_exact": scope_non_exact_counts[scope],
                "unknown": scope_unknown_counts[scope],
            }
            for scope in sorted(scope_counts)
        },
        "generation_disposition_request_counts": dict(sorted(disposition_counts.items())),
        "generation_disposition_recorded_cost_usd": {
            key: str(value) for key, value in sorted(disposition_costs.items())
        },
        "non_byok_evidence_counts": dict(sorted(category_counts.items())),
        "recorded_cost_usd": str(recorded_cost),
        "exact_cost_usd": str(exact_cost),
        "unknown_cost_request_count": unknown_cost_count,
        "non_exact_cost_request_count": non_exact_cost_count,
        "source_row_count": len(records),
        "distinct_generation_attempt_count": len(seen_generation_attempts),
        "selected_generation_pair_count": len(bindings),
        "selected_generation_attempt_count": len(selected_attempt_ids),
        "note": (
            "Built from all source-wave generation attempts and Judge attempts; "
            "copied repairs are deduplicated by stable response id or retained "
            "run-occurrence identity. Failed and replaced generation attempts remain."
        ),
    }
    return ledger_rows, summary


def ledger_model_metrics(
    ledger_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        scopes = row.get("scopes")
        phase = (
            "generation"
            if isinstance(scopes, list) and "generation" in scopes
            else "judge"
            if isinstance(scopes, list) and any("judge" in str(scope) for scope in scopes)
            else "other"
        )
        grouped[
            (
                phase,
                str(row.get("provider") or "<unknown>"),
                str(row.get("model") or "<unknown>"),
            )
        ].append(row)
    metrics: list[dict[str, Any]] = []
    for (phase, provider, model), rows in sorted(grouped.items()):
        known_costs = [
            required_decimal(
                row.get("recorded_cost_usd"),
                label="model ledger cost",
            )
            for row in rows
            if row.get("recorded_cost_usd") is not None
        ]
        metrics.append(
            {
                "phase": phase,
                "provider": provider,
                "model": model,
                "calls": len(rows),
                "input_tokens": sum(nonnegative_int(row.get("input_tokens")) for row in rows),
                "output_tokens": sum(nonnegative_int(row.get("output_tokens")) for row in rows),
                "recorded_cost_usd": str(sum(known_costs, Decimal(0))),
                "exact_request_count": sum(row.get("cost_precision") == "exact" for row in rows),
                "estimated_or_recorded_request_count": sum(
                    row.get("recorded_cost_usd") is not None
                    and row.get("cost_precision") != "exact"
                    for row in rows
                ),
                "unknown_request_count": sum(row.get("recorded_cost_usd") is None for row in rows),
                "upstream_providers": sorted(
                    {str(value) for row in rows for value in row.get("upstream_providers") or []}
                ),
                "upstream_models": sorted(
                    {str(value) for row in rows for value in row.get("upstream_models") or []}
                ),
                "roles": sorted({str(value) for row in rows for value in row.get("roles") or []}),
                "cost_sources": sorted(
                    {str(value) for row in rows for value in row.get("cost_sources") or []}
                ),
            }
        )
    return metrics


def paid_external_tool_path(row: Mapping[str, Any]) -> tuple[bool, set[str]]:
    policy = row.get("tool_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    local = policy.get("local_web_tools")
    local = local if isinstance(local, Mapping) else {}
    search = local.get("web_search")
    search = search if isinstance(search, Mapping) else {}
    fetch = local.get("web_fetch")
    fetch = fetch if isinstance(fetch, Mapping) else {}
    providers: set[str] = set()
    search_provider = str(search.get("provider") or "").strip().casefold()
    if search_provider:
        providers.add(search_provider)
    if fetch.get("allow_firecrawl") is True:
        providers.add("firecrawl")
    paid = str(policy.get("tool_mode") or "") == "local_web_tools" and (
        "brave" in providers or "firecrawl" in providers
    )
    return paid, providers


def attempt_tool_call_count(run: Mapping[str, Any]) -> int:
    declared = run.get("total_tool_call_count")
    if isinstance(declared, int) and not isinstance(declared, bool):
        return max(0, declared)
    return max(
        0,
        nonnegative_int(run.get("tool_call_count")),
        nonnegative_int(run.get("stream_tool_call_count"))
        + nonnegative_int(run.get("server_tool_call_count")),
    )


def parse_external_scope(
    value: Any,
    *,
    expected_calls: int,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    declared_calls = value.get("tool_call_count")
    if (
        not isinstance(declared_calls, int)
        or isinstance(declared_calls, bool)
        or declared_calls != expected_calls
    ):
        return None

    def optional_cost(field_name: str) -> Decimal | None:
        raw = value.get(field_name)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            parsed = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() and parsed >= 0 else None

    recorded = optional_cost("recorded_cost_usd")
    estimated = optional_cost("estimated_cost_usd")
    complete = value.get("cost_complete") is True
    exact = value.get("cost_exact") is True
    upper = nonnegative_int(value.get("potentially_unpriced_tool_call_count_upper_bound"))
    if exact and (not complete or recorded is None or upper):
        return None
    return {
        "tool_call_count": expected_calls,
        "recorded_cost": recorded,
        "estimated_cost": estimated,
        "potentially_unpriced_tool_call_count_upper_bound": upper,
        "cost_complete": complete,
        "cost_exact": exact,
    }


def derived_external_scope(
    *,
    tool_call_count: int,
    paid_path: bool,
) -> dict[str, Any]:
    unknown = paid_path and tool_call_count > 0
    return {
        "tool_call_count": tool_call_count,
        "recorded_cost": None if unknown else Decimal(0),
        "estimated_cost": None,
        "potentially_unpriced_tool_call_count_upper_bound": (tool_call_count if unknown else 0),
        "cost_complete": not unknown,
        "cost_exact": not unknown,
    }


def build_external_tool_cost_summary(
    records: Sequence[SourceRecord],
    *,
    manifest_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate Web/Brave spend separately, deduplicated by attempt ID."""

    seen_attempts: set[str] = set()
    scopes: list[dict[str, Any]] = []
    providers: set[str] = set()
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        execution = record.row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        new_attempts: list[Mapping[str, Any]] = []
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            if attempt_id in seen_attempts:
                continue
            seen_attempts.add(attempt_id)
            new_attempts.append(attempt)
        if not new_attempts:
            continue
        paid_path, row_providers = paid_external_tool_path(record.row)
        providers.update(row_providers)
        attempt_runs = [
            attempt.get("run")
            for attempt in new_attempts
            if isinstance(attempt.get("run"), Mapping)
        ]
        call_count = sum(
            attempt_tool_call_count(run) for run in attempt_runs if isinstance(run, Mapping)
        )
        run_scopes: list[dict[str, Any]] = []
        for run in attempt_runs:
            if not isinstance(run, Mapping):
                continue
            accounting = run.get("cost_accounting")
            external = (
                accounting.get("actual_external_tools") or accounting.get("external_tools")
                if isinstance(accounting, Mapping)
                else None
            )
            parsed = parse_external_scope(
                external,
                expected_calls=attempt_tool_call_count(run),
            )
            if parsed is None:
                run_scopes = []
                break
            run_scopes.append(parsed)
        if run_scopes and sum(scope["tool_call_count"] for scope in run_scopes) == call_count:
            scopes.extend(run_scopes)
            continue
        accounting = record.row.get("cost_accounting")
        row_external = (
            accounting.get("actual_external_tools") if isinstance(accounting, Mapping) else None
        )
        parsed_row = parse_external_scope(
            row_external,
            expected_calls=call_count,
        )
        scopes.append(
            parsed_row
            if parsed_row is not None
            else derived_external_scope(
                tool_call_count=call_count,
                paid_path=paid_path,
            )
        )

    task_tool_calls = sum(scope["tool_call_count"] for scope in scopes)
    task_upper_bound = sum(
        scope["potentially_unpriced_tool_call_count_upper_bound"] for scope in scopes
    )
    preflight_by_tool: Counter[str] = Counter()
    for source in manifest_sources:
        preflight = source.get("live_web_preflight")
        calls = preflight.get("preflight_calls") if isinstance(preflight, Mapping) else None
        if not isinstance(calls, Mapping):
            raise FinalizationError("manifest source lacks live Web preflight evidence")
        for tool_name in ("web_search", "web_fetch"):
            value = calls.get(tool_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FinalizationError("manifest Web preflight call count is invalid")
            preflight_by_tool[tool_name] += value
    preflight_tool_calls = sum(preflight_by_tool.values())
    tool_calls = task_tool_calls + preflight_tool_calls
    # Live preflight happens outside generation/Judge traces and has no
    # provider-dollar receipt.  It is campaign overhead, never zero-cost.
    upper_bound = task_upper_bound + preflight_tool_calls
    exact = bool(scopes) and all(scope["cost_exact"] for scope in scopes)
    complete = bool(scopes) and all(scope["cost_complete"] for scope in scopes)
    if preflight_tool_calls:
        exact = False
        complete = False
    known_lower_bound = sum(
        (scope["recorded_cost"] if isinstance(scope.get("recorded_cost"), Decimal) else Decimal(0))
        for scope in scopes
    )
    has_unknown = preflight_tool_calls > 0 or any(
        not scope["cost_complete"] and scope.get("estimated_cost") is None for scope in scopes
    )
    estimated_total: Decimal | None = None
    if scopes and not has_unknown:
        estimated_total = sum(
            (
                scope["recorded_cost"]
                if scope["cost_exact"] and isinstance(scope.get("recorded_cost"), Decimal)
                else scope["estimated_cost"]
                if isinstance(scope.get("estimated_cost"), Decimal)
                else Decimal(0)
            )
            for scope in scopes
        )
    status = "exact" if exact else "estimated" if estimated_total is not None else "unknown"
    return {
        "scope": "campaign_actual_external_tools",
        "providers": sorted(providers),
        "distinct_generation_attempt_count": len(seen_attempts),
        "tool_call_count": tool_calls,
        "task_generation_tool_call_count": task_tool_calls,
        "live_preflight_tool_call_count": preflight_tool_calls,
        "live_preflight_calls_by_tool": dict(sorted(preflight_by_tool.items())),
        "live_preflight_manifest_count": len(manifest_sources),
        "recorded_cost_usd": str(known_lower_bound) if complete else None,
        "recorded_cost_usd_lower_bound": str(known_lower_bound),
        "estimated_cost_usd": (
            str(estimated_total) if estimated_total is not None and not exact else None
        ),
        "potentially_unpriced_tool_call_count_upper_bound": upper_bound,
        "cost_complete": complete,
        "cost_exact": exact,
        "cost_status": status,
        "cost_precision": status,
        "recorded_cost_usd_is_lower_bound": not complete,
        "separate_from_openrouter_account_delta": True,
        "deduplication": (
            "task calls: unique generation attempt_id across every source wave; "
            "live preflight: once per supplied source manifest"
        ),
        "note": (
            "Unknown Brave/Firecrawl calls are not reported as zero-dollar spend "
            "and are never mixed into the OpenRouter LLM account delta."
        ),
    }


def stable_receipt_conflicts(units: Sequence[Mapping[str, Any]]) -> set[str]:
    conflicts: set[str] = set()
    providers: set[str] = set()
    models: set[str] = set()
    costs: set[Decimal] = set()
    token_values: dict[str, set[int]] = defaultdict(set)
    for unit in units:
        provider = str(unit.get("provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)
        provider_usage = unit.get("provider_usage")
        provider_reported = (
            provider_usage.get("provider_reported_cost")
            if isinstance(provider_usage, Mapping)
            else None
        )
        cost_source = str(unit.get("cost_source") or "").casefold()
        raw_costs = [provider_reported]
        if "estimate" not in cost_source:
            raw_costs.append(unit.get("billed_cost"))
        for raw_cost in raw_costs:
            if raw_cost is not None and not isinstance(raw_cost, bool):
                try:
                    cost = Decimal(str(raw_cost))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if cost.is_finite() and cost >= 0:
                    costs.add(cost.quantize(Decimal("0.000000001")))
        if isinstance(provider_usage, Mapping):
            evidence = provider_usage.get("stable_receipt_evidence")
            if isinstance(evidence, Mapping):
                if evidence.get("receipt_conflict") is True:
                    conflicts.add("inherited_receipt_conflict")
                conflicts.update(
                    str(value) for value in evidence.get("conflict_fields") or [] if str(value)
                )
        for key in USAGE_CONTRACT_KEYS[4:]:
            raw = unit.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                token_values[key].add(raw)
    if len(providers) > 1:
        conflicts.add("provider")
    if len(models) > 1:
        conflicts.add("model")
    if len(costs) > 1:
        conflicts.add("cost")
    for key, values in token_values.items():
        if len(values) > 1:
            conflicts.add(key)
    return conflicts


def router_provider_metadata_complete(router_metadata: Mapping[str, Any]) -> bool:
    attempts = router_metadata.get("attempts")
    if isinstance(attempts, list):
        if any(
            isinstance(attempt, Mapping)
            and str(attempt.get("provider") or "").strip()
            and str(attempt.get("model") or "").strip()
            and isinstance(attempt.get("status"), int)
            and not isinstance(attempt.get("status"), bool)
            and 200 <= int(attempt["status"]) < 300
            for attempt in attempts
        ):
            return True
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    return isinstance(available, list) and any(
        isinstance(endpoint, Mapping)
        and endpoint.get("selected") is True
        and str(endpoint.get("provider") or "").strip()
        and str(endpoint.get("model") or "").strip()
        for endpoint in available
    )


def unit_non_byok_flags(unit: Mapping[str, Any]) -> tuple[set[bool], set[bool]]:
    usage_values: set[bool] = set()
    router_values: set[bool] = set()
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return usage_values, router_values
    if provider_usage.get("is_byok") in {True, False}:
        usage_values.add(bool(provider_usage["is_byok"]))
    router_metadata = provider_usage.get("router_metadata")
    if isinstance(router_metadata, Mapping) and router_metadata.get("is_byok") in {
        True,
        False,
    }:
        router_values.add(bool(router_metadata["is_byok"]))
    evidence = provider_usage.get("stable_receipt_evidence")
    if isinstance(evidence, Mapping):
        usage_values.update(
            value for value in evidence.get("usage_is_byok_values") or [] if value in {True, False}
        )
        router_values.update(
            value for value in evidence.get("router_is_byok_values") or [] if value in {True, False}
        )
    return usage_values, router_values


def unit_exact_non_byok(unit: Mapping[str, Any]) -> bool:
    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    ids = response_ids(unit)
    routed_model = str(unit.get("requested_model") or unit.get("model") or "").strip()
    successful_models = {
        upstream_model
        for _, upstream_model in _successful_router_bindings(unit)
    }
    if (
        provider_usage.get("is_byok") is not False
        or not isinstance(router_metadata, Mapping)
        or router_metadata.get("is_byok") is not False
        or not router_provider_metadata_complete(router_metadata)
        or not routed_model
        or not successful_models
        or not all(
            _formal_openrouter_models_equivalent(routed_model, upstream_model)
            for upstream_model in successful_models
        )
        or not ids
    ):
        return False
    try:
        billed = required_decimal(unit.get("billed_cost"), label="billed cost")
        reported = required_decimal(
            provider_usage.get("provider_reported_cost"),
            label="provider-reported cost",
        )
    except FinalizationError:
        return False
    return billed.quantize(Decimal("0.000000001")) == reported.quantize(Decimal("0.000000001"))


def unit_cost_is_exact(unit: Mapping[str, Any]) -> bool:
    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping) or not response_ids(unit):
        return False
    try:
        billed = required_decimal(unit.get("billed_cost"), label="billed cost")
        reported = required_decimal(
            provider_usage.get("provider_reported_cost"),
            label="provider-reported cost",
        )
    except FinalizationError:
        return False
    return billed.quantize(Decimal("0.000000001")) == reported.quantize(Decimal("0.000000001"))


def ledger_entry_payload(entry: LedgerEntry) -> dict[str, Any]:
    conflicts = stable_receipt_conflicts(entry.units)
    usage_flags: set[bool] = set()
    router_flags: set[bool] = set()
    providers: set[str] = set()
    models: set[str] = set()
    upstream_providers: set[str] = set()
    upstream_models: set[str] = set()
    roles: set[str] = set()
    cost_sources: set[str] = set()
    for unit in entry.units:
        usage, router = unit_non_byok_flags(unit)
        usage_flags.update(usage)
        router_flags.update(router)
        provider = str(unit.get("provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)
        role = str(unit.get("role") or "").strip()
        if role:
            roles.add(role)
        cost_source = str(unit.get("cost_source") or "").strip()
        if cost_source:
            cost_sources.add(cost_source)
        for upstream_provider, upstream_model in _successful_router_bindings(unit):
            if upstream_provider:
                upstream_providers.add(upstream_provider)
            if upstream_model:
                upstream_models.add(upstream_model)
    combined_flags = usage_flags | router_flags
    if (
        conflicts
        or len(combined_flags) > 1
        or any(provider != "openrouter" for provider in providers)
    ):
        category = "conflict"
    elif True in combined_flags:
        category = "explicit_byok"
    elif any(unit_exact_non_byok(unit) for unit in entry.units):
        category = "exact"
    else:
        category = "unverified"

    exact_costs: set[Decimal] = set()
    costs: list[tuple[Decimal, str]] = []
    for unit in entry.units:
        provider_usage = unit.get("provider_usage")
        declared_unknown = (
            str(unit.get("role") or "").strip().casefold()
            in MISSING_USAGE_PLACEHOLDER_ROLES
        ) or (isinstance(provider_usage, Mapping) and provider_usage.get("usage_unknown") is True)
        reported = (
            provider_usage.get("provider_reported_cost")
            if isinstance(provider_usage, Mapping)
            else None
        )
        cost_source = str(unit.get("cost_source") or "none").strip().casefold()
        no_recorded_cost_evidence = (
            cost_source in {"none", "unavailable"}
            and reported is None
            and unit.get("estimated_cost_usd") is None
        )
        if unit_cost_is_exact(unit):
            exact_value = required_decimal(
                provider_usage.get("provider_reported_cost"),
                label="exact provider-reported cost",
            ).quantize(Decimal("0.000000001"))
            exact_costs.add(exact_value)
        if declared_unknown or no_recorded_cost_evidence:
            # A placeholder or cost_source=none/unavailable row records no
            # observed spend. Treating its numeric default as a recorded $0
            # would silently erase unknown cost from reconciliation.
            continue
        cost_candidates: list[tuple[Any, str]] = [(reported, "provider_reported")]
        if cost_source not in {"none", "unavailable"}:
            cost_candidates.append((unit.get("billed_cost"), cost_source or "recorded"))
        cost_candidates.append((unit.get("estimated_cost_usd"), "estimated"))
        for value, source in cost_candidates:
            if value is None or isinstance(value, bool):
                continue
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if parsed.is_finite() and parsed >= 0:
                costs.append((parsed, source))
                break
    recorded_cost: str | None = None
    cost_precision = "unknown"
    if len(exact_costs) > 1:
        conflicts.add("exact_cost")
        category = "conflict"
    if exact_costs:
        recorded_cost = str(next(iter(exact_costs)))
        cost_precision = "exact"
    elif costs:
        provider_costs = [
            value for value, source in costs if source.casefold() == "provider_reported"
        ]
        chosen = provider_costs[0] if provider_costs else costs[0][0]
        recorded_cost = str(chosen)
        sources = {source.casefold() for _, source in costs}
        cost_precision = (
            "estimated"
            if any("estimate" in source or source.startswith("opensquilla_") for source in sources)
            else "recorded"
        )
    input_tokens = max(
        (nonnegative_int(unit.get("input_tokens")) for unit in entry.units),
        default=0,
    )
    output_tokens = max(
        (nonnegative_int(unit.get("output_tokens")) for unit in entry.units),
        default=0,
    )
    generation_references = [
        reference for reference in entry.references if reference.get("phase") == "generation"
    ]
    generation_disposition: str | None = None
    if any(reference.get("selected_generation") is True for reference in generation_references):
        generation_disposition = "selected"
    elif any(reference.get("attempt_outcome") == "failed" for reference in generation_references):
        generation_disposition = "failed"
    elif generation_references:
        generation_disposition = "replaced"
    group_task_pairs = sorted(
        {
            (str(reference.get("group") or ""), str(reference.get("task_id") or ""))
            for reference in entry.references
            if reference.get("group") and reference.get("task_id")
        }
    )
    return {
        "schema": LEDGER_SCHEMA,
        "ledger_id": f"sha256:{canonical_sha256(entry.identity)}",
        "scopes": sorted(entry.scopes),
        "provider": sorted(providers)[0] if len(providers) == 1 else None,
        "model": sorted(models)[0] if len(models) == 1 else None,
        "upstream_providers": sorted(upstream_providers),
        "upstream_models": sorted(upstream_models),
        "roles": sorted(roles),
        "cost_sources": sorted(cost_sources),
        "response_id_sha256": [
            f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
            for value in sorted(entry.response_ids)
        ],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "recorded_cost_usd": recorded_cost,
        "cost_precision": cost_precision,
        "generation_disposition": generation_disposition,
        "group_task_pairs": [
            {"group": group, "task_id": task_id} for group, task_id in group_task_pairs
        ],
        "non_byok_evidence": category,
        "receipt_conflict_fields": sorted(conflicts),
        "source_references": sorted(
            {canonical_sha256(reference): reference for reference in entry.references}.values(),
            key=lambda reference: (
                nonnegative_int(reference.get("source_index")),
                nonnegative_int(reference.get("line")),
                str(reference.get("phase") or ""),
            ),
        ),
    }


def validate_runtime_environment(path: Path) -> tuple[dict[str, Any], str]:
    payload = load_json(path)
    if payload.get("schema") != RUNTIME_SCHEMA:
        raise FinalizationError("runtime environment schema differs")
    environment = payload.get("environment")
    fingerprint = str(payload.get("environment_sha256") or "")
    if (
        not isinstance(environment, dict)
        or not HEX64.fullmatch(fingerprint)
        or canonical_sha256(environment) != fingerprint
    ):
        raise FinalizationError("runtime environment fingerprint differs")
    return payload, fingerprint


def validate_lock(
    *,
    lock_file: Path,
    lock_fd: int,
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    path = require_regular_file(lock_file, owner_only=True)
    try:
        descriptor_stat = os.fstat(lock_fd)
    except OSError as exc:
        raise FinalizationError(f"lock fd {lock_fd} is not open") from exc
    path_stat = path.stat()
    if descriptor_stat.st_dev != path_stat.st_dev or descriptor_stat.st_ino != path_stat.st_ino:
        raise FinalizationError("lock fd does not reference --lock-file")
    # A separate open file description cannot acquire a shared lock while the
    # inherited fd owns an exclusive flock.  Merely acquiring the lock here
    # would not prove the before->run->after window was exclusive.
    probe = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise FinalizationError("lock file was not already held exclusively by the caller")
    finally:
        os.close(probe)
    expected_path = reconciliation.get("lock_file")
    if expected_path is None or Path(str(expected_path)).resolve() != path:
        raise FinalizationError("reconciliation lock_file does not match")
    expected_inode = reconciliation.get("lock_inode")
    if str(expected_inode) != str(path_stat.st_ino):
        raise FinalizationError("reconciliation lock_inode does not match")
    return {
        "lock_file": str(path),
        "lock_fd": lock_fd,
        "lock_inode": path_stat.st_ino,
        "lock_device": path_stat.st_dev,
        "exclusive_lock_held": True,
        "exclusive_lock_scope": "local_host_filesystem_only",
        "cross_host_exclusivity_proven": False,
    }


def validate_stable_observations(
    reconciliation: Mapping[str, Any],
    *,
    before_usage: Decimal,
    before_byok: Decimal,
    after_usage: Decimal,
    after_byok: Decimal,
) -> dict[str, Any]:
    def exact_int(field: str, expected: int | None = None) -> int:
        value = reconciliation.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise FinalizationError(f"reconciliation {field} must be an integer")
        if expected is not None and value != expected:
            raise FinalizationError(
                f"reconciliation {field} must equal the formal value {expected}"
            )
        return value

    required_stable_count = exact_int(
        "required_stable_poll_count",
        FORMAL_REQUIRED_STABLE_POLL_COUNT,
    )
    poll_interval_seconds = exact_int(
        "poll_interval_seconds",
        FORMAL_POLL_INTERVAL_SECONDS,
    )
    minimum_settlement_seconds = exact_int(
        "minimum_settlement_seconds",
        FORMAL_MINIMUM_SETTLEMENT_SECONDS,
    )
    minimum_stable_tail_seconds = exact_int(
        "minimum_stable_tail_seconds",
        FORMAL_MINIMUM_STABLE_TAIL_SECONDS,
    )
    observations = reconciliation.get("stable_observations")
    if not isinstance(observations, list) or len(observations) < required_stable_count:
        raise FinalizationError("reconciliation lacks the formal account observation count")
    poll_count = exact_int("poll_observation_count")
    if poll_count != len(observations):
        raise FinalizationError("reconciliation poll_observation_count differs from observations")
    stable_count = exact_int("stable_poll_count")
    if stable_count < required_stable_count or stable_count > len(observations):
        raise FinalizationError("reconciliation stable_poll_count is invalid")
    declared_tail_start = exact_int("stable_tail_start_index")
    normalized: list[tuple[Decimal, Decimal, datetime]] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise FinalizationError("stable account observation is not an object")
        normalized.append(
            (
                required_decimal(
                    observation.get("usage"),
                    label=f"stable observation {index} usage",
                ),
                required_decimal(
                    observation.get("byok_usage"),
                    label=f"stable observation {index} byok_usage",
                ),
                parse_iso(
                    observation.get("captured_at"),
                    label=f"stable observation {index} captured_at",
                ),
            )
        )
    if normalized[0][0] < before_usage:
        raise FinalizationError("account observation usage predates the before counter")
    if any(
        later[0] < earlier[0] for earlier, later in zip(normalized, normalized[1:], strict=False)
    ):
        raise FinalizationError("stable observation usage is not monotonic")
    if any(byok != before_byok for _, byok, _ in normalized):
        raise FinalizationError("stable observation BYOK usage changed")
    if normalized[-1][0] != after_usage or normalized[-1][1] != after_byok:
        raise FinalizationError("stable observation tail does not match account after")
    time_gaps = [
        (later[2] - earlier[2]).total_seconds()
        for earlier, later in zip(normalized, normalized[1:], strict=False)
    ]
    if any(gap < poll_interval_seconds for gap in time_gaps):
        raise FinalizationError("stable observation timestamps violate the formal poll interval")
    recomputed_stable_count = 0
    final_counters = normalized[-1][:2]
    for usage, byok, _ in reversed(normalized):
        if (usage, byok) != final_counters:
            break
        recomputed_stable_count += 1
    stable_tail_start = len(normalized) - recomputed_stable_count
    if stable_count != recomputed_stable_count or declared_tail_start != stable_tail_start:
        raise FinalizationError("reconciliation stable tail declaration differs from observations")
    observation_span = (normalized[-1][2] - normalized[0][2]).total_seconds()
    stable_tail_span = (normalized[-1][2] - normalized[stable_tail_start][2]).total_seconds()
    declared_observation_span = required_decimal(
        reconciliation.get("observation_span_seconds"),
        label="reconciliation observation_span_seconds",
    )
    declared_stable_tail_span = required_decimal(
        reconciliation.get("stable_tail_span_seconds"),
        label="reconciliation stable_tail_span_seconds",
    )
    if declared_observation_span != Decimal(str(observation_span)):
        raise FinalizationError(
            "reconciliation observation_span_seconds was not recomputed correctly"
        )
    if declared_stable_tail_span != Decimal(str(stable_tail_span)):
        raise FinalizationError(
            "reconciliation stable_tail_span_seconds was not recomputed correctly"
        )
    if observation_span < minimum_settlement_seconds:
        raise FinalizationError("formal account settlement window is too short")
    if stable_tail_span < minimum_stable_tail_seconds:
        raise FinalizationError("formal stable account tail is too short")
    return {
        "poll_observation_count": poll_count,
        "stable_poll_count": stable_count,
        "required_stable_poll_count": required_stable_count,
        "stable_tail_start_index": stable_tail_start,
        "poll_interval_seconds": poll_interval_seconds,
        "minimum_settlement_seconds": minimum_settlement_seconds,
        "minimum_stable_tail_seconds": minimum_stable_tail_seconds,
        "observation_span_seconds": str(declared_observation_span),
        "stable_tail_span_seconds": str(declared_stable_tail_span),
        "stable_usage_usd": str(after_usage),
        "stable_byok_usage_usd": str(after_byok),
        "first_observation_at": normalized[0][2].isoformat(),
        "last_stable_observation_at": normalized[-1][2].isoformat(),
    }


def selected_time_window(
    records: Sequence[SourceRecord],
) -> tuple[datetime, datetime]:
    starts: list[datetime] = []
    completions: list[datetime] = []
    for record in records:
        started = record.row.get("started_at")
        completed = record.row.get("completed_at")
        if finite_number(started):
            starts.append(datetime.fromtimestamp(float(started), tz=UTC))
        if finite_number(completed):
            completions.append(datetime.fromtimestamp(float(completed), tz=UTC))
    if not starts or not completions:
        raise FinalizationError("source rows lack numeric start/completion timestamps")
    return min(starts), max(completions)


def validate_prior_account_window(
    *,
    window_dir: Path,
    expected_key_fingerprint: str,
    current_before_time: datetime,
    current_before_usage: Decimal,
    current_before_byok: Decimal,
) -> dict[str, Any]:
    raw_window_dir = Path(window_dir)
    if raw_window_dir.is_symlink() or not raw_window_dir.is_dir():
        raise FinalizationError("prior account window must be a non-symlink directory")
    if raw_window_dir.stat().st_uid != os.getuid():
        raise FinalizationError("prior account window must be campaign-owned")
    window_dir = raw_window_dir.resolve(strict=True)
    before_path = window_dir / "openrouter-account-before.json"
    after_path = window_dir / "openrouter-account-after.json"
    reconciliation_path = window_dir / "openrouter-account-reconciliation.json"
    runtime_path = window_dir / "runtime-environment.json"
    before_path = require_regular_file(before_path, owner_only=True)
    after_path = require_regular_file(after_path, owner_only=True)
    reconciliation_path = require_regular_file(reconciliation_path, owner_only=True)
    runtime_path = require_regular_file(runtime_path, owner_only=True)
    before = load_json(before_path)
    after = load_json(after_path)
    reconciliation = load_json(reconciliation_path)
    _, runtime_fingerprint = validate_runtime_environment(runtime_path)
    if reconciliation.get("schema") != RECONCILIATION_SCHEMA:
        raise FinalizationError("prior account reconciliation schema differs")
    if reconciliation.get("settlement_status") != "stable":
        raise FinalizationError("prior account reconciliation is not stable")
    fingerprints = {
        normalize_key_fingerprint(before.get("api_key_sha256"), label="prior before key"),
        normalize_key_fingerprint(after.get("api_key_sha256"), label="prior after key"),
        normalize_key_fingerprint(
            reconciliation.get("api_key_sha256"),
            label="prior reconciliation key",
        ),
        expected_key_fingerprint,
    }
    if len(fingerprints) != 1:
        raise FinalizationError("prior and current account windows use different API keys")
    if (
        before.get("benchmark_environment_key_verified") is not True
        or after.get("benchmark_environment_key_verified") is not True
        or before.get("is_free_tier") is not False
        or after.get("is_free_tier") is not False
        or reconciliation.get("is_free_tier") is not False
    ):
        raise FinalizationError("prior account window is not bound to the verified paid key")

    before_usage = required_decimal(before.get("usage"), label="prior before usage")
    after_usage = required_decimal(after.get("usage"), label="prior after usage")
    before_byok = required_decimal(before.get("byok_usage"), label="prior before byok")
    after_byok = required_decimal(after.get("byok_usage"), label="prior after byok")
    if after_usage < before_usage or after_byok < before_byok:
        raise FinalizationError("prior account counters decreased")
    if after_usage > current_before_usage or after_byok > current_before_byok:
        raise FinalizationError("account counters decreased between prior and current windows")
    usage_delta = after_usage - before_usage
    byok_delta = after_byok - before_byok
    expected_values = {
        "usage_before_usd": before_usage,
        "usage_after_usd": after_usage,
        "usage_delta_usd": usage_delta,
        "byok_usage_before_usd": before_byok,
        "byok_usage_after_usd": after_byok,
        "byok_usage_delta_usd": byok_delta,
    }
    for field_name, expected in expected_values.items():
        actual = required_decimal(
            reconciliation.get(field_name),
            label=f"prior reconciliation {field_name}",
        )
        if actual != expected:
            raise FinalizationError(
                f"prior reconciliation {field_name} differs from account snapshots"
            )
    if byok_delta != Decimal(0):
        raise FinalizationError("prior account window BYOK delta is not exactly zero")
    if reconciliation.get("runtime_environment_sha256") != runtime_fingerprint:
        raise FinalizationError("prior reconciliation runtime fingerprint differs")
    if reconciliation.get("runtime_environment_file_sha256") != file_sha256(runtime_path):
        raise FinalizationError("prior reconciliation runtime file hash differs")
    stability = validate_stable_observations(
        reconciliation,
        before_usage=before_usage,
        before_byok=before_byok,
        after_usage=after_usage,
        after_byok=after_byok,
    )
    before_time = parse_iso(before.get("captured_at"), label="prior account before captured_at")
    after_time = parse_iso(after.get("captured_at"), label="prior account after captured_at")
    last_stable = parse_iso(
        stability["last_stable_observation_at"],
        label="prior last stable observation",
    )
    first_observation = parse_iso(
        stability["first_observation_at"],
        label="prior first stable observation",
    )
    if before_time > first_observation:
        raise FinalizationError(
            "prior account before snapshot does not precede settlement observations"
        )
    if after_time != last_stable:
        raise FinalizationError("prior account after differs from its stable reconciliation")
    if before_time >= after_time:
        raise FinalizationError("prior account window has a non-positive time span")
    if after_time > current_before_time:
        raise FinalizationError("prior and current account windows overlap")
    source_sha256 = {
        "account_before": file_sha256(before_path),
        "account_after": file_sha256(after_path),
        "account_reconciliation": file_sha256(reconciliation_path),
        "runtime_environment": file_sha256(runtime_path),
    }
    return {
        "kind": "prior_aborted",
        "admission_basis": "operator_supplied_unallocated_window",
        "path": str(window_dir),
        "usage_before_usd": str(before_usage),
        "usage_after_usd": str(after_usage),
        "usage_delta_usd": str(usage_delta),
        "byok_usage_before_usd": str(before_byok),
        "byok_usage_after_usd": str(after_byok),
        "byok_usage_delta_usd": str(byok_delta),
        "account_before_at": before_time.isoformat(),
        "account_after_at": after_time.isoformat(),
        "runtime_environment_sha256": runtime_fingerprint,
        "source_sha256": source_sha256,
        "sources": [
            {"path": str(before_path), "sha256": source_sha256["account_before"]},
            {"path": str(after_path), "sha256": source_sha256["account_after"]},
            {
                "path": str(reconciliation_path),
                "sha256": source_sha256["account_reconciliation"],
            },
            {"path": str(runtime_path), "sha256": source_sha256["runtime_environment"]},
        ],
        **stability,
    }


def validate_account_proof(
    *,
    before_path: Path,
    after_path: Path,
    reconciliation_path: Path,
    runtime_environment_path: Path,
    lock_file: Path,
    lock_fd: int,
    runtime_key_fingerprint: str,
    source_records: Sequence[SourceRecord],
    ledger_rows: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    prior_account_window_dirs: Sequence[Path] = (),
) -> dict[str, Any]:
    before_path = require_regular_file(before_path, owner_only=True)
    after_path = require_regular_file(after_path, owner_only=True)
    reconciliation_path = require_regular_file(reconciliation_path, owner_only=True)
    before = load_json(before_path)
    after = load_json(after_path)
    reconciliation = load_json(reconciliation_path)
    _, runtime_fingerprint = validate_runtime_environment(runtime_environment_path)
    if reconciliation.get("schema") != RECONCILIATION_SCHEMA:
        raise FinalizationError("account reconciliation schema differs")
    if reconciliation.get("settlement_status") != "stable":
        raise FinalizationError("account reconciliation is not stable")

    fingerprints = {
        normalize_key_fingerprint(before.get("api_key_sha256"), label="account before key"),
        normalize_key_fingerprint(after.get("api_key_sha256"), label="account after key"),
        normalize_key_fingerprint(
            reconciliation.get("api_key_sha256"),
            label="account reconciliation key",
        ),
        runtime_key_fingerprint,
    }
    if len(fingerprints) != 1:
        raise FinalizationError("runtime/account evidence uses different API keys")
    if (
        before.get("benchmark_environment_key_verified") is not True
        or after.get("benchmark_environment_key_verified") is not True
    ):
        raise FinalizationError("account snapshots do not bind the benchmark key")
    if (
        before.get("is_free_tier") is not False
        or after.get("is_free_tier") is not False
        or reconciliation.get("is_free_tier") is not False
    ):
        raise FinalizationError("formal account proof requires a paid key")

    before_usage = required_decimal(before.get("usage"), label="account before usage")
    after_usage = required_decimal(after.get("usage"), label="account after usage")
    before_byok = required_decimal(before.get("byok_usage"), label="account before byok_usage")
    after_byok = required_decimal(after.get("byok_usage"), label="account after byok_usage")
    if after_usage < before_usage or after_byok < before_byok:
        raise FinalizationError("account counters decreased")
    usage_delta = after_usage - before_usage
    byok_delta = after_byok - before_byok
    expected_values = {
        "usage_before_usd": before_usage,
        "usage_after_usd": after_usage,
        "usage_delta_usd": usage_delta,
        "byok_usage_before_usd": before_byok,
        "byok_usage_after_usd": after_byok,
        "byok_usage_delta_usd": byok_delta,
    }
    for field_name, expected in expected_values.items():
        actual = required_decimal(
            reconciliation.get(field_name), label=f"reconciliation {field_name}"
        )
        if actual != expected:
            raise FinalizationError(f"reconciliation {field_name} differs from account snapshots")
    if byok_delta != Decimal(0):
        raise FinalizationError("campaign account BYOK delta is not exactly zero")

    reconciliation_runtime = str(reconciliation.get("runtime_environment_sha256") or "")
    if reconciliation_runtime != runtime_fingerprint:
        raise FinalizationError("reconciliation runtime fingerprint differs")
    if reconciliation.get("runtime_environment_file_sha256") != file_sha256(
        require_regular_file(runtime_environment_path, owner_only=True)
    ):
        raise FinalizationError("reconciliation runtime file hash differs")
    lock = validate_lock(
        lock_file=lock_file,
        lock_fd=lock_fd,
        reconciliation=reconciliation,
    )
    stability = validate_stable_observations(
        reconciliation,
        before_usage=before_usage,
        before_byok=before_byok,
        after_usage=after_usage,
        after_byok=after_byok,
    )
    before_time = parse_iso(before.get("captured_at"), label="account before captured_at")
    after_time = parse_iso(after.get("captured_at"), label="account after captured_at")
    earliest_start, latest_completion = selected_time_window(source_records)
    last_stable = parse_iso(
        stability["last_stable_observation_at"],
        label="last stable observation",
    )
    first_observation = parse_iso(
        stability["first_observation_at"],
        label="first stable observation",
    )
    if before_time > first_observation:
        raise FinalizationError("account before snapshot does not precede settlement observations")
    if after_time != last_stable:
        raise FinalizationError("account after timestamp differs from the final stable observation")
    if before_time > earliest_start:
        raise FinalizationError("account before snapshot does not precede every source call")
    if after_time < latest_completion or last_stable < latest_completion:
        raise FinalizationError("account after evidence does not cover every source call")

    local_counts = Counter(str(row.get("non_byok_evidence") or "") for row in ledger_rows)
    explicit = local_counts["explicit_byok"]
    conflicts = local_counts["conflict"]
    if explicit or conflicts:
        raise FinalizationError(
            "explicit BYOK or contradictory provider evidence is fatal: "
            f"explicit={explicit}, conflict={conflicts}"
        )
    request_count = len(ledger_rows)
    exact = local_counts["exact"]
    unverified = local_counts["unverified"]
    if request_count <= 0 or exact + unverified != request_count:
        raise FinalizationError("campaign request evidence accounting is inconsistent")

    recorded_ledger_cost = required_decimal(
        ledger_summary.get("recorded_cost_usd"), label="ledger recorded cost"
    )
    exact_ledger_cost = required_decimal(
        ledger_summary.get("exact_cost_usd"), label="ledger exact cost"
    )
    tolerance = required_decimal(
        reconciliation.get("cost_reconciliation_tolerance_usd", "0.000001"),
        label="cost reconciliation tolerance",
    )
    if tolerance > Decimal("0.000001"):
        raise FinalizationError("cost reconciliation tolerance exceeds 0.000001 USD")
    if exact_ledger_cost > usage_delta + tolerance:
        raise FinalizationError(
            "exact physical receipt cost exceeds the settled account usage delta"
        )
    gap = usage_delta - recorded_ledger_cost
    unknown_cost_count = nonnegative_int(ledger_summary.get("unknown_cost_request_count"))
    non_exact_cost_count = nonnegative_int(ledger_summary.get("non_exact_cost_request_count"))
    if gap < -tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0:
        raise FinalizationError("physical receipt ledger exceeds the settled account usage delta")
    if abs(gap) > tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0:
        raise FinalizationError(f"unexplained OpenRouter account/ledger cost delta: {gap}")
    cost_reconciliation_status = (
        "exact"
        if abs(gap) <= tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0
        else "account_exact_per_request_incomplete"
    )
    campaign_attributable_exact = (
        cost_reconciliation_status == "exact" and exact == request_count and unverified == 0
    )
    attribution_precision = (
        "campaign-attributable-exact"
        if campaign_attributable_exact
        else "account_window_only_external-use-not-provable"
    )
    prior_windows = [
        validate_prior_account_window(
            window_dir=Path(window_dir),
            expected_key_fingerprint=next(iter(fingerprints)),
            current_before_time=before_time,
            current_before_usage=before_usage,
            current_before_byok=before_byok,
        )
        for window_dir in prior_account_window_dirs
    ]
    ordered_windows = sorted(
        prior_windows,
        key=lambda window: parse_iso(
            window["account_before_at"],
            label="prior account window account_before_at",
        ),
    )
    for earlier, later in zip(ordered_windows, ordered_windows[1:], strict=False):
        earlier_after = parse_iso(
            earlier["account_after_at"], label="prior account window account_after_at"
        )
        later_before = parse_iso(
            later["account_before_at"], label="prior account window account_before_at"
        )
        if earlier_after > later_before:
            raise FinalizationError("prior account windows overlap")
        if required_decimal(
            earlier["usage_after_usd"], label="prior account usage after"
        ) > required_decimal(later["usage_before_usd"], label="prior account usage before"):
            raise FinalizationError("account usage decreased between prior windows")
        if required_decimal(
            earlier["byok_usage_after_usd"], label="prior account BYOK usage after"
        ) > required_decimal(
            later["byok_usage_before_usd"], label="prior account BYOK usage before"
        ):
            raise FinalizationError("account BYOK usage decreased between prior windows")
    current_window = {
        "kind": "current",
        "path": str(before_path.parent.resolve()),
        "usage_before_usd": str(before_usage),
        "usage_after_usd": str(after_usage),
        "usage_delta_usd": str(usage_delta),
        "byok_usage_before_usd": str(before_byok),
        "byok_usage_after_usd": str(after_byok),
        "byok_usage_delta_usd": str(byok_delta),
        "account_before_at": before_time.isoformat(),
        "account_after_at": after_time.isoformat(),
        "runtime_environment_sha256": runtime_fingerprint,
        "source_sha256": {
            "account_before": file_sha256(before_path),
            "account_after": file_sha256(after_path),
            "account_reconciliation": file_sha256(reconciliation_path),
            "runtime_environment": file_sha256(
                require_regular_file(runtime_environment_path, owner_only=True)
            ),
        },
        **stability,
    }
    current_window["sources"] = [
        {"path": str(before_path), "sha256": current_window["source_sha256"]["account_before"]},
        {"path": str(after_path), "sha256": current_window["source_sha256"]["account_after"]},
        {
            "path": str(reconciliation_path),
            "sha256": current_window["source_sha256"]["account_reconciliation"],
        },
        {
            "path": str(Path(runtime_environment_path).resolve()),
            "sha256": current_window["source_sha256"]["runtime_environment"],
        },
    ]
    account_windows = [*ordered_windows, current_window]
    aborted_total = sum(
        (
            required_decimal(window["usage_delta_usd"], label="prior account usage delta")
            for window in ordered_windows
        ),
        Decimal(0),
    )
    account_window_total = aborted_total + usage_delta
    current_window_campaign_attributable_exact = campaign_attributable_exact
    if aborted_total > 0:
        campaign_attributable_exact = False
        attribution_precision = "multi-window-counter-exact-campaign-attribution-unproven"
    source_hashes = {
        "account_before": file_sha256(before_path),
        "account_after": file_sha256(after_path),
        "account_reconciliation": file_sha256(reconciliation_path),
        "runtime_environment": file_sha256(
            require_regular_file(runtime_environment_path, owner_only=True)
        ),
    }
    proof = {
        "schema": PROOF_SCHEMA,
        "pass": True,
        "created_at": utc_now(),
        "policy": (
            "explicit BYOK/provider conflicts are fatal; locally unverified physical "
            "requests are covered only for non-BYOK policy by a paid, same-key "
            "account-window proof with an exact zero Decimal BYOK delta; the local "
            "flock cannot prove that another host did not use the key"
        ),
        "api_key_sha256": next(iter(fingerprints)),
        "runtime_environment_sha256": runtime_fingerprint,
        "account_windows": account_windows,
        "account_window_total_usd": str(account_window_total),
        "unallocated_aborted_window_usd": str(aborted_total),
        "result_row_account_window_scope": "current_window_only",
        "account": {
            "usage_before_usd": str(before_usage),
            "usage_after_usd": str(after_usage),
            "usage_delta_usd": str(usage_delta),
            "byok_usage_before_usd": str(before_byok),
            "byok_usage_after_usd": str(after_byok),
            "byok_usage_delta_usd": str(byok_delta),
            "is_free_tier": False,
        },
        "window": {
            "account_before_at": before_time.isoformat(),
            "earliest_source_started_at": earliest_start.isoformat(),
            "latest_source_completed_at": latest_completion.isoformat(),
            "account_after_at": after_time.isoformat(),
            **stability,
            **lock,
        },
        "local_physical_request_evidence": {
            "request_count": request_count,
            "exact_non_byok_request_count": exact,
            "unverified_request_count": unverified,
            "explicit_byok_request_count": explicit,
            "conflict_request_count": conflicts,
            "campaign_covered_unverified_request_count": unverified,
            "resolved_request_count": exact + unverified,
        },
        "cost_scope": {
            "ledger_recorded_cost_usd": ledger_summary.get("recorded_cost_usd"),
            "ledger_exact_cost_usd": ledger_summary.get("exact_cost_usd"),
            "account_usage_delta_usd": str(usage_delta),
            "account_window_delta_usd": str(usage_delta),
            "account_windows": account_windows,
            "account_window_total_usd": str(account_window_total),
            "unallocated_aborted_window_usd": str(aborted_total),
            "reconciliation_gap_usd": str(gap),
            "reconciliation_tolerance_usd": str(tolerance),
            "reconciliation_status": cost_reconciliation_status,
            "unknown_cost_request_count": unknown_cost_count,
            "non_exact_cost_request_count": non_exact_cost_count,
            "attribution_precision": attribution_precision,
            "campaign_attributable_exact": campaign_attributable_exact,
            "current_window_campaign_attributable_exact": (
                current_window_campaign_attributable_exact
            ),
            "campaign_attributable_cost_usd": (
                str(usage_delta) if campaign_attributable_exact else None
            ),
            "account_total_precision": (
                attribution_precision
                if aborted_total > 0
                else (
                    "campaign-attributable-exact"
                    if campaign_attributable_exact
                    else "window-counter-exact-campaign-attribution-unproven"
                )
            ),
            "per_request_precision": (
                "exact" if cost_reconciliation_status == "exact" else "mixed_or_incomplete"
            ),
            "judge_included": True,
            "brave_external_cost_separate": True,
            "task_allocation_policy": ("account delta is not allocated to individual tasks"),
            "note": (
                "Account proof establishes non-BYOK for locally unverified requests; "
                "it does not convert missing or estimated per-request costs to exact. "
                "When any physical request is non-exact or unknown, the account "
                "delta is only a shared-key window delta because cross-host external "
                "use cannot be proven absent. "
                "Brave spend remains separate from the OpenRouter LLM ledger."
            ),
        },
        "source_sha256": source_hashes,
    }
    proof["proof_sha256"] = canonical_sha256(proof, prefix=True)
    return proof


def trace_row_from_result(row: Mapping[str, Any]) -> dict[str, Any]:
    trace = {
        "trace_schema": RESULT_EVIDENCE_SCHEMA,
        RESULT_EVIDENCE_SHA256_FIELD: row.get(RESULT_EVIDENCE_SHA256_FIELD),
    }
    for field_name in TRACE_FIELDS:
        if field_name in {
            "tool_policy",
            "generation_policy",
            "generation_config",
            "routing_trace",
            "server_tool_use",
            "execution",
            "usage",
            "cost_accounting",
            "openrouter_non_byok_audit",
            "openrouter_non_byok_resolution",
            "run_trace",
            "ensemble_trace",
        }:
            trace[field_name] = row.get(field_name) or {}
        elif field_name == "generation_retry_reasons":
            trace[field_name] = row.get(field_name) or []
        elif field_name != "fusion_delta":
            trace[field_name] = row.get(field_name)
    judge = row.get("judge")
    if isinstance(judge, Mapping):
        trace["judge"] = {
            "mode": judge.get("mode"),
            "score_status": judge.get("score_status"),
            "quality_total": row.get("quality_total"),
            "pass_rate": judge.get("pass_rate"),
            "valid_pass_rate": judge.get("valid_pass_rate"),
            "judge_error_count": judge.get("judge_error_count"),
            "criteria_count": judge.get("criteria_count"),
            "valid_criteria_count": judge.get("valid_criteria_count"),
            "invalid_criteria_count": judge.get("invalid_criteria_count"),
        }
    else:
        trace["judge"] = {}
    trace["candidate_judge_count"] = len(row.get("candidate_judges") or [])
    trace["fusion_delta"] = row.get("fusion_delta")
    return trace


def selected_generation_cost(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a value plus its explicit per-request cost precision."""

    accounting = row.get("cost_accounting")
    generation = (
        accounting.get("selected_generation_attempt") or accounting.get("generation")
        if isinstance(accounting, Mapping)
        else None
    )
    if isinstance(generation, Mapping) and generation.get("recorded_cost_usd") is not None:
        value = required_decimal(
            generation.get("recorded_cost_usd"), label="selected generation cost"
        )
        exact = generation.get("cost_exact") is True
        complete = generation.get("cost_complete") is True
        declared = str(
            generation.get("cost_precision") or generation.get("cost_status") or ""
        ).casefold()
        precision = "exact" if exact else "estimated" if "estimate" in declared else "recorded"
        return {
            "value": value,
            "precision": precision,
            "complete": complete,
            "exact": exact,
        }
    usage = row.get("usage")
    if isinstance(usage, Mapping):
        value = usage.get("billed_cost")
        if value is not None:
            parsed = required_decimal(value, label="selected usage cost")
            units = usage_units(usage)
            exact = bool(units) and all(unit_cost_is_exact(unit) for unit in units)
            return {
                "value": parsed,
                "precision": "exact" if exact else "recorded",
                "complete": exact,
                "exact": exact,
            }
    return {
        "value": None,
        "precision": "unknown",
        "complete": False,
        "exact": False,
    }


def percentile_nearest(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1),
    )
    return ordered[index]


def row_pass_rate(row: Mapping[str, Any]) -> Decimal:
    judge = row.get("judge")
    criteria = judge.get("criterion_judgments") if isinstance(judge, Mapping) else None
    if not isinstance(criteria, list) or not criteria:
        return Decimal(0)
    valid = [item for item in criteria if isinstance(item, Mapping)]
    if not valid:
        return Decimal(0)
    passed = sum(
        (
            item.get("met") is True
            if Decimal(str(item.get("weight") or 0)) >= 0
            else item.get("met") is False
        )
        for item in valid
    )
    return Decimal(passed) / Decimal(len(valid))


def selected_generation_costs_from_ledger(
    rows: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    expected = {f"{row.get('group')}/{row.get('task_id')}": row for row in rows}
    by_pair: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for ledger_row in ledger_rows:
        if ledger_row.get("generation_disposition") != "selected":
            continue
        pairs = ledger_row.get("group_task_pairs")
        if not isinstance(pairs, list) or len(pairs) != 1:
            raise FinalizationError("selected physical generation request is not owned by one pair")
        pair = pairs[0]
        if not isinstance(pair, Mapping):
            raise FinalizationError("selected generation pair evidence is invalid")
        key = f"{pair.get('group')}/{pair.get('task_id')}"
        if key not in expected:
            raise FinalizationError(f"selected ledger request belongs to an unexpected pair: {key}")
        by_pair[key].append(ledger_row)
    missing = sorted(set(expected) - set(by_pair))
    if missing:
        raise FinalizationError(f"selected generation ledger misses pair(s): {missing[:5]}")
    summaries: dict[str, dict[str, Any]] = {}
    for key, row in expected.items():
        physical = by_pair[key]
        known = [
            required_decimal(
                item.get("recorded_cost_usd"),
                label=f"{key} selected ledger cost",
            )
            for item in physical
            if item.get("recorded_cost_usd") is not None
        ]
        precision_counts = Counter(
            str(item.get("cost_precision") or "unknown") for item in physical
        )
        complete = len(known) == len(physical)
        exact = complete and precision_counts["exact"] == len(physical)
        lower_bound = sum(known, Decimal(0))
        value = lower_bound if complete else None
        summary = {
            "value": value,
            "recorded_cost_usd": str(value) if value is not None else None,
            "recorded_cost_usd_lower_bound": str(lower_bound),
            "request_count": len(physical),
            "known_cost_request_count": len(known),
            "unknown_cost_request_count": len(physical) - len(known),
            "precision_counts": dict(sorted(precision_counts.items())),
            "precision": (
                "exact" if exact else "recorded_or_estimated" if complete else "partial_or_unknown"
            ),
            "complete": complete,
            "exact": exact,
        }
        declared = selected_generation_cost(row)
        if (
            value is not None
            and isinstance(declared.get("value"), Decimal)
            and declared["value"].quantize(Decimal("0.000000001"))
            != value.quantize(Decimal("0.000000001"))
        ):
            raise FinalizationError(f"{key} selected generation row cost conflicts with ledger")
        if declared.get("exact") is True and not exact:
            raise FinalizationError(f"{key} selected generation row falsely declares exact cost")
        summaries[key] = summary
    return summaries, {
        "pair_count": len(summaries),
        "complete_pair_count": sum(value["complete"] is True for value in summaries.values()),
        "exact_pair_count": sum(value["exact"] is True for value in summaries.values()),
        "unknown_is_zero": False,
        "pairs": {
            key: {field: value for field, value in summary.items() if field != "value"}
            for key, summary in sorted(summaries.items())
        },
    }


def group_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_costs_by_pair: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("group") or "")].append(row)
    metrics: list[dict[str, Any]] = []
    for group in GROUPS:
        values = grouped[group]
        qualities = [Decimal(str(row["quality_total"])) for row in values]
        pass_rates = [row_pass_rate(row) for row in values]
        selected_costs = [
            dict(
                (selected_costs_by_pair or {}).get(
                    f"{row.get('group')}/{row.get('task_id')}",
                    selected_generation_cost(row),
                )
            )
            for row in values
        ]
        covered_costs = [
            item["value"] for item in selected_costs if isinstance(item.get("value"), Decimal)
        ]
        covered_count = len(covered_costs)
        exact_count = sum(item["exact"] is True for item in selected_costs)
        complete_count = sum(item["complete"] is True for item in selected_costs)
        all_covered = covered_count == len(values)
        all_exact = exact_count == len(values)
        cost_precision = (
            "exact" if all_exact else "mixed_or_estimated" if all_covered else "partial_or_unknown"
        )
        covered_total = sum(covered_costs, Decimal(0))
        lower_bound_total = sum(
            (
                required_decimal(
                    item.get("recorded_cost_usd_lower_bound"),
                    label="selected generation cost lower bound",
                )
                if item.get("recorded_cost_usd_lower_bound") is not None
                else item["value"]
                if isinstance(item.get("value"), Decimal)
                else Decimal(0)
            )
            for item in selected_costs
        )
        token_totals: Counter[str] = Counter()
        visible_exact = True
        tool_counts: list[int] = []
        step_counts: list[int] = []
        request_counts: list[int] = []
        latencies_ms: list[float] = []
        iteration_counts: list[int] = []
        judge_errors = 0
        for row in values:
            units = usage_units(row.get("usage"))
            for unit in units:
                input_tokens = nonnegative_int(unit.get("input_tokens"))
                output_tokens = nonnegative_int(unit.get("output_tokens"))
                reasoning_tokens = nonnegative_int(unit.get("reasoning_tokens"))
                cached_tokens = max(
                    nonnegative_int(unit.get("cached_tokens")),
                    nonnegative_int(unit.get("cache_read_tokens")),
                )
                token_totals["input"] += input_tokens
                token_totals["output"] += output_tokens
                token_totals["reasoning"] += reasoning_tokens
                token_totals["cached"] += cached_tokens
                if reasoning_tokens > output_tokens:
                    visible_exact = False
                else:
                    token_totals["visible"] += output_tokens - reasoning_tokens
            tool_counts.append(
                max(
                    nonnegative_int(row.get("total_tool_call_count")),
                    nonnegative_int(row.get("stream_tool_call_count"))
                    + nonnegative_int(row.get("server_tool_call_count")),
                )
            )
            step_counts.append(nonnegative_int(row.get("trajectory_steps")))
            request_counts.append(nonnegative_int(row.get("llm_request_count")))
            started = row.get("started_at")
            completed = row.get("completed_at")
            if finite_number(started) and finite_number(completed):
                latencies_ms.append(max(0.0, (float(completed) - float(started)) * 1000.0))
            trace = row.get("ensemble_trace")
            iteration_counts.append(
                nonnegative_int(
                    trace.get("agent_llm_call_count") or trace.get("agent_iterations")
                    if isinstance(trace, Mapping)
                    else 0
                )
            )
            judge = row.get("judge")
            if isinstance(judge, Mapping):
                judge_errors += nonnegative_int(judge.get("judge_error_count"))
        denominator = Decimal(len(values))
        metrics.append(
            {
                "group": group,
                "task_count": len(values),
                "done_count": len(values),
                "avg_quality_total": str(sum(qualities, Decimal(0)) / denominator),
                "avg_pass_rate": str(sum(pass_rates, Decimal(0)) / denominator),
                "judge_error_count": judge_errors,
                "avg_selected_generation_cost_usd": (
                    str(covered_total / Decimal(len(values))) if all_covered else None
                ),
                "covered_avg_selected_generation_cost_usd": (
                    str(covered_total / Decimal(covered_count)) if covered_count else None
                ),
                "selected_generation_cost_usd": (str(covered_total) if all_covered else None),
                "selected_generation_cost_usd_lower_bound": str(lower_bound_total),
                "selected_generation_cost_covered_task_count": covered_count,
                "selected_generation_cost_exact_task_count": exact_count,
                "selected_generation_cost_complete_task_count": complete_count,
                "selected_generation_cost_complete": complete_count == len(values),
                "selected_generation_cost_precision": cost_precision,
                "avg_llm_requests": str(Decimal(sum(request_counts)) / denominator),
                "avg_input_tokens": str(Decimal(token_totals["input"]) / denominator),
                "avg_output_tokens": str(Decimal(token_totals["output"]) / denominator),
                "avg_reasoning_tokens": str(Decimal(token_totals["reasoning"]) / denominator),
                "avg_cached_tokens": str(Decimal(token_totals["cached"]) / denominator),
                "avg_visible_tokens": (
                    str(Decimal(token_totals["visible"]) / denominator) if visible_exact else None
                ),
                "visible_tokens_exact": visible_exact,
                "avg_total_tokens": str(
                    Decimal(token_totals["input"] + token_totals["output"]) / denominator
                ),
                "avg_tool_calls": str(Decimal(sum(tool_counts)) / denominator),
                "tool_task_rate": str(
                    Decimal(sum(count > 0 for count in tool_counts)) / denominator
                ),
                "avg_trajectory_steps": str(Decimal(sum(step_counts)) / denominator),
                "avg_selected_agent_iterations": str(Decimal(sum(iteration_counts)) / denominator),
                "latency_p50_ms": str(percentile_nearest(latencies_ms, 0.50)),
                "latency_p95_ms": str(percentile_nearest(latencies_ms, 0.95)),
            }
        )
    return metrics


def finalize_rows(
    selected: Sequence[SourceRecord],
    *,
    tasks: Sequence[Mapping[str, Any]],
    proof: Mapping[str, Any],
    pair_audit: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selected_by_key = {record.key: record for record in selected}
    proof_sha = str(proof.get("proof_sha256") or "")
    final_rows: list[dict[str, Any]] = []
    row_index = 0
    for task in tasks:
        task_id = str(task["id"])
        for group in GROUPS:
            row_index += 1
            record = selected_by_key[(group, task_id)]
            original = record.row
            row = copy.deepcopy(original)
            generation_contract_before = {
                "final_text": original.get("final_text"),
                "usage": original.get("usage"),
                "execution": original.get("execution"),
                "generation_attempt_count": original.get("generation_attempt_count"),
                "generation_attempt_budget_used": original.get("generation_attempt_budget_used"),
                "generation_attempt_total_billed_cost": original.get(
                    "generation_attempt_total_billed_cost"
                ),
                "cost_accounting": original.get("cost_accounting"),
            }
            local_audit = original.get("openrouter_non_byok_audit")
            local_status = (
                str(local_audit.get("status") or "")
                if isinstance(local_audit, Mapping)
                else "unverified"
            )
            row["row_index"] = row_index
            row["error"] = None
            row["openrouter_non_byok_resolution"] = {
                "schema": RESOLUTION_SCHEMA,
                "status": (
                    "local_exact"
                    if local_status == "exact"
                    else "resolved_by_campaign_account_proof"
                ),
                "local_audit_status": local_status,
                "campaign_proof_path": "openrouter-non-byok-campaign-proof.json",
                "campaign_proof_sha256": proof_sha,
                "campaign_proof_pass": True,
                "cost_precision_unchanged": True,
            }
            cost_accounting = row.get("cost_accounting")
            llm_complete = (
                bool(cost_accounting.get("actual_llm_cost_complete"))
                if isinstance(cost_accounting, Mapping)
                else False
            )
            row["completion_status"] = {
                "generation_accepted": True,
                "judge_complete": True,
                "cost_metadata_complete": llm_complete,
                "cost_metadata_scope": "actual_llm_spend",
                "openrouter_non_byok_resolved": True,
                "status": "complete",
                "incomplete_reasons": [],
            }
            row["campaign_finalization"] = {
                "schema": MANIFEST_SCHEMA,
                "selected_source": record.reference,
                "selection": pair_audit[f"{group}/{task_id}"],
                "finalizer_version": FINALIZER_VERSION,
            }
            generation_contract_after = {
                "final_text": row.get("final_text"),
                "usage": row.get("usage"),
                "execution": row.get("execution"),
                "generation_attempt_count": row.get("generation_attempt_count"),
                "generation_attempt_budget_used": row.get("generation_attempt_budget_used"),
                "generation_attempt_total_billed_cost": row.get(
                    "generation_attempt_total_billed_cost"
                ),
                "cost_accounting": row.get("cost_accounting"),
            }
            if generation_contract_after != generation_contract_before:
                raise FinalizationError("finalization mutated generation/cost contracts")
            final_rows.append(seal_result_row(row))
    return final_rows


def artifact_record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.name,
        "sha256": file_sha256(path),
        "size_bytes": stat.st_size,
        "mode": oct(stat.st_mode & 0o777),
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def paired_quality_comparisons(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 20_000,
) -> list[dict[str, Any]]:
    by_group: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in rows:
        by_group[str(row.get("group") or "")][str(row.get("task_id") or "")] = Decimal(
            str(row.get("quality_total"))
        )
    comparisons: list[dict[str, Any]] = []
    for group in GROUPS:
        for baseline in ("B0", "B1"):
            common = sorted(set(by_group[group]) & set(by_group[baseline]))
            differences = [
                by_group[group][task_id] - by_group[baseline][task_id] for task_id in common
            ]
            if not differences:
                continue
            mean = sum(differences, Decimal(0)) / Decimal(len(differences))
            rng = random.Random(f"draco:{group}:{baseline}")
            bootstrap = sorted(
                sum(
                    (differences[rng.randrange(len(differences))] for _ in differences),
                    Decimal(0),
                )
                / Decimal(len(differences))
                for _ in range(bootstrap_samples)
            )
            low = bootstrap[int(0.025 * (bootstrap_samples - 1))]
            high = bootstrap[int(0.975 * (bootstrap_samples - 1))]
            comparisons.append(
                {
                    "group": group,
                    "baseline": baseline,
                    "pair_count": len(common),
                    "delta_quality": str(mean),
                    "ci95_low": str(low),
                    "ci95_high": str(high),
                    "wins": sum(value > 0 for value in differences),
                    "ties": sum(value == 0 for value in differences),
                    "losses": sum(value < 0 for value in differences),
                    "bootstrap_samples": bootstrap_samples,
                    "seed": f"draco:{group}:{baseline}",
                }
            )
    return comparisons


def rubric_section_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[Decimal, Decimal, int]]] = defaultdict(list)
    for row in rows:
        group = str(row.get("group") or "")
        judge = row.get("judge")
        judgments = judge.get("criterion_judgments") if isinstance(judge, Mapping) else None
        per_section: dict[tuple[str, str], dict[str, Decimal | int]] = defaultdict(
            lambda: {
                "raw_score": Decimal(0),
                "positive_weight_total": Decimal(0),
                "passed_count": 0,
                "criterion_count": 0,
            }
        )
        for judgment in judgments or []:
            if not isinstance(judgment, Mapping):
                continue
            section_id = str(judgment.get("section_id") or "rubric")
            title = str(judgment.get("section_title") or section_id)
            bucket = per_section[(section_id, title)]
            weight = Decimal(str(judgment.get("weight") or 0))
            met = judgment.get("met")
            bucket["criterion_count"] += 1
            bucket["positive_weight_total"] += max(Decimal(0), weight)
            if met is True:
                bucket["raw_score"] += weight
            if (weight >= 0 and met is True) or (weight < 0 and met is False):
                bucket["passed_count"] += 1
        for (section_id, title), bucket in per_section.items():
            positive_total = Decimal(bucket["positive_weight_total"])
            count = int(bucket["criterion_count"])
            raw_score = Decimal(bucket["raw_score"])
            normalized = (
                max(
                    Decimal(0),
                    min(
                        Decimal(100),
                        raw_score / positive_total * Decimal(100),
                    ),
                )
                if positive_total > 0
                else Decimal(0)
            )
            pass_rate = (
                Decimal(int(bucket["passed_count"])) / Decimal(count) if count else Decimal(0)
            )
            grouped[(group, section_id, title)].append((normalized, pass_rate, count))
    metrics: list[dict[str, Any]] = []
    for (group, section_id, title), row_values in sorted(grouped.items()):
        denominator = Decimal(len(row_values))
        metrics.append(
            {
                "group": group,
                "section_id": section_id,
                "section_title": title,
                "task_count": len(row_values),
                "criterion_repeat_count": sum(count for _, _, count in row_values),
                "avg_normalized_score": str(
                    sum(
                        (value for value, _, _ in row_values),
                        Decimal(0),
                    )
                    / denominator
                ),
                "pass_rate": str(
                    sum(
                        (value for _, value, _ in row_values),
                        Decimal(0),
                    )
                    / denominator
                ),
            }
        )
    return metrics


def repair_action_details(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    descriptions = {
        "judge_only": "复用已接受 generation，仅补跑并完成 Judge。",
        "metadata_only": "复用 generation/Judge，仅回填并核验 provider、费用或回执元数据。",
    }
    for row in rows:
        execution = row.get("execution")
        completion = row.get("resume_completion")
        action = str(
            (completion.get("action") if isinstance(completion, Mapping) else None)
            or (execution.get("resume_action") if isinstance(execution, Mapping) else None)
            or ""
        )
        if action not in descriptions:
            continue
        details.append(
            {
                "group": str(row.get("group") or ""),
                "task_id": str(row.get("task_id") or ""),
                "action": action,
                "generation_reused": bool(
                    isinstance(execution, Mapping) and execution.get("generation_reused") is True
                ),
                "judge_reran": bool(
                    isinstance(execution, Mapping) and execution.get("judge_reran") is True
                ),
                "metadata_repaired": bool(
                    isinstance(execution, Mapping) and execution.get("metadata_repaired") is True
                ),
                "detail": descriptions[action],
            }
        )
    return details


def experiment_results_markdown(
    *,
    task_count: int,
    final_rows: Sequence[Mapping[str, Any]],
    metrics: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    external_tool_cost: Mapping[str, Any],
    proof: Mapping[str, Any],
    comparisons: Sequence[Mapping[str, Any]],
    model_metrics: Sequence[Mapping[str, Any]],
    rubric_metrics: Sequence[Mapping[str, Any]],
    repair_details: Sequence[Mapping[str, Any]],
) -> str:
    account = proof["account"]
    evidence = proof["local_physical_request_evidence"]
    cost_scope = proof["cost_scope"]
    scope_costs = ledger_summary["scope_recorded_cost_usd"]
    judge_scope_cost = sum(
        (Decimal(value) for key, value in scope_costs.items() if "judge" in key),
        Decimal(0),
    )
    disposition_costs = ledger_summary["generation_disposition_recorded_cost_usd"]
    lines = [
        "# DRACO Mini B0/B1/B2/B4/G1 实验结果",
        "",
        "## 实验结论",
        "",
        f"- 严格完成 {len(final_rows)}/{task_count * len(GROUPS)} 个 "
        "group×task；无缺失、无重复、无失败。",
        "- 质量、成本、Token、工具、Agent Loop、路由、Judge 与修复证据"
        "均由最终 JSONL 和全 campaign 物理请求账本离线重建。",
        "",
        "## 实验配置",
        "",
        f"- 任务集：DRACO Mini（{task_count} 题）",
        "- 实验组：B0、B1、B2、B4、G1",
        "- 每个 `(group, task)` 最多 3 次 generation attempt",
        "- 结果选择：最后一个严格有效 generation，并采用该 generation 的最新兼容修复行",
        "- Judge：必须 `score_status=complete`、无 Judge error、存在 `quality_total`",
        "- B2/G1：proposer 至少达到 `ceil(2N/3)`，最终答案必须由 aggregator 请求绑定",
        "",
        "| Group | Kind | Declared model / selection mode |",
        "|---|---|---|",
        f"| B0 | single | `{B0_MODEL}` |",
        "| B1 | router_single | frozen `c0/c1/c2/c3` tiers |",
        "| B2 | selection_mode | `static_openrouter_b5` |",
        f"| B4 | single | `{B4_MODEL}` |",
        "| G1 | selection_mode | frozen-registry `router_dynamic` |",
        "",
        "## 覆盖与完整性",
        "",
        f"- 最终结果：{len(final_rows)}/{task_count * len(GROUPS)}",
        "- 每组任务数："
        + "、".join(
            f"{group}={sum(1 for row in final_rows if row.get('group') == group)}"
            for group in GROUPS
        ),
        "- 最终 JSONL 无缺失 pair、无重复 pair；每行已重新 seal，trace 由最终行确定性重建。",
        "",
        "## 分组指标",
        "",
        "| Group | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | "
        "Total Gen$ | Gen exact | Avg Prompt | Avg Completion | Avg Reason | "
        "Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | "
        "p50 ms | p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        "---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in metrics:
        raw_cost = metric["avg_selected_generation_cost_usd"]
        rendered_cost = (
            str(Decimal(str(raw_cost)).quantize(Decimal("0.000001")))
            if raw_cost is not None
            else "N/A"
        )
        total_cost = metric["selected_generation_cost_usd"]
        rendered_total = (
            str(Decimal(str(total_cost)).quantize(Decimal("0.000001")))
            if total_cost is not None
            else "N/A"
        )
        visible = metric["avg_visible_tokens"] or "N/A"
        lines.append(
            "| {group} | {task_count} | {done} | {quality} | {pass_rate}% | "
            "{judge_error} | {cost} | {total_cost} | {exact}/{tasks} | "
            "{prompt} | {completion} | {reason} | {visible} | {tokens} | "
            "{tools} | {tool_rate}% | {steps} | {requests} | {p50} | {p95} |".format(
                group=metric["group"],
                task_count=metric["task_count"],
                done=metric["done_count"],
                quality=Decimal(str(metric["avg_quality_total"])).quantize(Decimal("0.0001")),
                pass_rate=(Decimal(str(metric["avg_pass_rate"])) * Decimal(100)).quantize(
                    Decimal("0.01")
                ),
                judge_error=metric["judge_error_count"],
                cost=rendered_cost,
                total_cost=rendered_total,
                exact=metric["selected_generation_cost_exact_task_count"],
                tasks=metric["task_count"],
                prompt=Decimal(str(metric["avg_input_tokens"])).quantize(Decimal("0.1")),
                completion=Decimal(str(metric["avg_output_tokens"])).quantize(Decimal("0.1")),
                reason=Decimal(str(metric["avg_reasoning_tokens"])).quantize(Decimal("0.1")),
                visible=(
                    Decimal(str(visible)).quantize(Decimal("0.1")) if visible != "N/A" else visible
                ),
                tokens=Decimal(str(metric["avg_total_tokens"])).quantize(Decimal("0.1")),
                tools=Decimal(str(metric["avg_tool_calls"])).quantize(Decimal("0.1")),
                tool_rate=(Decimal(str(metric["tool_task_rate"])) * Decimal(100)).quantize(
                    Decimal("0.01")
                ),
                steps=Decimal(str(metric["avg_trajectory_steps"])).quantize(Decimal("0.1")),
                requests=Decimal(str(metric["avg_llm_requests"])).quantize(Decimal("0.01")),
                p50=Decimal(str(metric["latency_p50_ms"])).quantize(Decimal("1")),
                p95=Decimal(str(metric["latency_p95_ms"])).quantize(Decimal("1")),
            )
        )
    section_titles = sorted(
        {
            str(metric.get("section_title") or metric.get("section_id") or "")
            for metric in rubric_metrics
        }
    )
    lines.extend(
        [
            "",
            "## Rubric 分项平均",
            "",
            "| Group | " + " | ".join(section_titles) + " |",
            "|---|" + "---:|" * len(section_titles),
        ]
    )
    rubric_by_key = {
        (
            str(metric.get("group") or ""),
            str(metric.get("section_title") or metric.get("section_id") or ""),
        ): metric
        for metric in rubric_metrics
    }
    for group in GROUPS:
        values = [
            Decimal(str(rubric_by_key[(group, title)]["avg_normalized_score"])).quantize(
                Decimal("0.01")
            )
            if (group, title) in rubric_by_key
            else "N/A"
            for title in section_titles
        ]
        lines.append(f"| {group} | " + " | ".join(str(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "## 修复动作明细",
            "",
            "| Group | Task | Action | Generation reused | Judge reran | "
            "Metadata repaired | 说明 |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    if repair_details:
        for detail in repair_details:
            lines.append(
                "| {group} | `{task}` | `{action}` | {generation} | {judge} | "
                "{metadata} | {description} |".format(
                    group=detail["group"],
                    task=str(detail["task_id"])[:12],
                    action=detail["action"],
                    generation="是" if detail["generation_reused"] else "否",
                    judge="是" if detail["judge_reran"] else "否",
                    metadata="是" if detail["metadata_repaired"] else "否",
                    description=detail["detail"],
                )
            )
    else:
        lines.append("| — | — | `direct` | 否 | 否 | 否 | 无后处理修复。 |")
    lines.extend(["", "### 同题 Domain 矩阵", ""])
    lines.append("| Domain | Task | " + " | ".join(GROUPS) + " |")
    lines.append("|---|---|" + "---:|" * len(GROUPS))
    by_task_group = {
        (str(row.get("task_id") or ""), str(row.get("group") or "")): row for row in final_rows
    }
    for task_id in sorted({key[0] for key in by_task_group}):
        exemplar = next(
            by_task_group[(task_id, group)] for group in GROUPS if (task_id, group) in by_task_group
        )
        domain = str(exemplar.get("domain") or "Unknown")
        values = [
            Decimal(str(by_task_group[(task_id, group)]["quality_total"])).quantize(Decimal("0.01"))
            for group in GROUPS
        ]
        lines.append(
            f"| {domain} | `{task_id[:8]}…` | " + " | ".join(str(value) for value in values) + " |"
        )
    lines.extend(
        [
            "",
            "## 同题配对比较",
            "",
            "| Group | Baseline | Pairs | ΔQ (95% CI) | W/T/L |",
            "|---|---|---:|---|---|",
        ]
    )
    for comparison in comparisons:
        lines.append(
            "| {group} | {baseline} | {pairs} | {delta:+.2f} "
            "[{low:+.2f}, {high:+.2f}] | {wins}/{ties}/{losses} |".format(
                group=comparison["group"],
                baseline=comparison["baseline"],
                pairs=comparison["pair_count"],
                delta=float(comparison["delta_quality"]),
                low=float(comparison["ci95_low"]),
                high=float(comparison["ci95_high"]),
                wins=comparison["wins"],
                ties=comparison["ties"],
                losses=comparison["losses"],
            )
        )
    lines.extend(
        [
            "",
            "- CI 使用固定种子、20,000 次 paired bootstrap percentile 95% CI；"
            "W/T/L 为同题逐题比较。",
            "",
            "## Agent Loop 执行证据",
            "",
            "| Group | Avg selected iterations | Avg physical LLM req | Avg tools | Avg steps |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for metric in metrics:
        lines.append(
            "| {group} | {iterations:.2f} | {requests:.2f} | {tools:.2f} | {steps:.2f} |".format(
                group=metric["group"],
                iterations=float(metric["avg_selected_agent_iterations"]),
                requests=float(metric["avg_llm_requests"]),
                tools=float(metric["avg_tool_calls"]),
                steps=float(metric["avg_trajectory_steps"]),
            )
        )
    lines.extend(
        [
            "",
            "## 生成与 Judge 按模型统计",
            "",
            "| Phase | Model | Upstream provider/revision | Calls | Input | "
            "Output | Cost | Exact/Non-exact/Unknown | Roles |",
            "|---|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for metric in model_metrics:
        upstream = (
            ", ".join(
                [
                    *metric["upstream_providers"],
                    *metric["upstream_models"],
                ]
            )
            or "—"
        )
        precision = (
            f"{metric['exact_request_count']}/"
            f"{metric['estimated_or_recorded_request_count']}/"
            f"{metric['unknown_request_count']}"
        )
        lines.append(
            "| {phase} | `{model}` | {upstream} | {calls} | {input} | "
            "{output} | ${cost} | {precision} | {roles} |".format(
                phase=metric["phase"],
                model=metric["model"],
                upstream=upstream,
                calls=metric["calls"],
                input=metric["input_tokens"],
                output=metric["output_tokens"],
                cost=metric["recorded_cost_usd"],
                precision=precision,
                roles=", ".join(metric["roles"]) or "—",
            )
        )
    lines.extend(
        [
            "",
            "## 成本口径",
            "",
            f"- Actual ledger generation："
            f"${scope_costs.get('generation', '0')}；"
            f"Judge：${judge_scope_cost}。",
            f"- Generation disposition 成本：selected="
            f"${disposition_costs.get('selected', '0')}，"
            f"replaced=${disposition_costs.get('replaced', '0')}，"
            f"failed=${disposition_costs.get('failed', '0')}。",
            "- 上表成本只统计最终选中的成功 generation；不包含 Judge。",
            "- 缺失或不完整的成功 generation 成本不会按 $0 参与平均；表中 N/A "
            "表示无法对全组给出逐任务成本，coverage/precision 为机器可审计口径。",
            "- `actual-spend-ledger.jsonl` 从所有 wave 的 generation attempts "
            "与 Judge attempts 重建，"
            "失败或被替换 attempt 仍计入真实花费，复制到 repair row 的请求按物理回执去重。",
            f"- Campaign 物理 LLM 请求：{ledger_summary['physical_request_count']}；"
            f"账本已记录成本：${ledger_summary['recorded_cost_usd']}；"
            f"其中精确成本：${ledger_summary['exact_cost_usd']}。",
            f"- OpenRouter account window delta："
            f"${cost_scope['account_window_delta_usd']}；"
            f"归因精度：`{cost_scope['attribution_precision']}`。",
            f"- 纳入审计的账户窗口：{len(cost_scope['account_windows'])}；"
            f"各窗口 counter 精确增量合计：${cost_scope['account_window_total_usd']}；"
            f"其中中止窗口未分配成本：${cost_scope['unallocated_aborted_window_usd']}。",
            "- `results.jsonl` 的任务行只绑定当前正式窗口；中止窗口成本仅在 "
            "campaign 级 proof/audit/manifest/report 中归档，窗口间 gap 不计费。",
            (
                "- 每个物理请求均有 exact receipt 且账本与账户窗口增量一致，"
                "因此该窗口增量可作为 campaign attributable exact cost。"
                if cost_scope["campaign_attributable_exact"]
                else (
                    "- 当前正式窗口的物理回执与账户 counter 已精确对账；但 prior "
                    "aborted window 成本未分配到请求或任务，因此多窗口合计不能称为 "
                    "campaign attributable exact cost。"
                    if cost_scope["current_window_campaign_attributable_exact"]
                    and Decimal(cost_scope["unallocated_aborted_window_usd"]) > 0
                    else "- 当前正式窗口存在 non-exact/unknown 物理请求；该数值仅是"
                    "共享 key 的账户窗口增量，跨主机外部使用无法证明不存在，不能称为 "
                    "campaign total，也不会分摊到任务或实验组。"
                )
            ),
            "",
            "## Non-BYOK 与 Web 成本说明",
            "",
            f"- OpenRouter BYOK 增量（Decimal 精确值）：{account['byok_usage_delta_usd']}；"
            "同一 paid key、本机文件锁、完整 before→stable-after 窗口证明通过；"
            "本机锁不证明跨主机独占。",
            f"- 本地 exact non-BYOK 请求：{evidence['exact_non_byok_request_count']}；"
            f"由 campaign 账户证明覆盖的元数据不完整请求："
            f"{evidence['campaign_covered_unverified_request_count']}；"
            "明确 BYOK/冲突请求为 0。",
            f"- 任务内 Web/Brave 调用数："
            f"{external_tool_cost['task_generation_tool_call_count']}；"
            f"live wave preflight 额外调用数："
            f"{external_tool_cost['live_preflight_tool_call_count']}；"
            f"总调用数：{external_tool_cost['tool_call_count']}；"
            f"成本状态：{external_tool_cost['cost_status']}；"
            f"已知成本下界：${external_tool_cost['recorded_cost_usd_lower_bound']}；"
            f"可能未定价调用数上界："
            f"{external_tool_cost['potentially_unpriced_tool_call_count_upper_bound']}。",
            "- 任务内 Web/Brave 成本按 generation attempt ID 跨 wave 去重；"
            "每次 main/resume 的 live preflight 作为 campaign overhead 按 manifest "
            "单列。两者均与 OpenRouter LLM 账户增量严格分离，unknown 不会被报告"
            "为真实 $0。",
            "",
            "机器可审计详情见 `audit.json`、`manifest.json`、"
            "`openrouter-non-byok-campaign-proof.json` 与 `actual-spend-ledger.jsonl`。",
            "",
        ]
    )
    return "\n".join(lines)


def write_markdown(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def publish_atomically(
    *,
    output_dir: Path,
    final_rows: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    proof: Mapping[str, Any],
    audit: dict[str, Any],
    manifest_base: dict[str, Any],
    report_markdown: str,
) -> dict[str, Any]:
    output = output_dir.resolve(strict=False)
    if output.exists():
        raise FinalizationError(f"refusing to overwrite final output: {output}")
    parent = output.parent
    if not parent.is_dir():
        raise FinalizationError(f"final output parent does not exist: {parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=parent))
    staging.chmod(0o700)
    try:
        results_path = staging / "results.jsonl"
        trace_path = staging / "trace.jsonl"
        ledger_path = staging / "actual-spend-ledger.jsonl"
        proof_path = staging / "openrouter-non-byok-campaign-proof.json"
        audit_path = staging / "audit.json"
        report_path = staging / "EXPERIMENT_RESULTS.md"
        manifest_path = staging / "manifest.json"
        write_jsonl(results_path, final_rows)
        write_jsonl(trace_path, traces)
        write_jsonl(ledger_path, ledger_rows)
        write_json(proof_path, proof)
        audit["artifacts"] = {
            "results": artifact_record(results_path),
            "trace": artifact_record(trace_path),
            "actual_spend_ledger": artifact_record(ledger_path),
            "openrouter_non_byok_campaign_proof": artifact_record(proof_path),
        }
        audit["audit_sha256"] = canonical_sha256(audit, prefix=True)
        write_json(audit_path, audit)
        write_markdown(report_path, report_markdown)
        artifacts = {
            path.name: artifact_record(path)
            for path in (
                results_path,
                trace_path,
                ledger_path,
                proof_path,
                audit_path,
                report_path,
            )
        }
        manifest = {
            **manifest_base,
            "artifacts": artifacts,
            "ledger_summary": ledger_summary,
            "audit_sha256": audit["audit_sha256"],
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest, prefix=True)
        write_json(manifest_path, manifest)
        # Sync directory entries before the one atomic publication rename.
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(staging, output)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def final_audit(
    *,
    tasks: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    external_tool_cost: Mapping[str, Any],
    metrics: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    model_metrics: Sequence[Mapping[str, Any]],
    rubric_metrics: Sequence[Mapping[str, Any]],
    repair_details: Sequence[Mapping[str, Any]],
    proof: Mapping[str, Any],
    pair_audit: Mapping[str, Any],
    selected_attempt_bindings: Mapping[str, str],
    selected_cost_reconciliation: Mapping[str, Any],
    judge_attempt_evidence_audit: Mapping[str, Any],
    max_attempts: int,
) -> dict[str, Any]:
    expected = {(group, str(task["id"])) for task in tasks for group in GROUPS}
    observed = [(str(row.get("group") or ""), str(row.get("task_id") or "")) for row in rows]
    duplicate_count = len(observed) - len(set(observed))
    seal_failures = sum(not verify_result_row_evidence(row) for row in rows)
    trace_failures = sum(
        trace != trace_row_from_result(row) for row, trace in zip(rows, traces, strict=True)
    )
    attempt_violations = [
        f"{row.get('group')}/{row.get('task_id')}"
        for row in rows
        if nonnegative_int(row.get("generation_attempt_budget_used")) > max_attempts
    ]
    tasks_by_id = {str(task["id"]): task for task in tasks}
    strict_judge_failures = [
        f"{row.get('group')}/{row.get('task_id')}"
        for row in rows
        if judge_reasons(
            row,
            task=tasks_by_id.get(str(row.get("task_id") or "")),
        )
    ]
    passed = (
        set(observed) == expected
        and len(rows) == len(expected)
        and duplicate_count == 0
        and seal_failures == 0
        and trace_failures == 0
        and not attempt_violations
        and proof.get("pass") is True
        and nonnegative_int(ledger_summary.get("physical_request_count")) > 0
        and ledger_summary.get("selected_generation_pair_count") == len(expected)
        and len(selected_attempt_bindings) == len(expected)
        and selected_cost_reconciliation.get("pair_count") == len(expected)
        and not strict_judge_failures
    )
    audit = {
        "schema": AUDIT_SCHEMA,
        "pass": passed,
        "created_at": utc_now(),
        "groups": list(GROUPS),
        "task_count": len(tasks),
        "expected_result_count": len(expected),
        "result_count": len(rows),
        "unique_pair_count": len(set(observed)),
        "missing_pairs": sorted([list(key) for key in expected - set(observed)]),
        "unexpected_pairs": sorted([list(key) for key in set(observed) - expected]),
        "duplicate_pair_count": duplicate_count,
        "sealed_result_failure_count": seal_failures,
        "trace_projection_failure_count": trace_failures,
        "generation_attempt_limit": max_attempts,
        "generation_attempt_limit_violations": attempt_violations,
        "judge_complete_count": len(rows) - len(strict_judge_failures),
        "judge_contract_failure_pairs": strict_judge_failures,
        "judge_attempt_evidence": dict(judge_attempt_evidence_audit),
        "selected_generation_attempt_bindings": dict(sorted(selected_attempt_bindings.items())),
        "selected_generation_cost_reconciliation": dict(selected_cost_reconciliation),
        "openrouter_non_byok_campaign_proof_sha256": proof.get("proof_sha256"),
        "account_windows": proof.get("account_windows"),
        "account_window_total_usd": proof.get("account_window_total_usd"),
        "unallocated_aborted_window_usd": proof.get("unallocated_aborted_window_usd"),
        "physical_request_count": ledger_summary.get("physical_request_count"),
        "external_tool_cost": dict(external_tool_cost),
        "selected_generation_cost": {
            "all_groups_complete": all(
                metric.get("selected_generation_cost_complete") is True for metric in metrics
            ),
            "groups": [dict(metric) for metric in metrics],
            "unknown_costs_are_zero": False,
            "account_delta_allocated_to_tasks": False,
            "account_window_delta_usd": proof.get("cost_scope", {}).get("account_window_delta_usd"),
            "account_windows": proof.get("cost_scope", {}).get("account_windows"),
            "account_window_total_usd": proof.get("cost_scope", {}).get(
                "account_window_total_usd"
            ),
            "unallocated_aborted_window_usd": proof.get("cost_scope", {}).get(
                "unallocated_aborted_window_usd"
            ),
            "attribution_precision": proof.get("cost_scope", {}).get("attribution_precision"),
            "campaign_attributable_exact": proof.get("cost_scope", {}).get(
                "campaign_attributable_exact"
            ),
        },
        "paired_quality_comparisons": [dict(comparison) for comparison in comparisons],
        "model_provider_metrics": [dict(metric) for metric in model_metrics],
        "rubric_section_metrics": [dict(metric) for metric in rubric_metrics],
        "repair_action_details": [dict(detail) for detail in repair_details],
        "pair_selection": pair_audit,
    }
    if not passed:
        raise FinalizationError(f"final campaign audit failed: {audit}")
    return audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--result",
        type=Path,
        action="append",
        required=True,
        help="Sealed result JSONL, repeated in chronological wave order.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        required=True,
        help="Terminal source manifest; all manifests must bind the same contracts.",
    )
    parser.add_argument("--account-before", type=Path, required=True)
    parser.add_argument("--account-after", type=Path, required=True)
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument(
        "--prior-account-window-dir",
        type=Path,
        action="append",
        default=[],
        help=(
            "Archived prior aborted account window directory containing before, "
            "stable-after, reconciliation, and runtime-environment evidence."
        ),
    )
    parser.add_argument("--runtime-environment", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int, default=9)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--groups", default=",".join(GROUPS))
    parser.add_argument("--max-generation-attempts", type=int, default=3)
    return parser


def run_finalization(args: argparse.Namespace) -> dict[str, Any]:
    groups = normalize_groups(args.groups)
    if args.max_generation_attempts != 3:
        raise FinalizationError("formal campaign generation limit must be exactly 3")
    input_path = require_regular_file(args.input, owner_only=False)
    tasks = read_tasks(input_path)
    frozen_input_sha256 = validate_frozen_draco_input(input_path, tasks)
    source_records, source_snapshots = read_source_rows(args.result)
    validate_source_policy_history(source_records)
    critical_source_snapshots = dict(source_snapshots)
    for raw_path in (
        input_path,
        *args.manifest,
        args.account_before,
        args.account_after,
        args.account_reconciliation,
        args.runtime_environment,
    ):
        source_path = require_regular_file(Path(raw_path), owner_only=False)
        critical_source_snapshots[str(source_path)] = file_sha256(source_path)
    prior_account_window_dirs = list(getattr(args, "prior_account_window_dir", ()) or ())
    for prior_dir in prior_account_window_dirs:
        for name in (
            "openrouter-account-before.json",
            "openrouter-account-after.json",
            "openrouter-account-reconciliation.json",
            "runtime-environment.json",
        ):
            source_path = require_regular_file(Path(prior_dir) / name, owner_only=True)
            critical_source_snapshots[str(source_path)] = file_sha256(source_path)
    attempt_evidence_audit = validate_generation_attempt_evidence(
        source_records,
        max_attempts=args.max_generation_attempts,
    )
    fingerprints, contracts, runtime_key, manifest_sources = load_manifest_contracts(
        args.manifest,
        result_paths=args.result,
        groups=groups,
    )
    validate_formal_campaign_contracts(contracts)
    validate_physical_generation_routes(source_records, contracts=contracts)
    judge_attempt_evidence_audit = validate_judge_attempt_evidence(source_records)
    selected, pair_audit = select_results(
        source_records,
        tasks=tasks,
        groups=groups,
        fingerprints=fingerprints,
        contracts=contracts,
        max_attempts=args.max_generation_attempts,
    )
    selected_attempt_bindings = bind_selected_generation_attempts(
        source_records,
        selected,
    )
    for pair, attempt_id in selected_attempt_bindings.items():
        pair_audit[pair]["selected_generation_attempt_id"] = attempt_id
    ledger_rows, ledger_summary = build_actual_spend_ledger(
        source_records,
        selected=selected,
        selected_attempt_bindings=selected_attempt_bindings,
    )
    model_metrics = ledger_model_metrics(ledger_rows)
    external_tool_cost = build_external_tool_cost_summary(
        source_records,
        manifest_sources=manifest_sources,
    )
    proof = validate_account_proof(
        before_path=args.account_before,
        after_path=args.account_after,
        reconciliation_path=args.account_reconciliation,
        runtime_environment_path=args.runtime_environment,
        lock_file=args.lock_file,
        lock_fd=args.lock_fd,
        runtime_key_fingerprint=runtime_key,
        source_records=source_records,
        ledger_rows=ledger_rows,
        ledger_summary=ledger_summary,
        prior_account_window_dirs=prior_account_window_dirs,
    )
    final_rows = finalize_rows(
        selected,
        tasks=tasks,
        proof=proof,
        pair_audit=pair_audit,
    )
    traces = [trace_row_from_result(row) for row in final_rows]
    selected_costs, selected_cost_reconciliation = selected_generation_costs_from_ledger(
        final_rows, ledger_rows
    )
    metrics = group_metrics(
        final_rows,
        selected_costs_by_pair=selected_costs,
    )
    comparisons = paired_quality_comparisons(final_rows)
    rubric_metrics = rubric_section_metrics(final_rows)
    repair_details = repair_action_details(final_rows)
    audit = final_audit(
        tasks=tasks,
        rows=final_rows,
        traces=traces,
        ledger_summary=ledger_summary,
        external_tool_cost=external_tool_cost,
        metrics=metrics,
        comparisons=comparisons,
        model_metrics=model_metrics,
        rubric_metrics=rubric_metrics,
        repair_details=repair_details,
        proof=proof,
        pair_audit=pair_audit,
        selected_attempt_bindings=selected_attempt_bindings,
        selected_cost_reconciliation=selected_cost_reconciliation,
        judge_attempt_evidence_audit=judge_attempt_evidence_audit,
        max_attempts=args.max_generation_attempts,
    )
    audit["generation_attempt_evidence_schema"] = GENERATION_ATTEMPT_EVIDENCE_SCHEMA
    audit["generation_attempt_evidence"] = attempt_evidence_audit
    audit["judge_attempt_evidence_schema"] = JUDGE_ATTEMPT_EVIDENCE_SCHEMA
    audit["frozen_draco_mini_input"] = {
        "sha256": frozen_input_sha256,
        "task_count": len(tasks),
        "task_ids": [str(task["id"]) for task in tasks],
    }
    verify_source_snapshots(critical_source_snapshots)
    report = experiment_results_markdown(
        task_count=len(tasks),
        final_rows=final_rows,
        metrics=metrics,
        ledger_summary=ledger_summary,
        external_tool_cost=external_tool_cost,
        proof=proof,
        comparisons=comparisons,
        model_metrics=model_metrics,
        rubric_metrics=rubric_metrics,
        repair_details=repair_details,
    )
    manifest_base = {
        "schema": MANIFEST_SCHEMA,
        "status": "complete",
        "created_at": utc_now(),
        "finalizer_version": FINALIZER_VERSION,
        "groups": list(groups),
        "task_ids": [str(task["id"]) for task in tasks],
        "task_count": len(tasks),
        "result_count": len(final_rows),
        "input": {
            "path": str(input_path),
            "sha256": frozen_input_sha256,
            "frozen_expected_sha256": FROZEN_DRACO_MINI_SHA256,
            "frozen_expected_task_count": FROZEN_DRACO_MINI_TASK_COUNT,
        },
        "source_results": [
            {
                "path": path,
                "sha256": digest,
            }
            for path, digest in source_snapshots.items()
        ],
        "source_manifests": manifest_sources,
        "run_compatibility_fingerprints": fingerprints,
        "max_generation_attempts": args.max_generation_attempts,
        "generation_attempt_evidence_schema": GENERATION_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_evidence": judge_attempt_evidence_audit,
        "selected_generation_attempt_bindings": dict(sorted(selected_attempt_bindings.items())),
        "selected_generation_cost_reconciliation": (selected_cost_reconciliation),
        "group_metrics": metrics,
        "rubric_section_metrics": rubric_metrics,
        "repair_action_details": repair_details,
        "paired_quality_comparisons": comparisons,
        "model_provider_metrics": model_metrics,
        "external_tool_cost": external_tool_cost,
        "openrouter_non_byok_campaign_proof_sha256": proof["proof_sha256"],
        "account_windows": proof["account_windows"],
        "account_window_total_usd": proof["account_window_total_usd"],
        "unallocated_aborted_window_usd": proof["unallocated_aborted_window_usd"],
        "cost_attribution": {
            "account_window_delta_usd": proof["cost_scope"]["account_window_delta_usd"],
            "account_windows": proof["cost_scope"]["account_windows"],
            "account_window_total_usd": proof["cost_scope"]["account_window_total_usd"],
            "unallocated_aborted_window_usd": proof["cost_scope"][
                "unallocated_aborted_window_usd"
            ],
            "attribution_precision": proof["cost_scope"]["attribution_precision"],
            "campaign_attributable_exact": proof["cost_scope"]["campaign_attributable_exact"],
            "campaign_attributable_cost_usd": proof["cost_scope"]["campaign_attributable_cost_usd"],
            "account_delta_allocated_to_tasks": False,
        },
    }
    return publish_atomically(
        output_dir=args.output_dir,
        final_rows=final_rows,
        traces=traces,
        ledger_rows=ledger_rows,
        ledger_summary=ledger_summary,
        proof=proof,
        audit=audit,
        manifest_base=manifest_base,
        report_markdown=report,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = run_finalization(args)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "output_dir": str(args.output_dir.resolve()),
                "result_count": manifest["result_count"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
