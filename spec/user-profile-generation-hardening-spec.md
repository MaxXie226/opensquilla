# Single-User Offline Profile Generation Hardening Spec

Date: 2026-08-04
Status: Draft
Target branch: `feature/user-profile-generation`
Related PR: https://github.com/opensquilla/opensquilla/pull/928
Design reference: https://github.com/opensquilla/agentic-routing-docs/blob/main/Agentic-Routing-Step2-Offline-Plan.md

## 1. Summary

OpenSquilla has an opt-in offline producer that reads recent conversation
transcripts after a successful Dream run, asks the Dream-selected LLM to infer
stable user preferences, writes an immutable profile version, and atomically
updates an `active` pointer.

This spec hardens that producer without changing its product model:

- one OpenSquilla home represents one local human owner;
- each configured agent owns one generated profile;
- generation remains disabled by default;
- generation remains a post-Dream hook;
- the current 90-day input window and existing batch count behavior remain;
- produced profiles are still not consumed by ranking or routing in this scope.

The work addresses correctness, bounded per-session resource use, durable usage
accounting, storage serialization, dependency isolation, stream cleanup, and
concurrent publication safety raised during review of PR #928.

## 2. Decisions

### 2.1 Product identity

The producer creates exactly one profile per `agent_id` under one local
OpenSquilla home. It does not introduce a separate human-user identifier,
`profile_subject`, profile selector, or multiple active pointers.

The supported deployment contract is:

> One OpenSquilla state home belongs to one local human owner. Shared agents,
> shared state homes, and server-side multi-user profile generation are not
> supported by this version.

This is an explicit product limitation rather than an inferred security
boundary. A future multi-user design must add an authenticated subject identity
and namespace both session selection and profile storage by that subject.

### 2.2 Trigger behavior

This spec does not make `squilla_router.user_profile.enabled` enable Dream,
create Dream cron jobs, or reconcile Dream scheduling. Generation continues to
run only when an already-enabled Dream completes and invokes the post-Dream
hook.

### 2.3 Cost and total run volume

This spec does not add a maximum number of sessions per run, sampling policy,
maximum number of LLM batches, or monetary budget. Existing session selection
and batching semantics remain. Provider calls must, however, be recorded in the
durable usage ledger.

### 2.4 Publication commit point

The atomic replacement of the `active` pointer is the publication commit point.
Version files are immutable and must exist before `active` changes. Run state is
bookkeeping and may be repaired from `active`; it is not the source of truth for
which profile is live.

### 2.5 Review finding disposition

| Review finding | Disposition | Spec section |
| --- | --- | --- |
| Enabling profile generation does not start Dream | Deferred by product decision | 2.2, 4, 22 |
| Provider calls bypass durable usage accounting | Implement | 11 |
| Total sessions, calls, and cost are unbounded | Deferred by product decision | 2.3, 4, 22 |
| Compacted sessions use an incomplete transcript | Implement | 7.2, 7.3 |
| Structurally incomplete JSON is accepted | Implement | 10 |
| Raw text length undercounts serialized CJK input | Implement | 9.2 |
| Session-list reads bypass storage serialization | Implement | 7.1 |
| Profile storage imports optional self-learning dependencies | Implement | 13 |
| Minimum volume is not reapplied after transcript loading | Implement | 8 |
| Non-finite confidence can reach profile JSON | Implement | 10.5 |
| `agent_id` is not a multi-user privacy boundary | Accepted single-user limitation | 2.1, 7.4, 22 |
| Stream cleanup is outside the batch timeout | Implement | 12 |
| Full transcripts are materialized before truncation | Implement | 7.3, 9.1 |
| Concurrent publishers can collide | Implement | 15 |
| Environment disable is checked after IO | Implement | 6.2, 8 |

## 3. Goals

- Read the complete canonical history of compacted sessions.
- Keep per-session database reads and Python allocations bounded before prompt
  truncation.
- Require a structurally valid analyst response before a batch can contribute.
- Prevent non-finite confidence values from reaching a profile JSON file.
- Enforce the input budget against the final serialized request, including JSON
  escaping and the fixed system prompt.
