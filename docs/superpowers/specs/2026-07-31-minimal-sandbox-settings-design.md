# Minimal Sandbox Settings Design

## Goal

Make the Sandbox settings understandable at a glance without removing any
capability. The first screen should answer only three questions: is protection
available, which execution mode is active, and where do I change a specific
policy.

## Information architecture

The Sandbox page has two levels.

### Overview

- A quiet title row with a small availability dot and one-line explanation.
- A two-choice segmented control for **Safe mode** and **Full access**.
- One grouped settings list with four rows: **Files**, **Commands**,
  **Network**, and **Bundled tools**. Each row contains an icon, a short live
  summary, and a chevron.
- One separate **Advanced** row for named Web tokens and capability
  re-detection.
- No rule editors, long sensitive-path list, token form, repeated save bars, or
  diagnostic prose appears on the overview.

### Detail

Selecting a row replaces the overview inside the same settings surface. A
compact Back control, title, and one-sentence explanation establish context.
The existing controls and save/discard semantics remain unchanged. Only the
selected category is rendered, so a user never sees unrelated advanced rules.

The Advanced view contains named-token management, the current capability
reason, a re-detect action, and the existing warning reset action. Desktop LAN
binding and CIDR controls remain absent.

## Visual language

- Use one broad grouped surface instead of many bordered cards.
- Prefer whitespace, hairline separators, muted summaries, 12-14 px corner
  radii, and restrained accent colour.
- Use a native-looking segmented control for the two modes.
- Keep destructive or unavailable states legible but avoid red decoration when
  there is no active error.
- Keep text short enough for the Chinese interface to scan without wrapping on
  ordinary desktop widths.

## Behaviour and compatibility

- Existing RPC payloads, persisted policy shape, approvals, recursive-delete
  backup, runtime toggles, and token semantics do not change.
- Safe mode remains disabled when the live capability is unavailable; Full
  access remains selectable.
- Unsaved changes are confined to one detail section. Back returns to the
  overview without silently saving.
- The responsive layout collapses row metadata cleanly below 720 px without
  changing navigation.

## Verification

- Unit tests cover overview summaries, detail navigation, absence of LAN/CIDR
  controls, mode save behaviour, and token placement under Advanced.
- Architecture, i18n, typecheck, and production build checks must pass.
- The local app is launched and the overview/detail states are inspected at
  desktop width before a new installer is produced.
