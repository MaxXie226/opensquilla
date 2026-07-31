from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "experiments" / "recover_draco_finalization.py"


def _load():
    spec = importlib.util.spec_from_file_location("recover_draco_finalization_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _artifact_record(module, path: Path) -> dict[str, Any]:
    path_stat = path.stat()
    return {
        "path": path.name,
        "sha256": module._file_sha256(path),
        "size_bytes": path_stat.st_size,
        "mode": oct(stat.S_IMODE(path_stat.st_mode)),
    }


def _campaign_fixture(module, tmp_path: Path) -> tuple[Path, Path]:
    campaign = tmp_path / "campaign"
    archive = campaign / "archive"
    account = archive / "account"
    wave = archive / "waves" / "wave-1"
    account.mkdir(parents=True)
    wave.mkdir(parents=True)
    campaign.chmod(0o700)
    archive.chmod(0o700)
    account.chmod(0o700)
    wave.chmod(0o700)

    frozen_input = tmp_path / "mini.jsonl"
    _write_text(frozen_input, '{"id":"task-1"}\n')
    result = wave / "draco_ensemble_20260101-000000.jsonl"
    _write_text(result, '{"group":"B0","task_id":"task-1"}\n')
    source_manifest = wave / "draco_run_20260101-000000.manifest.json"
    _write_json(
        source_manifest,
        {
            "args": {"input": str(frozen_input)},
            "groups": ["B0"],
            "status": "result_incomplete",
        },
    )

    lock_file = tmp_path / "openrouter.lock"
    _write_text(lock_file, "")
    lock_inode = lock_file.stat().st_ino
    _write_json(account / "openrouter-account-before.json", {"usage": "1"})
    _write_json(account / "openrouter-account-after.json", {"usage": "2"})
    _write_json(
        account / "openrouter-account-reconciliation.json",
        {
            "schema": "opensquilla.openrouter-account-reconciliation/v1",
            "settlement_status": "stable",
            "lock_file": str(lock_file),
            "lock_inode": lock_inode,
        },
    )
    _write_json(
        archive / "runtime-environment.json",
        {
            "schema": "opensquilla.draco-runtime-environment/v1",
            "environment": {},
        },
    )
    return campaign, lock_file


def _fake_finalizer(module, plan, _lock_fd: int) -> dict[str, Any]:
    formal = plan.formal_dir
    formal.mkdir(mode=0o700)
    _write_text(formal / "results.jsonl", '{"group":"B0","task_id":"task-1"}\n')
    _write_text(formal / "trace.jsonl", '{"group":"B0","task_id":"task-1"}\n')
    _write_text(formal / "actual-spend-ledger.jsonl", "")
    reconciliation = {
        "pass": True,
        "status": "exact",
        "gap_usd": "0",
        "tolerance_usd": "0.000001",
    }
    _write_json(
        formal / "openrouter-non-byok-campaign-proof.json",
        {
            "pass": True,
            "publication_eligible": True,
            "execution_pass": True,
            "policy_pass": True,
            "reconciliation": reconciliation,
            "status": "passed",
            "cost_scope": {
                "ledger_window_reconciliation": [
                    {
                        "reconciliation_status": "exact",
                        "reconciliation_gap_usd": "0",
                        "unknown_cost_request_count": 0,
                        "non_exact_cost_request_count": 0,
                    }
                ]
            },
        },
    )
    _write_json(
        formal / "audit.json",
        {
            "pass": True,
            "status": "passed",
            "execution_pass": True,
            "policy_pass": True,
            "reconciliation": reconciliation,
            "warnings": [],
        },
    )
    _write_text(formal / "EXPERIMENT_RESULTS.md", "# Complete\n")

    current_sources = [
        plan.account_before,
        plan.account_after,
        plan.account_reconciliation,
        plan.runtime_environment,
    ]
    manifest: dict[str, Any] = {
        "schema": module.MANIFEST_SCHEMA,
        "status": "complete",
        "execution_pass": True,
        "policy_pass": True,
        "reconciliation": reconciliation,
        "audit_pass": True,
        "audit_status": "passed",
        "warnings": [],
        "finalizer_version": 5,
        "groups": ["B0"],
        "task_count": 1,
        "result_count": 1,
        "input": {
            "path": str(plan.input_path),
            "sha256": module._file_sha256(plan.input_path),
        },
        "source_results": [
            {
                "path": str(plan.results[0]),
                "sha256": module._file_sha256(plan.results[0]),
            }
        ],
        "source_manifests": [
            {
                "path": str(plan.manifests[0]),
                "sha256": module._file_sha256(plan.manifests[0]),
                "result_path": str(plan.results[0]),
                "result_sha256": module._file_sha256(plan.results[0]),
            }
        ],
        "cost_attribution": {
            "account_windows": [
                {
                    "kind": "current",
                    "sources": [
                        {
                            "path": str(path),
                            "sha256": module._file_sha256(path),
                        }
                        for path in current_sources
                    ],
                }
            ]
        },
        "artifacts": {
            name: _artifact_record(module, formal / name) for name in module.ARTIFACT_NAMES
        },
    }
    manifest["manifest_sha256"] = module._canonical_sha256(manifest)
    _write_json(formal / "manifest.json", manifest)
    return manifest


def _rewrite_formal_states(
    module,
    formal: Path,
    *,
    audit_pass: bool,
    policy_pass: bool,
    proof_pass: bool,
    publication_eligible: bool,
    reconciliation: dict[str, Any],
    warnings: list[str],
) -> None:
    audit_status = "passed" if audit_pass else "complete_with_warnings"
    audit = json.loads((formal / "audit.json").read_text(encoding="utf-8"))
    audit.update(
        {
            "pass": audit_pass,
            "status": audit_status,
            "execution_pass": True,
            "policy_pass": policy_pass,
            "reconciliation": reconciliation,
            "warnings": warnings,
        }
    )
    proof = json.loads(
        (formal / "openrouter-non-byok-campaign-proof.json").read_text(encoding="utf-8")
    )
    proof.update(
        {
            "pass": proof_pass,
            "publication_eligible": publication_eligible,
            "execution_pass": True,
            "policy_pass": policy_pass,
            "reconciliation": reconciliation,
            "status": (
                "policy_failed"
                if not policy_pass
                else "passed"
                if reconciliation.get("pass") is True
                else "reconciliation_incomplete"
            ),
            "cost_scope": {
                "ledger_window_reconciliation": [
                    {
                        "reconciliation_status": reconciliation["status"],
                        "reconciliation_gap_usd": reconciliation["gap_usd"],
                        "unknown_cost_request_count": (
                            0 if reconciliation.get("pass") is True else 1
                        ),
                        "non_exact_cost_request_count": 0,
                    }
                ]
            },
        }
    )
    _write_json(formal / "audit.json", audit)
    _write_json(formal / "openrouter-non-byok-campaign-proof.json", proof)

    manifest = json.loads((formal / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "execution_pass": True,
            "policy_pass": policy_pass,
            "reconciliation": reconciliation,
            "audit_pass": audit_pass,
            "audit_status": audit_status,
            "warnings": warnings,
        }
    )
    manifest["artifacts"] = {
        name: _artifact_record(module, formal / name) for name in module.ARTIFACT_NAMES
    }
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = module._canonical_sha256(manifest)
    _write_json(formal / "manifest.json", manifest)


def test_status_reports_ready_without_writing_campaign(module, tmp_path: Path) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)

    before = sorted(str(path.relative_to(campaign)) for path in campaign.rglob("*"))
    status = module.inspect_campaign(campaign)
    after = sorted(str(path.relative_to(campaign)) for path in campaign.rglob("*"))

    assert status["state"] == "ready"
    assert status["reason_code"] == "settled_evidence_ready_for_offline_finalizer"
    assert status["offline_only"] is True
    assert status["model_requests_allowed"] is False
    assert status["account_settlement_status"] == "stable"
    assert status["source_result_count"] == 1
    assert before == after
    assert not (campaign / "archive" / module.STATUS_NAME).exists()


