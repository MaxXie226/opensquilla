#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly EXPECTED_INPUT_SHA256="1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
readonly SUPPORTED_DRACO_GROUPS="B0,B1,B2,B4,G1"
readonly DEFAULT_DRACO_GROUPS="$SUPPORTED_DRACO_GROUPS"
readonly ACCOUNT_SETTLEMENT_MIN_SECONDS=180
readonly ACCOUNT_SETTLEMENT_STABLE_POLLS=6
readonly ACCOUNT_SETTLEMENT_POLL_SECONDS=15
readonly ACCOUNT_SETTLEMENT_BREAK_SECONDS=195

usage() {
  cat >&2 <<'EOF'
Usage:
  run_draco_mini_b0_b1_b2_b4_g1_campaign.sh
    [--snapshot-repo CLEAN_GIT_SNAPSHOT]
    [--output-name NEW_DIRECTORY_NAME]
    [--prior-account-window-dir ABORTED_CAMPAIGN_ACCOUNT_DIR]
    [--groups CANONICAL_GROUP_SUBSET]
    [--experiment-config-override-json JSON_OBJECT]
    [--confirmatory-schedule IMMUTABLE_PAIRED_COHORT_JSON]
    [--confirmatory-schedule-fd SEALED_INHERITED_FD]

By default, output is a new direct child of:
  SNAPSHOT_REPO/reports/draco
The default group set is B0,B1,B2,B4,G1. --groups accepts only a non-empty,
non-duplicated subset in that canonical order.

Environment overrides:
  DRACO_CAMPAIGN_REPORT_ROOT       Alternate report root
  DRACO_CAMPAIGN_REFERENCE_REPO   Checkout containing data/draco/mini.jsonl
                                  and .local-state/config.toml
  DRACO_CAMPAIGN_PYTHON           Python executable (default:
                                  SNAPSHOT_REPO/.venv/bin/python)
  DRACO_CAMPAIGN_TASK_CONCURRENCY Explicit high-priority generation concurrency.
                                  When unset, runner.concurrency is read from the
                                  effective experiment JSON.
EOF
}

validate_draco_groups() {
  local requested="$1"
  local reconstructed=""
  local group
  local group_index=-1
  local previous_index=-1
  local -a requested_groups=()

  if [[ -z "$requested" ]]; then
    echo "--groups must contain at least one experiment group" >&2
    return 2
  fi
  IFS=',' read -r -a requested_groups <<<"$requested"
  for group in "${requested_groups[@]}"; do
    case "$group" in
      B0) group_index=0 ;;
      B1) group_index=1 ;;
      B2) group_index=2 ;;
      B4) group_index=3 ;;
      G1) group_index=4 ;;
      *)
        echo "--groups must be a canonical-order subset of $SUPPORTED_DRACO_GROUPS" >&2
        return 2
        ;;
    esac
    if (( group_index <= previous_index )); then
      echo "--groups must be non-duplicated and in canonical order: $SUPPORTED_DRACO_GROUPS" >&2
      return 2
    fi
    previous_index="$group_index"
    reconstructed+="${reconstructed:+,}$group"
  done
  if [[ "$reconstructed" != "$requested" ]]; then
    echo "--groups must be a canonical-order subset of $SUPPORTED_DRACO_GROUPS" >&2
    return 2
  fi
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_REPO="${DRACO_CAMPAIGN_SNAPSHOT_REPO:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TASK_CONCURRENCY_OVERRIDE_PRESENT=0
TASK_CONCURRENCY_OVERRIDE=""
if [[ "${DRACO_CAMPAIGN_TASK_CONCURRENCY+x}" == "x" ]]; then
  TASK_CONCURRENCY_OVERRIDE_PRESENT=1
  TASK_CONCURRENCY_OVERRIDE="$DRACO_CAMPAIGN_TASK_CONCURRENCY"
  if [[ ! "$TASK_CONCURRENCY_OVERRIDE" =~ ^[1-9][0-9]*$ ]]; then
    echo "DRACO_CAMPAIGN_TASK_CONCURRENCY must be a positive integer" >&2
    exit 2
  fi
fi
readonly TASK_CONCURRENCY_OVERRIDE_PRESENT TASK_CONCURRENCY_OVERRIDE
DRACO_GROUPS="$DEFAULT_DRACO_GROUPS"
OUTPUT_NAME="${DRACO_CAMPAIGN_OUTPUT_NAME:-}"
EXPERIMENT_CONFIG_OVERRIDE_JSON=""
EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT=0
CONFIRMATORY_SCHEDULE=""
CONFIRMATORY_SCHEDULE_FD=""
PRIOR_ACCOUNT_WINDOW_SOURCES=()
if [[ -n "${DRACO_CAMPAIGN_PRIOR_ACCOUNT_WINDOW_DIR:-}" ]]; then
  PRIOR_ACCOUNT_WINDOW_SOURCES+=("$DRACO_CAMPAIGN_PRIOR_ACCOUNT_WINDOW_DIR")
fi

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --snapshot-repo)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      SNAPSHOT_REPO="$2"
      shift 2
      ;;
    --output-name)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      OUTPUT_NAME="$2"
      shift 2
      ;;
    --prior-account-window-dir)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      PRIOR_ACCOUNT_WINDOW_SOURCES+=("$2")
      shift 2
      ;;
    --groups)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      DRACO_GROUPS="$2"
      shift 2
      ;;
    --experiment-config-override-json)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      EXPERIMENT_CONFIG_OVERRIDE_JSON="$2"
      EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT=1
      shift 2
      ;;
    --confirmatory-schedule)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      CONFIRMATORY_SCHEDULE="$2"
      shift 2
      ;;
    --confirmatory-schedule-fd)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      CONFIRMATORY_SCHEDULE_FD="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

validate_draco_groups "$DRACO_GROUPS" || exit 2
if [[ -n "$CONFIRMATORY_SCHEDULE" && -n "$CONFIRMATORY_SCHEDULE_FD" ]]; then
  echo "Only one confirmatory schedule input mode may be used" >&2
  exit 2
fi
if [[ -n "$CONFIRMATORY_SCHEDULE_FD" ]]; then
  if [[ ! "$CONFIRMATORY_SCHEDULE_FD" =~ ^[0-9]+$ ]] || [[ ! -r "/proc/self/fd/$CONFIRMATORY_SCHEDULE_FD" ]]; then
    echo "--confirmatory-schedule-fd must name one readable inherited descriptor" >&2
    exit 2
  fi
  CONFIRMATORY_SCHEDULE="/proc/self/fd/$CONFIRMATORY_SCHEDULE_FD"
fi
if [[ -n "$CONFIRMATORY_SCHEDULE" ]]; then
  if [[ "$DRACO_GROUPS" != "G1" ]]; then
    echo "Confirmatory paired cohorts require --groups G1" >&2
    exit 2
  fi
  if [[ "$EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT" == "1" ]]; then
    echo "--confirmatory-schedule cannot be combined with a single-arm override" >&2
    exit 2
  fi
  if [[ "${#PRIOR_ACCOUNT_WINDOW_SOURCES[@]}" -ne 0 ]]; then
    echo "Confirmatory paired cohorts require one new shared account window" >&2
    exit 2
  fi
  if [[ -z "$CONFIRMATORY_SCHEDULE_FD" ]]; then
    if [[ ! -f "$CONFIRMATORY_SCHEDULE" || -L "$CONFIRMATORY_SCHEDULE" ]]; then
      echo "Confirmatory schedule must be a regular non-symlink file" >&2
      exit 2
    fi
  fi
  if [[ -z "$CONFIRMATORY_SCHEDULE_FD" ]]; then
    CONFIRMATORY_SCHEDULE="$(realpath "$CONFIRMATORY_SCHEDULE")"
  fi
fi
readonly DRACO_GROUPS
DRACO_GROUP_SLUG="${DRACO_GROUPS,,}"
DRACO_GROUP_SLUG="${DRACO_GROUP_SLUG//,/-}"
readonly DRACO_GROUP_SLUG

SNAPSHOT_REPO="$(realpath "$SNAPSHOT_REPO")"
REPORT_ROOT="${DRACO_CAMPAIGN_REPORT_ROOT:-$SNAPSHOT_REPO/reports/draco}"
REFERENCE_REPO="${DRACO_CAMPAIGN_REFERENCE_REPO:-$(dirname "$SNAPSHOT_REPO")/opensquilla}"
REPORT_ROOT="$(realpath -m "$REPORT_ROOT")"
REFERENCE_REPO="$(realpath "$REFERENCE_REPO")"
INPUT="$REFERENCE_REPO/data/draco/mini.jsonl"
CONFIG="$REFERENCE_REPO/.local-state/config.toml"
readonly REPORT_ROOT REFERENCE_REPO INPUT CONFIG
EXPERIMENT_CONFIG="$SNAPSHOT_REPO/configs/benchmarks/draco_b2_g12.json"
PYTHON="${DRACO_CAMPAIGN_PYTHON:-$SNAPSHOT_REPO/.venv/bin/python}"
MAIN_RUNNER="$SNAPSHOT_REPO/scripts/run_draco_routing_experiment.py"
RESUME_RUNNER="$SNAPSHOT_REPO/scripts/run_draco_routing_experiment_resume.py"
FINALIZER="$SNAPSHOT_REPO/scripts/experiments/finalize_draco_campaign.py"
RECOVERY_STATUS="$SNAPSHOT_REPO/scripts/experiments/recover_draco_finalization.py"
CAPTURE_ACCOUNT="$SNAPSHOT_REPO/scripts/experiments/capture_openrouter_account_usage.py"
CAPTURE_RUNTIME="$SNAPSHOT_REPO/scripts/experiments/capture_draco_runtime_environment.py"
ROUTE_PREFLIGHT="$SNAPSHOT_REPO/scripts/experiments/validate_openrouter_b2_routes.py"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME:?HOME is required}/.config}"
OPENROUTER_SECRET_FILE="${OPENSQUILLA_OPENROUTER_SECRET_FILE:-$CONFIG_HOME/opensquilla/secrets/openrouter.key}"
LOCK_FILE="${DRACO_OPENROUTER_LOCK_FILE:-/tmp/opensquilla-draco-openrouter.lock}"

