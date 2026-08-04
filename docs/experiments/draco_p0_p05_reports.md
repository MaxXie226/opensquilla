# P0 / P0.5 DRACO-mini report generator

This is the read-only terminal reporting implementation for the frozen
`p0-p05-20260804-234508` campaign. It does not contain or invoke any
model/provider client.

## Files

- `scripts/experiments/generate_draco_p0_p05_reports.py`: terminal campaign
  report generator.
- `tests/test_scripts/test_generate_draco_p0_p05_reports.py`: deterministic
  synthetic fixture plus focused
  cost and pairing tests.

## Default invocation on the ECS

Run only after the controller has reached `succeeded` or
`completed_with_failures`:

```bash
python3 scripts/experiments/generate_draco_p0_p05_reports.py \
  --plan /home/codex/draco-runs/p0-p05-20260804-234508/campaign-plan.json \
  --strict
```

Defaults derived from the plan:

- immutable terminal status: `<run_root>/terminal-status-input.json`
- derived receipts: exactly `<run_root>/derived-plan.json`; neither the status
  descriptor nor `--derived-plan` may redirect this path
- frozen prices:
  `<snapshot>/src/opensquilla/provider/router_dynamic_model_profiles.json`
- output: `plan.paths.report_root`

For a nonterminal diagnostic snapshot, explicitly add `--allow-nonterminal`.
It is always labelled nonterminal and never presented as a complete result.
`--strict` returns exit code 2 if the generated report is partial/failed; the
reports are still written so terminal failures retain evidence.
Top-level completion additionally requires `comparison_evidence_valid=true`
for every active experiment in `plan.experiments`; a missing or invalid active
comparison can therefore never produce a strict-success report.

## Outputs

- `<report_root>/<experiment directory>/EXPERIMENT_RESULTS.md` for every new
  live group.
- A no-live `P0-5-07/EXPERIMENT_RESULTS.md` bound to its formal no-op receipt.
- Source-index reports for excluded P0-01, P0-02, P0.5-31 and stopped P0-15,
  with the prior report path and expected SHA-256; they are not represented as
  new executions.
- `<report_root>/EXPERIMENT_RESULTS.md`, the cross-group summary.
- `<report_root>/EXPERIMENT_RESULTS.json`, the compact machine-readable
  evidence, per-task metrics, comparisons, receipts and completion state.

Each normal group Markdown includes the established table columns:

`Arm, Rows, Done, AvgQ, AvgPass, JudgeErr, Avg Gen$, Total Gen$, Gen exact,
Avg Input, Avg Output, Avg Reason, Avg Cache, Avg Visible, Avg Tokens,
Avg Tools, Tool%, Avg Steps, Avg LLMReq, p50 ms, p95 ms`.

It also includes separate selected-generation/Judge cost evidence, separate
execution/policy/audit/account status, task-level quality deltas, routing
changes, retry/fallback/degraded counters and evidence paths.

## Comparison controls and schedule

Controls are resolved from the frozen top-level `comparison_controls`
contract, never from a reporter hard-code. The live Analyzer source is
`common-E0-source`; `common-E0-R1/R2/R3` are separate frozen-replay controls.
Live Analyzer candidates compare only with the live source, all other
candidates compare with their declared replay control, and the reporter
rejects every pairing whose `analyzer_mode` differs.

The controller runs `execution.schedule.mode=anchored_serial` with
`strict_task_interleaving=false`. The reporter authenticates the schedule SHA
recorded by terminal status and every arm's 1-based `schedule_ordinal` and
`anchor_arm_id`. It reports replay-E0 drift and candidate start lag from its
anchor. This is intentionally described as whole-arm serial anchoring, not as
per-task AB/BA interleaving; provider/time drift can remain in paired deltas.

The source plan requires per-task E0/candidate interleaving before the
cost-reduction-chain C3 arm may advance. Consequently `P0-20-E3` is always
published as `mini_diagnostic_only` and is not C3 promotion evidence in this
campaign. It is scheduled after the R1 anchor in the nearby serial tranche to
reduce drift, but that proximity is not task interleaving.

## Pairing and uncertainty

- Pair exclusively by exact `task_id`; missing tasks are disclosed and never
  included in a fake complete mean.
- Use fixed seed `20260803` and 20,000 paired bootstrap resamples.
- Report W/T/L with a numerical tie tolerance of `1e-12`.
- For P0.5-11 and P0.5-36, pair E1-R1/R2/R3 with
  common-E0-R1/R2/R3 respectively. The repeat summary first averages the three
  deltas within each task and then bootstraps the ten task-level means. It does
  not pretend the 30 correlated observations are 30 independent tasks.
- P0.5-11 does not configure or send a model sampling seed on the current
  production path. Its repeats are stochastic diagnostics and are explicitly
  not exact replays.
- P0.5-36 freezes candidate-order seeds `0`, `1`, and `4`. For each trace call,
  configured and effective seeds must match only when candidate shuffling
  produced a non-empty display order and an Aggregator physical request
  actually started. A pre-aggregation failure (for example, no proposer
  quorum) is `not_applicable`, not an execution failure. Applicable mismatches
  invalidate comparison evidence; valid task slices remain reportable with
  the limitation disclosed. One or more pre-aggregation `not_applicable` tasks
  do not invalidate an otherwise non-empty valid slice, but an arm with no
  valid aggregation slice makes the active experiment comparison evidence
  invalid and the top-level report partial/failed.