def test_status_blocks_offline_recovery_when_terminal_model_budget_is_exhausted(
    module,
    tmp_path: Path,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    gates = campaign / "archive" / "gates"
    gates.mkdir()
    _write_json(
        gates / "wave-2-summary.json",
        {
            "generation_budget_exhausted_pair_count": 1,
            "generation_budget_exhausted_pairs": [
                {
                    "group": "G1",
                    "task_id": "task-1",
                    "action": "regenerate",
                }
            ],
            "judge_budget_exhausted_pair_count": 0,
            "judge_budget_exhausted_pairs": [],
        },
    )

    status = module.inspect_campaign(campaign)

    assert status["state"] == "blocked"
    assert status["reason_code"] == "model_attempt_budget_exhausted"
    assert status["generation_budget_exhausted_pair_count"] == 1
    assert status["judge_budget_exhausted_pair_count"] == 0
    assert status["offline_only"] is True
    assert status["model_requests_allowed"] is False
    assert not (campaign / "archive" / module.STATUS_NAME).exists()


def test_recovery_publishes_manifest_last_and_is_idempotent(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    replaced_root_names: list[str] = []
    real_replace = module.os.replace

    def recording_replace(source, destination) -> None:
        destination_path = Path(destination)
        if destination_path.parent == campaign and destination_path.name in module.FORMAL_NAMES:
            replaced_root_names.append(destination_path.name)
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", recording_replace)
    status = module.recover_campaign(
        campaign,
        finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
        _runner=lambda plan, lock_fd: _fake_finalizer(module, plan, lock_fd),
    )

    assert status["state"] == "complete"
    assert status["result_count"] == 1
    assert replaced_root_names[-1] == "manifest.json"
    assert set(replaced_root_names[:-1]) == set(module.ARTIFACT_NAMES)
    assert not (campaign / module.FORMAL_STAGING_NAME).exists()
    assert (campaign / "manifest.json").is_file()
    assert (
        json.loads((campaign / "archive" / module.STATUS_NAME).read_text(encoding="utf-8"))["state"]
        == "complete"
    )

    def must_not_run(_plan, _lock_fd):
        raise AssertionError("an already completed campaign must not rerun the finalizer")

    second = module.recover_campaign(
        campaign,
        finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
        _runner=must_not_run,
    )
    assert second["state"] == "complete"


def test_valid_staging_resumes_publication_without_rerunning_finalizer(
    module,
    tmp_path: Path,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)

    assert module.inspect_campaign(campaign)["state"] == "publish_ready"

    def must_not_run(_plan, _lock_fd):
        raise AssertionError("valid formal staging must be published without recomputation")

    status = module.recover_campaign(
        campaign,
        finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
        _runner=must_not_run,
    )
    assert status["state"] == "complete"
    assert (campaign / "manifest.json").is_file()


def test_recovery_publishes_execution_complete_with_reconciliation_warning(
    module,
    tmp_path: Path,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)
    reconciliation = {
        "pass": False,
        "status": "account_exact_per_request_incomplete",
        "gap_usd": "0E-9",
        "tolerance_usd": "0.000001",
    }
    _rewrite_formal_states(
        module,
        plan.formal_dir,
        audit_pass=False,
        policy_pass=True,
        proof_pass=True,
        publication_eligible=False,
        reconciliation=reconciliation,
        warnings=["per-request cost evidence is incomplete"],
    )

    inspected = module.inspect_campaign(campaign)
    assert inspected["state"] == "publish_ready"
    assert inspected["execution_pass"] is True
    assert inspected["audit_pass"] is False
    assert inspected["audit_status"] == "complete_with_warnings"
    assert inspected["policy_pass"] is True
    assert inspected["reconciliation"] == reconciliation

    def must_not_run(_plan, _lock_fd):
        raise AssertionError("coherent warning-only staging must not rerun the finalizer")

    status = module.recover_campaign(
        campaign,
        finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
        _runner=must_not_run,
    )
    assert status["state"] == "complete"
    assert status["execution_pass"] is True
    assert status["audit_pass"] is False
    assert (campaign / "manifest.json").is_file()


def test_recovery_publishes_explicitly_eligible_policy_warning(
    module,
    tmp_path: Path,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)
    reconciliation = {
        "pass": True,
        "status": "exact",
        "gap_usd": "0",
        "tolerance_usd": "0.000001",
    }
    _rewrite_formal_states(
        module,
        plan.formal_dir,
        audit_pass=False,
        policy_pass=False,
        proof_pass=False,
        publication_eligible=True,
        reconciliation=reconciliation,
        warnings=["explicit BYOK evidence is preserved as a policy warning"],
    )

    status = module.recover_campaign(
        campaign,
        finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
        _runner=lambda _plan, _lock_fd: (_ for _ in ()).throw(
            AssertionError("eligible policy warning must not rerun the finalizer")
        ),
    )
    assert status["state"] == "complete"
    assert status["execution_pass"] is True
    assert status["audit_pass"] is False
    assert status["policy_pass"] is False


def test_recovery_rejects_incoherent_publication_states(module, tmp_path: Path) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)
    manifest_path = plan.formal_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy_pass"] = False
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = module._canonical_sha256(manifest)
    _write_json(manifest_path, manifest)

    status = module.inspect_campaign(campaign)
    assert status["state"] == "blocked"
    assert status["reason_code"] == "policy_state_mismatch"
    assert not (campaign / "manifest.json").exists()


def test_recovery_rejects_warning_only_account_gap(module, tmp_path: Path) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)
    _rewrite_formal_states(
        module,
        plan.formal_dir,
        audit_pass=False,
        policy_pass=True,
        proof_pass=True,
        publication_eligible=False,
        reconciliation={
            "pass": False,
            "status": "account_exact_per_request_incomplete",
            "gap_usd": "0.01",
            "tolerance_usd": "0.000001",
        },
        warnings=["per-request cost evidence is incomplete"],
    )

    status = module.inspect_campaign(campaign)
    assert status["state"] == "blocked"
    assert status["reason_code"] == "unsafe_incomplete_reconciliation"
    assert not (campaign / "manifest.json").exists()


