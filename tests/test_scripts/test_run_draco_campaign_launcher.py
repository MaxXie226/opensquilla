from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "experiments" / "run_draco_mini_b0_b1_b2_b4_g1_campaign.sh"


def _script() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def _embedded_python_blocks() -> list[str]:
    return re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", _script(), flags=re.DOTALL)


def _bash_function(name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n",
        _script(),
    )
    assert match is not None
    return match.group(0)


def _run_embedded_python(
    block: str,
    *args: str,
    lock_fd: int,
) -> subprocess.CompletedProcess[str]:
    try:
        saved_fd9 = os.dup(9)
    except OSError:
        saved_fd9 = None
    os.dup2(lock_fd, 9)
    os.set_inheritable(9, True)
    try:
        return subprocess.run(
            [sys.executable, "-c", block, *args],
            check=False,
            capture_output=True,
            text=True,
            pass_fds=(9,),
        )
    finally:
        if saved_fd9 is None:
            os.close(9)
        else:
            os.dup2(saved_fd9, 9)
            os.close(saved_fd9)


def test_launcher_is_valid_bash_without_executing_it() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_embedded_python_blocks_compile_without_executing_them() -> None:
    blocks = _embedded_python_blocks()
    assert len(blocks) == 4
    for index, block in enumerate(blocks, start=1):
        compile(block, f"{LAUNCHER.name}:embedded-python-{index}", "exec")


def test_lock_validation_runs_before_paid_window_and_fails_closed(
    tmp_path: Path,
) -> None:
    lock_block = _embedded_python_blocks()[0]
    lock_path = tmp_path / "campaign.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        valid = _run_embedded_python(
            lock_block,
            str(lock_path),
            lock_fd=lock_fd,
        )
        assert valid.returncode == 0, valid.stderr

        lock_path.chmod(0o644)
        unsafe = _run_embedded_python(
            lock_block,
            str(lock_path),
            lock_fd=lock_fd,
        )
        assert unsafe.returncode != 0
        assert "mode 600" in unsafe.stderr
    finally:
        os.close(lock_fd)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _account_snapshot(
    *,
    captured_at: datetime,
    usage: str,
    fingerprint: str,
) -> dict[str, object]:
    return {
        "captured_at": captured_at.isoformat(),
        "usage": usage,
        "byok_usage": "0",
        "api_key_sha256": fingerprint,
        "benchmark_environment_key_verified": True,
        "is_free_tier": False,
    }


def test_reconciliation_requires_long_stable_same_key_non_byok_window(
    tmp_path: Path,
) -> None:
    reconciliation_block = _embedded_python_blocks()[1]
    fingerprint = "a" * 64
    base = datetime(2026, 7, 25, tzinfo=UTC)
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    output_path = tmp_path / "reconciliation.json"
    runtime_path = tmp_path / "runtime.json"
    lock_path = tmp_path / "campaign.lock"
    lock_path.touch(mode=0o600)
    _write_json(
        before_path,
        _account_snapshot(
            captured_at=base - timedelta(seconds=1),
            usage="10.0",
            fingerprint=fingerprint,
        ),
    )
    _write_json(
        runtime_path,
        {"environment_sha256": "b" * 64},
    )
    offsets = (0, 60, 120, 180, 195, 210)
    poll_paths: list[Path] = []
    for index, offset in enumerate(offsets, start=1):
        poll_path = tmp_path / f"poll-{index}.json"
        _write_json(
            poll_path,
            _account_snapshot(
                captured_at=base + timedelta(seconds=offset),
                usage="11.25",
                fingerprint=fingerprint,
            ),
        )
        poll_paths.append(poll_path)
    after_path.write_bytes(poll_paths[-1].read_bytes())

    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            reconciliation_block,
            str(before_path),
            str(after_path),
            str(output_path),
            str(runtime_path),
            str(lock_path),
            "180",
            "6",
            "15",
            *(str(path) for path in poll_paths),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode == 0, completed.stderr
    reconciliation = json.loads(output_path.read_text(encoding="utf-8"))
    assert reconciliation["usage_delta_usd"] == "1.25"
    assert reconciliation["byok_usage_delta_usd"] == "0"
    assert reconciliation["stable_poll_count"] == 6
    assert reconciliation["required_stable_poll_count"] == 6
    assert reconciliation["observation_span_seconds"] == 210.0
    assert reconciliation["minimum_settlement_seconds"] == 180
    assert reconciliation["minimum_stable_tail_seconds"] == 75


