# Sandbox Settings Reliability and Access Design

Date: 2026-08-03
Status: Approved for implementation

## Context

The current Sandbox settings experience has four related problems:

1. Safe-mode copy wraps and makes the run-mode selector visually noisy.
2. New profiles start in Safe mode even though the product default should be Full Access.
3. Named-token creation and management appears inside Sandbox settings, although the original Web UI only accepted a token supplied by the host.
4. Live sandbox capability verification can take 30 seconds, time out, and block the whole Sandbox page. Other Settings sections also show a full-pane spinner while shared setup data loads.

The live Windows failure is reproducible. Repeated capability probes create disposable paths, while Windows ACL journals retain entries for paths that no longer exist. The installed profile currently contains hundreds of stale capability-probe entries. A cancelled probe can also leave a taint-recovery transaction for the next probe. Once the 30-second deadline is reached, the unavailable result is cached with the same one-hour lifetime as a successful result.

## Goals

- Keep the first Sandbox screen visually simple and immediately interactive.
- Make Full Access the default for profiles without an explicit user preference.
- Keep Safe mode real and fail closed without allowing its live verification to freeze Settings.
- Remove named-token creation and management from the visible Sandbox UI.
- Preserve token authentication, guest restrictions, named-token storage, and old protocol aliases for upgrade compatibility.
- Keep local desktop ownership automatic and keep remote Web token entry in the existing Connection page.

## Non-goals

- Removing token authentication or the named-token backend.
- Allowing a remote guest to issue or manage tokens.
- Adding HTTPS or changing the gateway listening model.
- Implementing automatic rollback for failed application upgrades.
- Reworking the detailed File, Command, Network, or Bundled Runtime policy editors beyond their loading behavior.

## User experience

### Sandbox overview

The overview contains only:

- a one-line explanation: `在沙箱中运行，并遵循你的安全规则。`
- the two run-mode choices, Safe mode and Full Access;
- the existing File, Command, Network, and Bundled Runtime rows.

The `Web 访问与具名 Token` row and its detail page are removed. The page does not display explanatory text about missing or invalid tokens.

The Settings shell and selected section render immediately. Loading is local to the data that is actually pending; changing sections never replaces the whole content pane with a blank spinner.

### Run-mode default

Full Access is the product default when no explicit preference exists. An explicit user preference continues to win. Existing stored aliases such as `trusted` remain readable, while all new writes use the canonical value `full`.

The currently installed test profile is switched to Full Access through the normal preference API after the new build is installed, so the packaged result demonstrates the requested default without editing its database directly.

### Remote Web authentication

Token authentication remains a backend concern:

- the local loopback desktop client is recognized as the owner and does not need manual token entry;
- a remote Web visitor may leave the existing Connection token field empty or paste a host-provided token;
- missing and invalid tokens resolve to the same guest principal and restricted Safe-mode policy;
- the UI does not expose token issuance or token management;
- owner checks remain on token-management RPC methods for backward compatibility and defense in depth.

The gateway process, running on the host, is the authority that configures or issues tokens. A visitor browser never generates an authority-bearing token.

## Loading architecture

Sandbox settings data is divided into independent groups:

1. **Core**: policy, policy defaults, and run-mode preference. This data controls the visible overview and loads first.
2. **Capability**: the live sandbox report. It loads in the background and updates only the Safe-mode control/status.
3. **Desktop preference**: the optional warning-suppression preference. It cannot block the page.

Token-list loading is removed from Sandbox settings. A capability failure does not discard successfully loaded policy data. Retrying capability verification affects only the capability state.

The shared Settings dialog keeps its chrome and active panel mounted while common setup data loads. Panels that need that data show a small local pending state; panels such as Connection and Sandbox remain usable independently.

## Capability verification reliability

The Windows ACL state must converge instead of growing with every disposable probe:

- stale entries whose target paths no longer exist are pruned from persisted ACL journals without attempting unnecessary ACL teardown;
- capability probes stop accumulating unique retained ACL entries;
- cancellation and timeout paths still fail closed and leave recoverable intent markers;
- successful reports retain a long cache lifetime;
- timeout and failure reports use a short negative cache lifetime, preventing a retry storm without making Safe mode unavailable for an hour;
- the UI consumes cached/prewarmed status immediately and runs an explicit live refresh in the background rather than awaiting it before rendering.

The live canary remains authoritative. The implementation must not replace it with a setup-marker-only check or silently enable Safe mode when required capabilities are missing.

## Compatibility

- `safe` and `full` remain the canonical stored run modes.
- Legacy `sandboxed` and `trusted` values continue to decode.
- Existing named-token database records and RPC methods remain intact.
- Existing remote Web connection URLs and the Connection token field continue to work.
- Existing explicit Safe-mode preferences are not overwritten during an application update.
- Profiles with no explicit preference receive Full Access as the new default.

## Error handling

- Core policy load errors are shown inside the Sandbox panel without blanking Settings.
- Capability verification has distinct `checking`, `available`, and `unavailable` states.
- A timeout cannot block navigation and is eligible for a later retry.
- Run-mode selection updates immediately and rolls back only if persistence fails.
- Remote guests cannot list, create, or revoke tokens even if they call preserved RPC methods directly.

## Verification

Automated coverage must include:

- one-line Safe-mode copy and absence of the named-token UI;
- Full Access fallback for a new profile and preservation of an explicit Safe preference;
- legacy `trusted` decoding and canonical `full` writes;
- Sandbox core data rendering while capability verification is pending or fails;
- Settings section navigation without a full-pane loading gate;
- short negative caching and long successful caching for capability reports;
- pruning of nonexistent Windows ACL journal entries;
- unauthenticated and invalid-token callers resolving to the same guest authority;
- guest callers being unable to list, create, or revoke named tokens.

After tests pass, build and install a local Windows package, open the actual desktop application, verify responsive Settings navigation, verify Full Access is selected, and verify Safe mode becomes selectable after live capability verification succeeds.
