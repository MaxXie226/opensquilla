# On-Demand Windows Sandbox Setup Design

Date: 2026-08-03
Status: Approved for implementation

## Context

The Windows Gateway currently schedules `ensure_sandbox_setup_auto()` during boot even when the selected run mode is Full Access. On a new Windows profile this can launch the elevated setup helper before the user has asked to use Safe mode. At the same time, Sandbox settings disable the Safe mode control while capability verification is unavailable, so the user cannot intentionally start setup from the place where the choice is explained.

OpenSquilla's Windows sandbox is its own restricted local-account, ACL, Firewall, and WFP boundary. It does not depend on the Microsoft Windows Sandbox optional feature.

## Goals

- Starting OpenSquilla in Full Access must never launch the sandbox UAC helper.
- A new Windows user starts in Full Access and can use the application without configuring Safe mode.
- The first intentional Safe mode selection in Sandbox settings explains the one-time administrator requirement before any UAC prompt appears.
- Only an authenticated local desktop owner can request setup.
- Cancelling the explanation or the Windows UAC prompt leaves Full Access selected and keeps the application responsive.
- A successful setup must run live capability verification before Safe mode becomes selectable.
- Existing configured users and direct upgrades from the official release must keep working without a new UAC prompt.

## Non-goals

- Microsoft Windows Sandbox or a VM dependency.
- Allowing a remote Web visitor or named-token client to start administrator setup.
- Removing the existing setup marker, capability canary, local sandbox account, Firewall rules, or WFP filters.
- Automatically switching an existing running task between Full Access and Safe mode.
- Adding a production sandbox-uninstall button.

## Considered approaches

### A. Owner-confirmed setup RPC from Sandbox settings (selected)

Gateway boot performs a read-only setup status check. The Sandbox settings Safe mode control remains actionable when Windows setup is supported but incomplete. Clicking it opens an OpenSquilla confirmation dialog. Only after confirmation does the owner-only `sandbox.setup.ensure` RPC launch the elevated helper. The UI then forces a capability refresh and selects Safe mode only after the live canary succeeds.

This preserves the existing security boundary, makes the UAC prompt attributable to an explicit user action, and requires the smallest architectural change.

### B. Automatic setup only when the stored mode is Safe

This avoids UAC for new Full Access profiles, but an upgraded profile carrying an old Safe preference could still receive an unexpected UAC prompt during startup. It does not meet the explicit-interaction requirement.

### C. Move Windows setup ownership into Electron

Electron could own confirmation, UAC, and setup state directly. This would duplicate Gateway setup logic and create separate behavior for desktop, CLI, and future hosts. The refactor is not justified for this change.

## Startup behavior

`_ensure_sandbox_setup_on_boot()` becomes non-elevating:

1. If automatic inspection is disabled, return without work.
2. Read the current setup status without calling the elevated helper.
3. If setup is already ready, prewarm the live capability report.
4. If setup is missing or failed, log that setup is deferred until an explicit owner request and continue startup.

No boot path may call `ensure_sandbox_setup_auto()` for an incomplete setup. Full Access remains available. A stored Safe preference whose sandbox is not ready is constrained to the existing Full Access soft landing until setup succeeds; it must not cause background elevation.

## Settings interaction

Sandbox settings distinguish three states:

- **Ready:** Safe mode is selectable normally.
- **Needs setup:** the Safe mode button is actionable and starts the confirmation flow.
- **Unsupported or verification failed after setup:** Safe mode remains unavailable and no elevation is attempted automatically.

The confirmation uses neutral styling:

- Title: `配置安全模式`
- Body: `OpenSquilla 需要一次管理员授权，以创建隔离账户并配置文件与网络保护。仅首次配置或修复时需要。`
- Primary action: `继续`
- Secondary action: `取消`

After `继续`:

1. Call owner-only `sandbox.setup.ensure`.
2. If setup reports ready, call `sandbox.capability.status` with `refresh: true`.
3. Select Safe mode only when the refreshed report is available.
4. Keep the existing mode-save boundary: Sandbox settings still persist the selected default through the existing Save action.

If the user cancels the explanation, cancels UAC, or setup fails, the draft and persisted run mode remain Full Access. The UI shows a short non-red inline result and allows retry. Raw helper output stays in logs.

The composer Safe mode option remains disabled until setup and capability verification are ready. It never launches UAC. This keeps remote Web and ordinary chat interactions outside the administrator boundary.

## Authority and error handling

- `sandbox.setup.ensure` remains guarded by `_require_owner`.
- Missing/invalid-token Web guests cannot request setup even by calling the RPC directly.
- Concurrent ensure calls continue to use the existing setup lock.
- UAC cancellation is a normal soft-landing result, not an application crash.
- Setup success is not sufficient by itself: Safe mode remains unavailable until the live process, filesystem, and deny-boundary canary passes.
- Gateway startup and Settings navigation must not wait on the elevated helper.

## Compatibility

- Existing valid Windows setup markers are inspected and capability-prewarmed without UAC.
- Existing Full Access profiles without a marker start without UAC.
- Existing Safe preferences with an unavailable marker do not auto-elevate; they soft-land to Full Access until the owner explicitly configures Safe mode.
- Legacy `trusted` and `sandboxed` aliases remain readable; new writes remain canonical `full` and `safe`.
- Tasks, sessions, config, named tokens, and Web guest restrictions are unchanged.

## Verification

Automated tests must prove:

- boot with Full Access and missing setup never calls the ensure helper;
- boot with a stored Safe preference and missing setup never calls the ensure helper;
- boot with ready setup still prewarms capability verification;
- Settings confirmation appears before `sandbox.setup.ensure`;
- cancelling the explanation performs no RPC and keeps Full Access;
- UAC cancellation/failure keeps Full Access and exposes a retryable neutral result;
- successful setup forces live capability refresh before selecting Safe mode;
- remote/non-owner callers remain unable to call `sandbox.setup.ensure`.

Packaged Windows acceptance has two independent paths:

1. **Upgrade:** install the current official release into an isolated profile, seed representative config/task/token data, overwrite with the new package, and verify preserved data, Full Access startup without UAC, and working existing Safe setup.
2. **First-time setup:** stop OpenSquilla, back up the current sandbox marker, remove the test sandbox local account under explicit UAC, launch the new package with a fresh desktop profile, verify no UAC at startup, open Sandbox settings, select Safe mode, accept the OpenSquilla explanation, let the user approve Windows UAC, verify account/marker/Firewall/WFP creation and the live Safe canary, then restore a healthy normal desktop state.

The first-time test must record UAC cancellation and successful approval as separate outcomes. Test-only reset tooling must not ship as a production settings action.