- Reapply the minimum-session gate to transcripts actually supplied to the LLM.
- Serialize the new session-list read against multi-statement writes.
- Account every physical profile-generation provider call in the durable usage
  ledger.
- Ensure the advertised batch timeout also bounds stream cleanup.
- Publish versions, the active pointer, and run state under a per-agent
  cross-process publication lock.
- Remove the user-profile package's import-time dependency on self-learning
  extras such as NumPy.
- Make the environment kill switch return before state, session, provider, or
  profile IO.
- Preserve fail-open behavior for Dream and normal interactive turns.

## 4. Non-Goals

- Do not automatically enable or schedule Dream.
- Do not add a session-count cap, batch-count cap, sampling strategy, or spend
  budget.
- Do not add multi-user identity, `profile_subject`, per-human directories, or
  multiple profiles per agent.
- Do not add profile consumption to ranking, router scoring, model selection,
  ensemble behavior, DRACO, or preference projection.
- Do not change the generated profile field vocabulary or aggregation
  algorithm, except to reject invalid analyst responses.
- Do not add a new third-party dependency.
- Do not migrate or rewrite existing profile version files.
- Do not delete historical profile versions.
- Do not make profile-generation failure fail a Dream run or an interactive
  turn.

## 5. Existing Flow

The current producer follows this path:

1. The gateway checks `squilla_router.user_profile.enabled`.
2. A successful Dream invokes the post-Dream hook.
3. The producer loads per-agent run state.
4. It lists sessions updated during the previous 90 days.
5. It evaluates environment, activity, cooldown, and minimum-session gates.
6. It resolves the Dream provider.
7. It loads and renders each transcript.
8. It groups rendered sessions into batches of ten.
9. It directly invokes `provider.chat` for each batch.
10. It parses and aggregates successful batch analyses.
11. It allocates a same-day version, writes the JSON file, replaces `active`,
    and writes run state.

The hardening work changes steps 3, 4, 7, 8, 9, 10, and 11 while preserving
the feature switch, post-Dream integration point, profile schema, and
aggregation vocabulary.

## 6. Configuration and Kill Switches

### 6.1 Persistent feature switch

The existing configuration remains:

```toml
[squilla_router.user_profile]
enabled = false
```

`false` remains the default. When false, the gateway must return before session
storage access, provider resolution, LLM calls, or profile writes.

### 6.2 Environment kill switch

The existing emergency override remains:

```text
OPENSQUILLA_USER_PROFILE_DISABLED
```

Truthy values are `1`, `true`, `yes`, and `on`, case-insensitive.

The environment override must be checked at two boundaries:

1. In the gateway post-Dream adapter, before obtaining session storage or
   resolving profile-generation dependencies.
2. At the beginning of `maybe_produce_user_profile()`, before loading run state
   or querying sessions.

The orchestrator check is authoritative and protects non-gateway callers. A
disabled result is:

```python
ProfileRunResult(ran=False, reason="disabled")
```

The disabled path must not create or modify `.profile_state.json`.

## 7. Session Selection and Read Consistency

### 7.1 Session-window query

`SessionStorage.list_session_ids_updated_since()` remains the focused 90-day
session selector. Its SQL, ordering, optional `agent_id`, and optional `limit`
contract remain unchanged.

The method must be decorated with `_serialized_read` so it cannot observe
temporary rows in the middle of a multi-statement transaction on the shared
`aiosqlite` connection.

### 7.2 Canonical transcript source

The producer must not use `get_transcript()` because that method intentionally
returns only the active replay tail after compaction.

Profile generation must read the canonical view that combines:

- archived rows from `compacted_transcript_entries`; and
- the active tail from `transcript_entries`.

The canonical ordering is ascending by `(created_at, entry_id)`.

### 7.3 Bounded canonical profile window

Calling the unbounded `get_canonical_transcript()` is also insufficient because
it materializes the complete session before the producer truncates it.

Add a storage API dedicated to a bounded canonical text window:

```python
async def get_canonical_transcript_window(
    session_id: str,
    *,
    head_rows: int,
    tail_rows: int,
    per_entry_max_chars: int,
) -> list[ProfileTranscriptRow]:
    ...
```

`ProfileTranscriptRow` contains only the fields needed by the producer:

```python
@dataclass(frozen=True)
class ProfileTranscriptRow:
    role: str
    content: str | None
```

Required behavior:

- read archived and active rows in one SQLite statement and one read snapshot;
- return at most `head_rows + tail_rows` rows;
- deduplicate overlap when the transcript has fewer rows than the requested
  window;
- preserve canonical ascending order;
- select only `role` and a bounded `content` projection;
- truncate an individual content value in SQL using a head/tail projection so
  one very large message is not fully copied into Python;
- use the same truncation marker as the renderer;
- apply `_serialized_read` to the public method.

Initial internal constants:

```text
PROFILE_TRANSCRIPT_HEAD_ROWS = 64
PROFILE_TRANSCRIPT_TAIL_ROWS = 64
PROFILE_TRANSCRIPT_ENTRY_MAX_CHARS = 6000
```

These are implementation safety limits, not user-facing cost controls. The
final rendered session remains limited by `PER_SESSION_MAX_CHARS = 6000`.

### 7.4 Single-user limitation

Session selection remains keyed by `agent_id`; no human subject filter is added
in this spec. Documentation must state that shared agents, shared profile homes,
and multi-user server deployments are unsupported for profile generation.

## 8. Gates and Attempt Ordering

The gate sequence must be:

1. persistent `enabled` check in the gateway;
2. environment kill-switch check in the gateway;
3. environment kill-switch check in the orchestrator;
4. load run state;
5. list recent session rows;
6. evaluate raw session count, idle time, and cooldown;
7. load and render bounded canonical transcript windows;
8. reapply the minimum-session requirement to readable rendered sessions;
9. record the production attempt;
10. resolve the provider;
11. invoke the analyst.

The post-read gate is:

```python
len(rendered_sessions) >= MIN_SESSIONS
```

If it fails, return:

```python
ProfileRunResult(
    ran=False,
    reason="insufficient_readable_sessions",
    sessions_read=len(rendered_sessions),
)
```

No attempt timestamp, provider resolution, LLM call, profile version, active
pointer change, or failure-counter increment occurs on this path.

## 9. Transcript Rendering and Request Budget

### 9.1 Rendering

`render_transcript()` continues to:

- include only non-empty rows with a non-empty role;
- render each row as `<role>: <content>`;
- retain a head and tail separated by `[transcript truncated]`;
- return a `SessionTranscript` containing only `session_id` and rendered text.

Because storage already returns a bounded row and content window, the renderer
must not build an unbounded list of strings. It should append within a bounded
head/tail buffer and never construct the complete pre-truncation transcript.

### 9.2 Serialized request budget

The batching budget applies to the final request text, not raw transcript
characters.

For every candidate batch, compute:

```python
user_prompt = build_batch_prompt(candidate_batch)
request_chars = len(SYSTEM_PROMPT) + len(user_prompt)
```

The candidate fits only when:

```python
request_chars <= BATCH_INPUT_MAX_CHARS
```

This calculation includes:

- `ensure_ascii=True` expansion;
- JSON field names and punctuation;
- session IDs;
- allowed enum lists;
- the fixed system prompt.

Batch construction remains deterministic and preserves session order. A single
session whose final serialized request exceeds the budget is dropped instead
of being split or sent partially. Existing `BATCH_SIZE = 10` remains.

## 10. Analyst Response Contract

### 10.1 Required JSON shape

The analyst must return one JSON object with these required top-level keys:

```json
{
  "session_labels": [],
  "quality_latency_tradeoff": {},
  "cost_sensitivity": {},
  "model_mentions": []
}
```

Required container types:

| Field | Required type |
| --- | --- |
| `session_labels` | array |
| `quality_latency_tradeoff` | object |
| `cost_sensitivity` | object |
| `model_mentions` | array |

Missing keys, wrong container types, a non-object root, or unrelated JSON make
the complete batch fail. Unknown extra top-level keys may be ignored for
forward compatibility.

### 10.2 Session labels

Every sent session must have exactly one label. Requirements:

- `session_id` is a string from the sent batch;
- every sent session ID occurs exactly once;
- `capability` is one of the six supported axes or `unknown`;
- duplicate, missing, foreign, or structurally invalid labels fail the batch.

