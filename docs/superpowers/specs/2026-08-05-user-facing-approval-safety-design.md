# User-facing approval safety design

## Problem

Approval cards currently expose implementation vocabulary such as
`sandbox_elevation`, `exec_command`, namespaces, action kinds, and raw generic
argument objects. The browser-facing projection redacts credentials, but it
still chooses internal tool identifiers as its display name, and the Web UI
renders that value verbatim.

An explicit user denial also does not end the current task. The suspended tool
returns `approval_denied`, the engine sends that result back to the model, and
the model may retry the same intent through a different tool. A denial must be
an enforced runtime boundary rather than prompt guidance.

Finally, destructive approvals do not consistently explain whether the action
can be recovered or what the configured backup policy will do.

## Goals

- Show approvals in user language without exposing internal tool, action,
  namespace, reviewer, fingerprint, or policy identifiers.
- Identify the concrete operation and target so the user can make an informed
  decision.
- End the current task immediately after an explicit human denial while keeping
  the chat session available for a later user message.
- Give destructive file operations a clear recoverability and backup contract.
- Preserve fresh-install and upgraded-install compatibility on macOS, Windows,
  Linux, and the Web UI.

## Non-goals

- Do not change the meaning of Safe or Full Access.
- Do not automatically enable backups on the user's behalf.
- Do not expose the canonical elevation action to the browser as a shortcut for
  rendering.
- Do not make ordinary file creation require approval or backup.

## Approval display contract

The Gateway owns a small allowlisted display projection. Queue parameters and
canonical actions remain internal. The public approval payload adds semantic
fields such as:

- `displayKind`: one of `delete`, `modify`, `run_command`, `run_code`,
  `network_access`, `path_access`, `plugin_permission`, or
  `sensitive_operation`;
- `displayTarget`: an exact sanitized path, host, package bundle, plugin name,
  or other user-recognizable target when one exists;
- `destructive`: whether the operation can destroy or overwrite user data;
- `irreversible`: whether the pending execution currently has no recoverable
  backup guarantee;
- `backupState`: `not_applicable`, `enabled`, `disabled`, or
  `unavailable_requires_confirmation`.

The projector derives these values from an explicit mapping of known approval
kinds and canonical elevation action kinds. Unknown values fail closed to
`sensitive_operation` and expose no raw identifier. The Web UI localizes the
semantic kind and renders only the public target, backup state, sanitized
warning, and—when command consent genuinely requires it—the exact redacted
command. It never renders raw `toolName`, namespace, agent ID, action kind, or a
generic JSON dump.

Examples:

- recursive directory deletion: **Delete files or folders** plus the exact
  target path;
- protected-file edit or overwrite: **Modify an existing file** plus the exact
  path;
- approved shell command: **Run a command** plus the redacted command;
- unknown approval: **Sensitive operation** with no internal fallback name.

The collapsed outcome row uses the semantic label and target as well.

## Denial semantics

An explicit human denial is terminal for the current task:

1. the approval queue records the denial and publishes the resolved event;
2. the suspended tool emits the denial result for transcript/audit purposes;
3. the engine marks that result as a turn-terminating boundary;
4. no second provider request is made, so the model cannot switch tools and
   request the same destructive intent again;
5. the task completes cleanly and the chat session remains available.

Expiry, unavailable approval surfaces, policy blocks, and ordinary tool errors
retain their existing behavior unless they already define a terminal boundary.
The new rule applies specifically to an explicit resolved human denial.

## Destructive operations and backups

The existing backup setting is widened from recursive deletion to destructive
mutations of existing file content:

- recursive directory deletion;
- single-file deletion;
- overwrite or edit of an existing file;
- patch deletion or modification of an existing file.

Creating a new file does not need a backup. Operations that cannot be
attributed to an exact file target retain their existing command approval and
must not claim a backup guarantee.

When backup is enabled, the approval explains that OpenSquilla will create a
recoverable copy before mutation. Backup space management happens before the
mutation:

1. preserve complete backup entries only;
2. evict the oldest complete backups as needed;
3. retry the new backup;
4. perform the approved mutation after the backup succeeds.

The approval tells the user that older backups may be removed to make room.
The execution result records whether a backup was created and its receipt.

When backup is disabled, the card prominently states that no backup will be
created, the operation may be impossible to recover, and the user can enable
the setting under **Settings → Sandbox → File safety**. The warning is advice;
it does not silently change the setting.

If all old backups have been removed and the current object still cannot be
backed up—for example because it exceeds the configured quota or a disk or
permission error persists—the original approval does not silently authorize an
unbacked mutation. OpenSquilla issues a second, distinct confirmation with
`backupState=unavailable_requires_confirmation`, prominently stating that this
specific execution has no backup and is irreversible. Approval of that second
confirmation permits the operation without a backup; denial ends the task.

## Compatibility and migration

Existing sandbox policy files continue to load. The public configuration keeps
the current setting compatible while its user-facing meaning is widened to
"Back up before destructive file changes." Missing fields use the safe default
enabled state. The Gateway accepts old approval records and projects them to
the generic safe display when it cannot derive a semantic operation.

Older clients may ignore the new display fields, but the updated Web UI never
falls back to internal identifiers. Fresh and upgraded desktop profiles are
covered separately in package-level tests.

## Test strategy

### Gateway and projection

- recursive delete projects `delete`, the exact target, and backup state;
- protected modification projects `modify` without tool/action identifiers;
- unknown approvals project `sensitive_operation` without raw identifiers;
- snapshots and WebSocket events use the same projection;
- secrets and canonical action objects remain absent.

### Runtime

- an explicit denial ends the current task with one provider request;
- the model cannot issue an alternate delete tool after denial;
- the chat session accepts a later independent message;
- approval expiry is not mistaken for explicit denial.

### Backup behavior

- enabled backups cover recursive delete, single-file delete, overwrite, edit,
  and patch mutation of existing files;
- oldest complete backups are evicted and the current backup is retried;
- new-file creation creates no backup;
- disabled backup state is represented honestly;
- persistent failure produces a second unbacked irreversible confirmation;
- denial of either approval leaves the target unchanged.

### Web UI and packaged clients

- approval cards in every supported locale render semantic labels and never
  render `sandbox_elevation`, `exec_command`, or raw namespaces/action kinds;
- destructive warnings visually emphasize irreversibility and backup state;
- the disabled state includes the Settings path guidance;
- macOS packaged-client manual flow verifies delete → deny → task ends with no
  second approval;
- fresh-profile and upgraded-profile package probes exercise the new setting
  compatibility.
