# Sandbox Auto-save and Background Setup Design

## Goal

Make Sandbox settings feel immediate and quiet: remove explicit Save/Discard controls, preserve every accepted change automatically, and let first-time Safe-mode setup continue without blocking normal use.

## User experience

### Settings

- The Sandbox overview and all four detail pages show no Save or Discard buttons.
- Mode buttons, switches, add/remove actions, and select controls save immediately.
- Free-form text and numeric inputs save after a short idle delay or when focus leaves the field. Enter still commits add-row actions immediately.
- A pending save does not add a large progress control. The changed control may be temporarily disabled only when another edit would make its result ambiguous.
- A failed save rolls back only the affected mode or policy section to the last server-confirmed value and shows a bottom-right error toast.
- Successful ordinary saves stay quiet.

### First-time Safe-mode setup

- Before setup begins, the dialog offers **Cancel** and **Start setup**.
- After Start setup is pressed, the dialog offers **Run in background** and a disabled **Configuring…** action. Cancel is no longer shown.
- Run in background closes only the dialog. It does not cancel the administrator helper, live capability verification, or the user's intent to select Safe mode.
- The user may continue creating or using tasks while setup runs. Existing tasks retain their pinned execution mode.
- When setup and live verification succeed, OpenSquilla persists Safe as the default for subsequent tasks and shows one bottom-right success toast.
- If setup is cancelled at the Windows prompt, fails, or does not pass verification, Full Access remains selected and OpenSquilla shows one concise failure toast with a retry path through the normal Safe-mode control.
- If the dialog remains open, the same completion or failure outcome is shown there as well; closing it never changes the background operation.

## Architecture

### Auto-save coordinator

`useSandboxSettings` remains responsible for loading the server-confirmed baseline and editing a local draft. It gains queued auto-save entry points for the default mode and each policy section:

- mode changes are serialized with policy saves;
- each policy section coalesces rapid changes into its newest snapshot;
- text and numeric controls use a UI-level debounce before entering the queue;
- a successful response replaces that section's baseline without overwriting drafts in other sections;
- a failed response restores only that section and reports the error through the shared toast service.

The existing optimistic-concurrency `basePolicyVersion` contract remains authoritative. Section saves continue to use one queue so concurrent edits cannot write an older policy version over a newer one.

### Background setup coordinator

Safe-mode installation becomes one shared, application-lifetime operation rather than work owned by a settings panel or chat view. The coordinator:

- deduplicates setup requests from Settings and the composer;
- holds pending/outcome state after either caller closes or unmounts;
- runs `sandbox.setup.ensure`, reconnection recovery, and forced live capability verification;
- remembers whether the triggering action requested Safe mode;
- persists Safe only after verification succeeds;
- emits exactly one success or failure toast per operation;
- exposes state to any currently visible setup dialog.

The existing RPC coordinator remains the low-level setup/reconnect implementation. The new application-lifetime layer owns operation lifetime, user intent, persistence, and notifications.

## State and concurrency rules

- Only one setup operation may run at a time.
- Repeated Start setup clicks return the same in-flight operation.
- Run in background never sends a cancellation request.
- Closing Settings, navigating away from a new-task page, or opening a different task does not dispose the operation.
- Setup success is not enough: Safe is selected only after the live capability report is available.
- A later explicit Full Access selection made after setup began wins over the earlier Safe intent. Completion still produces a setup-success toast but does not overwrite that newer user choice.
- Auto-save errors never discard unrelated unsaved edits.

## Accessibility and copy

- The background action remains a normal enabled button and receives the label translated in all supported locales.
- The disabled configuring action exposes progress text and `aria-disabled` through the native disabled state.
- Background completion uses the existing toast live region.
- Copy is short and non-technical: “Safe mode is ready” on success and “Safe mode setup could not finish. Try again from Safe mode.” on failure.

## Testing

Automated tests cover:

- no Save/Discard controls on overview or detail pages;
- mode, switch, add/remove, text, and numeric edits auto-save;
- rapid edits coalesce and policy versions remain ordered;
- a failed section save rolls back only that section and creates one toast;
- pending setup replaces Cancel with Run in background;
- backgrounding closes the dialog without cancelling the promise;
- setup continues after the initiating component unmounts;
- successful verification persists Safe and emits one success toast;
- failure retains Full Access and emits one failure toast;
- an explicit Full selection made while setup runs is not overwritten;
- Settings and composer share and deduplicate the same operation.

Packaged Windows verification repeats the isolated first-run probe and confirms the normal client remains usable while setup runs. The test must not invoke a second real UAC prompt unless explicitly requested.
