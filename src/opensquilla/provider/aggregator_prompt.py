"""Versioned, replayable aggregator prompt policy for routed ensembles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

AGGREGATOR_PROMPT_EVIDENCE_SCHEMA: Final = "opensquilla.router-dynamic-aggregator-prompt/v1"
AGGREGATOR_PROMPT_VERSION_CURRENT: Final = "aggregator-v1-current"
AGGREGATOR_PROMPT_VERSIONS: Final = frozenset(
    {
        AGGREGATOR_PROMPT_VERSION_CURRENT,
        "aggregator-v2-verify-first",
        "aggregator-v3-preserve-best",
    }
)

_PROMPT_CONTRACTS: Final = {
    AGGREGATOR_PROMPT_VERSION_CURRENT: {
        "description": (
            "Use the exact pre-versioning aggregator prompt. No version-specific "
            "instruction is inserted, so rendered prompts remain byte-equivalent "
            "to the current baseline for identical inputs and runtime flags."
        ),
        "additional_instructions": (),
    },
    "aggregator-v2-verify-first": {
        "description": (
            "Verify material candidate conflicts, calculations, dates, units, and "
            "source-to-claim alignment before synthesis; preserve unresolved "
            "uncertainty instead of blending incompatible claims."
        ),
        "additional_instructions": (
            "Verification mode: before synthesizing, compare candidate claims and "
            "identify material conflicts in facts, calculations, citations, and "
            "requested constraints.",
            "Use only claims supported by the original conversation or corroborated "
            "evidence. If a material conflict cannot be resolved, preserve the "
            "uncertainty explicitly instead of blending incompatible claims.",
            "Check calculations, units, dates, and source-to-claim alignment before "
            "writing the final answer.",
        ),
    },
    "aggregator-v3-preserve-best": {
        "description": (
            "Use the strongest candidate as the answer backbone and merge only "
            "compatible, supported additions, preserving useful specificity rather "
            "than averaging all drafts."
        ),
        "additional_instructions": (
            "Preserve-best mode: choose the strongest candidate draft as the backbone "
            "of the final answer rather than averaging all drafts.",
            "Merge only additions that are compatible with the backbone and supported "
            "by the original conversation or verified evidence.",
            "Preserve the backbone's useful specificity, calculations, citations, "
            "caveats, and structure; do not dilute them merely to include every "
            "candidate.",
        ),
    },
}


def _normalized_version(value: Any) -> str:
    version = str(value or AGGREGATOR_PROMPT_VERSION_CURRENT).strip()
    if version not in AGGREGATOR_PROMPT_VERSIONS:
        supported = ", ".join(sorted(AGGREGATOR_PROMPT_VERSIONS))
        raise ValueError(
            f"aggregator prompt version {version!r} is unsupported; expected one of {supported}"
        )
    return version


def aggregator_prompt_version_evidence(value: Any = None) -> dict[str, Any]:
    """Return the complete public prompt policy plus its canonical SHA-256."""

    version = _normalized_version(value)
    contract = _PROMPT_CONTRACTS[version]
    payload = {
        "schema": AGGREGATOR_PROMPT_EVIDENCE_SCHEMA,
        "version": version,
        "description": str(contract["description"]),
        "additional_instructions": list(contract["additional_instructions"]),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {**payload, "sha256": digest}


def aggregator_prompt_additional_instructions(value: Any = None) -> tuple[str, ...]:
    """Return only the exact version-specific lines inserted into the prompt."""

    version = _normalized_version(value)
    return tuple(str(item) for item in _PROMPT_CONTRACTS[version]["additional_instructions"])


def valid_aggregator_prompt_evidence(
    value: Any,
    *,
    expected_version: Any = None,
) -> bool:
    """Return whether evidence exactly matches the frozen in-code prompt contract."""

    if not isinstance(value, Mapping):
        return False
    try:
        expected = aggregator_prompt_version_evidence(expected_version)
    except ValueError:
        return False
    return dict(value) == expected