def test_reconciliation_rejects_short_or_mismatched_settlement(
    tmp_path: Path,
) -> None:
    reconciliation_block = _embedded_python_blocks()[1]
    fingerprint = "a" * 64
    base = datetime(2026, 7, 25, tzinfo=UTC)
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    output_path = tmp_path / "reconciliation.json"
    runtime_path = tmp_path / "runtime.json"
    lock_path = tmp_path / "campaign.lock"
    lock_path.touch(mode=0o600)
    _write_json(
        before_path,
        _account_snapshot(
            captured_at=base - timedelta(seconds=1),
            usage="10.0",
            fingerprint=fingerprint,
        ),
    )
    _write_json(runtime_path, {"environment_sha256": "b" * 64})
    poll_paths: list[Path] = []
    for index in range(6):
        poll_path = tmp_path / f"short-poll-{index}.json"
        _write_json(
            poll_path,
            _account_snapshot(
                captured_at=base + timedelta(seconds=index * 10),
                usage="11.0",
                fingerprint=fingerprint,
            ),
        )
        poll_paths.append(poll_path)
    _write_json(
        after_path,
        _account_snapshot(
            captured_at=base + timedelta(seconds=50),
            usage="12.0",
            fingerprint=fingerprint,
        ),
    )

    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            reconciliation_block,
            str(before_path),
            str(after_path),
            str(output_path),
            str(runtime_path),
            str(lock_path),
            "180",
            "6",
            "15",
            *(str(path) for path in poll_paths),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode != 0
    assert "does not match the final stable poll" in completed.stderr
    assert not output_path.exists()


def test_campaign_shape_and_runtime_policy_are_frozen() -> None:
    script = _script()
    required_fragments = (
        'readonly DRACO_GROUPS="B0,B1,B2,B4,G1"',
        '--groups "$DRACO_GROUPS"',
        "--max-tasks 10",
        "--concurrency 5",
        "--experiment-config-set timeouts.task_seconds=10800",
        "--experiment-config-set runner.agent_max_iterations=12",
        "--experiment-config-set runner.concurrency=5",
        "--judge-concurrency 6",
        "--experiment-config-set judge.concurrency=6",
        "--timeout 10800",
        "--ensemble-proposer-timeout 907.5",
        "--ensemble-aggregator-timeout 2662.5",
        "--runner-mode agent_loop",
        "--agent-max-iterations 12",
        "--generation-max-attempts 3",
        "--generation-max-tokens 16384",
        "--judge-model google/gemini-3.1-pro-preview",
        "--judge-repeats 3",
        "--judge-max-attempts 3",
        "--tool-mode local_web_tools",
        "--local-web-search-provider brave",
        "--local-web-search-api-key-env BRAVE_SEARCH_API_KEY",
        "--require-openrouter-non-byok",
    )
    for fragment in required_fragments:
        assert fragment in script
    # Static compatibility, wave 1, and resume waves must fingerprint the
    # same strict non-BYOK policy.
    assert script.count("--require-openrouter-non-byok") == 3
    frozen_groups = re.search(
        r'readonly DRACO_GROUPS="([^"]+)"',
        script,
    )
    assert frozen_groups is not None
    assert "B3" not in frozen_groups.group(1).split(",")
    # GROUPS is a Bash special readonly array containing the process group IDs.
    # Reusing it silently turns --groups into values such as "1000".
    assert "readonly GROUPS=" not in script


