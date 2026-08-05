from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/experiments/build_draco_p1_campaign_plan.py"
)
SPEC = importlib.util.spec_from_file_location("build_draco_p1_campaign_plan", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_task_ids_require_exact_unique_ten(tmp_path: Path) -> None:
    benchmark = tmp_path / "mini.jsonl"
    benchmark.write_text(
        "".join(json.dumps({"id": f"task-{index}"}) + "\n" for index in range(10)),
        encoding="utf-8",
    )
    assert module._task_ids(benchmark) == [f"task-{index}" for index in range(10)]
    benchmark.write_text(
        "".join(json.dumps({"id": "duplicate"}) + "\n" for _ in range(10)),
        encoding="utf-8",
    )
    with pytest.raises(module.BuildError, match="invalid task id"):
        module._task_ids(benchmark)


def test_cli_freezes_p1_35_progression_threshold() -> None:
    args = module.parse_args(
        [
            "--run-id",
            "p1-test",
            "--run-root",
            "/tmp/run",
            "--report-root",
            "/tmp/report",
            "--snapshot",
            "/tmp/snapshot",
            "--reference-repo",
            "/tmp/reference",
            "--python",
            "/tmp/python",
            "--source-repo",
            "/tmp/docs",
            "--source-document",
            "/tmp/docs/plan.md",
            "--e0-source-plan",
            "/tmp/p0/plan.json",
            "--e0-source-snapshot",
            "/tmp/p0/snapshot",
            "--e0-source-output",
            "/tmp/p0/output",
            "--controller-unit",
            "p1.service",
            "--global-openrouter-lock",
            "/tmp/p1.lock",
            "--openrouter-secret-file",
            "/tmp/openrouter.key",
        ]
    )
    assert args.p1_35_sufficient_cost_reduction == pytest.approx(0.10)
    assert args.output is None
