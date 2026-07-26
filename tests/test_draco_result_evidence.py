from __future__ import annotations

import json

from opensquilla.eval.draco_artifact_integrity import (
    RESULT_EVIDENCE_SCHEMA,
    compact_tool_result_diagnostic,
    seal_result_row,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.provider.types import ProviderBillingReceipt


def test_result_evidence_commits_to_every_nested_field() -> None:
    sealed = seal_result_row(
        {
            "row_index": 1,
            "group": "B2",
            "task_id": "task-1",
            "execution": {"generation_attempts": [{"attempt": 1}]},
            "judge": {"score_status": "complete"},
        }
    )

    assert sealed["result_evidence_schema"] == RESULT_EVIDENCE_SCHEMA
    assert verify_result_row_evidence(sealed) is True

    sealed["execution"]["generation_attempts"][0]["attempt"] = 2
    assert verify_result_row_evidence(sealed) is False


def test_seal_result_row_normalizes_nested_billing_receipt_for_json() -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=12_000_000,
        usd_equivalent_nanos=12_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    row = {
        "group": "B2",
        "task_id": "task-1",
        "generation_usage": {
            "model_usage_breakdown": [{"provider": "openrouter", "billing_receipt": receipt}]
        },
    }

    sealed = seal_result_row(row)

    assert sealed["generation_usage"]["model_usage_breakdown"][0]["billing_receipt"] == {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 12_000_000,
        "usd_equivalent_nanos": 12_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
    }
    assert row["generation_usage"]["model_usage_breakdown"][0]["billing_receipt"] is receipt
    round_tripped = json.loads(json.dumps(sealed, ensure_ascii=False, allow_nan=False))
    assert verify_result_row_evidence(sealed) is True
    assert verify_result_row_evidence(round_tripped) is True


def test_trace_is_an_exact_projection_bound_to_the_sealed_result() -> None:
    first = seal_result_row(
        {
            "row_index": 1,
            "group": "B2",
            "task_id": "task-1",
            "final_text_sha256": "a" * 64,
            "judge": {"mode": "draco_criterion_judgments"},
            "quality_total": 100.0,
        }
    )
    first_trace = trace_row_from_result(first)
    second = seal_result_row({**first, "quality_total": 0.0})
    second_trace = trace_row_from_result(second)

    assert first_trace["result_evidence_sha256"] == first["result_evidence_sha256"]
    assert first_trace != second_trace
    assert second_trace["judge"]["quality_total"] == 0.0


def test_tool_diagnostic_retains_status_without_copying_error_body() -> None:
    diagnostic = compact_tool_result_diagnostic(
        '{"ok":false,"error":{"message":"private response body"},'
        '"error_class":"ConnectTimeout","reason":"network timeout"}'
    )

    assert diagnostic["error_present"] is True
    assert diagnostic["ok"] is False
    assert diagnostic["error_class"] == "ConnectTimeout"
    assert diagnostic["reason"] == "network timeout"
    assert "private response body" not in repr(diagnostic)