def test_exact_input_and_new_main_repo_report_child_are_enforced() -> None:
    script = _script()
    assert "/home" + "/codex" not in script
    assert 'REPORT_ROOT="${DRACO_CAMPAIGN_REPORT_ROOT:-$SNAPSHOT_REPO/reports/draco}"' in script
    assert (
        'REFERENCE_REPO="${DRACO_CAMPAIGN_REFERENCE_REPO:-$(dirname '
        '"$SNAPSHOT_REPO")/opensquilla}"' in script
    )
    assert 'INPUT="$REFERENCE_REPO/data/draco/mini.jsonl"' in script
    assert 'CONFIG="$REFERENCE_REPO/.local-state/config.toml"' in script
    assert "DRACO_CAMPAIGN_REPORT_ROOT" in script
    assert "DRACO_CAMPAIGN_REFERENCE_REPO" in script
    assert (
        'readonly EXPECTED_INPUT_SHA256="'
        "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
        '"' in script
    )
    assert 'OUTPUT_DIR="$REPORT_ROOT/$OUTPUT_NAME"' in script
    assert '[[ ! "$OUTPUT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]' in script
    assert '[[ -e "$OUTPUT_DIR" || -L "$OUTPUT_DIR" ]]' in script
    assert 'mkdir "$OUTPUT_DIR"' in script
    assert "length == 10" in script
    assert "unique | length == 10" in script


def test_snapshot_is_parameterized_and_must_be_clean() -> None:
    script = _script()
    assert "--snapshot-repo" in script
    assert "DRACO_CAMPAIGN_SNAPSHOT_REPO" in script
    assert "--output-name" in script
    assert "DRACO_CAMPAIGN_OUTPUT_NAME" in script
    assert 'git -C "$SNAPSHOT_REPO" status --porcelain=v1 --untracked-files=all' in script
    assert "Formal campaign requires a completely clean source snapshot" in script
    assert script.count("--require-clean-source") == 3


def test_secrets_are_loaded_from_safe_files_and_never_embedded() -> None:
    script = _script()
    assert "sk-or-v1-" not in script
    assert "stat -c '%a' \"$OPENROUTER_SECRET_FILE\"" in script
    assert "OpenRouter secret file must have mode 600" in script
    assert "load_draco_benchmark_credentials" in script
    assert '--secret-file "$OPENROUTER_SECRET_FILE"' in script
    assert "--expected-key-env OPENROUTER_API_KEY" in script
    assert "--api-key" not in script
    assert "set -x" not in script


def test_direct_openrouter_and_local_web_fetch_policy_fail_closed() -> None:
    script = _script()
    for fragment in (
        "unset OPENROUTER_BASE_URL OPENSQUILLA_LLM_PROXY",
        "unset OPENSQUILLA_LLM_BASE_URL",
        "unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy",
        "unset FIRECRAWL_API_KEY",
        "export OPENSQUILLA_TRUST_ENV=0",
        "export OPENSQUILLA_PROVIDER_ROUTING_STRICT=1",
        "export OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES=1",
        "export OPENSQUILLA_OPENROUTER_METADATA_REQUIRED=1",
        "export OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS=1",
        "export OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE=1",
        "export DRACO_OPENROUTER_KEY_EXCLUSIVE=1",
    ):
        assert fragment in script
    forbidden_flag = "--continue-" + "after-cost-audit-failure"
    assert forbidden_flag not in script
    assert "--allow-firecrawl-web-fetch" not in script


