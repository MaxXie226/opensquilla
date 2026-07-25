#!/usr/bin/env python3
"""Snapshot and finalize authoritative DRACO B2 experiment artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "opensquilla.draco-b2-artifact-snapshot/v1"
SUCCESS_SCHEMA = "opensquilla.draco-b2-formal-success/v1"
ROUTE_PREFLIGHT_V1_SCHEMA = "opensquilla.openrouter-route-preflight/v1"
ROUTE_PREFLIGHT_V2_SCHEMA = "opensquilla.openrouter-route-preflight/v2"
ROUTE_PREFLIGHT_SCHEMAS = frozenset({ROUTE_PREFLIGHT_V1_SCHEMA, ROUTE_PREFLIGHT_V2_SCHEMA})
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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
FORMAL_REASONING_INELIGIBLE_MODELS = frozenset(
    {
        "kwaipilot/kat-coder-air-v2.5",
        "kwaipilot/kat-coder-pro-v2.5",
        "meta-llama/llama-4-scout",
    }
)
FORMAL_UNSUPPORTED_TEMPERATURE_MODELS = frozenset(
    {
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-5",
        "moonshotai/kimi-k2.7-code",
        "openai/gpt-5.5",
    }
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


def relative_file_record(root: Path, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes snapshot root: {path}") from exc
    stat = resolved.stat()
    permissions = stat.st_mode & 0o777
    if permissions & 0o077:
        raise ValueError(f"artifact permissions expose benchmark data outside the owner: {path}")
    return {
        "path": relative.as_posix(),
        "sha256": file_sha256(resolved),
        "size_bytes": stat.st_size,
        "mode": oct(permissions),
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"invalid artifact snapshot: {path}")
    return value


def safe_relative_path(value: Any, *, label: str) -> Path:
    relative = Path(str(value or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} is not a safe relative path: {value!r}")
    return relative


def recursive_artifact_paths(root: Path, *, excluded: set[Path]) -> list[Path]:
    artifacts: list[Path] = []
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"artifact tree contains a symlink: {candidate}")
        if candidate.is_file() and candidate.resolve(strict=True) not in excluded:
            artifacts.append(candidate.resolve(strict=True))
    return artifacts


def verify_snapshot(path: Path) -> dict[str, Any]:
    snapshot = load_snapshot(path)
    root_reference = safe_relative_path(snapshot.get("root"), label="snapshot root")
    root = (path.resolve(strict=True).parent / root_reference).resolve(strict=True)
    if root != path.resolve(strict=True).parent:
        raise ValueError(f"snapshot root must be its containing directory: {path}")
    records = snapshot.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ValueError(f"artifact snapshot has no files: {path}")
    recorded_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"artifact snapshot contains a non-object record: {path}")
        relative = safe_relative_path(record.get("path"), label="artifact path")
        relative_key = relative.as_posix()
        if relative_key in recorded_paths:
            raise ValueError(f"artifact snapshot contains duplicate paths: {path}")
        recorded_paths.add(relative_key)
        artifact = root / relative
        actual = relative_file_record(root, artifact)
        if actual != record:
            raise ValueError(f"artifact changed after audit: {artifact}")
    if snapshot.get("closed_world") is True:
        allowed_values = snapshot.get("allowed_after_snapshot") or []
        if not isinstance(allowed_values, list):
            raise ValueError(f"snapshot allowed-after list is invalid: {path}")
        allowed = {
            safe_relative_path(value, label="allowed-after path").as_posix()
            for value in allowed_values
        }
        snapshot_relative = path.resolve(strict=True).relative_to(root).as_posix()
        excluded = {
            path.resolve(strict=True),
            *{(root / relative).resolve(strict=False) for relative in allowed},
        }
        actual_paths = {
            artifact.relative_to(root).as_posix()
            for artifact in recursive_artifact_paths(root, excluded=excluded)
        }
        if actual_paths != recorded_paths:
            missing = sorted(recorded_paths - actual_paths)
            extra = sorted(actual_paths - recorded_paths)
            raise ValueError(
                f"artifact set changed after audit: missing={missing}, extra={extra}, "
                f"snapshot={snapshot_relative}"
            )
    return snapshot


def _normalized_routes(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} expected_routes must be a non-empty object")
    routes: dict[str, str] = {}
    for raw_model, raw_provider in value.items():
        if not isinstance(raw_model, str) or not isinstance(raw_provider, str):
            raise ValueError(f"{label} expected_routes must contain string pairs")
        model = raw_model.strip().lower()
        provider = raw_provider.strip().lower()
        if model != raw_model or provider != raw_provider or "/" not in model:
            raise ValueError(f"{label} expected_routes are not canonical")
        routes[model] = provider
    if len(routes) != len(value):
        raise ValueError(f"{label} expected_routes contain duplicate identities")
    return routes


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _formal_required_parameters(
    expected_routes: dict[str, str],
) -> dict[str, list[str]]:
    required = {model: {"max_tokens", "tools"} for model in expected_routes}
    for model in set(expected_routes) - FORMAL_REASONING_INELIGIBLE_MODELS:
        required[model].add("reasoning")
    for model in set(expected_routes) - FORMAL_UNSUPPORTED_TEMPERATURE_MODELS:
        required[model].add("temperature")
    return {model: sorted(parameters) for model, parameters in required.items()}


def _tag_matches(tag: str, expected: str) -> bool:
    return tag == expected or tag.startswith(f"{expected}/")


def _recompute_endpoint_counts(
    *,
    model: str,
    expected_provider: str,
    required_parameters: list[str],
    endpoints: Any,
    label: str,
) -> tuple[int, int]:
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError(f"{label} route preflight v2 endpoint evidence is incomplete: {model}")
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not expected_provider_name:
        raise ValueError(
            f"{label} route preflight v2 provider contract is unknown: {expected_provider}"
        )
    required = set(required_parameters)
    operational_count = 0
    compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"{label} route preflight v2 endpoint row is invalid: {model}")
        if not _tag_matches(
            str(endpoint.get("tag") or ""),
            expected_provider,
        ):
            raise ValueError(f"{label} route preflight v2 endpoint provider tag differs: {model}")
        status = endpoint.get("status")
        operational = isinstance(status, int) and not isinstance(status, bool) and status == 0
        if not operational:
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"{label} route preflight v2 endpoint parameters are invalid: {model}")
        supported_parameters = (
            {str(item) for item in supported} if isinstance(supported, list) else set()
        )
        if (
            endpoint.get("provider_name") == expected_provider_name
            and endpoint.get("model_id") == model
            and required <= supported_parameters
        ):
            compatible_count += 1
    if operational_count <= 0 or compatible_count <= 0:
        raise ValueError(f"{label} route preflight v2 model has no compatible route: {model}")
    return operational_count, compatible_count


def validate_route_preflight_payload(
    payload: Any,
    *,
    experiment_config_sha256: str,
    label: str,
) -> dict[str, Any]:
    """Validate one contract-bound formal/G1 v2 preflight payload."""

    if not isinstance(payload, dict):
        raise ValueError(f"{label} route preflight payload must be an object")
    schema = payload.get("schema")
    if schema not in ROUTE_PREFLIGHT_SCHEMAS:
        raise ValueError(f"{label} route preflight schema is unsupported: {schema!r}")
    if schema == ROUTE_PREFLIGHT_V1_SCHEMA:
        raise ValueError(
            f"{label} route preflight v1 lacks endpoint details required for "
            "fail-closed compatibility verification"
        )

    if payload.get("route_metadata_pass") is not True:
        raise ValueError(f"{label} route preflight v2 metadata did not pass")
    if payload.get("scope") != "formal":
        raise ValueError(f"{label} route preflight v2 scope must be formal")
    if payload.get("api_origin") != "https://openrouter.ai":
        raise ValueError(f"{label} route preflight v2 API origin differs")
    if payload.get("trust_env") is not False:
        raise ValueError(f"{label} route preflight v2 must disable trust_env")
    providers_hash = payload.get("providers_response_sha256")
    if not isinstance(providers_hash, str) or not HEX64.fullmatch(providers_hash):
        raise ValueError(f"{label} route preflight v2 providers hash is invalid")

    expected_routes = _normalized_routes(payload.get("expected_routes"), label=label)
    expected_routes_sha256 = payload.get("expected_routes_sha256")
    if (
        not isinstance(expected_routes_sha256, str)
        or not HEX64.fullmatch(expected_routes_sha256)
        or canonical_sha256(expected_routes) != expected_routes_sha256
    ):
        raise ValueError(f"{label} route preflight v2 expected-routes hash differs")

    experiment_evidence = payload.get("experiment_config")
    if not isinstance(experiment_evidence, dict):
        raise ValueError(f"{label} route preflight v2 lacks experiment config evidence")
    evidence_config_hash = experiment_evidence.get("sha256")
    if (
        evidence_config_hash != experiment_config_sha256
        or not isinstance(evidence_config_hash, str)
        or not HEX64.fullmatch(evidence_config_hash)
    ):
        raise ValueError(f"{label} route preflight v2 experiment config hash differs")
    config_path_raw = experiment_evidence.get("path")
    if not isinstance(config_path_raw, str) or not Path(config_path_raw).is_absolute():
        raise ValueError(f"{label} route preflight v2 experiment config path is invalid")
    config_path = Path(config_path_raw)
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"{label} route preflight v2 experiment config is unavailable")
    if file_sha256(config_path) != experiment_config_sha256:
        raise ValueError(f"{label} route preflight v2 experiment config changed")
    try:
        experiment_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} route preflight v2 experiment config is invalid: {exc}") from exc
    g1_contract = (
        experiment_config.get("g1_routing") if isinstance(experiment_config, dict) else None
    )
    if not isinstance(g1_contract, dict):
        raise ValueError(f"{label} route preflight v2 lacks the G1 config contract")
    if g1_contract.get("selection_mode") != "router_dynamic":
        raise ValueError(f"{label} route preflight v2 G1 selection mode differs")
    candidate_count = g1_contract.get("expected_candidate_count")
    if not _positive_int(candidate_count) or candidate_count != len(expected_routes):
        raise ValueError(f"{label} route preflight v2 G1 candidate count differs")
    config_routes = _normalized_routes(
        g1_contract.get("expected_routes"),
        label=f"{label} G1 config",
    )
    if config_routes != expected_routes:
        raise ValueError(f"{label} route preflight v2 G1 routes differ")
    if g1_contract.get("expected_routes_sha256") != expected_routes_sha256:
        raise ValueError(f"{label} route preflight v2 G1 route hash differs")
    profile_id = experiment_evidence.get("g1_routing_profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError(f"{label} route preflight v2 G1 profile is missing")
    if profile_id != g1_contract.get("profile_id"):
        raise ValueError(f"{label} route preflight v2 G1 profile differs")
    source_version = experiment_evidence.get("source_registry_snapshot_version")
    if not isinstance(source_version, str) or not source_version.strip():
        raise ValueError(f"{label} route preflight v2 registry version is missing")
    if source_version != g1_contract.get("source_registry_snapshot_version"):
        raise ValueError(f"{label} route preflight v2 registry version differs")

    models = payload.get("models")
    if not isinstance(models, dict) or set(models) != set(expected_routes):
        raise ValueError(f"{label} route preflight v2 model evidence set differs")
    frozen_required_parameters = _formal_required_parameters(expected_routes)
    required_parameters: dict[str, list[str]] = {}
    for model, expected_provider in expected_routes.items():
        row = models.get(model)
        if not isinstance(row, dict):
            raise ValueError(f"{label} route preflight v2 model evidence is invalid")
        if row.get("expected_provider") != expected_provider:
            raise ValueError(f"{label} route preflight v2 model provider differs: {model}")
        if row.get("response_model_id") != model:
            raise ValueError(f"{label} route preflight v2 endpoint response model differs: {model}")
        response_sha256 = row.get("response_sha256")
        if not isinstance(response_sha256, str) or not HEX64.fullmatch(response_sha256):
            raise ValueError(
                f"{label} route preflight v2 endpoint response hash is invalid: {model}"
            )
        parameters = row.get("required_parameters")
        if (
            not isinstance(parameters, list)
            or not parameters
            or any(not isinstance(item, str) or not item for item in parameters)
            or parameters != sorted(set(parameters))
        ):
            raise ValueError(f"{label} route preflight v2 parameters are invalid: {model}")
        if parameters != frozen_required_parameters[model]:
            raise ValueError(f"{label} route preflight v2 frozen parameters differ: {model}")
        operational_count, compatible_count = _recompute_endpoint_counts(
            model=model,
            expected_provider=expected_provider,
            required_parameters=parameters,
            endpoints=row.get("matching_endpoints"),
            label=label,
        )
        if (
            row.get("operational_match_count") != operational_count
            or row.get("compatible_operational_match_count") != compatible_count
        ):
            raise ValueError(
                f"{label} route preflight v2 precomputed endpoint counts differ: {model}"
            )
        required_parameters[model] = parameters
    required_parameters_sha256 = payload.get("required_parameters_sha256")
    if (
        not isinstance(required_parameters_sha256, str)
        or not HEX64.fullmatch(required_parameters_sha256)
        or canonical_sha256(required_parameters) != required_parameters_sha256
    ):
        raise ValueError(f"{label} route preflight v2 required-parameters hash differs")

    return {
        "schema": schema,
        "scope": "formal",
        "expected_routes_sha256": expected_routes_sha256,
        "expected_candidate_count": candidate_count,
        "experiment_config_sha256": experiment_config_sha256,
        "g1_routing_profile_id": profile_id,
        "source_registry_snapshot_version": source_version,
    }


def validate_route_preflight_set(
    payloads: list[Any],
    *,
    experiment_config_sha256: str,
    labels: list[str],
) -> list[dict[str, Any]]:
    if len(payloads) != len(labels):
        raise ValueError("route preflight payload and label counts differ")
    validations = [
        validate_route_preflight_payload(
            payload,
            experiment_config_sha256=experiment_config_sha256,
            label=label,
        )
        for payload, label in zip(payloads, labels, strict=True)
    ]
    schemas = {validation["schema"] for validation in validations}
    if len(schemas) != 1:
        raise ValueError("route preflight evidence schemas differ")
    if schemas == {ROUTE_PREFLIGHT_V2_SCHEMA} and any(
        validation != validations[0] for validation in validations[1:]
    ):
        raise ValueError("route preflight v2 G1 contracts differ")
    return validations


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("output", type=Path)
    snapshot_parser.add_argument("--root", type=Path, required=True)
    snapshot_parser.add_argument("--file", type=Path, action="append", default=[])
    snapshot_parser.add_argument("--recursive", action="store_true")
    snapshot_parser.add_argument("--allow-after", action="append", default=[])

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("snapshot", type=Path)

    success_parser = subparsers.add_parser("success")
    success_parser.add_argument("output", type=Path)
    success_parser.add_argument("--source-git-head", required=True)
    success_parser.add_argument("--input-sha256", required=True)
    success_parser.add_argument("--gateway-config-sha256", required=True)
    success_parser.add_argument("--experiment-config-sha256", required=True)
    success_parser.add_argument("--snapshot", type=Path, action="append", default=[])
    success_parser.add_argument("--evidence", type=Path, action="append", default=[])
    args = parser.parse_args()

    if args.command == "snapshot":
        if args.output.exists():
            parser.error(f"refusing to overwrite artifact snapshot: {args.output}")
        root = args.root.resolve(strict=True)
        output_resolved = args.output.resolve(strict=False)
        if output_resolved.parent != root:
            parser.error("artifact snapshot must be created directly inside --root")
        if args.recursive and args.file:
            parser.error("--recursive cannot be combined with --file")
        try:
            allowed_after = sorted(
                {
                    safe_relative_path(value, label="allowed-after path").as_posix()
                    for value in args.allow_after
                }
            )
        except ValueError as exc:
            parser.error(str(exc))
        excluded = {
            output_resolved,
            *{(root / relative).resolve(strict=False) for relative in allowed_after},
        }
        files = (
            recursive_artifact_paths(root, excluded=excluded)
            if args.recursive
            else sorted({path.resolve(strict=True) for path in args.file})
        )
        if not files:
            parser.error("artifact snapshot requires at least one --file")
        if output_resolved in files:
            parser.error("artifact snapshot cannot include itself")
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "created_at": datetime.now(UTC).isoformat(),
            "root": ".",
            "closed_world": bool(args.recursive),
            "allowed_after_snapshot": allowed_after,
            "artifacts": [relative_file_record(root, path) for path in files],
        }
        atomic_write_json(args.output, payload)
        return 0

    if args.command == "verify":
        verify_snapshot(args.snapshot)
        return 0

    if args.output.exists():
        parser.error(f"refusing to overwrite success sentinel: {args.output}")
    if not HEX40.fullmatch(args.source_git_head):
        parser.error("--source-git-head must be a 40-character lowercase hex commit")
    for field, value in (
        ("--input-sha256", args.input_sha256),
        ("--gateway-config-sha256", args.gateway_config_sha256),
        ("--experiment-config-sha256", args.experiment_config_sha256),
    ):
        if not HEX64.fullmatch(value):
            parser.error(f"{field} must be a 64-character lowercase hex digest")
    for path in args.snapshot:
        if path.is_symlink() or not path.is_file():
            parser.error(f"snapshot is not a regular non-symlink file: {path}")
        if path.stat().st_mode & 0o077:
            parser.error(f"snapshot permissions are not owner-only: {path}")
    resolved_snapshot_paths = [path.resolve(strict=True) for path in args.snapshot]
    if len(resolved_snapshot_paths) != 3 or len(set(resolved_snapshot_paths)) != 3:
        parser.error("formal success requires exactly three distinct static/canary/full snapshots")
    for path in args.evidence:
        if path.is_symlink() or not path.is_file():
            parser.error(f"evidence is not a regular non-symlink file: {path}")
        if path.stat().st_mode & 0o077:
            parser.error(f"evidence permissions are not owner-only: {path}")
    resolved_evidence_paths = [path.resolve(strict=True) for path in args.evidence]
    if len(resolved_evidence_paths) != 2 or len(set(resolved_evidence_paths)) != 2:
        parser.error("formal success requires exactly two distinct route preflight artifacts")
    success_root = args.output.resolve(strict=False).parent
    snapshots = []
    for snapshot_path in resolved_snapshot_paths:
        snapshot = verify_snapshot(snapshot_path)
        try:
            relative_snapshot = snapshot_path.relative_to(success_root).as_posix()
        except ValueError:
            parser.error(f"snapshot escapes success directory: {snapshot_path}")
        snapshots.append(
            {
                "path": relative_snapshot,
                "sha256": file_sha256(snapshot_path),
                "snapshot_schema": snapshot["schema"],
            }
        )
    route_payloads: list[dict[str, Any]] = []
    for resolved in resolved_evidence_paths:
        if resolved.is_symlink() or not resolved.is_file():
            parser.error(f"evidence is not a regular non-symlink file: {resolved}")
        try:
            route_payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"invalid route preflight evidence {resolved}: {exc}")
        route_payloads.append(route_payload)
    try:
        route_validations = validate_route_preflight_set(
            route_payloads,
            experiment_config_sha256=args.experiment_config_sha256,
            labels=[str(path) for path in resolved_evidence_paths],
        )
    except ValueError as exc:
        parser.error(str(exc))

    evidence = []
    for resolved, validation in zip(
        resolved_evidence_paths,
        route_validations,
        strict=True,
    ):
        try:
            relative_evidence = resolved.relative_to(success_root).as_posix()
        except ValueError:
            parser.error(f"evidence escapes success directory: {resolved}")
        evidence_record = {
            "path": relative_evidence,
            "sha256": file_sha256(resolved),
            "size_bytes": resolved.stat().st_size,
            "route_preflight_schema": validation["schema"],
        }
        if validation["schema"] == ROUTE_PREFLIGHT_V2_SCHEMA:
            evidence_record["formal_g1_contract"] = {
                key: validation[key]
                for key in (
                    "scope",
                    "expected_routes_sha256",
                    "expected_candidate_count",
                    "experiment_config_sha256",
                    "g1_routing_profile_id",
                    "source_registry_snapshot_version",
                )
            }
        evidence.append(evidence_record)
    payload = {
        "schema": SUCCESS_SCHEMA,
        "status": "complete",
        "created_at": datetime.now(UTC).isoformat(),
        "source_git_head": args.source_git_head,
        "input_sha256": args.input_sha256,
        "gateway_config_sha256": args.gateway_config_sha256,
        "experiment_config_sha256": args.experiment_config_sha256,
        "artifact_snapshots": snapshots,
        "route_preflight_evidence": evidence,
    }
    atomic_write_json(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