def test_staging_bound_to_an_older_wave_set_is_blocked(module, tmp_path: Path) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    original_plan = module.discover_plan(campaign)
    _fake_finalizer(module, original_plan, -1)

    second_wave = campaign / "archive" / "waves" / "wave-2"
    second_wave.mkdir()
    _write_text(
        second_wave / "draco_ensemble_20260101-000100.jsonl",
        '{"group":"B0","task_id":"task-1"}\n',
    )
    _write_json(
        second_wave / "draco_run_20260101-000100.manifest.json",
        {
            "args": {"input": str(original_plan.input_path)},
            "groups": ["B0"],
            "status": "metadata_incomplete",
        },
    )

    status = module.inspect_campaign(campaign)

    assert status["state"] == "blocked"
    assert status["reason_code"] == "source_set_changed"
    assert not (campaign / "manifest.json").exists()


def test_manifest_publication_failure_rolls_everything_back(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    plan = module.discover_plan(campaign)
    _fake_finalizer(module, plan, -1)
    real_replace = module.os.replace

    def fail_manifest_commit(source, destination) -> None:
        if Path(destination) == campaign / "manifest.json":
            raise OSError("simulated manifest commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_manifest_commit)
    with pytest.raises(OSError, match="simulated manifest commit failure"):
        module.publish_manifest_last(plan)

    assert not (campaign / "manifest.json").exists()
    assert not any((campaign / name).exists() for name in module.ARTIFACT_NAMES)
    assert {path.name for path in plan.formal_dir.iterdir()} == set(module.FORMAL_NAMES)


def test_partial_root_publication_is_blocked(module, tmp_path: Path) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)
    _write_text(campaign / "results.jsonl", "{}\n")

    status = module.inspect_campaign(campaign)

    assert status["state"] == "blocked"
    assert status["reason_code"] == "partial_root_publication"


def test_unexpected_inspection_error_is_still_machine_readable(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _ = _campaign_fixture(module, tmp_path)

    def fail_io(_archive_dir):
        raise OSError("simulated unreadable archive")

    monkeypatch.setattr(module, "_wave_sources", fail_io)
    status = module.inspect_campaign(campaign)

    assert status["state"] == "blocked"
    assert status["reason_code"] == "inspection_failed"
    assert status["error"]["type"] == "OSError"


def test_changed_lock_inode_fails_closed_and_persists_failure_status(
    module,
    tmp_path: Path,
) -> None:
    campaign, lock_file = _campaign_fixture(module, tmp_path)
    replacement = lock_file.with_suffix(".replacement")
    _write_text(replacement, "")
    os.replace(replacement, lock_file)

    with pytest.raises(module.RecoveryError, match="lock no longer matches"):
        module.recover_campaign(
            campaign,
            finalizer_path=SCRIPT.with_name("finalize_draco_campaign.py"),
            _runner=lambda plan, lock_fd: _fake_finalizer(module, plan, lock_fd),
        )

    status = json.loads((campaign / "archive" / module.STATUS_NAME).read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["reason_code"] == "lock_binding_changed"
    assert not (campaign / "manifest.json").exists()


def test_secret_environment_is_scrubbed_without_exposing_values(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-survive")
    monkeypatch.setenv("EXAMPLE_ACCESS_TOKEN", "must-not-survive")
    monkeypatch.setenv("UNRELATED_SETTING", "preserved")

    module._scrub_secret_environment()

    assert "OPENROUTER_API_KEY" not in os.environ
    assert "EXAMPLE_ACCESS_TOKEN" not in os.environ
    assert os.environ["UNRELATED_SETTING"] == "preserved"
    assert os.environ["OPENSQUILLA_OFFLINE"] == "1"


def test_offline_audit_guard_blocks_socket_creation_in_isolated_process() -> None:
    code = f"""
import importlib.util
import socket
import sys

spec = importlib.util.spec_from_file_location("recovery_guard_test", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._install_offline_audit_guard()
try:
    socket.socket()
except module.OfflinePolicyError as exc:
    print(exc.code)
else:
    raise SystemExit("socket creation unexpectedly succeeded")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "offline_policy_violation"
