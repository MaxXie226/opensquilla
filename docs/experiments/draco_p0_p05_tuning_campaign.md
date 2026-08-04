# P0 / P0.5 DRACO-mini campaign controller

This directory contains the staging controller, frozen plan template, expanded
arm inventory, and offline regression tests. It has not launched a paid
request; live execution remains impossible until every freeze placeholder is
replaced and `validate-only` succeeds against the clean isolated snapshot.

## What is already implemented

- Exact remaining matrix from `Agentic-Routing-Step2-Tuning-Plan.md`:
  31 live experiment groups, 65 candidate live campaigns before offline
  wire-effect deletion, plus the P0.5-07 no-op receipt.
- Explicit exclusions: completed P0-01, P0-02, P0.5-31 and stopped P0-15.
- `common-E0-R1` uses live Analyzer; its ten task profiles are frozen once.
  The source manifest/audit/non-BYOK proof self-hashes, manifest artifact
  size/raw-SHA bindings, result-row evidence hashes, and trace/result bindings
  are verified before replay. A profile is accepted only from a schema-valid
  `llm_provider` Analyzer trace whose final physical attempt has known usage.
  Normalization warnings are retained verbatim.
  `common-E0-R2/R3` and every non-Analyzer candidate replay exactly that bundle
  with zero Analyzer requests.
- P0-03, P0.5-05 and P0.5-06 remain live Analyzer-variable experiments.
- P0.5-06 derives `T` from the **final successful physical Analyzer attempt**
  for each new E0 task (never retry-aggregated usage), with
  Hyndman-Fan linear type-7 p99, then freezes `floor(0.8*T)` and
  `ceil(1.1*T)` in a receipt before dependent runs.
- P0.5-07 calls production `_should_send_temperature` for both values, bound to
  the effective Analyzer provider/model and OpenRouter official base URL, and
  records config/compat/payload source hashes. It is deleted only when both
  wire temperatures are truly omitted.
- After E0, the production main runner is invoked with `--dry-run` and the
  frozen replay bundle once per unique non-Analyzer overlay (55 unique
  overlays across 60 arms). The baseline dry replay must reproduce E0's
  ordered selected P/A, recovery roster, quorum, shuffle behavior, prompt
  contract, and request-visible member configuration on all ten tasks.
- Every non-Analyzer candidate receives an offline-effect receipt. Any arm
  whose complete ten-task behavior projection is unchanged is eligible for a
  general `no_op_deleted` state; uncertainty always runs. Required statistical
  replicates are never deleted.
- P0.5-11 records, per task and selected/recovery member,
  `temperature_parameter_sent` and `wire_temperature` using the production
  official-host compatibility helper. Reports can therefore analyze only
  requests on which temperature is real.
- P0.5-36 treats `effective_shuffle_candidates` as an actual execution
  behavior change even when P/A identities are unchanged.
- P0.5-10/38/39 retain an additional production
  `_proposer_chat_config`/`_aggregator_chat_config` budget gate. Both possible
  proposer-cap explicitness modes are projected; request-budget rebinding is
  source-proven disabled for this DRACO builder. If that proof drifts, the arm
  runs conservatively rather than being deleted.
- Arms run serially; the launcher receives task concurrency `6`; each arm keeps
  its own account window, preflight, settlement, finalizer, and formal report.
- A controller lock prevents duplicate controllers. `status.json`, the frozen
  Analyzer artifact, receipts, and `derived-plan.json` are written atomically
  with semantic and raw hashes.
- Restart skips only authenticated `status=complete`, execution-passing, exactly
  10-row/10-task G1 outputs whose source manifests bind the exact arm/output,
  merged effective config, benchmark, snapshot commit, main/resume runner hash,
  task concurrency 6, Judge concurrency 6, and generation attempt limit 3.
  The first source wave must be the frozen main runner; any later source wave
  must be the frozen resume runner with a verified, non-empty, unique G1/task
  schedule.
  Audit/policy warnings remain visible but do not trigger a costly duplicate
  execution.
- A failed arm does not stop later runnable arms. Existing incomplete output is
  never overwritten or appended to.
- The benchmark input, external reference `.local-state/config.toml`, complete
  and formal 79-model registry identity sets, ranking raw/canonical identities,
  launcher, controller, reporter, main runner, resume runner, snapshot commit,
  and tree are all frozen. They are rechecked immediately before every paid arm
  so a long campaign cannot silently consume a changed shared reference.
- After all arms reach terminal states, the controller publishes an immutable,
  self-hashed `terminal-status-input.json` and invokes the separately frozen
  reporter in strict mode. Reporter output hashes are recorded in a terminal
  receipt; reporter failure downgrades mutable campaign status without changing
  the reporter's immutable input or creating a self-referential hash loop.

## Required freeze steps before `run`

1. Apply the Aggregator prompt and frozen Analyzer replay commits to the
   isolated snapshot and finish the registry/finalizer contract sync.
2. Run the relevant unit/finalizer tests and make the snapshot clean.
3. Replace every `TODO_FREEZE_*` value with the resulting commit/tree, input,
   registry/ranking, launcher, controller, reporter, main-runner, and
   resume-runner identities.
4. Run `validate-only` below in the snapshot Python environment. It checks
   production imports/signatures and all static live-Analyzer overlays without
   writing an artifact or calling a provider.
5. Copy the finalized controller and plan into the run root, freeze their raw
   and semantic SHA-256 values, then start one user systemd unit with
   `PrivateTmp=false`.

Dry validation (safe; no model call):

```bash
python controller.py validate-plan campaign-plan.template.json --allow-placeholders
python controller.py expand-plan campaign-plan.template.json --allow-placeholders
# After all freeze placeholders have been filled:
python controller.py validate-only campaign-plan.json
```

The live command must only be used after all placeholders are removed:

```bash
python controller.py run campaign-plan.json
```

## Operating limit

- Recovery of an incomplete formal output is deliberately not automatic. A
  fresh retry must use a new output directory and explicitly archive the prior
  account window; this avoids silently double-spending or losing reconciliation
  evidence.