def test_fd9_covers_account_window_waves_settlement_and_finalizer() -> None:
    script = _script()
    lock_open = script.index('exec 9<>"$LOCK_FILE"')
    lock_validation = script.index("validate_lock_file", lock_open)
    account_before = script.index('capture_account_snapshot "$ACCOUNT_BEFORE"')
    wave_one = script.index('"$PYTHON" "$MAIN_RUNNER"', account_before)
    stable_after = script.index("capture_stable_after", wave_one)
    finalizer = script.index('"$PYTHON" "$FINALIZER" "${FINALIZER_ARGS[@]}"')
    unlock = script.index("flock -u 9")
    assert (
        lock_open < lock_validation < account_before < wave_one < stable_after < finalizer < unlock
    )
    assert "--lock-fd 9" in script
    assert "os.fstat(9)" in script
    assert "os.lstat(lock_path)" in script
    assert "stat.S_ISLNK(path_stat.st_mode)" in script
    assert "stat.S_IMODE(path_stat.st_mode) != 0o600" in script
    assert "path_stat.st_uid != os.getuid()" in script
    assert "path_stat.st_nlink != 1" in script
    assert '"schema": "opensquilla.openrouter-account-reconciliation/v1"' in script
    assert '"settlement_status": "stable"' in script
    assert "stable_tail_count < required_stable_polls" in script
    assert '"stable_poll_count": stable_tail_count' in script
    assert '"required_stable_poll_count": required_stable_polls' in script
    assert '"poll_observation_count": len(observations)' in script
    assert '"stable_tail_start_index": stable_tail_start_index' in script
    assert '"minimum_settlement_seconds": minimum_settlement_seconds' in script
    assert '"minimum_stable_tail_seconds": minimum_stable_tail_seconds' in script
    assert '"observation_span_seconds": observation_span_seconds' in script
    assert '"stable_tail_span_seconds": stable_tail_span_seconds' in script
    assert '"runtime_environment_sha256": runtime_environment_sha256' in script
    assert '"runtime_environment_file_sha256": hashlib.sha256(' in script
    assert '"runtime_environment_sha256": hashlib.sha256(' not in script
    assert "readonly ACCOUNT_SETTLEMENT_MIN_SECONDS=180" in script
    assert "readonly ACCOUNT_SETTLEMENT_STABLE_POLLS=6" in script
    assert "readonly ACCOUNT_SETTLEMENT_POLL_SECONDS=15" in script
    assert "readonly ACCOUNT_SETTLEMENT_BREAK_SECONDS=195" in script
    assert '"$stable_streak" -ge "$ACCOUNT_SETTLEMENT_STABLE_POLLS"' in script
    assert '"$elapsed_seconds" -ge "$ACCOUNT_SETTLEMENT_BREAK_SECONDS"' in script
    assert "ACCOUNT_POLL_SEQUENCE=0" in script
    assert "ACCOUNT_POLL_SEQUENCE=$((ACCOUNT_POLL_SEQUENCE + 1))" in script
    assert "poll-$(printf '%06d' \"$ACCOUNT_POLL_SEQUENCE\").json" in script
    assert 'capture_account_snapshot "$poll_path" || return 2' in script
    assert "write_account_reconciliation || return 2" in script
    assert "required stable settlement window" in script
    assert "if byok_delta != 0:" in script


def test_settlement_retry_keeps_unique_polls_and_cannot_false_settle(
    tmp_path: Path,
) -> None:
    harness = (
        "set -Eeuo pipefail\n"
        + _bash_function("capture_stable_after")
        + r"""
mkdir "$WORK/polls"
ACCOUNT_POLL_DIR="$WORK/polls"
ACCOUNT_AFTER="$WORK/after.json"
ACCOUNT_POLL_FILES=()
ACCOUNT_POLL_SEQUENCE=0
ACCOUNT_SETTLED=0
ACCOUNT_SETTLEMENT_STABLE_POLLS=2
ACCOUNT_SETTLEMENT_BREAK_SECONDS=0
ACCOUNT_SETTLEMENT_POLL_SECONDS=0
CAPTURE_CALLS=0
capture_account_snapshot() {
  CAPTURE_CALLS=$((CAPTURE_CALLS + 1))
  if [[ "$CAPTURE_CALLS" == "2" ]]; then
    return 2
  fi
  printf '{"usage":"1","byok_usage":"0"}\n' >"$1"
}
jq() {
  if [[ "$2" == ".usage" ]]; then
    printf '1\n'
  else
    printf '0\n'
  fi
}
date() { printf '100\n'; }
sleep() { return 0; }
install() { cp "$3" "$4"; }
write_account_reconciliation() {
  [[ "${#ACCOUNT_POLL_FILES[@]}" == "3" ]]
}
first_status=0
capture_stable_after || first_status=$?
[[ "$first_status" == "2" ]]
[[ "$ACCOUNT_SETTLED" == "0" ]]
capture_stable_after
printf 'status=%s settled=%s sequence=%s\n' \
  "$first_status" "$ACCOUNT_SETTLED" "$ACCOUNT_POLL_SEQUENCE"
printf '%s\n' "${ACCOUNT_POLL_FILES[@]##*/}"
"""
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "WORK": str(tmp_path)},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "status=2 settled=1 sequence=4",
        "poll-000001.json",
        "poll-000003.json",
        "poll-000004.json",
    ]
    assert not (tmp_path / "polls" / "poll-000002.json").exists()
    assert (tmp_path / "after.json").read_text() == '{"usage":"1","byok_usage":"0"}\n'


