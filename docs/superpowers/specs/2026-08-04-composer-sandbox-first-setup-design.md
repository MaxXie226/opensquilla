# Composer Safe Mode First-Setup Design

## Goal

Make the execution-mode control on the new-task composer truthful and usable during first-time Windows sandbox setup.

- A fresh installation starts in Full Access.
- The composer always shows the mode that can actually be used.
- Safe mode can be selected and configured directly from the composer.
- The user's last successfully selected mode is remembered.
- Safe mode is never persisted before setup and live verification succeed.

## Current problem

The composer reads the saved Safe preference while sandbox setup status is still `not_setup`, `failed`, or otherwise not ready. `ChatView` removes Safe from the allowed choices but continues passing the stale Safe value to the shield control. The result is a green Safe indicator even though only Full Access can run.

The existing first-time setup confirmation and progress UI lives only inside Sandbox settings. The composer disables Safe when setup is not ready, so users cannot start the required setup from the place where they choose an execution mode.

## Behaviour

### Effective mode

1. A fresh profile has Full Access as its default execution mode.
2. After a user successfully selects Safe or Full Access, that mode becomes the remembered choice for later new tasks.
3. When authoritative sandbox setup status is `not_setup`, `setting_up`, `failed`, or `unavailable`, the effective composer mode is Full Access even if an older Safe preference exists.
4. A missing or not-yet-loaded status does not invent a setup failure. The composer waits for the authoritative status and then reconciles its display.
5. An active task keeps its locked execution mode and cannot be changed through the composer.

### Composer selection

The shield popover continues to contain exactly two choices: Safe mode and Full Access.

- Selecting Full Access switches immediately and persists the confirmed choice.
- Selecting Safe while setup is `ready` switches immediately and persists the confirmed choice.
- Selecting Safe while Windows setup is `not_setup` or repairable `failed` closes the popover and opens the shared first-setup dialog.
- Safe remains unavailable without a setup action on unsupported platforms or terminal `unavailable` states.

### First-setup dialog

The composer and Sandbox settings use one shared dialog component and one shared setup state flow.

Before setup begins, the dialog says that:

- Windows administrator approval is required;
- setup normally takes about 20–30 seconds;
- OpenSquilla should remain open during setup.

The dialog offers Cancel and Start setup. No setup RPC or UAC request occurs before Start setup is pressed.

After setup starts:

- the primary action is disabled;
- the dialog shows elapsed time and a short phase message;
- the rest of the renderer remains responsive;
- cancelling the Windows UAC prompt, setup failure, and live-verification failure all leave Full Access selected;
- success closes the dialog, selects Safe, and only then persists Safe as the user's remembered mode.

The UI must not show a fake percentage because the privileged Windows operations do not expose reliable progress. Elapsed time and phase text provide honest feedback.

## Architecture

### Shared dialog

Extract the existing setup dialog from `SandboxSettingsPanel.vue` into a reusable component. It owns presentation, elapsed-time messaging, Cancel/Start controls, and accessibility semantics. It does not own RPC calls or mode persistence.

### Shared setup coordinator

Use one composable-level setup operation for both entry points. It exposes authoritative setup status, pending/outcome state, `canSetup`, and an `ensureReady` operation. This prevents the settings and composer paths from implementing different UAC, capability-refresh, cancellation, or failure rules.

### Composer mode reconciliation

Derive a composer-effective mode from the remembered preference, active task lock, and authoritative setup status. When Safe is not runnable, the control renders Full Access and outgoing new-task requests use Full Access. The remembered Safe preference is not treated as a successful current selection until setup is ready again.

Mode persistence remains confirmation-only: optimistic visual changes may be used for already-available modes, but browser storage and backend preference updates happen only after the backend confirms the selected mode.

## Error handling

- UAC cancelled: keep Full Access and show a concise cancelled result with Retry available.
- Setup command failed: keep Full Access and show a retryable failure.
- Live capability verification failed: keep Full Access and explain that setup completed but verification did not pass.
- Connection recycled or temporarily unavailable: do not leave the UI in Safe; recover the authoritative status and allow retry.
- Unsupported platform or terminal unavailable state: Safe is disabled without presenting a setup action that cannot work.

No failure path blocks the rest of the app or changes the user's projects.

## Compatibility

- Legacy `trusted`, `standard`, and other historical mode names continue to be normalized by the existing compatibility layer.
- Existing users with a valid Safe setup keep their remembered Safe preference.
- Existing users with a stale Safe preference and missing/broken setup soft-land in Full Access and may repair Safe from the composer.
- No configuration key is renamed or deleted for this change.
- The existing official-version-to-feature-version upgrade path remains covered by the upgrade smoke test.

## Verification

Automated tests cover:

1. Fresh profile renders Full Access before any user selection.
2. Stale Safe preference plus authoritative `not_setup` renders and sends Full Access.
3. Safe selection before setup opens confirmation without calling setup.
4. Cancel leaves Full Access and does not persist Safe.
5. Start setup shows elapsed/phase feedback and calls setup once.
6. Setup success performs live verification, selects Safe, and persists it.
7. UAC cancellation, setup failure, verification failure, and connection interruption all stay in Full Access.
8. Ready Safe switches immediately without showing the first-setup dialog.
9. Full Access switches immediately and remains the next-task default.
10. Active-task mode locking remains unchanged.
11. Settings and composer entry points share the same setup dialog and coordinator.

Manual Windows verification covers a real reset sandbox account, real UAC approval, measured setup duration, UI responsiveness during setup, successful cold restart, and the existing-profile upgrade path.
