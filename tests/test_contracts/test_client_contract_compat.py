from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.check_client_contract_compat import (
    CONTRACT_FILES,
    ContractSnapshot,
    compare_contracts,
    load_contract_directory,
    main,
)


def _files() -> dict[str, dict]:
    return {
        "contract.json": {
            "schemaVersion": 1,
            "digest": "sha256:" + ("1" * 64),
        },
        "protocol.schema.json": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["request", "response"]},
                "payload": {"type": "object"},
            },
            "required": ["kind"],
            "additionalProperties": True,
        },
        "rpc-methods.json": {
            "methods": [
                {"name": "health", "required_scope": "operator.read"},
                {"name": "chat.send", "required_scope": "operator.write"},
            ]
        },
        "events.json": {
            "declared_events": ["health"],
            "event_patterns": ["session.event.*"],
            "observed_only": ["session.event.text_delta"],
        },
        "http-routes.json": {
            "policies": {"origin": "same-origin"},
            "routes": [
                {
                    "transport": "http",
                    "path": "/healthz",
                    "methods": ["GET"],
                    "auth": {"mode": "none"},
                    "origin": {"policy": "not-required"},
                    "surface": "operations",
                }
            ],
        },
        "golden/connect.json": {
            "type": "req",
            "method": "connect",
            "params": {"minProtocol": 1, "maxProtocol": 3},
        },
        "golden/error.json": {
            "type": "res",
            "ok": False,
            "error": {"code": "INVALID_REQUEST", "retryable": False},
        },
        "golden/hello-ok.json": {
            "type": "hello-ok",
            "protocol": 3,
            "protocolRange": {"min": 1, "max": 3},
        },
    }


def _snapshot(source: str, files: dict[str, dict]) -> ContractSnapshot:
    return ContractSnapshot(source=source, files=files)


def _write_bundle(root: Path, files: dict[str, dict]) -> None:
    for relative in CONTRACT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(files[relative], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def test_additive_contract_changes_are_compatible() -> None:
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_files["protocol.schema.json"]["properties"]["traceId"] = {
        "type": ["string", "null"]
    }
    candidate_files["rpc-methods.json"]["methods"].append(
        {"name": "sessions.list", "required_scope": "operator.read"}
    )
    candidate_files["events.json"]["declared_events"].append("sessions.changed")
    candidate_files["http-routes.json"]["routes"].append(
        {
            "transport": "http",
            "path": "/api/sessions",
            "methods": ["GET"],
            "auth": {"mode": "token"},
            "origin": {"policy": "same-origin"},
            "surface": "client-api",
        }
    )

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "compatible"
    assert report["blocking"] is False
    assert report["summary"]["additive"] == 4
    assert report["summary"]["breaking"] == 0
    assert report["summary"]["review"] == 0


def test_breaking_contract_changes_fail_closed() -> None:
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_schema = candidate_files["protocol.schema.json"]
    candidate_schema["required"].append("payload")
    candidate_schema["properties"]["kind"]["enum"] = ["request"]
    candidate_files["rpc-methods.json"]["methods"] = [
        {"name": "health", "required_scope": "operator.read"}
    ]
    candidate_files["golden/hello-ok.json"]["protocol"] = 5
    candidate_files["golden/hello-ok.json"]["protocolRange"] = {"min": 5, "max": 6}

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "breaking"
    assert report["blocking"] is True
    codes = {change["code"] for change in report["changes"]}
    assert "schema-required-added" in codes
    assert "schema-enum-narrowed" in codes
    assert "rpc-method-removed" in codes
    assert "protocol-range-disjoint" in codes


def test_security_and_wire_semantic_changes_require_review() -> None:
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_files["rpc-methods.json"]["methods"][0]["required_scope"] = "operator.admin"
    candidate_files["http-routes.json"]["routes"][0]["auth"] = {"mode": "token"}
    candidate_files["golden/error.json"]["error"]["retryable"] = True

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "review-required"
    assert report["blocking"] is True
    assert {change["code"] for change in report["changes"]} == {
        "http-auth-changed",
        "rpc-scope-changed",
        "wire-behaviour-golden-changed",
    }


def test_observed_only_events_do_not_define_a_stable_compatibility_surface() -> None:
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_files["events.json"]["observed_only"] = ["new.observed.event"]

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "compatible"
    assert report["changes"] == []


def test_optional_field_inside_a_stable_union_variant_is_additive() -> None:
    baseline_files = _files()
    baseline_files["protocol.schema.json"]["properties"]["payload"] = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            {"type": "null"},
        ]
    }
    candidate_files = copy.deepcopy(baseline_files)
    object_variant = candidate_files["protocol.schema.json"]["properties"]["payload"][
        "anyOf"
    ][0]
    object_variant["properties"]["traceId"] = {"type": "string"}
    object_variant["description"] = "Annotation changes do not affect compatibility."

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "compatible"
    assert [change["code"] for change in report["changes"]] == ["schema-property-added"]


def test_widened_existing_field_type_requires_review() -> None:
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_files["protocol.schema.json"]["properties"]["payload"]["type"] = [
        "object",
        "null",
    ]

    report = compare_contracts(
        _snapshot("baseline", baseline_files),
        _snapshot("candidate", candidate_files),
    )

    assert report["status"] == "review-required"
    assert [change["code"] for change in report["changes"]] == ["schema-type-widened"]


def test_directory_cli_writes_a_machine_readable_report(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "report.json"
    files = _files()
    _write_bundle(baseline, files)
    _write_bundle(candidate, copy.deepcopy(files))

    assert main(
        [
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
        ]
    ) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "compatible"
    assert report["baseline"]["digest"] == files["contract.json"]["digest"]
    assert load_contract_directory(candidate).digest == files["contract.json"]["digest"]


def test_cli_returns_nonzero_and_keeps_report_for_breaking_change(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "report.json"
    baseline_files = _files()
    candidate_files = copy.deepcopy(baseline_files)
    candidate_files["rpc-methods.json"]["methods"] = []
    _write_bundle(baseline, baseline_files)
    _write_bundle(candidate, candidate_files)

    assert main(
        [
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
        ]
    ) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "breaking"
