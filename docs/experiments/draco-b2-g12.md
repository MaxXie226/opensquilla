# DRACO B2 Quality-First Profile (G12-derived)

`B2` loads [`configs/benchmarks/draco_b2_g12.json`](../../configs/benchmarks/draco_b2_g12.json)
by default. Its model lineup and per-member generation settings are derived from the scored
OpenSquilla `G12` run at source commit `153e5ff267950b0e285efcdb180cea8724c0471d`,
but its Agent policy is now intentionally quality-first. It must not be described as an exact
execution reproduction of historical G12.

The `reference` block is retained as lineage metadata. The effective config artifact and its
SHA-256 identify this experiment variant. New runs use profile ID
`opensquilla_b2_quality_first_v1`; G12 remains only the historical reference.

## Effective profile

| Setting | Current B2 quality-first | Historical G12 reference |
| --- | --- | --- |
| Proposers | DeepSeek V4 Pro, GLM 5.2, Kimi K2.7 Code, Qwen 3.7 Max | same lineup |
| Aggregator | GLM 5.2 | GLM 5.2 |
| Thinking | `xhigh`, `xhigh`, `max`, `xhigh`; aggregator `xhigh` | same requested levels |
| Completion cap | 16,384 tokens per member | 16,384 tokens per member |
| Temperature | 0.0 per member | 0.0 per member |
| Tool permissions | proposers disabled; aggregator enabled | same |
| Local tools | Brave `web_search` plus `web_fetch` | same tool names/provider |
| Proposer completion rule | wait for all four; require 3 successes | wait for all four; 1 success was enough |
| Timeouts | task 10,800s; proposer 907.5s; aggregator 2662.5s; margin 30s | task 3,600s; same member timeouts/margin |
| Runner | Agent loop, 20 work iterations plus finalization; base concurrency 2, formal launcher 5 | Agent loop, 12 iterations, concurrency 2 |
| Retrieval-only cutoff | disabled (`0`) | no equivalent cutoff |
| Endgame policy | retain thinking; no-tool finalization after 20 work rounds or upon entering the last 300s | legacy runtime behavior |
| Judge | Gemini 3.1 Pro Preview, 3 repeats, concurrency 6, 3 attempts | same requested Judge alias/settings |
| Generation retries | 3 attempts, 2s initial backoff | same |

The proposers do not execute research tools in the reference experiment. The aggregator
receives `web_search` and `web_fetch`; a tool request is surfaced to the outer Agent loop,
which executes it and calls the ensemble again with the result.

The quality-first policy removes the three-consecutive-retrieval forced stop, preserves
thinking during final synthesis, allows 20 complete work iterations before a separate
finalization attempt, and extends the task wall-clock budget to three hours. The final
attempt is no-tool; entering the final five-minute deadline window triggers the same no-tool
wrap-up early as a last-resort guarantee that a task returns an answer.

The profile is run-wide: every group in a combined campaign inherits its Runner, tool, timeout,
generation, and Judge envelope. The static four-proposer-plus-aggregator mapping remains B2-only.
G1-only and joint B2/G1 runs therefore keep the same envelope, preserving comparability.

## Historical alignment lineage

The July 15 B2 result was not execution-equivalent to G12 even though its model names matched:

| Setting | Earlier B2 | Reference G12 / aligned B2 |
| --- | --- | --- |
| Single-model routing before ensemble | enabled | skipped |
| Successful proposers required | 3 | 1 |
| Proposer completion | 3 successes plus 30s grace | wait for all 4 |
| Proposer timeout | 300s | 907.5s |
| Aggregator timeout | 480s | 2662.5s |
| Kimi native thinking request | degraded from `max` | preserved as `max` |

These are behavioral differences, not reporting-only changes. In particular, the earlier B2
trace contains three-candidate aggregation calls when a slower proposer did not finish inside
the quorum window; G12's implementation used `asyncio.gather` and waited for every proposer.

## Overrides

Resolution order is deterministic:

1. Base JSON (`--experiment-config`, or the bundled file)
2. Each repeated `--experiment-config-override` JSON, in command-line order
3. Each repeated `--experiment-config-set dotted.path=JSON_VALUE`, in command-line order

Examples:

```bash
--experiment-config-override configs/benchmarks/my-b2-overlay.json
--experiment-config-set runner.concurrency=4
--experiment-config-set ensemble.proposers.2.max_tokens=8192
```

`OPENSQUILLA_DRACO_EXPERIMENT_CONFIG` can supply the base JSON path. Unknown JSON fields,
unknown dotted paths, inconsistent timeout budgets, and accidental thinking downgrades fail
before any model request. To deliberately test lower thinking, first set
`generation.require_highest_thinking=false` and then override the member setting.

For B2 model calls, `ensemble.proposers[*]` and `ensemble.aggregator` are the authoritative
member settings: their `max_tokens` and `thinking` values override the shared generation
defaults, and a non-null member `temperature` does the same. The shared
`generation.thinking_budget_tokens` and retry settings still apply to every member. Change a
specific B2 model through its `ensemble` path; the effective artifact and routing trace record
the resolved per-member values.

## Run artifacts

Every B2 output directory contains:

- `*.experiment-config.base.json`: parsed base input
- `*.experiment-config.override-NN.json`: every file overlay, when supplied
- `*.experiment-config.inline-overrides.json`: inline overrides, when supplied
- `*.experiment-config.effective.json`: fully merged and validated runtime configuration
- `*.experiment-config.resolution.json`: source paths, SHA-256 hashes, precedence, and input check

The manifest references all of these files and includes the requested CLI values, effective
values, reference source commit, and DRACO mini input hash verification.

## Reproduction boundary

The current profile locks the benchmark input, model lineup, generation settings, tool policy,
quorum, quality-first Agent policy, retry, and Judge settings. Credentials remain external and
are never written to the JSON artifacts. Sandbox posture and provider transport come from the
selected OpenSquilla TOML config.

This profile is suitable for a new quality-first B2/G1 campaign, not a direct score reproduction
of historical G12. OpenRouter aliases can resolve to a different dated backend snapshot,
provider-side serving behavior can change, and running a different mix of experiment groups
changes concurrent provider load. The manifest and request trace preserve the resolved model
names returned by the provider so that drift is visible after a run.