def test_resume_gate_schedules_one_offline_metadata_repair_without_model_work() -> None:
    script = _script()
    assert 'action": "regenerate"' in script
    assert 'action": "judge_only"' in script
    assert 'state.get("judge_attempt_budget_exhausted") is True' in script
    assert '"judge_budget_exhausted_pair_count": len(judge_budget_exhausted)' in script
    assert '"judge_budget_exhausted_pairs": judge_budget_exhausted' in script
    assert 'elif action == "metadata_only":' in script
    assert 'state.get("metadata_repair_attempted") is True' in script
    assert '{"group": group, "task_id": task_id, "action": "metadata_only"}' in script
    assert "campaign_proof_only.append" in script
    assert "prior_used < 3" in script
    assert "generation_budget_exhausted" in script
    assert 'if [[ ! -s "$ACTIONABLE_KEYS" ]]' in script
    assert '--only-group-task-keys "$ACTIONABLE_KEYS"' in script
    assert "for wave_number in 2 3" in script
    live_manifest_assignment = script.index('COMPATIBILITY_MANIFEST="${MANIFESTS[-1]}"')
    wave_one_record = script.index('record_wave_artifacts "$WAVE1_DIR"')
    resume_gate = script.index("write_actionable_keys", wave_one_record)
    assert wave_one_record < live_manifest_assignment < resume_gate
    assert 'STATIC_COMPATIBILITY_MANIFEST="${STATIC_MANIFESTS[0]}"' in script


def test_recoverable_exit_two_is_recorded_before_wave_status_is_gated() -> None:
    script = _script()
    wave_one_status = script.index("WAVE1_STATUS=$?")
    wave_one_record = script.index('record_wave_artifacts "$WAVE1_DIR"')
    wave_one_gate = script.index('accept_or_reject_wave_status "$WAVE1_STATUS" "${MANIFESTS[-1]}"')
    resume_status = script.index("WAVE_STATUS=$?")
    resume_record = script.index('record_wave_artifacts "$WAVE_DIR"')
    resume_gate = script.index('accept_or_reject_wave_status "$WAVE_STATUS" "${MANIFESTS[-1]}"')
    assert wave_one_status < wave_one_record < wave_one_gate
    assert resume_status < resume_record < resume_gate
    assert '[[ "$runner_status" != "0" && "$runner_status" != "2" ]]' in script
    for recoverable in (
        "metadata_incomplete",
        "judge_incomplete",
        "result_incomplete",
        "resume_repair_incomplete",
    ):
        assert recoverable in script
    assert "Runner exit 2 is expected" in script


def test_explicit_policy_preflight_and_malformed_waves_fail_closed() -> None:
    script = _script()
    assert '. == "openrouter_non_byok_policy_violation"' in script
    assert '. == "openrouter_byok_detected"' in script
    assert '. == "cost_audit_failed"' in script
    assert "cost_audit_failed|preflight_failed" in script
    assert "Missing/duplicate artifacts remain fatal" in script
    assert "Campaign wave exited unexpectedly" in script
    assert "Campaign wave has an unexpected manifest status" in script


