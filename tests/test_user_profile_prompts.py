"""Prompt building + fail-open parsing of one batch's LLM reply.

The parser is the trust boundary: the LLM output is untrusted, so a malformed
reply must degrade to a failed batch (skipped, never aborting) and individual
bad items must be dropped rather than poison the batch. It also enforces the
denominator anchor — a label for a session that was never sent is discarded.
"""

from __future__ import annotations

import json

from opensquilla.squilla_router.user_profile.prompts import (
    SYSTEM_PROMPT,
    build_batch_prompt,
    parse_batch_response,
)
from opensquilla.squilla_router.user_profile.schema import SessionTranscript

_SENT = ("s1", "s2", "s3")


def _reply(**overrides: object) -> str:
    payload = {
        "session_labels": [
            {"session_id": "s1", "capability": "code_generation", "confidence": 0.9},
            {"session_id": "s2", "capability": "reasoning", "confidence": 0.7},
            {"session_id": "s3", "capability": "unknown", "confidence": 0.0},
        ],
        "quality_latency_tradeoff": {
            "value": "quality_first",
            "confidence": 0.8,
            "session_ids": ["s1", "s2"],
        },
        "cost_sensitivity": {
            "value": "unknown",
            "confidence": 0.0,
        },
        "model_mentions": [
            {
                "model_id": "deepseek-v4",
                "direction": "praise",
                "session_ids": ["s1"],
                "confidence": 0.9,
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_build_batch_prompt_carries_every_session_and_the_vocab() -> None:
    batch = [SessionTranscript("s1", "user: hi"), SessionTranscript("s2", "user: yo")]
    payload = json.loads(build_batch_prompt(batch))
    assert [s["session_id"] for s in payload["sessions"]] == ["s1", "s2"]
    assert "code_generation" in payload["allowed_capabilities"]
    assert "quality_first" in payload["allowed_tradeoffs"]


def test_system_prompt_forbids_continuing_the_task_and_quoting() -> None:
    assert "do not continue" in SYSTEM_PROMPT
    assert "never quote" in SYSTEM_PROMPT


def test_a_clean_reply_parses_into_a_batch_analysis() -> None:
    analysis = parse_batch_response(_reply(), _SENT)
    assert analysis.ok
    caps = {label.session_id: label.capability for label in analysis.session_labels}
    assert caps == {"s1": "code_generation", "s2": "reasoning", "s3": "unknown"}
    assert analysis.tradeoff == "quality_first"
    assert analysis.tradeoff_confidence == 0.8
    assert analysis.tradeoff_session_ids == ("s1", "s2")
    assert analysis.model_mentions[0].model_id == "deepseek-v4"


def test_prose_around_the_json_is_tolerated() -> None:
    text = "Sure, here is the analysis:\n" + _reply() + "\nHope that helps!"
    assert parse_batch_response(text, _SENT).ok


def test_non_json_is_a_failed_batch_not_a_raise() -> None:
    analysis = parse_batch_response("I cannot help with that.", _SENT)
    assert analysis.ok is False
    assert analysis.session_ids == _SENT


def test_incomplete_or_wrong_root_contract_fails_the_batch() -> None:
    assert parse_batch_response("{}", _SENT).ok is False
    assert parse_batch_response(json.dumps({"session_labels": []}), _SENT).ok is False
    assert parse_batch_response(
        _reply(cost_sensitivity=[]),
        _SENT,
    ).ok is False


def test_bare_nonfinite_json_constants_are_rejected() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning", "confidence": float("nan")},
            {"session_id": "s2", "capability": "writing", "confidence": 0.5},
            {"session_id": "s3", "capability": "unknown", "confidence": 0.0},
        ]
    )
    assert "NaN" in reply
    assert parse_batch_response(reply, _SENT).ok is False


def test_string_nan_and_nonfinite_confidence_coerce_to_zero() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning", "confidence": "NaN"},
            {"session_id": "s2", "capability": "writing", "confidence": "Infinity"},
            {"session_id": "s3", "capability": "unknown", "confidence": -1},
        ],
        quality_latency_tradeoff={
            "value": "quality_first",
            "confidence": "NaN",
            "session_ids": ["s1"],
        },
        cost_sensitivity={"value": "high", "confidence": "Infinity"},
        model_mentions=[
            {
                "model_id": "x",
                "direction": "praise",
                "session_ids": ["s1"],
                "confidence": "-Infinity",
            }
        ],
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.ok
    assert [label.confidence for label in analysis.session_labels] == [0.0, 0.0, 0.0]
    assert analysis.tradeoff_confidence == 0.0
    assert analysis.cost_sensitivity_confidence == 0.0
    assert analysis.model_mentions[0].confidence == 0.0


def test_missing_or_foreign_session_labels_fail_the_batch() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning"},
            {"session_id": "s2", "capability": "writing"},
            {"session_id": "not-sent", "capability": "writing"},
        ]
    )
    assert parse_batch_response(reply, _SENT).ok is False


def test_non_list_labels_fail_the_batch() -> None:
    assert parse_batch_response(_reply(session_labels={}), _SENT).ok is False


def test_invalid_capability_fails_the_batch_but_unknown_is_valid() -> None:
    invalid = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "telepathy"},
            {"session_id": "s2", "capability": "reasoning"},
            {"session_id": "s3", "capability": "unknown"},
        ]
    )
    assert parse_batch_response(invalid, _SENT).ok is False
    valid = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "unknown"},
            {"session_id": "s2", "capability": "reasoning"},
            {"session_id": "s3", "capability": "unknown"},
        ]
    )
    assert parse_batch_response(valid, _SENT).ok is True


def test_a_duplicate_session_label_fails_the_batch() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning"},
            {"session_id": "s1", "capability": "writing"},
            {"session_id": "s2", "capability": "writing"},
            {"session_id": "s3", "capability": "unknown"},
        ]
    )
    assert parse_batch_response(reply, _SENT).ok is False


def test_a_mention_of_an_unrated_direction_is_dropped() -> None:
    reply = _reply(model_mentions=[{"model_id": "x", "direction": "meh", "session_ids": ["s1"]}])
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.model_mentions == ()
    assert analysis.dropped_model_mentions == 1


def test_mention_with_foreign_session_id_is_dropped_as_malformed() -> None:
    reply = _reply(
        model_mentions=[
            {
                "model_id": "x",
                "direction": "praise",
                "session_ids": ["s1", "ghost"],
            }
        ]
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.model_mentions == ()
    assert analysis.dropped_model_mentions == 1


def test_model_mentions_without_valid_session_ids_are_dropped() -> None:
    reply = _reply(
        model_mentions=[
            {
                "model_id": "x",
                "direction": "praise",
                "session_ids": ["ghost"],
            }
        ]
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.model_mentions == ()
    assert analysis.dropped_model_mentions == 1


def test_an_unknown_tradeoff_is_no_batch_vote() -> None:
    reply = _reply(
        quality_latency_tradeoff={
            "value": "unknown",
            "confidence": 0.2,
            "session_ids": [],
        }
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.tradeoff == "unknown"  # excluded from the builder's vote


def test_a_real_tradeoff_without_valid_evidence_is_no_batch_vote() -> None:
    reply = _reply(
        quality_latency_tradeoff={
            "value": "quality_first",
            "confidence": 0.9,
            "session_ids": ["ghost"],
        }
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.ok is False