for required_path in \
  "$INPUT" \
  "$CONFIG" \
  "$EXPERIMENT_CONFIG" \
  "$PYTHON" \
  "$MAIN_RUNNER" \
  "$RESUME_RUNNER" \
  "$FINALIZER" \
  "$RECOVERY_STATUS" \
  "$CAPTURE_ACCOUNT" \
  "$CAPTURE_RUNTIME" \
  "$ROUTE_PREFLIGHT"; do
  if [[ ! -e "$required_path" ]]; then
    echo "Required campaign input is missing: $required_path" >&2
    exit 2
  fi
done

if [[ ! -f "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "Campaign Python must be an executable file: $PYTHON" >&2
  exit 2
fi

if [[ "$(git -C "$SNAPSHOT_REPO" rev-parse --show-toplevel)" != "$SNAPSHOT_REPO" ]]; then
  echo "--snapshot-repo must be the root of a Git worktree" >&2
  exit 2
fi
if [[ -n "$(git -C "$SNAPSHOT_REPO" status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Formal campaign requires a completely clean source snapshot" >&2
  exit 2
fi

# Resolve the same sparse overlay before creating output paths. Paired mode
# validates both role configs and the immutable 5 AB / 5 BA schedule before
# loading credentials or opening an account window.
CONFIRMATORY_VALIDATED_SCHEDULE_SHA256=""
if [[ -n "$CONFIRMATORY_SCHEDULE" ]]; then
  if ! EFFECTIVE_CONFIG_RUNTIME_VALUES="$(
    "$PYTHON" -c '
import hashlib
import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from opensquilla.eval.draco_experiment_config import load_draco_experiment_config

schedule_fd = sys.argv[5]
if schedule_fd:
    fd = int(schedule_fd)
    required_seals = (
        getattr(fcntl, "F_SEAL_WRITE", 0x0008)
        | getattr(fcntl, "F_SEAL_GROW", 0x0004)
        | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
        | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
    )
    seals = fcntl.fcntl(fd, getattr(fcntl, "F_GET_SEALS", 1034))
    if seals & required_seals != required_seals:
        raise SystemExit("confirmatory schedule fd is not fully sealed")
    size = os.fstat(fd).st_size
    schedule_bytes = os.pread(fd, size, 0)
else:
    schedule_bytes = Path(sys.argv[3]).read_bytes()
schedule = json.loads(schedule_bytes)
if schedule.get("schema") != "opensquilla.draco-p0-p05-confirmatory-schedule/v1":
    raise SystemExit("confirmatory schedule schema differs")

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

without_hash = dict(schedule)
recorded = without_hash.pop("schedule_sha256", None)
if recorded != hashlib.sha256(canonical(without_hash)).hexdigest():
    raise SystemExit("confirmatory schedule self-hash differs")
tasks = schedule.get("task_schedule")
if not isinstance(tasks, list) or len(tasks) != 10:
    raise SystemExit("confirmatory schedule must contain ten tasks")
orders = [row.get("order") for row in tasks if isinstance(row, dict)]
if orders.count("AB") != 5 or orders.count("BA") != 5:
    raise SystemExit("confirmatory schedule must freeze five AB and five BA tasks")
if schedule.get("order_balance") != {"AB": 5, "BA": 5}:
    raise SystemExit("confirmatory order receipt differs")
input_task_ids = []
for line in Path(sys.argv[4]).read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    input_task_ids.append(str(row.get("task_id") or row.get("id") or ""))
schedule_task_ids = [str(row.get("task_id") or "") for row in tasks]
benchmark = schedule.get("benchmark")
if (
    len(set(input_task_ids)) != 10
    or schedule_task_ids != input_task_ids
    or not isinstance(benchmark, dict)
    or benchmark.get("task_ids") != input_task_ids
    or benchmark.get("task_count") != 10
    or benchmark.get("group") != "G1"
):
    raise SystemExit("confirmatory task identities differ from frozen DRACO mini")
tranches = schedule.get("tranches")
if not isinstance(tranches, list) or [len(row.get("task_ids", [])) for row in tranches] != [6, 4]:
    raise SystemExit("confirmatory schedule must use 6+4 tranches")
for tranche in tranches:
    for phase in tranche.get("phases", []):
        control = phase.get("control_task_ids")
        candidate = phase.get("candidate_task_ids")
        if not isinstance(control, list) or not isinstance(candidate, list):
            raise SystemExit("confirmatory phase task lists are malformed")
        if len(control) + len(candidate) > 6 or phase.get("max_inflight_tasks") != len(control) + len(candidate):
            raise SystemExit("confirmatory phase exceeds global concurrency six")
execution = schedule.get("execution_contract")
if (
    not isinstance(execution, dict)
    or execution.get("global_task_concurrency") != 6
    or execution.get("phase_barrier_between_legs") is not True
    or execution.get("generation_leg_failure_policy") != "cohort_incomplete_no_unpaired_retry"
):
    raise SystemExit("confirmatory execution contract differs")
account = schedule.get("account_window_contract")
if (
    not isinstance(account, dict)
    or account.get("scope") != "paired_cohort_shared"
    or account.get("companion_ledger_required") is not True
    or account.get("report_account_delta_once") is not True
):
    raise SystemExit("confirmatory account-window contract differs")
screening = schedule.get("screening_contract")
if (
    not isinstance(screening, dict)
    or screening.get("source_screening_is_diagnostic_only") is not True
    or screening.get("confirmatory_mini_is_diagnostic_only") is not True
    or screening.get("automatic_winner_promotion") is not False
):
    raise SystemExit("confirmatory screening contract differs")
roles = schedule.get("roles")
if not isinstance(roles, dict) or set(roles) != {"control", "candidate"}:
    raise SystemExit("confirmatory roles differ")
runtime_values = []
replays = []
for role in ("control", "candidate"):
    row = roles[role]
    override = row.get("override")
    if not isinstance(override, dict):
        raise SystemExit("confirmatory role override is malformed")
    bundle = load_draco_experiment_config(
        Path(sys.argv[2]),
        inline_overlay_json=json.dumps(override, sort_keys=True, separators=(",", ":")),
    )
    config = bundle.config
    runtime_values.append(
        (config.runner.concurrency, config.judge.concurrency, config.generation.max_attempts)
    )
    replay = override.get("g1_routing", {}).get("task_analysis_execution")
    replays.append(replay)
if len(set(runtime_values)) != 1 or runtime_values[0][0] != 6:
    raise SystemExit("confirmatory role runtime concurrency/contracts differ")
if roles["control"].get("analyzer_mode") == "frozen_replay":
    if replays[0] != replays[1] or not isinstance(replays[0], dict):
        raise SystemExit("confirmatory roles do not share frozen Analyzer replay")
    entries = replays[0].get("entries")
    if (
        replays[0].get("mode") != "frozen_replay"
        or not isinstance(entries, dict)
        or set(entries) != set(input_task_ids)
    ):
        raise SystemExit("confirmatory frozen Analyzer replay must retain ten entries")
print("\t".join(str(value) for value in (*runtime_values[0], schedule["schedule_sha256"])))
' "$SNAPSHOT_REPO/src" "$EXPERIMENT_CONFIG" "$CONFIRMATORY_SCHEDULE" "$INPUT" "$CONFIRMATORY_SCHEDULE_FD"
  )"; then
    echo "Unable to validate the confirmatory schedule/effective configs" >&2
    exit 2
  fi
else
  if ! EFFECTIVE_CONFIG_RUNTIME_VALUES="$(
    "$PYTHON" -c '
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from opensquilla.eval.draco_experiment_config import load_draco_experiment_config

bundle = load_draco_experiment_config(
    Path(sys.argv[2]),
    inline_overlay_json=(sys.argv[3] if sys.argv[4] == "1" else None),
)
config = bundle.config
print(
    f"{config.runner.concurrency}\t"
    f"{config.judge.concurrency}\t"
    f"{config.generation.max_attempts}"
)
' "$SNAPSHOT_REPO/src" "$EXPERIMENT_CONFIG" \
    "$EXPERIMENT_CONFIG_OVERRIDE_JSON" "$EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT"
  )"; then
    echo "Unable to resolve the effective DRACO experiment config" >&2
    exit 2
  fi
fi
IFS=$'\t' read -r CONFIG_TASK_CONCURRENCY JUDGE_CONCURRENCY \
  GENERATION_MAX_ATTEMPTS CONFIRMATORY_VALIDATED_SCHEDULE_SHA256 \
  <<<"$EFFECTIVE_CONFIG_RUNTIME_VALUES"
if [[
  ! "$CONFIG_TASK_CONCURRENCY" =~ ^[1-9][0-9]*$
  || ! "$JUDGE_CONCURRENCY" =~ ^[1-9][0-9]*$
  || ! "$GENERATION_MAX_ATTEMPTS" =~ ^[1-9][0-9]*$
]]; then
  echo "Effective DRACO runtime values are invalid" >&2
  exit 2
fi
TASK_CONCURRENCY="$CONFIG_TASK_CONCURRENCY"
if [[ "$TASK_CONCURRENCY_OVERRIDE_PRESENT" == "1" ]]; then
  TASK_CONCURRENCY="$TASK_CONCURRENCY_OVERRIDE"
fi
readonly TASK_CONCURRENCY JUDGE_CONCURRENCY GENERATION_MAX_ATTEMPTS

if [[ -z "$OUTPUT_NAME" ]]; then
  OUTPUT_NAME="draco-mini-${DRACO_GROUP_SLUG}-c${TASK_CONCURRENCY}-j${JUDGE_CONCURRENCY}-a${GENERATION_MAX_ATTEMPTS}-$(date +%Y%m%d-%H%M%S)"
fi
if [[ ! "$OUTPUT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "--output-name must be one safe path component" >&2
  exit 2
fi
OUTPUT_DIR="$REPORT_ROOT/$OUTPUT_NAME"
ARCHIVE_DIR="$OUTPUT_DIR/archive"
FINAL_OUTPUT_DIR="$OUTPUT_DIR/.formal-results"
readonly OUTPUT_DIR ARCHIVE_DIR FINAL_OUTPUT_DIR

if [[ "$(sha256sum "$INPUT" | awk '{print $1}')" != "$EXPECTED_INPUT_SHA256" ]]; then
  echo "DRACO mini input hash does not match the frozen 10-task set" >&2
  exit 2
fi
if ! jq -e -s '
  length == 10
  and all(.[];
    type == "object"
    and ((.task_id // .id // "") | type == "string" and length > 0)
  )
  and ([.[] | (.task_id // .id)] | unique | length == 10)
' "$INPUT" >/dev/null; then
  echo "DRACO mini input must contain exactly 10 unique tasks" >&2
  exit 2
fi

if [[ -e "$OUTPUT_DIR" || -L "$OUTPUT_DIR" ]]; then
  echo "Formal campaign output must not already exist: $OUTPUT_DIR" >&2
  exit 2
fi
if [[ ! -d "$REPORT_ROOT" ]]; then
  echo "Required report root does not exist: $REPORT_ROOT" >&2
  exit 2
fi

if [[ ! -f "$OPENROUTER_SECRET_FILE" || -L "$OPENROUTER_SECRET_FILE" ]]; then
  echo "Missing or unsafe OpenRouter secret file" >&2
  exit 2
fi
if [[ "$(stat -c '%a' "$OPENROUTER_SECRET_FILE")" != "600" ]]; then
  echo "OpenRouter secret file must have mode 600" >&2
  exit 2
fi
if [[ "$(stat -c '%u' "$OPENROUTER_SECRET_FILE")" != "$(id -u)" ]]; then
  echo "OpenRouter secret file must be owned by the campaign user" >&2
  exit 2
fi

export OPENSQUILLA_REPO="$SNAPSHOT_REPO"
export OPENSQUILLA_REFERENCE_REPO="$REFERENCE_REPO"
# shellcheck source=../lib/load_draco_benchmark_credentials.sh
source "$SNAPSHOT_REPO/scripts/lib/load_draco_benchmark_credentials.sh"
load_draco_benchmark_credentials

unset OPENROUTER_BASE_URL OPENSQUILLA_LLM_PROXY
unset OPENSQUILLA_LLM_BASE_URL
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
unset OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE
unset OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE_REQUIRED
unset FIRECRAWL_API_KEY
export OPENSQUILLA_TRUST_ENV=0
export OPENSQUILLA_PROVIDER_ROUTING_STRICT=1
export OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES=1
export OPENSQUILLA_OPENROUTER_METADATA_REQUIRED=1
export OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS=1
export OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE=1
export DRACO_OPENROUTER_KEY_EXCLUSIVE=1
export PYTHONPATH="$SNAPSHOT_REPO/src"

mkdir "$OUTPUT_DIR"
mkdir "$ARCHIVE_DIR"
mkdir \
  "$ARCHIVE_DIR/account" \
  "$ARCHIVE_DIR/gates" \
  "$ARCHIVE_DIR/preflight" \
  "$ARCHIVE_DIR/waves"

RUNTIME_ENV="$ARCHIVE_DIR/runtime-environment.json"
ACCOUNT_BEFORE="$ARCHIVE_DIR/account/openrouter-account-before.json"
ACCOUNT_AFTER="$ARCHIVE_DIR/account/openrouter-account-after.json"
ACCOUNT_RECON="$ARCHIVE_DIR/account/openrouter-account-reconciliation.json"
ACCOUNT_POLL_DIR="$ARCHIVE_DIR/account/stable-polls"
PRIOR_ACCOUNT_WINDOW_DIRS=()
mkdir "$ACCOUNT_POLL_DIR"

archive_prior_account_window() {
  local index=0
  local requested_source
  for requested_source in "${PRIOR_ACCOUNT_WINDOW_SOURCES[@]}"; do
    index=$((index + 1))
    local source_dir
    local raw_source_runtime
    local source_runtime
    local destination
    source_dir="$(realpath "$requested_source")"
    raw_source_runtime="$source_dir/../runtime-environment.json"
    if [[ -L "$raw_source_runtime" || ! -f "$raw_source_runtime" ]]; then
      echo "Prior aborted account window runtime source is missing or unsafe" >&2
      exit 2
    fi
    source_runtime="$(realpath "$raw_source_runtime")"
    destination="$(printf '%s/account/prior-aborted-window-%03d' "$ARCHIVE_DIR" "$index")"
    if [[
      -L "$requested_source"
      || ! -d "$source_dir"
      || ! -f "$source_runtime"
    ]]; then
      echo "Prior aborted account window source is missing or unsafe" >&2
      exit 2
    fi
    mkdir "$destination"
    local name
    for name in \
      openrouter-account-before.json \
      openrouter-account-after.json \
      openrouter-account-reconciliation.json; do
      if [[ ! -f "$source_dir/$name" || -L "$source_dir/$name" ]]; then
        echo "Prior aborted account window lacks safe $name" >&2
        exit 2
      fi
      install -m 600 "$source_dir/$name" "$destination/$name"
    done
    install -m 600 "$source_runtime" "$destination/runtime-environment.json"
    PRIOR_ACCOUNT_WINDOW_DIRS+=("$destination")
  done
}

archive_prior_account_window

COMMON_ARGS=(
  --input "$INPUT"
  --config "$CONFIG"
  --experiment-config "$EXPERIMENT_CONFIG"
  --groups "$DRACO_GROUPS"
  --max-tasks 10
)
if [[ "$EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT" == "1" ]]; then
  COMMON_ARGS+=(
    --experiment-config-override-json "$EXPERIMENT_CONFIG_OVERRIDE_JSON"
  )
fi
if [[ "$TASK_CONCURRENCY_OVERRIDE_PRESENT" == "1" ]]; then
  COMMON_ARGS+=(
    --experiment-config-set "runner.concurrency=$TASK_CONCURRENCY"
  )
fi

RESULT_JSONLS=()
MANIFESTS=()
ACCOUNT_POLL_FILES=()
ACCOUNT_POLL_SEQUENCE=0
ACCOUNT_WINDOW_OPEN=0
ACCOUNT_SETTLED=0
LOCK_HELD=0

validate_lock_file() {
  "$PYTHON" - "$LOCK_FILE" <<'PY'
from __future__ import annotations

import os
import stat
import sys

lock_path = sys.argv[1]
path_stat = os.lstat(lock_path)
fd_stat = os.fstat(9)
if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
    raise SystemExit("campaign lock must be a regular non-symlink file")
if path_stat.st_uid != os.getuid() or fd_stat.st_uid != os.getuid():
    raise SystemExit("campaign lock must be owned by the campaign user")
if stat.S_IMODE(path_stat.st_mode) != 0o600:
    raise SystemExit("campaign lock must have mode 600")
if path_stat.st_nlink != 1:
    raise SystemExit("campaign lock must have exactly one hard link")
if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("fd 9 does not identify the campaign lock file")
PY
}

capture_account_snapshot() {
  local output="$1"
  "$PYTHON" "$CAPTURE_ACCOUNT" \
    "$output" \
    --secret-file "$OPENROUTER_SECRET_FILE" \
    --expected-key-env OPENROUTER_API_KEY >/dev/null
}

write_account_reconciliation() {
  "$PYTHON" - \
    "$ACCOUNT_BEFORE" \
    "$ACCOUNT_AFTER" \
    "$ACCOUNT_RECON" \
    "$RUNTIME_ENV" \
    "$LOCK_FILE" \
    "$ACCOUNT_SETTLEMENT_MIN_SECONDS" \
    "$ACCOUNT_SETTLEMENT_STABLE_POLLS" \
    "$ACCOUNT_SETTLEMENT_POLL_SECONDS" \
    "${ACCOUNT_POLL_FILES[@]}" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

before_path = Path(sys.argv[1])
after_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
runtime_path = Path(sys.argv[4])
lock_path = Path(sys.argv[5])
minimum_settlement_seconds = int(sys.argv[6])
required_stable_polls = int(sys.argv[7])
poll_interval_seconds = int(sys.argv[8])
poll_paths = [Path(value) for value in sys.argv[9:]]

if len(poll_paths) < required_stable_polls:
    raise SystemExit(
        "stable reconciliation does not have the required account polls"
    )

before = json.loads(before_path.read_text(encoding="utf-8"))
after = json.loads(after_path.read_text(encoding="utf-8"))
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
polls = [json.loads(path.read_text(encoding="utf-8")) for path in poll_paths]
fingerprint = str(before.get("api_key_sha256") or "")
if (
    len(fingerprint) != 64
    or fingerprint != str(after.get("api_key_sha256") or "")
    or before.get("benchmark_environment_key_verified") is not True
    or after.get("benchmark_environment_key_verified") is not True
):
    raise SystemExit("account snapshots do not prove the same benchmark key")
if before.get("is_free_tier") is not False or after.get("is_free_tier") is not False:
    raise SystemExit("formal campaign requires a paid OpenRouter key")
runtime_environment_sha256 = str(runtime.get("environment_sha256") or "")
if (
    len(runtime_environment_sha256) != 64
    or any(
        character not in "0123456789abcdef"
        for character in runtime_environment_sha256
    )
):
    raise SystemExit("runtime environment artifact has an invalid environment_sha256")

usage_before = Decimal(str(before["usage"]))
usage_after = Decimal(str(after["usage"]))
byok_before = Decimal(str(before["byok_usage"]))
byok_after = Decimal(str(after["byok_usage"]))
usage_delta = usage_after - usage_before
byok_delta = byok_after - byok_before
if usage_delta < 0:
    raise SystemExit("OpenRouter usage decreased during the account window")
if any(
    str(poll.get("api_key_sha256") or "") != fingerprint
    or poll.get("benchmark_environment_key_verified") is not True
    or poll.get("is_free_tier") is not False
    for poll in polls
):
    raise SystemExit("account poll does not prove the same verified paid key")

poll_usage = [Decimal(str(poll["usage"])) for poll in polls]
poll_byok_usage = [Decimal(str(poll["byok_usage"])) for poll in polls]
if poll_usage[0] < usage_before or any(
    later < earlier for earlier, later in zip(poll_usage, poll_usage[1:])
):
    raise SystemExit("OpenRouter account poll usage is not monotonic")
if poll_byok_usage[0] < byok_before or any(
    later < earlier
    for earlier, later in zip(poll_byok_usage, poll_byok_usage[1:])
):
    raise SystemExit("OpenRouter BYOK account poll usage is not monotonic")
if poll_usage[-1] != usage_after or poll_byok_usage[-1] != byok_after:
    raise SystemExit("account-after snapshot does not match the final stable poll")

observations = [
    {
        "captured_at": str(poll["captured_at"]),
        "usage": str(poll["usage"]),
        "byok_usage": str(poll["byok_usage"]),
    }
    for poll in polls
]


def parse_observation_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("account poll has an invalid captured_at timestamp") from exc
    if parsed.tzinfo is None:
        raise SystemExit("account poll captured_at timestamp must include a timezone")
    return parsed


observation_times = [
    parse_observation_time(observation["captured_at"])
    for observation in observations
]
if any(
    later < earlier
    for earlier, later in zip(observation_times, observation_times[1:])
):
    raise SystemExit("account poll timestamps are not monotonic")
observation_span_seconds = (
    observation_times[-1] - observation_times[0]
).total_seconds()
if observation_span_seconds < minimum_settlement_seconds:
    raise SystemExit("OpenRouter account settlement window is too short")

final_usage = observations[-1]["usage"]
final_byok_usage = observations[-1]["byok_usage"]
stable_tail_count = 0
for observation in reversed(observations):
    if (
        observation["usage"] != final_usage
        or observation["byok_usage"] != final_byok_usage
    ):
        break
    stable_tail_count += 1
if stable_tail_count < required_stable_polls:
    raise SystemExit(
        "final OpenRouter account observations do not contain the required stable tail"
    )
stable_tail_start_index = len(observations) - stable_tail_count
stable_tail_span_seconds = (
    observation_times[-1] - observation_times[stable_tail_start_index]
).total_seconds()
minimum_stable_tail_seconds = (
    required_stable_polls - 1
) * poll_interval_seconds
if stable_tail_span_seconds < minimum_stable_tail_seconds:
    raise SystemExit("OpenRouter stable settlement tail is too short")

lock_stat = os.stat(lock_path)
fd_stat = os.fstat(9)
if (lock_stat.st_dev, lock_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("fd 9 does not identify the campaign lock file")

payload = {
    "schema": "opensquilla.openrouter-account-reconciliation/v1",
    "settlement_status": "stable",
    "api_key_sha256": fingerprint,
    "usage_before_usd": str(usage_before),
    "usage_after_usd": str(usage_after),
    "usage_delta_usd": str(usage_delta),
    "byok_usage_before_usd": str(byok_before),
    "byok_usage_after_usd": str(byok_after),
    "byok_usage_delta_usd": str(byok_delta),
    # Preserve policy truth without turning a BYOK finding into execution
    # failure. Finalization reports this independently from usable outputs.
    "non_byok_policy_pass": byok_delta == 0,
    "policy_status": "compliant" if byok_delta == 0 else "byok_detected",
    "is_free_tier": False,
    # stable_poll_count is deliberately the length of the consecutive stable
    # tail, not the total number of polls made while waiting for settlement.
    "stable_poll_count": stable_tail_count,
    "required_stable_poll_count": required_stable_polls,
    "poll_observation_count": len(observations),
    "stable_tail_start_index": stable_tail_start_index,
    "poll_interval_seconds": poll_interval_seconds,
    "minimum_settlement_seconds": minimum_settlement_seconds,
    "minimum_stable_tail_seconds": minimum_stable_tail_seconds,
    "observation_span_seconds": observation_span_seconds,
    "stable_tail_span_seconds": stable_tail_span_seconds,
    "stable_observations": observations,
    "lock_file": str(lock_path),
    "lock_inode": lock_stat.st_ino,
    "runtime_environment_sha256": runtime_environment_sha256,
    "runtime_environment_file_sha256": hashlib.sha256(
        runtime_path.read_bytes()
    ).hexdigest(),
}
temporary = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
temporary.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(temporary, 0o600)
os.replace(temporary, output_path)
PY
}

capture_stable_after() {
  if [[ "$ACCOUNT_SETTLED" == "1" ]]; then
    return 0
  fi
  local previous_usage=""
  local previous_byok=""
  local stable_streak=0
  local settlement_started_epoch elapsed_seconds=0
  local attempt poll_path usage byok now_epoch
  settlement_started_epoch="$(date +%s)" || return 2
  for ((attempt = 1; attempt <= 120; attempt++)); do
    ACCOUNT_POLL_SEQUENCE=$((ACCOUNT_POLL_SEQUENCE + 1))
    poll_path="$ACCOUNT_POLL_DIR/poll-$(printf '%06d' "$ACCOUNT_POLL_SEQUENCE").json"
    capture_account_snapshot "$poll_path" || return 2
    ACCOUNT_POLL_FILES+=("$poll_path")
    usage="$(jq -r '.usage' "$poll_path")" || return 2
    byok="$(jq -r '.byok_usage' "$poll_path")" || return 2
    if [[ -n "$previous_usage" && "$usage" == "$previous_usage" && "$byok" == "$previous_byok" ]]; then
      stable_streak=$((stable_streak + 1))
    else
      stable_streak=1
    fi
    now_epoch="$(date +%s)" || return 2
    elapsed_seconds=$((now_epoch - settlement_started_epoch))
    if [[
      "$stable_streak" -ge "$ACCOUNT_SETTLEMENT_STABLE_POLLS"
      && "$elapsed_seconds" -ge "$ACCOUNT_SETTLEMENT_BREAK_SECONDS"
    ]]; then
      break
    fi
    previous_usage="$usage"
    previous_byok="$byok"
    sleep "$ACCOUNT_SETTLEMENT_POLL_SECONDS" || return 2
  done
  if [[
    "$stable_streak" -lt "$ACCOUNT_SETTLEMENT_STABLE_POLLS"
    || "$elapsed_seconds" -lt "$ACCOUNT_SETTLEMENT_BREAK_SECONDS"
  ]]; then
    echo "OpenRouter account usage did not reach the required stable settlement window" >&2
    return 2
  fi
  install -m 600 "${ACCOUNT_POLL_FILES[-1]}" "$ACCOUNT_AFTER" || return 2
  write_account_reconciliation || return 2
  ACCOUNT_SETTLED=1
}

capture_after_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "$ACCOUNT_WINDOW_OPEN" == "1" && "$ACCOUNT_SETTLED" != "1" ]]; then
    capture_stable_after || true
  fi
  # A successfully assembled but not yet committed formal result is forensic
  # process material, not a completed experiment. Keep it under archive/ and
  # leave the root without manifest.json so consumers fail closed.
  if [[
    -d "$FINAL_OUTPUT_DIR"
    && ! -L "$FINAL_OUTPUT_DIR"
    && ! -e "$OUTPUT_DIR/manifest.json"
    && ! -e "$ARCHIVE_DIR/incomplete-formal-results"
  ]]; then
    mv -- "$FINAL_OUTPUT_DIR" "$ARCHIVE_DIR/incomplete-formal-results"
  fi
  exit "$status"
}
trap capture_after_on_failure EXIT

publish_final_artifacts() {
  local formal_dir="${1:-$FINAL_OUTPUT_DIR}"
  local output_dir="${2:-$OUTPUT_DIR}"
  local archive_dir="${3:-$ARCHIVE_DIR}"
  "$PYTHON" - "$formal_dir" "$output_dir" "$archive_dir" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

formal_dir = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
archive_dir = Path(sys.argv[3])
artifact_names = (
    "results.jsonl",
    "trace.jsonl",
    "actual-spend-ledger.jsonl",
    "openrouter-non-byok-campaign-proof.json",
    "audit.json",
    "EXPERIMENT_RESULTS.md",
)
manifest_name = "manifest.json"
expected_formal_names = {*artifact_names, manifest_name}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_plain_directory(path: Path, *, label: str) -> Path:
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise SystemExit(f"{label} must be a non-symlink directory")
    if path_stat.st_uid != os.getuid():
        raise SystemExit(f"{label} must be owned by the campaign user")
    return path.resolve()


def require_plain_file(path: Path, *, label: str) -> os.stat_result:
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise SystemExit(f"{label} must be a regular non-symlink file")
    if path_stat.st_uid != os.getuid() or path_stat.st_nlink != 1:
        raise SystemExit(f"{label} must be singly linked and campaign-owned")
    return path_stat


output_resolved = require_plain_directory(output_dir, label="campaign output")
archive_resolved = require_plain_directory(archive_dir, label="campaign archive")
formal_resolved = require_plain_directory(formal_dir, label="formal result staging")
if archive_resolved.parent != output_resolved:
    raise SystemExit("archive/ is not a direct child of the campaign output")
if formal_resolved.parent != output_resolved:
    raise SystemExit("formal result staging is not inside the campaign output")
root_names = {path.name for path in output_dir.iterdir()}
if root_names != {archive_dir.name, formal_dir.name}:
    raise SystemExit(
        "campaign root contains files other than archive/ and formal staging"
    )
formal_names = {path.name for path in formal_dir.iterdir()}
if formal_names != expected_formal_names:
    raise SystemExit("formal result staging has a missing or unexpected artifact")

manifest_path = formal_dir / manifest_name
require_plain_file(manifest_path, label="formal manifest")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if (
    not isinstance(manifest, dict)
    or manifest.get("schema")
    != "opensquilla.draco.campaign-final-manifest/v1"
    or manifest.get("status") != "complete"
):
    raise SystemExit("formal manifest is not a completed campaign manifest")
declared_manifest_sha256 = str(manifest.get("manifest_sha256") or "")
manifest_without_digest = dict(manifest)
manifest_without_digest.pop("manifest_sha256", None)
if declared_manifest_sha256 != canonical_sha256(manifest_without_digest):
    raise SystemExit("formal manifest self-hash differs")

artifact_records = manifest.get("artifacts")
if (
    not isinstance(artifact_records, Mapping)
    or set(artifact_records) != set(artifact_names)
):
    raise SystemExit("formal manifest artifact inventory differs")
for name in artifact_names:
    artifact_path = formal_dir / name
    artifact_stat = require_plain_file(
        artifact_path, label=f"formal artifact {name}"
    )
    record = artifact_records.get(name)
    if (
        not isinstance(record, Mapping)
        or record.get("path") != name
        or record.get("sha256") != file_sha256(artifact_path)
        or record.get("size_bytes") != artifact_stat.st_size
        or record.get("mode") != oct(stat.S_IMODE(artifact_stat.st_mode))
    ):
        raise SystemExit(f"formal artifact record differs for {name}")


def require_archived_source(
    raw_path: object,
    raw_digest: object,
    *,
    label: str,
) -> None:
    source_path = Path(str(raw_path or ""))
    if not source_path.is_absolute():
        raise SystemExit(f"{label} path is not absolute")
    source_resolved = source_path.resolve(strict=True)
    try:
        source_resolved.relative_to(archive_resolved)
    except ValueError as exc:
        raise SystemExit(f"{label} is outside archive/") from exc
    require_plain_file(source_path, label=label)
    if raw_digest != file_sha256(source_path):
        raise SystemExit(f"{label} hash differs")


source_results = manifest.get("source_results")
source_manifests = manifest.get("source_manifests")
if not isinstance(source_results, list) or not source_results:
    raise SystemExit("formal manifest lacks source result evidence")
if not isinstance(source_manifests, list) or not source_manifests:
    raise SystemExit("formal manifest lacks source manifest evidence")
for index, source in enumerate(source_results):
    if not isinstance(source, Mapping):
        raise SystemExit("source result evidence is not an object")
    require_archived_source(
        source.get("path"),
        source.get("sha256"),
        label=f"source result {index}",
    )
for index, source in enumerate(source_manifests):
    if not isinstance(source, Mapping):
        raise SystemExit("source manifest evidence is not an object")
    require_archived_source(
        source.get("path"),
        source.get("sha256"),
        label=f"source manifest {index}",
    )
    require_archived_source(
        source.get("result_path"),
        source.get("result_sha256"),
        label=f"source manifest result {index}",
    )

cost_attribution = manifest.get("cost_attribution")
if not isinstance(cost_attribution, Mapping):
    raise SystemExit("formal manifest lacks cost attribution")
account_windows = cost_attribution.get("account_windows")
proof = json.loads(
    (formal_dir / "openrouter-non-byok-campaign-proof.json").read_text(
        encoding="utf-8"
    )
)
audit = json.loads((formal_dir / "audit.json").read_text(encoding="utf-8"))
publishable_account_audit_conflict = bool(
    isinstance(proof, Mapping)
    and proof.get("publication_eligible") is True
    and proof.get("audit_conflict_kind") == "account_proof_incomplete"
    and proof.get("status") == "audit_conflict"
    and proof.get("pass") is False
    and proof.get("policy_pass") is False
    and proof.get("execution_pass") is True
    and isinstance(proof.get("reconciliation"), Mapping)
    and proof["reconciliation"].get("pass") is False
    and proof["reconciliation"].get("status") == "audit_conflict"
    and proof.get("account_windows") == []
    and isinstance(proof.get("cost_scope"), Mapping)
    and proof["cost_scope"].get("account_windows") == []
    and isinstance(audit, Mapping)
    and audit.get("execution_pass") is True
    and audit.get("pass") is False
    and audit.get("status") == "complete_with_warnings"
    and manifest.get("execution_pass") is True
    and manifest.get("audit_pass") is False
    and manifest.get("audit_status") == "complete_with_warnings"
    and manifest.get("reconciliation") == proof.get("reconciliation")
    and manifest.get("account_windows") == []
)
if not isinstance(account_windows, list) or (
    not account_windows and not publishable_account_audit_conflict
):
    raise SystemExit("formal manifest lacks account windows")
allowed_window_kinds = {"current", "prior_aborted", "prior_campaign"}
window_kinds: list[str] = []
for window in account_windows:
    if not isinstance(window, Mapping):
        raise SystemExit("formal account window is not an object")
    kind = window.get("kind")
    if not isinstance(kind, str) or kind not in allowed_window_kinds:
        raise SystemExit("formal account window kind differs")
    window_kinds.append(kind)
if account_windows and window_kinds.count("current") != 1:
    raise SystemExit("formal account window kinds differ")
try:
    has_positive_prior = any(
        window.get("kind") == "prior_aborted"
        and Decimal(str(window.get("usage_delta_usd"))) > 0
        for window in account_windows
        if isinstance(window, Mapping)
    )
except (InvalidOperation, TypeError, ValueError) as exc:
    raise SystemExit("formal account window delta is invalid") from exc
if has_positive_prior and (
    cost_attribution.get("attribution_precision")
    != "multi-window-counter-exact-campaign-attribution-unproven"
    or cost_attribution.get("campaign_attributable_exact") is not False
):
    raise SystemExit("formal positive-prior attribution semantics differ")
for window_index, window in enumerate(account_windows):
    sources = window.get("sources")
    if not isinstance(sources, list) or len(sources) != 4:
        raise SystemExit("formal account window source inventory differs")
    for source_index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise SystemExit("formal account window source is not an object")
        require_archived_source(
            source.get("path"),
            source.get("sha256"),
            label=f"account window {window_index} source {source_index}",
        )

pending_manifest = output_dir / f".manifest.pending-{os.getpid()}"
if pending_manifest.exists() or pending_manifest.is_symlink():
    raise SystemExit("manifest publication staging path already exists")
for name in expected_formal_names:
    target = output_dir / name
    if target.exists() or target.is_symlink():
        raise SystemExit(f"refusing to overwrite campaign root artifact: {name}")

moved: list[str] = []
manifest_location = manifest_path
try:
    # Move all immutable data first. manifest.json is the final commit marker:
    # its presence in the experiment root means every declared artifact exists.
    for name in artifact_names:
        os.replace(formal_dir / name, output_dir / name)
        moved.append(name)
    os.replace(manifest_path, pending_manifest)
    manifest_location = pending_manifest
    formal_dir.rmdir()
    directory_fd = os.open(output_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.replace(pending_manifest, output_dir / manifest_name)
    manifest_location = output_dir / manifest_name
    directory_fd = os.open(output_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    formal_dir.mkdir(mode=0o700, exist_ok=True)
    if manifest_location.exists() or manifest_location.is_symlink():
        os.replace(manifest_location, formal_dir / manifest_name)
    for name in reversed(moved):
        target = output_dir / name
        if target.exists() or target.is_symlink():
            os.replace(target, formal_dir / name)
    directory_fd = os.open(output_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    raise
PY
}

record_wave_artifacts() {
  local wave_dir="$1"
  local -a results=()
  local -a manifests=()
  mapfile -t results < <(
    find "$wave_dir" -maxdepth 1 -type f -name 'draco_ensemble_*.jsonl' | sort
  )
  mapfile -t manifests < <(
    find "$wave_dir" -maxdepth 1 -type f -name 'draco_run_*.manifest.json' | sort
  )
  if [[ "${#results[@]}" -ne 1 || "${#manifests[@]}" -ne 1 ]]; then
    echo "Each campaign wave must produce exactly one result JSONL and manifest" >&2
    return 2
  fi
  RESULT_JSONLS+=("${results[0]}")
  MANIFESTS+=("${manifests[0]}")
}

accept_or_reject_wave_status() {
  local runner_status="$1"
  local manifest="$2"
  local manifest_status
  manifest_status="$(jq -er '.status | select(type == "string" and length > 0)' "$manifest")"

  if [[ "$runner_status" != "0" && "$runner_status" != "2" ]]; then
    echo "Campaign wave exited unexpectedly with status $runner_status" >&2
    return 2
  fi
  case "$manifest_status" in
    complete)
      if [[ "$runner_status" != "0" ]]; then
        echo "Complete manifest is inconsistent with runner status $runner_status" >&2
        return 2
      fi
      ;;
    metadata_incomplete|judge_incomplete|result_incomplete|resume_repair_incomplete|cost_audit_failed|audit_incomplete)
      # These statuses carry sealed rows that the offline action classifier can
      # route to regeneration, Judge-only repair, or audit-only finalization.
      # Cost and BYOK findings remain visible but do not invalidate usable
      # model output.
      ;;
    preflight_failed)
      echo "Campaign wave failed closed with manifest status: $manifest_status" >&2
      return 2
      ;;
    *)
      echo "Campaign wave has an unexpected manifest status: $manifest_status" >&2
      return 2
      ;;
  esac
}

write_actionable_keys() {
  local output="$1"
  local summary="$2"
  shift 2
  "$PYTHON" - \
    "$RESUME_RUNNER" \
    "$FINALIZER" \
    "$INPUT" \
    "$COMPATIBILITY_MANIFEST" \
    "$output" \
    "$summary" \
    "$DRACO_GROUPS" \
    "$@" <<'PY'
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

runner_path = Path(sys.argv[1])
finalizer_path = Path(sys.argv[2])
input_path = Path(sys.argv[3])
manifest_path = Path(sys.argv[4])
output_path = Path(sys.argv[5])
summary_path = Path(sys.argv[6])
groups_raw = sys.argv[7]
result_paths = [Path(value) for value in sys.argv[8:]]

spec = importlib.util.spec_from_file_location("draco_campaign_resume_gate", runner_path)
if spec is None or spec.loader is None:
    raise SystemExit("unable to load the DRACO resume classifier")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

finalizer_spec = importlib.util.spec_from_file_location(
    "draco_campaign_finalizer_gate",
    finalizer_path,
)
if finalizer_spec is None or finalizer_spec.loader is None:
    raise SystemExit("unable to load the DRACO finalizer usage contract")
finalizer = importlib.util.module_from_spec(finalizer_spec)
sys.modules[finalizer_spec.name] = finalizer
finalizer_spec.loader.exec_module(finalizer)

supported_groups = ("B0", "B1", "B2", "B4", "G1")
groups = tuple(groups_raw.split(","))
if (
    not groups
    or any(group not in supported_groups for group in groups)
    or len(set(groups)) != len(groups)
    or tuple(group for group in supported_groups if group in groups) != groups
):
    raise SystemExit("unsafe or non-canonical campaign group selection")
tasks = module.load_tasks(input_path, max_tasks=0)
selected = {(group, str(task["id"])) for task in tasks for group in groups}
prompt_hashes = {
    str(task["id"]): module.text_sha256(str(task.get("prompt") or ""))
    for task in tasks
}
task_hashes = {
    str(task["id"]): module.canonical_json_sha256(task)
    for task in tasks
}
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
compatibility = manifest.get("run_compatibility")
if not isinstance(compatibility, dict):
    raise SystemExit("compatibility manifest lacks run_compatibility")
contracts = compatibility.get("contracts")
if not isinstance(contracts, dict):
    raise SystemExit("compatibility manifest lacks authenticated contracts")
generation_attempt_limits = {}
for group in groups:
    contract = contracts.get(group)
    generation = contract.get("generation") if isinstance(contract, dict) else None
    limit = generation.get("max_attempts") if isinstance(generation, dict) else None
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= module.GENERATION_MAX_ATTEMPTS
    ):
        raise SystemExit(
            f"compatibility contract for {group} has an invalid generation attempt limit"
        )
    generation_attempt_limits[group] = limit

states, audit = module.load_resume_group_task_states(
    resume_paths=result_paths,
    selected_keys=selected,
    prompt_hashes=prompt_hashes,
    task_input_hashes=task_hashes,
    run_compatibility_fingerprints=compatibility["fingerprints"],
    run_compatibility_contracts=compatibility["contracts"],
    require_openrouter_non_byok=True,
    judge_required=True,
)

actionable = []
campaign_proof_only = []
budget_exhausted = []
judge_budget_exhausted = []
complete = []
policy_violations = []
for group, task_id in sorted(selected):
    state = states.get((group, task_id))
    if state is None:
        actionable.append({"group": group, "task_id": task_id, "action": "regenerate"})
        continue
    action = str(state["action"])
    prior_used = module.coerce_metric_int(state.get("prior_generation_attempts_used"))
    if action == "policy_violation":
        policy_violations.append({"group": group, "task_id": task_id})
        campaign_proof_only.append({"group": group, "task_id": task_id})
    elif not state["generation_valid"]:
        target = {"group": group, "task_id": task_id, "action": "regenerate"}
        if prior_used < generation_attempt_limits[group]:
            actionable.append(target)
        else:
            budget_exhausted.append(target)
    elif not state["judge_complete"]:
        # The resume classifier may call this metadata_only when a historical
        # receipt is absent. It is still an actionable Judge-only gap: the
        # accepted generation is reused verbatim and no generation call starts.
        target = {"group": group, "task_id": task_id, "action": "judge_only"}
        if state.get("judge_attempt_budget_exhausted") is True:
            judge_budget_exhausted.append(target)
        else:
            actionable.append(target)
    elif action == "metadata_only":
        # Run the deterministic metadata repair at most once.  It never calls
        # generation or Judge; any gap left afterwards belongs to the locked
        # campaign account proof/finalizer and must not create another wave.
        if state.get("metadata_repair_attempted") is True:
            proof_only_reasons = finalizer.proof_only_usage_evidence_reasons(
                state["row"]
            )
            if proof_only_reasons:
                policy_violations.append(
                    {
                        "group": group,
                        "task_id": task_id,
                        "audit_reasons": proof_only_reasons[:5],
                    }
                )
            campaign_proof_only.append({"group": group, "task_id": task_id})
        else:
            actionable.append(
                {"group": group, "task_id": task_id, "action": "metadata_only"}
            )
    elif action == "audit_only":
        campaign_proof_only.append({"group": group, "task_id": task_id})
        policy_violations.append(
            {
                "group": group,
                "task_id": task_id,
                "audit_reasons": list(state.get("audit_reasons") or []),
            }
        )
    elif action == "complete":
        complete.append({"group": group, "task_id": task_id})
    else:
        raise SystemExit(f"unsupported resume action: {action}")

temporary = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
with temporary.open("w", encoding="utf-8") as handle:
    for item in actionable:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, output_path)

summary_payload = {
    "schema": "opensquilla.draco-campaign-wave-gate/v1",
    "groups": list(groups),
    "selected_pair_count": len(selected),
    "actionable_pair_count": len(actionable),
    "campaign_proof_only_pair_count": len(campaign_proof_only),
    "generation_budget_exhausted_pair_count": len(budget_exhausted),
    "judge_budget_exhausted_pair_count": len(judge_budget_exhausted),
    "complete_pair_count": len(complete),
    "audit_finding_pair_count": len(policy_violations),
    "actionable_pairs": actionable,
    "campaign_proof_only_pairs": campaign_proof_only,
    "generation_budget_exhausted_pairs": budget_exhausted,
    "judge_budget_exhausted_pairs": judge_budget_exhausted,
    "audit_findings": policy_violations,
    "resume_audit": audit,
}
temporary_summary = summary_path.with_name(
    f".{summary_path.name}.tmp-{os.getpid()}"
)
temporary_summary.write_text(
    json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(temporary_summary, 0o600)
os.replace(temporary_summary, summary_path)
PY
}

run_confirmatory_pair() {
  local schedule_copy="$ARCHIVE_DIR/confirmatory-schedule.json"
  local control_override="$ARCHIVE_DIR/control-override.json"
  local candidate_override="$ARCHIVE_DIR/candidate-override.json"
  local shard_root="$ARCHIVE_DIR/confirmatory-shards"
  local control_output="$OUTPUT_DIR/control"
  local candidate_output="$OUTPUT_DIR/candidate"
  local control_archive="$control_output/archive"
  local candidate_archive="$candidate_output/archive"
  local cohort_id
  local schedule_sha256
  local role
  local tranche_index
  local leg
  local phase_failed=0
  local control_pid=""
  local candidate_pid=""
  local control_rc=0
  local candidate_rc=0
  local control_shard=""
  local candidate_shard=""
  local -a control_results=()
  local -a control_manifests=()
  local -a candidate_results=()
  local -a candidate_manifests=()

  install -m 600 "$CONFIRMATORY_SCHEDULE" "$schedule_copy"
  "$PYTHON" - "$schedule_copy" "$control_override" "$candidate_override" \
    "$CONFIRMATORY_VALIDATED_SCHEDULE_SHA256" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

schedule = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
without_hash = dict(schedule)
recorded_hash = without_hash.pop("schedule_sha256", None)
canonical = json.dumps(
    without_hash,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode()
if (
    recorded_hash != sys.argv[4]
    or recorded_hash != hashlib.sha256(canonical).hexdigest()
):
    raise SystemExit("confirmatory schedule changed after preflight validation")
for role, destination in zip(("control", "candidate"), sys.argv[2:4], strict=True):
    path = Path(destination)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(schedule["roles"][role]["override"], ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
PY
  cohort_id="$(jq -er '.cohort_id | select(type == "string" and length > 0)' "$schedule_copy")"
  schedule_sha256="$(jq -er '.schedule_sha256 | select(type == "string" and test("^[0-9a-f]{64}$"))' "$schedule_copy")"
  mkdir "$shard_root"

  cd "$SNAPSHOT_REPO"
  if [[ -L "$LOCK_FILE" ]]; then
    echo "Campaign lock path must not be a symlink" >&2
    return 2
  fi
  exec 9<>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another benchmark owns the exclusive OpenRouter attribution window" >&2
    return 2
  fi
  LOCK_HELD=1
  validate_lock_file

  "$PYTHON" "$CAPTURE_RUNTIME" capture "$RUNTIME_ENV" --repo "$SNAPSHOT_REPO"
  for role in control candidate; do
    local role_override
    local role_preflight
    local role_override_json
    if [[ "$role" == "control" ]]; then
      role_override="$control_override"
    else
      role_override="$candidate_override"
    fi
    role_preflight="$ARCHIVE_DIR/preflight/openrouter-route-$role.json"
    role_override_json="$(jq -c . "$role_override")"
    "$PYTHON" "$ROUTE_PREFLIGHT" \
      "$role_preflight" \
      --scope formal \
      --groups G1 \
      --experiment-config "$EXPERIMENT_CONFIG" \
      --experiment-config-override-json "$role_override_json" >/dev/null

    local static_dir="$ARCHIVE_DIR/preflight/static-$role"
    mkdir "$static_dir"
    "$PYTHON" "$MAIN_RUNNER" \
      --input "$INPUT" \
      --config "$CONFIG" \
      --experiment-config "$EXPERIMENT_CONFIG" \
      --experiment-config-override "$role_override" \
      --experiment-config-set "runner.concurrency=$TASK_CONCURRENCY" \
      --groups G1 \
      --max-tasks 10 \
      --output-dir "$static_dir" \
      --dry-run \
      --require-openrouter-non-byok \
      --require-clean-source >"$static_dir/run.log" 2>&1
  done

  capture_account_snapshot "$ACCOUNT_BEFORE"
  if ! jq -e '
    .benchmark_environment_key_verified == true
    and .is_free_tier == false
    and (.api_key_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
  ' "$ACCOUNT_BEFORE" >/dev/null; then
    echo "OpenRouter account-before snapshot is not a verified paid-key boundary" >&2
    return 2
  fi
  ACCOUNT_WINDOW_OPEN=1

  record_confirmatory_shard() {
    local shard_role="$1"
    local shard_dir="$2"
    local runner_status="$3"
    local -a results=()
    local -a manifests=()
    mapfile -t results < <(
      find "$shard_dir" -maxdepth 1 -type f -name 'draco_ensemble_*.jsonl' | sort
    )
    mapfile -t manifests < <(
      find "$shard_dir" -maxdepth 1 -type f -name 'draco_run_*.manifest.json' | sort
    )
    if [[ "${#results[@]}" -ne 1 || "${#manifests[@]}" -ne 1 ]]; then
      echo "Confirmatory shard did not produce one result and manifest" >&2
      return 2
    fi
    accept_or_reject_wave_status "$runner_status" "${manifests[0]}"
    if [[ "$runner_status" != "0" ]] || [[ "$(jq -r '.status' "${manifests[0]}")" != "complete" ]]; then
      echo "Confirmatory generation leg is incomplete; unpaired retry is forbidden" >&2
      return 2
    fi
    if [[ "$shard_role" == "control" ]]; then
      control_results+=("${results[0]}")
      control_manifests+=("${manifests[0]}")
    else
      candidate_results+=("${results[0]}")
      candidate_manifests+=("${manifests[0]}")
    fi
  }

  launch_confirmatory_shard() {
    local shard_role="$1"
    local override_path="$2"
    local shard_dir="$3"
    shift 3
    local -a task_ids=("$@")
    local -a command=(
      "$PYTHON" "$MAIN_RUNNER"
      --input "$INPUT"
      --config "$CONFIG"
      --experiment-config "$EXPERIMENT_CONFIG"
      --experiment-config-override "$override_path"
      --experiment-config-set "runner.concurrency=$TASK_CONCURRENCY"
      --groups G1
      --max-tasks 10
      --output-dir "$shard_dir"
      --require-clean-source
      --require-openrouter-non-byok
    )
    local task_id
    for task_id in "${task_ids[@]}"; do
      command+=(--task-ids "$task_id")
    done
    mkdir "$shard_dir"
    "${command[@]}" >"$shard_dir/run.log" 2>&1
  }

  for tranche_index in 0 1; do
    for leg in 0 1; do
      local -a control_tasks=()
      local -a candidate_tasks=()
      mapfile -t control_tasks < <(
        jq -er ".tranches[$tranche_index].phases[$leg].control_task_ids[]" "$schedule_copy"
      )
      mapfile -t candidate_tasks < <(
        jq -er ".tranches[$tranche_index].phases[$leg].candidate_task_ids[]" "$schedule_copy"
      )
      if (( ${#control_tasks[@]} + ${#candidate_tasks[@]} > TASK_CONCURRENCY )); then
        echo "Confirmatory phase exceeds global task concurrency" >&2
        return 2
      fi
      control_pid=""
      candidate_pid=""
      control_rc=0
      candidate_rc=0
      if (( ${#control_tasks[@]} > 0 )); then
        control_shard="$shard_root/control-t$((tranche_index + 1))-l$((leg + 1))"
        launch_confirmatory_shard \
          control "$control_override" "$control_shard" "${control_tasks[@]}" &
        control_pid=$!
      fi
      if (( ${#candidate_tasks[@]} > 0 )); then
        candidate_shard="$shard_root/candidate-t$((tranche_index + 1))-l$((leg + 1))"
        launch_confirmatory_shard \
          candidate "$candidate_override" "$candidate_shard" "${candidate_tasks[@]}" &
        candidate_pid=$!
      fi
      if [[ -n "$control_pid" ]]; then
        if wait "$control_pid"; then
          control_rc=0
        else
          control_rc=$?
        fi
      fi
      if [[ -n "$candidate_pid" ]]; then
        if wait "$candidate_pid"; then
          candidate_rc=0
        else
          candidate_rc=$?
        fi
      fi
      phase_failed=0
      if [[ -n "$control_pid" ]]; then
        if ! record_confirmatory_shard control "$control_shard" "$control_rc"; then
          phase_failed=1
        fi
      fi
      if [[ -n "$candidate_pid" ]]; then
        if ! record_confirmatory_shard candidate "$candidate_shard" "$candidate_rc"; then
          phase_failed=1
        fi
      fi
      if [[ "$phase_failed" == "1" ]]; then
        "$PYTHON" -c '
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
tmp.write_text(json.dumps({
    "schema": "opensquilla.draco-confirmatory-terminal/v1",
    "status": "incomplete",
    "reason": "generation_leg_incomplete_no_unpaired_retry",
    "cohort_id": sys.argv[2],
    "schedule_sha256": sys.argv[3],
}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$ARCHIVE_DIR/confirmatory-terminal.json" "$cohort_id" "$schedule_sha256"
        return 2
      fi
    done
  done

  capture_stable_after
  ACCOUNT_WINDOW_OPEN=0

  mkdir "$control_output" "$candidate_output"
  cp -a "$ARCHIVE_DIR" "$control_archive"
  cp -a "$ARCHIVE_DIR" "$candidate_archive"

  map_copied_paths() {
    local source_archive="$1"
    local destination_archive="$2"
    local source_name="$3"
    local destination_name="$4"
    local -n source_values="$source_name"
    local -n destination_values="$destination_name"
    local path relative
    destination_values=()
    for path in "${source_values[@]}"; do
      relative="${path#"$source_archive"/}"
      destination_values+=("$destination_archive/$relative")
    done
  }

  local -a control_role_results=()
  local -a control_role_manifests=()
  local -a control_companion_results=()
  local -a control_companion_manifests=()
  local -a candidate_role_results=()
  local -a candidate_role_manifests=()
  local -a candidate_companion_results=()
  local -a candidate_companion_manifests=()
  map_copied_paths "$ARCHIVE_DIR" "$control_archive" control_results control_role_results
  map_copied_paths "$ARCHIVE_DIR" "$control_archive" control_manifests control_role_manifests
  map_copied_paths "$ARCHIVE_DIR" "$control_archive" candidate_results control_companion_results
  map_copied_paths "$ARCHIVE_DIR" "$control_archive" candidate_manifests control_companion_manifests
  map_copied_paths "$ARCHIVE_DIR" "$candidate_archive" candidate_results candidate_role_results
  map_copied_paths "$ARCHIVE_DIR" "$candidate_archive" candidate_manifests candidate_role_manifests
  map_copied_paths "$ARCHIVE_DIR" "$candidate_archive" control_results candidate_companion_results
  map_copied_paths "$ARCHIVE_DIR" "$candidate_archive" control_manifests candidate_companion_manifests

  rebase_copied_manifest_artifacts() {
    local destination_archive="$1"
    shift
    "$PYTHON" - "$ARCHIVE_DIR" "$destination_archive" "$@" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

source_archive = Path(sys.argv[1]).resolve(strict=True)
destination_archive = Path(sys.argv[2]).resolve(strict=True)
for raw_manifest in sys.argv[3:]:
    manifest_path = Path(raw_manifest).resolve(strict=True)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise SystemExit("confirmatory copied manifest lacks artifact paths")
    rebased = {}
    for name, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise SystemExit("confirmatory copied manifest artifact path is malformed")
        try:
            relative = Path(raw_path).resolve(strict=True).relative_to(source_archive)
        except (OSError, ValueError) as exc:
            raise SystemExit(
                "confirmatory source artifact is outside the shared archive"
            ) from exc
        copied_path = destination_archive / relative
        if not copied_path.is_file() or copied_path.is_symlink():
            raise SystemExit("confirmatory copied artifact is missing or unsafe")
        rebased[name] = str(copied_path)
    payload["artifacts"] = rebased
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, manifest_path)
PY
  }
  rebase_copied_manifest_artifacts \
    "$control_archive" \
    "${control_role_manifests[@]}" \
    "${control_companion_manifests[@]}"
  rebase_copied_manifest_artifacts \
    "$candidate_archive" \
    "${candidate_role_manifests[@]}" \
    "${candidate_companion_manifests[@]}"

  finalize_confirmatory_role() {
    local cohort_role="$1"
    local role_output="$2"
    local role_archive="$3"
    local active_results_name="$4"
    local active_manifests_name="$5"
    local companion_results_name="$6"
    local companion_manifests_name="$7"
    local -n active_results="$active_results_name"
    local -n active_manifests="$active_manifests_name"
    local -n companion_results="$companion_results_name"
    local -n companion_manifests="$companion_manifests_name"
    local formal_dir="$role_output/.formal-results"
    local -a finalizer_args=(
      --input "$INPUT"
      --account-before "$role_archive/account/openrouter-account-before.json"
      --account-after "$role_archive/account/openrouter-account-after.json"
      --account-reconciliation "$role_archive/account/openrouter-account-reconciliation.json"
      --runtime-environment "$role_archive/runtime-environment.json"
      --lock-file "$LOCK_FILE"
      --lock-fd 9
      --output-dir "$formal_dir"
      --groups G1
      --max-generation-attempts "$GENERATION_MAX_ATTEMPTS"
      --expected-task-concurrency "$TASK_CONCURRENCY"
      --expected-judge-concurrency "$JUDGE_CONCURRENCY"
      --cohort-id "$cohort_id"
      --cohort-role "$cohort_role"
    )
    local path
    for path in "${active_results[@]}"; do
      finalizer_args+=(--result "$path")
    done
    for path in "${active_manifests[@]}"; do
      finalizer_args+=(--manifest "$path")
    done
    for path in "${companion_results[@]}"; do
      finalizer_args+=(--cohort-companion-result "$path")
    done
    for path in "${companion_manifests[@]}"; do
      finalizer_args+=(--cohort-companion-manifest "$path")
    done
    "$PYTHON" "$FINALIZER" "${finalizer_args[@]}" >"$role_archive/finalizer.log" 2>&1
    publish_final_artifacts "$formal_dir" "$role_output" "$role_archive"
  }

  finalize_confirmatory_role \
    control "$control_output" "$control_archive" \
    control_role_results control_role_manifests \
    control_companion_results control_companion_manifests
  finalize_confirmatory_role \
    candidate "$candidate_output" "$candidate_archive" \
    candidate_role_results candidate_role_manifests \
    candidate_companion_results candidate_companion_manifests

  "$PYTHON" - "$schedule_copy" "$control_output/manifest.json" \
    "$candidate_output/manifest.json" "$OUTPUT_DIR/cohort-manifest.json" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

schedule_path, control_path, candidate_path, output_path = map(Path, sys.argv[1:])
schedule = json.loads(schedule_path.read_text(encoding="utf-8"))

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

payload = {
    "schema": "opensquilla.draco-confirmatory-cohort-manifest/v1",
    "status": "complete",
    "cohort_id": schedule["cohort_id"],
    "schedule_sha256": schedule["schedule_sha256"],
    "roles": {
        "control": {"path": "control/manifest.json", "sha256": digest(control_path)},
        "candidate": {"path": "candidate/manifest.json", "sha256": digest(candidate_path)},
    },
    "account_delta_report_scope": "paired_cohort_once",
    "screening_is_diagnostic_only": True,
}
payload["manifest_sha256"] = hashlib.sha256(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
temporary = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, output_path)
PY

  flock -u 9
  exec 9>&-
  LOCK_HELD=0
  trap - EXIT
  echo "DRACO confirmatory paired cohort completed: $OUTPUT_DIR"
}

if [[ -n "$CONFIRMATORY_SCHEDULE" ]]; then
  run_confirmatory_pair
  exit $?
fi

cd "$SNAPSHOT_REPO"

# fd 9 remains open in every child, from the account-before boundary through
# all generation/Judge waves, stable account polling, and finalization.
if [[ -L "$LOCK_FILE" ]]; then
  echo "Campaign lock path must not be a symlink" >&2
  exit 2
fi
exec 9<>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another benchmark owns the exclusive OpenRouter attribution window" >&2
  exit 2
fi
LOCK_HELD=1
validate_lock_file

"$PYTHON" "$CAPTURE_RUNTIME" capture \
  "$RUNTIME_ENV" \
  --repo "$SNAPSHOT_REPO"

ROUTE_EVIDENCE="$ARCHIVE_DIR/preflight/openrouter-route-preflight.json"
ROUTE_PREFLIGHT_ARGS=(
  "$ROUTE_EVIDENCE"
  --scope formal
  --groups "$DRACO_GROUPS"
  --experiment-config "$EXPERIMENT_CONFIG"
)
if [[ "$EXPERIMENT_CONFIG_OVERRIDE_JSON_PRESENT" == "1" ]]; then
  ROUTE_PREFLIGHT_ARGS+=(
    --experiment-config-override-json "$EXPERIMENT_CONFIG_OVERRIDE_JSON"
  )
fi
"$PYTHON" "$ROUTE_PREFLIGHT" "${ROUTE_PREFLIGHT_ARGS[@]}" >/dev/null

STATIC_DIR="$ARCHIVE_DIR/preflight/static"
mkdir "$STATIC_DIR"
"$PYTHON" "$MAIN_RUNNER" \
  "${COMMON_ARGS[@]}" \
  --output-dir "$STATIC_DIR" \
  --dry-run \
  --require-openrouter-non-byok \
  --require-clean-source >"$STATIC_DIR/run.log" 2>&1
mapfile -t STATIC_MANIFESTS < <(
  find "$STATIC_DIR" -maxdepth 1 -type f -name 'draco_run_*.manifest.json' | sort
)
if [[ "${#STATIC_MANIFESTS[@]}" -ne 1 ]]; then
  echo "Static preflight did not produce one compatibility manifest" >&2
  exit 2
fi
STATIC_COMPATIBILITY_MANIFEST="${STATIC_MANIFESTS[0]}"

capture_account_snapshot "$ACCOUNT_BEFORE"
if ! jq -e '
  .benchmark_environment_key_verified == true
  and .is_free_tier == false
  and (.api_key_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
' "$ACCOUNT_BEFORE" >/dev/null; then
  echo "OpenRouter account-before snapshot is not a verified paid-key boundary" >&2
  exit 2
fi
ACCOUNT_WINDOW_OPEN=1

WAVE1_DIR="$ARCHIVE_DIR/waves/wave-1"
mkdir "$WAVE1_DIR"
if "$PYTHON" "$MAIN_RUNNER" \
    "${COMMON_ARGS[@]}" \
    --output-dir "$WAVE1_DIR" \
    --require-clean-source \
    --require-openrouter-non-byok >"$WAVE1_DIR/run.log" 2>&1; then
  WAVE1_STATUS=0
else
  WAVE1_STATUS=$?
fi
# A recoverable runner exit 2 still publishes sealed rows and a useful
# incomplete manifest. Record those artifacts before deciding whether to gate
# another wave. Missing/duplicate artifacts remain fatal.
record_wave_artifacts "$WAVE1_DIR"
accept_or_reject_wave_status "$WAVE1_STATUS" "${MANIFESTS[-1]}"
# The dry-run preflight deliberately has dry_run=true in its compatibility
# contract.  Resume waves must instead inherit the first live wave's exact
# contract (including dry_run=false and the strict non-BYOK policy).
COMPATIBILITY_MANIFEST="${MANIFESTS[-1]}"

for wave_number in 2 3; do
  ACTIONABLE_KEYS="$ARCHIVE_DIR/gates/wave-$wave_number-actionable.jsonl"
  GATE_SUMMARY="$ARCHIVE_DIR/gates/wave-$wave_number-summary.json"
  write_actionable_keys \
    "$ACTIONABLE_KEYS" \
    "$GATE_SUMMARY" \
    "${RESULT_JSONLS[@]}"
  if [[ ! -s "$ACTIONABLE_KEYS" ]]; then
    break
  fi

  WAVE_DIR="$ARCHIVE_DIR/waves/wave-$wave_number"
  mkdir "$WAVE_DIR"
  RESUME_ARGS=(
    "${COMMON_ARGS[@]}"
    --output-dir "$WAVE_DIR"
    --expected-compatibility-manifest "$COMPATIBILITY_MANIFEST"
    --only-group-task-keys "$ACTIONABLE_KEYS"
    --require-clean-source
    --require-openrouter-non-byok
  )
  for result_jsonl in "${RESULT_JSONLS[@]}"; do
    RESUME_ARGS+=(--resume-from-jsonl "$result_jsonl")
  done
  if "$PYTHON" "$RESUME_RUNNER" \
      "${RESUME_ARGS[@]}" >"$WAVE_DIR/run.log" 2>&1; then
    WAVE_STATUS=0
  else
    WAVE_STATUS=$?
  fi
  # As in wave 1, inspect the emitted contract rather than treating every
  # manifest_failure/exit 2 as fatal.
  record_wave_artifacts "$WAVE_DIR"
  accept_or_reject_wave_status "$WAVE_STATUS" "${MANIFESTS[-1]}"
done

capture_stable_after
ACCOUNT_WINDOW_OPEN=0

# Reclassify after the final resume wave.  The per-wave gate is intentionally
# computed before each wave to select its work; it cannot describe attempts
# consumed by that wave and therefore must not drive the terminal decision.
FINAL_ACTIONABLE_KEYS="$ARCHIVE_DIR/gates/final-actionable.jsonl"
FINAL_GATE_SUMMARY="$ARCHIVE_DIR/gates/final-summary.json"
write_actionable_keys \
  "$FINAL_ACTIONABLE_KEYS" \
  "$FINAL_GATE_SUMMARY" \
  "${RESULT_JSONLS[@]}"

# A model-attempt budget exhaustion cannot be repaired by the offline
# finalizer.  Persist a machine-readable terminal disposition and stop before
# running a finalizer that is guaranteed to reject the incomplete pair.
if jq -e '
  (.generation_budget_exhausted_pair_count // 0) > 0
  or (.judge_budget_exhausted_pair_count // 0) > 0
' "$FINAL_GATE_SUMMARY" >/dev/null; then
  FINALIZATION_STATUS_TMP="$ARCHIVE_DIR/.finalization-status.json.tmp-$$"
  if "$PYTHON" "$RECOVERY_STATUS" status "$OUTPUT_DIR" \
      >"$FINALIZATION_STATUS_TMP"; then
    FINALIZATION_STATUS_RC=0
  else
    FINALIZATION_STATUS_RC=$?
  fi
  if [[ "$FINALIZATION_STATUS_RC" != "2" ]] || ! jq -e '
    .state == "blocked"
    and .reason_code == "model_attempt_budget_exhausted"
  ' "$FINALIZATION_STATUS_TMP" >/dev/null; then
    rm -f -- "$FINALIZATION_STATUS_TMP"
    echo "Unable to persist the terminal model-budget disposition" >&2
    exit 2
  fi
  chmod 600 "$FINALIZATION_STATUS_TMP"
  mv "$FINALIZATION_STATUS_TMP" "$ARCHIVE_DIR/finalization-status.json"
  echo "Campaign exhausted a model-attempt budget; offline finalization is blocked" >&2
  exit 2
fi

FINALIZER_ARGS=(
  --input "$INPUT"
  --account-before "$ACCOUNT_BEFORE"
  --account-after "$ACCOUNT_AFTER"
  --account-reconciliation "$ACCOUNT_RECON"
  --runtime-environment "$RUNTIME_ENV"
  --lock-file "$LOCK_FILE"
  --lock-fd 9
  --output-dir "$FINAL_OUTPUT_DIR"
  --groups "$DRACO_GROUPS"
  --max-generation-attempts "$GENERATION_MAX_ATTEMPTS"
  --expected-task-concurrency "$TASK_CONCURRENCY"
  --expected-judge-concurrency "$JUDGE_CONCURRENCY"
)
for prior_account_window_dir in "${PRIOR_ACCOUNT_WINDOW_DIRS[@]}"; do
  FINALIZER_ARGS+=(--prior-account-window-dir "$prior_account_window_dir")
done
for result_jsonl in "${RESULT_JSONLS[@]}"; do
  FINALIZER_ARGS+=(--result "$result_jsonl")
done
for manifest in "${MANIFESTS[@]}"; do
  FINALIZER_ARGS+=(--manifest "$manifest")
done
"$PYTHON" "$FINALIZER" "${FINALIZER_ARGS[@]}" \
  >"$ARCHIVE_DIR/finalizer.log" 2>&1
publish_final_artifacts

# Release only after the finalizer has assembled the audited artifacts and
# manifest.json has committed them directly in the campaign root.
flock -u 9
exec 9>&-
LOCK_HELD=0
trap - EXIT

echo "DRACO campaign completed: $OUTPUT_DIR"