def test_every_prior_result_is_passed_to_resume_and_finalizer() -> None:
    script = _script()
    resume_loop = re.search(
        r'for result_jsonl in "\$\{RESULT_JSONLS\[@\]\}"; do\s+'
        r'RESUME_ARGS\+=\(--resume-from-jsonl "\$result_jsonl"\)\s+done',
        script,
    )
    assert resume_loop is not None
    assert 'FINALIZER_ARGS+=(--result "$result_jsonl")' in script
    assert 'FINALIZER_ARGS+=(--manifest "$manifest")' in script
    for fragment in (
        '--account-before "$ACCOUNT_BEFORE"',
        '--account-after "$ACCOUNT_AFTER"',
        '--account-reconciliation "$ACCOUNT_RECON"',
        '--runtime-environment "$RUNTIME_ENV"',
        '--lock-file "$LOCK_FILE"',
        '--output-dir "$FINAL_OUTPUT_DIR"',
        '--groups "$DRACO_GROUPS"',
        "--max-generation-attempts 3",
    ):
        assert fragment in script


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _write_formal_publication_fixture(
    tmp_path: Path,
    *,
    source_outside_archive: bool = False,
    include_prior: bool = True,
    include_prior_campaign: bool = False,
) -> tuple[Path, Path, Path]:
    output_dir = tmp_path / "campaign"
    archive_dir = output_dir / "archive"
    formal_dir = output_dir / ".formal-results"
    source_dir = (
        tmp_path / "outside" if source_outside_archive else archive_dir / "waves" / "wave-1"
    )
    archive_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    formal_dir.mkdir(parents=True)
    source_result = source_dir / "draco_ensemble_fixture.jsonl"
    source_manifest = source_dir / "draco_run_fixture.manifest.json"
    source_result.write_text('{"fixture": true}\n', encoding="utf-8")
    source_manifest.write_text('{"fixture": true}\n', encoding="utf-8")
    source_result.chmod(0o600)
    source_manifest.chmod(0o600)

    artifact_names = (
        "results.jsonl",
        "trace.jsonl",
        "actual-spend-ledger.jsonl",
        "openrouter-non-byok-campaign-proof.json",
        "audit.json",
        "EXPERIMENT_RESULTS.md",
    )
    artifact_records: dict[str, dict[str, object]] = {}
    for name in artifact_names:
        artifact_path = formal_dir / name
        artifact_path.write_text(f"{name}\n", encoding="utf-8")
        artifact_path.chmod(0o600)
        artifact_records[name] = {
            "path": name,
            "sha256": _file_sha256(artifact_path),
            "size_bytes": artifact_path.stat().st_size,
            "mode": "0o600",
        }
    manifest: dict[str, object] = {
        "schema": "opensquilla.draco.campaign-final-manifest/v1",
        "status": "complete",
        "source_results": [
            {
                "path": str(source_result.resolve()),
                "sha256": _file_sha256(source_result),
            }
        ],
        "source_manifests": [
            {
                "path": str(source_manifest.resolve()),
                "sha256": _file_sha256(source_manifest),
                "result_path": str(source_result.resolve()),
                "result_sha256": _file_sha256(source_result),
            }
        ],
        "artifacts": artifact_records,
    }
    account_windows = []
    kinds = []
    if include_prior:
        kinds.append("prior_aborted")
    if include_prior_campaign:
        kinds.append("prior_campaign")
    kinds.append("current")
    usage_deltas = {
        "prior_aborted": "4.598438756",
        "prior_campaign": "118.828938801",
        "current": "0.4",
    }
    for kind in kinds:
        window_dir = archive_dir / "account" / kind
        window_dir.mkdir(parents=True)
        sources = []
        for name in (
            "openrouter-account-before.json",
            "openrouter-account-after.json",
            "openrouter-account-reconciliation.json",
            "runtime-environment.json",
        ):
            path = window_dir / name
            path.write_text(f"{kind}:{name}\n", encoding="utf-8")
            path.chmod(0o600)
            sources.append({"path": str(path.resolve()), "sha256": _file_sha256(path)})
        account_windows.append(
            {
                "kind": kind,
                "usage_delta_usd": usage_deltas[kind],
                "sources": sources,
            }
        )
    manifest["cost_attribution"] = {
        "account_windows": account_windows,
        "account_window_total_usd": "5" if include_prior else "0.4",
        "unallocated_aborted_window_usd": "4.598438756" if include_prior else "0",
        "attribution_precision": (
            "multi-window-counter-exact-campaign-attribution-unproven"
            if include_prior
            else "campaign-attributable-exact"
        ),
        "campaign_attributable_exact": not include_prior,
    }
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    manifest_path = formal_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return output_dir, archive_dir, formal_dir