## Cost semantics

Selected-generation cost is scoped to `cost_accounting.selected_generation_attempt`
and its sealed `usage.model_usage_breakdown` only. This preserves the finalizer
binding to the final successful selected attempt and excludes
`actual_generation_spend`, so failed or replaced outer retries cannot leak
into the comparison.

For every selected physical usage unit:

1. Use provider actual/billed/reported USD only when backed by a confirmed
   billing receipt, an explicit `provider_billed`/`openrouter_usage` source, or
   a legacy positive amount. Prefer nested `provider_reported_cost` so an outer
   placeholder zero cannot hide a positive provider amount; a nested explicit
   provider source also overrides an outer missing-source marker. Legacy
   positive amounts are accepted only with an empty/`none`/`unavailable`
   source. `opensquilla_*`, `mixed`, and other non-actual sources always use
   the estimate path even when their compatibility `billed_cost` is positive.
   A zero paired with `none`/`unavailable`/`unknown` is missing cost evidence,
   not actual `$0`.
2. Otherwise require a frozen-registry price plus token usage and calculate:

   ```text
   (fresh_input * input_rate
    + cache_read * cache_read_rate
    + cache_write * cache_write_rate
    + output * output_rate) / 1_000_000
   ```

3. Clamp cache-read/write tokens within total input. The current frozen 79-model
   registry publishes only input/output rates; in that case the missing cache
   rate is conservatively replaced by the normal input rate and counted as
   `estimated_cache_price_fallback`, never mislabeled provider actual.
4. A BYOK zero-dollar OpenRouter receipt is not treated as underlying provider
   cost; it follows the token-estimation path.
5. If both money and tokens are missing, ignore the dollar amount, increment
   `ignored_requests`, and label arm totals/averages as lower bounds rather than
   `$0`.

Judge cost is independently taken from `cost_accounting.judge` or its physical
Judge attempt units. Campaign account delta is independently taken from the
reconciliation/proof window and includes Judge. These three quantities are
never summed together.

## Evidence gates

For each succeeded live arm the generator verifies:

- the controller at
  `<snapshot>/scripts/experiments/run_draco_p0_p05_tuning_campaign.py` has the
  exact `plan.freeze.sources.controller_raw_sha256`, then imports that frozen
  file and runs its `validate_plan`, `validate_snapshot`, and
  `validate_runtime_freeze` gates;
- the controller-authenticated derived plan/Analyzer artifact, followed by an
  independent per-arm `resolve_arm_override`, `arm_completion_identity`, and
  `inspect_complete_arm` reinspection;
- exact equality between the reinspection evidence and the immutable terminal
  status completion evidence, so a donor directory, relabelled arm, or legacy
  single-runner identity cannot be accepted;
- formal `manifest.json`, `results.jsonl`, `audit.json` and non-BYOK proof;
- self-hashes and manifest size/SHA bindings;
- ten unique frozen task IDs, G1 only, result-evidence hashes;
- manifest status/result/task/execution contract;
- source-manifest task concurrency exactly 6;
- manifest/audit/proof hash bindings.
- the frozen anchored-serial schedule, status schedule SHA, 1-based ordinal,
  anchor binding, and same-Analyzer-mode comparison contract.

Audit or policy warnings remain warnings and do not erase an execution-success
answer. Artifact corruption, missing tasks, wrong concurrency or a missing
formal result make the reporting state partial/failed.

The generator validates `derived-plan.json`, the frozen Analyzer file binding,
P0.5-06/P0.5-07 receipts, and P0.5-10/38/39 wire-effect receipts. Deleted wire
no-op arms require a valid receipt.

All reports explicitly state that DRACO mini has no independent SafetyGate,
contains only ten diagnostic tasks, and cannot automatically promote a winner.

## Tests

The end-to-end test constructs five complete ten-row G1 arm roots (one live
source, three replay controls and one candidate), a
hash-frozen controller fixture using the current `runner_identities` mapping,
a frozen 79-model price registry, campaign plan/terminal-status/derived
artifacts, self-hashed result rows, manifests/audit/proof files and account
windows. It then generates and inspects group Markdown, root Markdown and root
JSON. Negative tests cover controller drift, donor/relabelled status evidence,
the legacy single-runner identity schema, and terminal failure without a
derived plan.

```bash
UV_CACHE_DIR=/private/tmp/p0-p05-uv-cache \
PYTHONPYCACHEPREFIX=/private/tmp/p0-p05-report-generator-pycache \
  uv run --python 3.12 python -m unittest -v \
  tests.test_scripts.test_generate_draco_p0_p05_reports
```

The focused tests prove actual-cost precedence, cache-aware estimation,
missing-cache-rate disclosure, no-money/no-token ignore behavior, exclusion of
whole-generation retry spend, exact task pairing, deterministic 20k bootstrap,
W/T/L and R1-R3 task-level aggregation. They also cover dynamic controls,
same-mode pairing, schedule/status tampering, replicate seed expansion, and
the P0.5-36 aggregation-applicability seed gate. They also prove that a valid
slice may coexist with pre-aggregation `not_applicable` tasks, while an empty
slice or any other invalid active comparison forces strict exit code 2.