### 10.3 Tradeoff and cost values

`quality_latency_tradeoff.value` must be one of:

```text
quality_first | balanced | latency_first | unknown
```

Its `session_ids` must be an array containing only IDs from the sent batch. A
real value without evidence session IDs contributes no vote. `unknown` may use
an empty evidence list.

`cost_sensitivity.value` must be one of:

```text
high | medium | low | unknown
```

### 10.4 Model mentions

Each usable model mention requires:

- a non-empty `model_id`;
- `direction` equal to `praise` or `blame`;
- at least one evidence session from the sent batch;
- a valid confidence.

Malformed model-mention items may be dropped without failing an otherwise valid
batch because model mentions are optional evidence. The root
`model_mentions` field must still be an array.

### 10.5 Confidence handling

The JSON decoder must reject non-standard bare constants `NaN`, `Infinity`, and
`-Infinity`.

For values passed to confidence coercion:

- missing, non-numeric, string `NaN`, and non-finite values become `0.0`;
- finite values below `0.0` become `0.0`;
- finite values above `1.0` become `1.0`;
- finite values in range are preserved.

No non-finite float may enter `BatchAnalysis`, the builder, or a profile JSON
file.

### 10.6 Batch success

A batch is successful only after the root contract, required containers, and
complete session-label coverage pass validation. Syntactically valid but
structurally incomplete JSON such as `{}` is a failed batch.

If every batch fails, the producer must leave the existing active profile
unchanged and return `all_batches_failed`.

## 11. Provider Usage Accounting

### 11.1 Dedicated accounting scope

Profile generation must use a dedicated `UsageAccountingScope` with:

```text
run_kind = user_profile_generation
agent_id = current agent
execution_id = unique per producer run
agent_run_id = execution_id
turn_id = execution_id
session_id = stable system-session identity for the agent
```

The gateway already owns `usage_event_sink`; it must create and bind the scope
around the orchestrator call. A missing sink preserves existing no-ledger
behavior for tests or non-gateway embeddings.

### 11.2 Physical provider call wrapper

The gateway stream adapter must follow the same physical-accounting rules used
by Dream:

- if the provider already accounts physical usage, call its stream directly;
- otherwise wrap `provider.chat(...)` with `account_provider_stream(...)`;
- obtain provider and model names from `provider_metadata(provider)`;
- keep the usage scope active through stream completion and close.

Required ledger outcomes:

- commit `start` before sending the provider request;
- finalize from a terminal usage-bearing event;
- mark usage unknown on provider exception, early stream end, timeout, or
  cancellation;
- if ledger start is unavailable, do not send the provider request.

This work records existing calls. It does not add a spend budget or reduce the
number of calls.

## 12. Stream Deadline and Cleanup

The per-batch timeout must cover both event consumption and stream cleanup.

Required structure:

```python
async with asyncio.timeout(timeout):
    try:
        async for event in stream:
            ...
    finally:
        await close_stream_within_current_deadline(stream)
```

The exact helper may differ, but these invariants hold:

- a `DoneEvent` does not end the deadline before `aclose()`;
- a hanging `aclose()` cannot extend the batch indefinitely;
- timeout, cleanup failure, provider error, and missing `DoneEvent` return
  `BatchAnalysis.failed(...)`;
- cancellation still propagates far enough for usage accounting to mark the
  call unknown;
- one failed batch does not abort subsequent batches.

## 13. Dependency-Free Router Data Paths

The user-profile store must not import through
`opensquilla.squilla_router.self_learning`, because importing that package
eagerly loads optional training dependencies.

Create a provider- and NumPy-free module:

```text
src/opensquilla/squilla_router/data_paths.py
```

Move these helpers into it:

```text
_safe_agent_id
router_data_root
agent_data_dir
```

Then:

- `user_profile.store` imports `agent_data_dir` from `data_paths`;
- `self_learning.store` imports and re-exports the same helpers for backward
  compatibility;
- `self_learning.__init__` public imports remain compatible;
- no new dependency is added.

A core installation without NumPy must be able to import and run the
user-profile package.

## 14. Profile Schema

The produced JSON schema remains unchanged.

Example:

```json
{
  "profile_version": "2026-08-04.1",
  "permission": {
    "allow_models": [],
    "deny_models": [],
    "allow_tools": ["read_file", "search"],
    "risk_allowlist": ["low", "medium", "high"]
  },
  "preference": {
    "quality_latency_tradeoff": "quality_first",
    "cost_sensitivity": "medium"
  },
  "history": {
    "positive_model_ids": ["example/model-a"],
    "negative_model_ids": [],
    "feedback_count": 24,
    "last_updated_at": "2026-08-04",
    "capability_prior": {
      "code_generation": 0.5,
      "reasoning": 0.3,
      "writing": 0.2
    }
  },
  "_meta": {
    "window_days": 90,
    "batches": 3,
    "fields": {
      "preference.quality_latency_tradeoff": {
        "confidence": 0.82,
        "vote": "2/3",
        "evidence": ["session:example-session-1"]
      }
    }
  }
}
```

Rules:

- `profile_version` is `<YYYY-MM-DD>.<positive sequence>`;
- inferred no-signal values may be `null`, allowing consumers to retain their
  baseline defaults;
- `_meta` contains bounded provenance only and never transcript text;
- `feedback_count` is the number of sessions in successful batches;
- permission fields are a generation-time snapshot and do not replace live
  runtime permission enforcement.

## 15. Storage Layout and Publication

### 15.1 Layout

The existing layout remains:

```text
<opensquilla-home>/router/data/<agent_id>/profiles/
    user_profile.<YYYY-MM-DD>.<N>.json
    active
    .profile_state.json
```

There is one `active` pointer per agent. No human-user namespace is added.

### 15.2 Publication lock

Introduce one synchronous store operation executed outside the event-loop
thread:

```python
def publish_profile(
    *,
    day: str,
    agent_id: str,
    home: Path | None,
    build_payload: Callable[[str], dict[str, Any]],
    next_state: Callable[[str], ProfileRunState],
) -> PublishedProfile:
    ...
```

The orchestrator calls it through `asyncio.to_thread()` or the repository's
equivalent thread helper.

Inside `publish_profile()`:

1. acquire the existing cross-process profile lock keyed by the agent's
   `profiles_dir`;
2. compute the next version while holding the lock;
3. build the payload with that version;
4. create the immutable version file with exclusive-create semantics;
5. prepare unique same-directory temporary files for `active` and state;
6. atomically replace `active`; this is the commit point;
7. atomically replace `.profile_state.json`;
8. release the lock.

The implementation should reuse the repository's cross-platform
`acquire_profile_locks` / `ProfileOperationLock` infrastructure rather than
introducing another lock implementation.

`publish_profile()` is the only production entry point allowed to allocate a
profile version and replace `active`. The orchestrator must not continue to
compose `next_version()`, `write_profile_version()`, and
`write_active_atomic()` as a second split publication path.

### 15.3 Temporary files

Do not use the shared name `active.tmp`. Temporary files must:

- have unique names in the destination directory;
- be opened with exclusive-create semantics;
- be flushed before replacement;
- be removed best-effort after failure;
- never be referenced by `active`.

### 15.4 Failure semantics

- Failure before the active-pointer replacement leaves the old profile active.
- An orphan immutable version file is acceptable and may be retained for
  diagnosis.
- Failure after the active-pointer replacement must not roll back to an older
  pointer without an explicit recovery decision.
- State-write failure after commit is logged; `active` remains authoritative.
- A concurrent writer either waits within a bounded publication-lock timeout or
  returns `publication_busy`; it must not publish without the lock.
- `load_active_profile()` continues to fail open to `None` for a missing,
  dangling, malformed, or escaping pointer.

## 16. Run-State Semantics

Run state retains:

```text
last_attempt_ts
last_run_ts
last_version
consecutive_failures
```

Rules:

- environment-disabled and persistent-disabled paths do not read or write
  state;
- insufficient raw or readable sessions do not record an attempt;
- a provider-resolving production attempt records `last_attempt_ts` before the
  first provider call;
- all-batch failure increments `consecutive_failures`;
- successful publication sets `last_run_ts`, `last_version`, and resets
  `consecutive_failures`;
