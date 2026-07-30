#!/usr/bin/env python3
"""Compare two public Gateway client-contract snapshots.

The checker is intentionally dependency-free so it can run on fork pull
requests without credentials.  It distinguishes backward-compatible additions
from changes that require a new protocol contract or an explicit security /
behaviour review.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

CONTRACT_FILES: Final[tuple[str, ...]] = (
    "contract.json",
    "protocol.schema.json",
    "rpc-methods.json",
    "events.json",
    "http-routes.json",
    "golden/connect.json",
    "golden/error.json",
    "golden/hello-ok.json",
)
DEFAULT_CONTRACT_PATH: Final[str] = "contracts/client/v3"
_SEVERITY_ORDER: Final[dict[str, int]] = {
    "breaking": 0,
    "review": 1,
    "additive": 2,
    "informational": 3,
}
_SCHEMA_ANNOTATIONS: Final[frozenset[str]] = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


class ContractCompatibilityError(ValueError):
    """Raised when a contract snapshot cannot be loaded or compared."""


@dataclass(frozen=True)
class ContractChange:
    severity: str
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class ContractSnapshot:
    source: str
    files: Mapping[str, Any]

    @property
    def digest(self) -> str:
        value = self.files["contract.json"].get("digest")
        return value if isinstance(value, str) else ""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractCompatibilityError(f"{source} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ContractCompatibilityError(f"{source} must contain a JSON object")
    return value


def load_contract_directory(path: Path) -> ContractSnapshot:
    display_path = path.as_posix()
    root = path.resolve()
    files: dict[str, Any] = {}
    for relative in CONTRACT_FILES:
        source = root / relative
        if not source.is_file():
            raise ContractCompatibilityError(f"missing contract artifact: {source}")
        files[relative] = _json_object(source.read_bytes(), source=str(source))
    return ContractSnapshot(source=f"directory:{display_path}", files=files)


def load_contract_git_ref(
    ref: str,
    *,
    repository: Path = Path("."),
    contract_path: str = DEFAULT_CONTRACT_PATH,
) -> ContractSnapshot:
    if not ref.strip() or ref.startswith("-") or "\x00" in ref:
        raise ContractCompatibilityError(f"invalid Git baseline ref: {ref!r}")
    files: dict[str, Any] = {}
    for relative in CONTRACT_FILES:
        object_name = f"{ref}:{contract_path}/{relative}"
        result = subprocess.run(
            ["git", "-C", str(repository), "show", object_name],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise ContractCompatibilityError(
                f"unable to load {object_name}: {message or 'git show failed'}"
            )
        files[relative] = _json_object(result.stdout, source=object_name)
    return ContractSnapshot(source=f"git:{ref}:{contract_path}", files=files)


def git_ref_has_contract(
    ref: str,
    *,
    repository: Path = Path("."),
    contract_path: str = DEFAULT_CONTRACT_PATH,
) -> bool:
    if not ref.strip() or ref.startswith("-") or "\x00" in ref:
        raise ContractCompatibilityError(f"invalid Git baseline ref: {ref!r}")
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if commit.returncode != 0:
        message = commit.stderr.decode("utf-8", errors="replace").strip()
        raise ContractCompatibilityError(
            f"unable to resolve Git baseline ref {ref!r}: {message or 'git rev-parse failed'}"
        )
    contract = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "cat-file",
            "-e",
            f"{ref}:{contract_path}/contract.json",
        ],
        check=False,
        capture_output=True,
    )
    return contract.returncode == 0


def _change(severity: str, code: str, path: str, detail: str) -> ContractChange:
    return ContractChange(severity=severity, code=code, path=path, detail=detail)


def _schema_types(value: Any) -> frozenset[str] | None:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    return None


def _compare_value_set(
    old: Iterable[Any],
    new: Iterable[Any],
    *,
    path: str,
    code: str,
) -> list[ContractChange]:
    old_values = {_canonical(value): value for value in old}
    new_values = {_canonical(value): value for value in new}
    changes: list[ContractChange] = []
    removed = [old_values[key] for key in sorted(old_values.keys() - new_values.keys())]
    added = [new_values[key] for key in sorted(new_values.keys() - old_values.keys())]
    if removed:
        changes.append(
            _change(
                "breaking",
                f"{code}-narrowed",
                path,
                f"removed accepted values: {removed!r}",
            )
        )
    if added:
        changes.append(
            _change(
                "additive",
                f"{code}-widened",
                path,
                f"added accepted values: {added!r}",
            )
        )
    return changes


def _schema_variant_key(value: Any) -> str:
    if not isinstance(value, dict):
        return f"value:{_canonical(value)}"
    if isinstance(value.get("$ref"), str):
        return f"ref:{value['$ref']}"
    if "const" in value:
        return f"const:{_canonical(value['const'])}"
    schema_types = _schema_types(value.get("type"))
    if schema_types is not None:
        return "type:" + ",".join(sorted(schema_types))
    return f"schema:{_canonical(value)}"


def _schema_variants(values: Iterable[Any]) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    for value in values:
        key = _schema_variant_key(value)
        if key in variants:
            key = f"{key}:{_canonical(value)}"
        variants[key] = value
    return variants


def _compare_schema_composition(
    old_values: list[Any],
    new_values: list[Any],
    *,
    path: str,
    keyword: str,
) -> list[ContractChange]:
    old_variants = _schema_variants(old_values)
    new_variants = _schema_variants(new_values)
    changes: list[ContractChange] = []
    for key in sorted(old_variants.keys() - new_variants.keys()):
        changes.append(
            _change(
                "breaking",
                f"schema-{keyword.lower()}-narrowed",
                path,
                f"removed accepted variant: {key}",
            )
        )
    for key in sorted(new_variants.keys() - old_variants.keys()):
        changes.append(
            _change(
                "additive",
                f"schema-{keyword.lower()}-widened",
                path,
                f"added accepted variant: {key}",
            )
        )
    for key in sorted(old_variants.keys() & new_variants.keys()):
        old_variant = old_variants[key]
        new_variant = new_variants[key]
        if isinstance(old_variant, dict) and isinstance(new_variant, dict):
            changes.extend(
                _compare_schema(
                    old_variant,
                    new_variant,
                    path=f"{path}[{key}]",
                )
            )
    return changes


def _compare_limit(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    key: str,
    path: str,
    higher_is_tighter: bool,
) -> list[ContractChange]:
    old_value = old.get(key)
    new_value = new.get(key)
    if not isinstance(old_value, (int, float)) or not isinstance(new_value, (int, float)):
        if old_value == new_value:
            return []
        if old_value is None:
            return [
                _change("breaking", "schema-constraint-added", f"{path}.{key}", str(new_value))
            ]
        if new_value is None:
            return [
                _change("additive", "schema-constraint-removed", f"{path}.{key}", str(old_value))
            ]
        return [
            _change(
                "breaking",
                "schema-constraint-changed",
                f"{path}.{key}",
                f"{old_value!r} -> {new_value!r}",
            )
        ]
    tighter = new_value > old_value if higher_is_tighter else new_value < old_value
    severity = "breaking" if tighter else "additive"
    return [
        _change(
            severity,
            f"schema-constraint-{'tightened' if tighter else 'relaxed'}",
            f"{path}.{key}",
            f"{old_value!r} -> {new_value!r}",
        )
    ]


def _compare_schema(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    path: str,
) -> list[ContractChange]:
    changes: list[ContractChange] = []

    old_ref = old.get("$ref")
    new_ref = new.get("$ref")
    if old_ref != new_ref:
        changes.append(
            _change(
                "breaking",
                "schema-ref-changed",
                f"{path}.$ref",
                f"{old_ref!r} -> {new_ref!r}",
            )
        )

    old_types = _schema_types(old.get("type"))
    new_types = _schema_types(new.get("type"))
    if old_types is not None and new_types is not None and old_types != new_types:
        removed = sorted(old_types - new_types)
        added = sorted(new_types - old_types)
        if removed:
            changes.append(
                _change(
                    "breaking",
                    "schema-type-narrowed",
                    f"{path}.type",
                    f"removed accepted types: {removed!r}",
                )
            )
        if added:
            changes.append(
                _change(
                    "review",
                    "schema-type-widened",
                    f"{path}.type",
                    f"added accepted types: {added!r}",
                )
            )
    elif old_types != new_types:
        changes.append(
            _change(
                "breaking",
                "schema-type-changed",
                f"{path}.type",
                f"{old.get('type')!r} -> {new.get('type')!r}",
            )
        )

    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        changes.extend(
            _compare_value_set(
                old_enum,
                new_enum,
                path=f"{path}.enum",
                code="schema-enum",
            )
        )
    elif old_enum != new_enum:
        severity = "additive" if old_enum is not None and new_enum is None else "breaking"
        changes.append(
            _change(
                severity,
                "schema-enum-changed",
                f"{path}.enum",
                f"{old_enum!r} -> {new_enum!r}",
            )
        )

    for keyword in ("oneOf", "anyOf"):
        old_values = old.get(keyword)
        new_values = new.get(keyword)
        if isinstance(old_values, list) and isinstance(new_values, list):
            changes.extend(
                _compare_schema_composition(
                    old_values,
                    new_values,
                    path=f"{path}.{keyword}",
                    keyword=keyword,
                )
            )
        elif old_values != new_values:
            severity = "additive" if old_values is not None and new_values is None else "breaking"
            changes.append(
                _change(
                    severity,
                    f"schema-{keyword.lower()}-changed",
                    f"{path}.{keyword}",
                    f"{old_values!r} -> {new_values!r}",
                )
            )

    if old.get("const") != new.get("const"):
        changes.append(
            _change(
                "breaking",
                "schema-const-changed",
                f"{path}.const",
                f"{old.get('const')!r} -> {new.get('const')!r}",
            )
        )

    old_required = set(old.get("required") or [])
    new_required = set(new.get("required") or [])
    for name in sorted(new_required - old_required):
        changes.append(
            _change(
                "breaking",
                "schema-required-added",
                f"{path}.required",
                f"property became required: {name}",
            )
        )
    for name in sorted(old_required - new_required):
        changes.append(
            _change(
                "additive",
                "schema-required-removed",
                f"{path}.required",
                f"property became optional: {name}",
            )
        )

    old_properties = old.get("properties") or {}
    new_properties = new.get("properties") or {}
    if isinstance(old_properties, dict) and isinstance(new_properties, dict):
        for name in sorted(old_properties.keys() - new_properties.keys()):
            changes.append(
                _change(
                    "breaking",
                    "schema-property-removed",
                    f"{path}.properties.{name}",
                    "property was removed",
                )
            )
        for name in sorted(new_properties.keys() - old_properties.keys()):
            severity = "breaking" if name in new_required else "additive"
            changes.append(
                _change(
                    severity,
                    "schema-property-added",
                    f"{path}.properties.{name}",
                    "required property was added"
                    if severity == "breaking"
                    else "optional property was added",
                )
            )
        for name in sorted(old_properties.keys() & new_properties.keys()):
            old_property = old_properties[name]
            new_property = new_properties[name]
            if isinstance(old_property, dict) and isinstance(new_property, dict):
                changes.extend(
                    _compare_schema(
                        old_property,
                        new_property,
                        path=f"{path}.properties.{name}",
                    )
                )

    old_defs = old.get("$defs") or old.get("definitions") or {}
    new_defs = new.get("$defs") or new.get("definitions") or {}
    if isinstance(old_defs, dict) and isinstance(new_defs, dict):
        for name in sorted(old_defs.keys() - new_defs.keys()):
            changes.append(
                _change(
                    "breaking",
                    "schema-definition-removed",
                    f"{path}.$defs.{name}",
                    "schema definition was removed",
                )
            )
        for name in sorted(new_defs.keys() - old_defs.keys()):
            changes.append(
                _change(
                    "additive",
                    "schema-definition-added",
                    f"{path}.$defs.{name}",
                    "schema definition was added",
                )
            )
        for name in sorted(old_defs.keys() & new_defs.keys()):
            old_definition = old_defs[name]
            new_definition = new_defs[name]
            if isinstance(old_definition, dict) and isinstance(new_definition, dict):
                changes.extend(
                    _compare_schema(
                        old_definition,
                        new_definition,
                        path=f"{path}.$defs.{name}",
                    )
                )

    old_additional = old.get("additionalProperties", True)
    new_additional = new.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        changes.append(
            _change(
                "breaking",
                "schema-additional-properties-tightened",
                f"{path}.additionalProperties",
                "additional properties are no longer accepted",
            )
        )
    elif old_additional is False and new_additional is not False:
        changes.append(
            _change(
                "additive",
                "schema-additional-properties-relaxed",
                f"{path}.additionalProperties",
                "additional properties are now accepted",
            )
        )
    elif isinstance(old_additional, dict) and isinstance(new_additional, dict):
        changes.extend(
            _compare_schema(
                old_additional,
                new_additional,
                path=f"{path}.additionalProperties",
            )
        )

    old_items = old.get("items")
    new_items = new.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        changes.extend(_compare_schema(old_items, new_items, path=f"{path}.items"))
    elif old_items != new_items:
        changes.append(
            _change(
                "breaking",
                "schema-items-changed",
                f"{path}.items",
                "array item schema changed",
            )
        )

    for key, higher_is_tighter in (
        ("minimum", True),
        ("exclusiveMinimum", True),
        ("minLength", True),
        ("minItems", True),
        ("maximum", False),
        ("exclusiveMaximum", False),
        ("maxLength", False),
        ("maxItems", False),
    ):
        if old.get(key) != new.get(key):
            changes.extend(
                _compare_limit(
                    old,
                    new,
                    key=key,
                    path=path,
                    higher_is_tighter=higher_is_tighter,
                )
            )

    compared = {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "definitions",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "properties",
        "required",
        "type",
    }
    for key in sorted((old.keys() | new.keys()) - compared - _SCHEMA_ANNOTATIONS):
        if key.startswith("x-"):
            continue
        if old.get(key) != new.get(key):
            changes.append(
                _change(
                    "review",
                    "schema-keyword-changed",
                    f"{path}.{key}",
                    f"{old.get(key)!r} -> {new.get(key)!r}",
                )
            )
    return changes


def _compare_named_capabilities(
    old_items: Iterable[str],
    new_items: Iterable[str],
    *,
    path: str,
    noun: str,
) -> list[ContractChange]:
    old = set(old_items)
    new = set(new_items)
    changes = [
        _change("breaking", f"{noun}-removed", path, name)
        for name in sorted(old - new)
    ]
    changes.extend(
        _change("additive", f"{noun}-added", path, name)
        for name in sorted(new - old)
    )
    return changes


def _compare_rpc(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[ContractChange]:
    old_methods = {
        item["name"]: item.get("required_scope")
        for item in old.get("methods", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    new_methods = {
        item["name"]: item.get("required_scope")
        for item in new.get("methods", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    changes = _compare_named_capabilities(
        old_methods,
        new_methods,
        path="rpc-methods.json.methods",
        noun="rpc-method",
    )
    for name in sorted(old_methods.keys() & new_methods.keys()):
        if old_methods[name] != new_methods[name]:
            changes.append(
                _change(
                    "review",
                    "rpc-scope-changed",
                    f"rpc-methods.json.methods.{name}.required_scope",
                    f"{old_methods[name]!r} -> {new_methods[name]!r}",
                )
            )
    return changes


def _compare_events(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[ContractChange]:
    changes: list[ContractChange] = []
    for field, noun in (
        ("declared_events", "declared-event"),
        ("event_patterns", "event-pattern"),
    ):
        changes.extend(
            _compare_named_capabilities(
                old.get(field, []),
                new.get(field, []),
                path=f"events.json.{field}",
                noun=noun,
            )
        )
    return changes


def _route_key(route: Mapping[str, Any]) -> tuple[str, str]:
    return str(route.get("transport") or ""), str(route.get("path") or "")


def _compare_http(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[ContractChange]:
    old_routes = {
        _route_key(route): route
        for route in old.get("routes", [])
        if isinstance(route, dict)
    }
    new_routes = {
        _route_key(route): route
        for route in new.get("routes", [])
        if isinstance(route, dict)
    }
    changes: list[ContractChange] = []
    for transport, path in sorted(old_routes.keys() - new_routes.keys()):
        changes.append(
            _change(
                "breaking",
                "http-route-removed",
                f"http-routes.json.routes.{transport}:{path}",
                "route was removed",
            )
        )
    for transport, path in sorted(new_routes.keys() - old_routes.keys()):
        changes.append(
            _change(
                "additive",
                "http-route-added",
                f"http-routes.json.routes.{transport}:{path}",
                "route was added",
            )
        )
    for key in sorted(old_routes.keys() & new_routes.keys()):
        old_route = old_routes[key]
        new_route = new_routes[key]
        route_path = f"http-routes.json.routes.{key[0]}:{key[1]}"
        old_methods = set(old_route.get("methods") or [])
        new_methods = set(new_route.get("methods") or [])
        for method in sorted(old_methods - new_methods):
            changes.append(
                _change(
                    "breaking",
                    "http-method-removed",
                    f"{route_path}.methods",
                    method,
                )
            )
        for method in sorted(new_methods - old_methods):
            changes.append(
                _change(
                    "additive",
                    "http-method-added",
                    f"{route_path}.methods",
                    method,
                )
            )
        for field in ("auth", "origin", "surface"):
            if old_route.get(field) != new_route.get(field):
                changes.append(
                    _change(
                        "review",
                        f"http-{field}-changed",
                        f"{route_path}.{field}",
                        "security or exposure policy changed",
                    )
                )
    if old.get("policies") != new.get("policies"):
        changes.append(
            _change(
                "review",
                "http-global-policy-changed",
                "http-routes.json.policies",
                "global HTTP security policy changed",
            )
        )
    return changes


def _protocol_range(snapshot: ContractSnapshot) -> tuple[int, int, int]:
    hello = snapshot.files["golden/hello-ok.json"]
    protocol_range = hello.get("protocolRange")
    current = hello.get("protocol")
    if (
        not isinstance(protocol_range, dict)
        or type(protocol_range.get("min")) is not int
        or type(protocol_range.get("max")) is not int
        or type(current) is not int
    ):
        raise ContractCompatibilityError(
            f"{snapshot.source}/golden/hello-ok.json has an invalid protocol range"
        )
    minimum = protocol_range["min"]
    maximum = protocol_range["max"]
    if minimum > maximum or current < minimum or current > maximum:
        raise ContractCompatibilityError(
            f"{snapshot.source}/golden/hello-ok.json has an inconsistent protocol range"
        )
    return minimum, maximum, current


def _compare_protocol(
    baseline: ContractSnapshot,
    candidate: ContractSnapshot,
) -> list[ContractChange]:
    old_min, old_max, old_current = _protocol_range(baseline)
    new_min, new_max, new_current = _protocol_range(candidate)
    if max(old_min, new_min) > min(old_max, new_max):
        return [
            _change(
                "breaking",
                "protocol-range-disjoint",
                "golden/hello-ok.json.protocolRange",
                f"[{old_min}, {old_max}] -> [{new_min}, {new_max}]",
            )
        ]
    changes: list[ContractChange] = []
    if new_min > old_min or new_max < old_max:
        changes.append(
            _change(
                "review",
                "protocol-range-narrowed",
                "golden/hello-ok.json.protocolRange",
                f"[{old_min}, {old_max}] -> [{new_min}, {new_max}]",
            )
        )
    elif new_min < old_min or new_max > old_max:
        changes.append(
            _change(
                "additive",
                "protocol-range-widened",
                "golden/hello-ok.json.protocolRange",
                f"[{old_min}, {old_max}] -> [{new_min}, {new_max}]",
            )
        )
    if new_current != old_current:
        changes.append(
            _change(
                "review",
                "protocol-current-changed",
                "golden/hello-ok.json.protocol",
                f"{old_current} -> {new_current}",
            )
        )
    return changes


def compare_contracts(
    baseline: ContractSnapshot | None,
    candidate: ContractSnapshot,
) -> dict[str, Any]:
    _protocol_range(candidate)
    if baseline is None:
        return {
            "schemaVersion": 1,
            "status": "bootstrap",
            "blocking": False,
            "baseline": None,
            "candidate": {
                "source": candidate.source,
                "digest": candidate.digest,
            },
            "summary": {
                "breaking": 0,
                "review": 0,
                "additive": 0,
                "informational": 1,
            },
            "changes": [
                asdict(
                    _change(
                        "informational",
                        "baseline-unavailable",
                        DEFAULT_CONTRACT_PATH,
                        "first public contract release has no comparable baseline",
                    )
                )
            ],
        }

    changes: list[ContractChange] = []
    changes.extend(
        _compare_schema(
            baseline.files["protocol.schema.json"],
            candidate.files["protocol.schema.json"],
            path="protocol.schema.json",
        )
    )
    changes.extend(
        _compare_rpc(
            baseline.files["rpc-methods.json"],
            candidate.files["rpc-methods.json"],
        )
    )
    changes.extend(
        _compare_events(
            baseline.files["events.json"],
            candidate.files["events.json"],
        )
    )
    changes.extend(
        _compare_http(
            baseline.files["http-routes.json"],
            candidate.files["http-routes.json"],
        )
    )
    changes.extend(_compare_protocol(baseline, candidate))

    for relative in ("golden/connect.json", "golden/error.json"):
        if baseline.files[relative] != candidate.files[relative]:
            changes.append(
                _change(
                    "review",
                    "wire-behaviour-golden-changed",
                    relative,
                    "canonical request or error semantics changed",
                )
            )

    baseline_schema = baseline.files["contract.json"].get("schemaVersion")
    candidate_schema = candidate.files["contract.json"].get("schemaVersion")
    if baseline_schema != candidate_schema:
        changes.append(
            _change(
                "review",
                "contract-schema-version-changed",
                "contract.json.schemaVersion",
                f"{baseline_schema!r} -> {candidate_schema!r}",
            )
        )

    changes = sorted(
        set(changes),
        key=lambda item: (
            _SEVERITY_ORDER[item.severity],
            item.path,
            item.code,
            item.detail,
        ),
    )
    summary = {
        severity: sum(change.severity == severity for change in changes)
        for severity in _SEVERITY_ORDER
    }
    if summary["breaking"]:
        status = "breaking"
    elif summary["review"]:
        status = "review-required"
    else:
        status = "compatible"
    return {
        "schemaVersion": 1,
        "status": status,
        "blocking": status in {"breaking", "review-required"},
        "baseline": {
            "source": baseline.source,
            "digest": baseline.digest,
        },
        "candidate": {
            "source": candidate.source,
            "digest": candidate.digest,
        },
        "summary": summary,
        "changes": [asdict(change) for change in changes],
    }


def write_report(report: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline", type=Path)
    baseline.add_argument("--baseline-ref")
    parser.add_argument(
        "--allow-missing-baseline",
        action="store_true",
        help="emit a bootstrap report only when a valid Git baseline predates the contract",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=Path(DEFAULT_CONTRACT_PATH),
    )
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.baseline is not None:
            baseline = load_contract_directory(args.baseline)
        elif args.allow_missing_baseline and not git_ref_has_contract(
            args.baseline_ref,
            repository=args.repository,
        ):
            baseline = None
        else:
            baseline = load_contract_git_ref(args.baseline_ref, repository=args.repository)
        candidate = load_contract_directory(args.candidate)
        report = compare_contracts(baseline, candidate)
        write_report(report, args.output)
    except ContractCompatibilityError as error:
        print(f"client contract compatibility check failed: {error}", file=sys.stderr)
        return 2

    print(
        "Client contract compatibility: "
        f"{report['status']} "
        f"(breaking={report['summary']['breaking']}, "
        f"review={report['summary']['review']}, "
        f"additive={report['summary']['additive']})"
    )
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