def test_output_layout_archives_process_material_and_has_no_final_directory() -> None:
    script = _script()
    assert 'ARCHIVE_DIR="$OUTPUT_DIR/archive"' in script
    assert 'FINAL_OUTPUT_DIR="$OUTPUT_DIR/.formal-results"' in script
    for fragment in (
        'RUNTIME_ENV="$ARCHIVE_DIR/runtime-environment.json"',
        'ACCOUNT_BEFORE="$ARCHIVE_DIR/account/openrouter-account-before.json"',
        "PRIOR_ACCOUNT_WINDOW_DIRS=()",
        'ROUTE_EVIDENCE="$ARCHIVE_DIR/preflight/openrouter-route-preflight.json"',
        'STATIC_DIR="$ARCHIVE_DIR/preflight/static"',
        'WAVE1_DIR="$ARCHIVE_DIR/waves/wave-1"',
        'ACTIONABLE_KEYS="$ARCHIVE_DIR/gates/wave-$wave_number-actionable.jsonl"',
        'WAVE_DIR="$ARCHIVE_DIR/waves/wave-$wave_number"',
        '>"$ARCHIVE_DIR/finalizer.log" 2>&1',
    ):
        assert fragment in script
    assert '"$OUTPUT_DIR/final"' not in script
    assert 'FINALIZER_ARGS+=(--prior-account-window-dir "$prior_account_window_dir")' in script
    assert "archive_prior_account_window" in script
    assert "DEFAULT_PRIOR_ACCOUNT_WINDOW_DIR" not in script
    assert "prior-aborted-window-20260726-000227" not in script
    assert "prior-aborted-window-%03d" in script
    assert "PRIOR_ACCOUNT_WINDOW_SOURCES=()" in script
    finalizer = script.index('"$PYTHON" "$FINALIZER" "${FINALIZER_ARGS[@]}"')
    publisher = script.index("publish_final_artifacts", finalizer)
    unlock = script.index("flock -u 9", publisher)
    assert finalizer < publisher < unlock


def test_final_artifacts_are_promoted_with_manifest_as_commit_marker(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(tmp_path)
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode == 0, completed.stderr
    assert not formal_dir.exists()
    assert {path.name for path in output_dir.iterdir()} == {
        "archive",
        "results.jsonl",
        "trace.jsonl",
        "actual-spend-ledger.jsonl",
        "openrouter-non-byok-campaign-proof.json",
        "audit.json",
        "EXPERIMENT_RESULTS.md",
        "manifest.json",
    }
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    for source in manifest["source_results"]:
        assert Path(source["path"]).is_file()
    for source in manifest["source_manifests"]:
        assert Path(source["path"]).is_file()
        assert Path(source["result_path"]).is_file()


def test_final_publication_accepts_current_only_account_window(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(
        tmp_path,
        include_prior=False,
    )
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert [window["kind"] for window in manifest["cost_attribution"]["account_windows"]] == [
        "current"
    ]


def test_final_publication_accepts_prior_campaign_account_window(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(
        tmp_path,
        include_prior_campaign=True,
    )
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert [window["kind"] for window in manifest["cost_attribution"]["account_windows"]] == [
        "prior_aborted",
        "prior_campaign",
        "current",
    ]


def test_final_publication_rejects_unknown_account_window_kind(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(
        tmp_path,
        include_prior_campaign=True,
    )
    manifest_path = formal_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prior_campaign = next(
        window
        for window in manifest["cost_attribution"]["account_windows"]
        if window["kind"] == "prior_campaign"
    )
    prior_campaign["kind"] = "future_campaign"
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode != 0
    assert "formal account window kind differs" in completed.stderr
    assert formal_dir.is_dir()
    assert not (output_dir / "manifest.json").exists()


def test_final_publication_rejects_source_evidence_outside_archive(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(
        tmp_path,
        source_outside_archive=True,
    )
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)

    assert completed.returncode != 0
    assert "outside archive/" in completed.stderr
    assert formal_dir.is_dir()
    assert not (output_dir / "manifest.json").exists()
    assert {path.name for path in output_dir.iterdir()} == {
        "archive",
        ".formal-results",
    }


def test_final_publication_rejects_prior_account_source_outside_archive(
    tmp_path: Path,
) -> None:
    publisher_block = _embedded_python_blocks()[2]
    output_dir, archive_dir, formal_dir = _write_formal_publication_fixture(tmp_path)
    manifest_path = formal_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside-prior.json"
    outside.write_text("prior\n", encoding="utf-8")
    outside.chmod(0o600)
    manifest["cost_attribution"]["account_windows"][0]["sources"][0] = {
        "path": str(outside.resolve()),
        "sha256": _file_sha256(outside),
    }
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    lock_path = tmp_path / "publisher.lock"
    lock_path.touch(mode=0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        completed = _run_embedded_python(
            publisher_block,
            str(formal_dir),
            str(output_dir),
            str(archive_dir),
            lock_fd=lock_fd,
        )
    finally:
        os.close(lock_fd)
    assert completed.returncode != 0
    assert "outside archive/" in completed.stderr