- state writes use unique temporary files plus `os.replace()`;
- active profile resolution never depends solely on `last_version`.

## 17. Error and Observability Contract

The producer remains fail-open to Dream. `maybe_produce_user_profile()` never
raises into the post-Dream hook.

Stable result reasons include:

```text
disabled
no_sessions
agent_active
cooldown
insufficient_sessions
insufficient_readable_sessions
no_provider
all_batches_failed
publication_busy
error
ready
```

Structured logs must not include transcript text or raw analyst responses.

Required events:

```text
user_profile.gated
user_profile.insufficient_readable_sessions
user_profile.no_provider
user_profile.batch_failed
user_profile.all_batches_failed
user_profile.publication_busy
user_profile.produced
user_profile.produce_error
```

Useful fields are limited to agent ID, reason, counts, version, elapsed time,
and stable error category. Provider credentials, prompts, transcripts, channel
identifiers, and raw session metadata must not be logged.

## 18. Implementation Surface

Expected production files:

| File | Change |
| --- | --- |
| `src/opensquilla/gateway/boot.py` | early environment guard, dedicated usage scope, accounted stream adapter |
| `src/opensquilla/session/storage.py` | serialized session-list read and bounded canonical profile window |
| `src/opensquilla/squilla_router/data_paths.py` | new dependency-free shared path helpers |
| `src/opensquilla/squilla_router/self_learning/store.py` | import and re-export shared path helpers |
| `src/opensquilla/squilla_router/user_profile/extractor.py` | bounded rendering, serialized request sizing, bounded stream cleanup |
| `src/opensquilla/squilla_router/user_profile/gates.py` | stable readable-session reason and environment helper reuse |
| `src/opensquilla/squilla_router/user_profile/orchestrator.py` | early kill switch, canonical window, post-read gate, accounted production flow, publication call |
| `src/opensquilla/squilla_router/user_profile/prompts.py` | required response schema and finite confidence handling |
| `src/opensquilla/squilla_router/user_profile/state.py` | atomic state-file preparation or store-integrated state commit |
| `src/opensquilla/squilla_router/user_profile/store.py` | per-agent publication transaction, unique temp files, lock reuse |

No ranking, dynamic-router, ensemble, DRACO, or profile-consumption files are in
scope.

## 19. Test Plan

### 19.1 Configuration and kill switch

- generation remains disabled by default;
- persistent `enabled=false` returns before storage and provider access;
- environment disable returns before state and session access;
- environment disable creates no profile or state files;
- environment truthy parsing remains case-insensitive.

### 19.2 Session storage

- `list_session_ids_updated_since()` waits behind the operation lock;
- it cannot observe rows that are later rolled back;
- the bounded canonical window combines archived and active rows;
- the window preserves canonical order;
- head/tail overlap is deduplicated;
- returned row and content sizes are bounded;
- a compacted session includes evidence from its archived prefix.

### 19.3 Gates and orchestration

- twenty selected rows with nineteen readable transcripts do not call the
  provider;
- twenty readable transcripts pass the second gate;
- unreadable-session rejection does not write an attempt timestamp;
- all failed batches leave the active pointer unchanged;
- one failed batch does not prevent later batches from running;
- successful-session accounting includes only successful batches.

### 19.4 Prompt sizing

- ASCII batches stay within the serialized request budget;
- CJK text expanded by `ensure_ascii=True` is measured after serialization;
- escape-heavy text is measured after serialization;
- fixed JSON and system-prompt overhead are counted;
- a single oversized serialized session is dropped;
- batching remains deterministic.

### 19.5 Response validation

- `{}` fails;
- unrelated JSON fails;
- each missing required top-level key fails;
- wrong required container types fail;
- missing, duplicate, and foreign session labels fail;
- a complete all-`unknown` response succeeds;
- malformed optional model mentions are dropped;
- bare and string NaN never reach `BatchAnalysis`;
- Infinity and negative Infinity never reach profile JSON;
- `json.dumps(..., allow_nan=False)` succeeds for every produced payload.

### 19.6 Usage accounting

- a provider call records ledger start before the physical request;
- a completed call records final token and cost usage;
- provider error, early stream end, timeout, and cancellation mark usage
  unknown;
- ledger-start failure prevents the provider request;
- providers that account physical usage are not double-counted;
- profile events use `run_kind=user_profile_generation`.

### 19.7 Stream cleanup

- a stream that emits done and hangs in `aclose()` returns failed within the
  batch timeout;
- a cleanup exception fails only that batch;
- a stream without `DoneEvent` fails;
- response-size overflow still closes the stream within the deadline.

### 19.8 Dependency isolation

- importing `user_profile.store` does not import self-learning alignment,
  dataset, or schema modules;
- a core-only environment without NumPy imports the user-profile package;
- existing self-learning imports continue to resolve the shared path helpers.

### 19.9 Publication concurrency

- two concurrent publishers cannot allocate the same version;
- each publisher uses a unique temporary filename;
- active always names an existing complete version file;
- failure before active replacement preserves the previous pointer;
- state failure after active replacement does not corrupt the pointer;
- lock contention returns a stable result instead of `FileNotFoundError`;
- same-process and cross-process tests cover Linux, macOS, and Windows lock
  behavior where CI supports them.

### 19.10 Regression suite

Run at minimum:

- all user-profile tests;
- gateway profile configuration and Dream post-hook tests;
- session storage and canonical transcript tests;
- usage-accounting tests;
- recovery/profile-lock tests touched by lock reuse;
- Windows test-shard contract;
- Ruff for all changed source and test files;
- targeted mypy for changed source files;
- the repository's required offline CI matrix.

## 20. Acceptance Criteria

- Default configuration performs zero profile-generation IO or provider calls.
- The environment kill switch performs zero state, session, provider, and
  profile IO.
- Compacted sessions contribute archived and active canonical content.
- A single session cannot cause unbounded transcript rows or content to be
  materialized in Python.
- Every final analyst request is within the configured serialized-character
  budget.
- Structurally incomplete analyst JSON cannot activate a new profile.
- Produced JSON contains no non-finite numbers.
- Fewer than `MIN_SESSIONS` readable transcripts cannot activate a profile.
- Every physical profile provider call is durably accounted or explicitly
  marked unknown.
- Batch timeout includes stream close.
- Core installation does not require NumPy to import profile generation.
- Concurrent publishers cannot reuse a version or corrupt `active`.
- `active` remains an atomic one-line pointer to an immutable existing profile
  filename.
- Existing profile field names, aggregation vocabulary, and version filename
  format remain compatible.
- No ranking, routing, ensemble, DRACO, preference-projection, or profile
  consumption behavior changes.

## 21. Rollout

- Keep generation disabled by default.
- Land hardening with deterministic offline tests and no credentials.
- Re-run the complete PR CI matrix.
- After CI passes, a maintainer may perform an opt-in live Dream run with a
  configured provider and inspect:
  - usage-ledger entries;
  - generated version JSON;
  - active pointer;
  - run state;
  - absence of raw transcript text in logs.
- Do not enable generation automatically during rollout.
- Do not delete or rewrite profiles produced by the pre-hardening branch.

## 22. Deferred Work

The following items are intentionally deferred and require separate approval:

- automatically enabling or reconciling Dream when profile generation is
  enabled;
- maximum session count, sampling, batch count, elapsed-time budget, or spend
  budget;
- authenticated multi-user profile subjects;
- filtering or splitting profiles by chat participant;
- user-facing profile management;
- profile consumption by ranking or routing;
- retention and garbage collection for historical profile versions.

## 23. Resolved Implementation Decisions

- Implement the bounded canonical profile window as a dedicated single-SQL,
  one-snapshot API rather than composing multiple canonical pages.
- Wait for the publication lock for a fixed, bounded interval. On timeout,
  return `publication_busy`; never publish without the lock.
- State-file preparation may remain in `state.py` or be integrated into
  `store.py`, but its unique temporary file and atomic replacement must occur
  while the publication lock is held. A state failure after the `active` commit
  point never rolls `active` back.
- Drop malformed optional `model_mentions` without failing an otherwise valid
  batch. Expose only a low-cardinality `dropped_model_mentions` count or rate;
  never record raw items, raw model IDs, raw provider JSON, or ranking signals.
