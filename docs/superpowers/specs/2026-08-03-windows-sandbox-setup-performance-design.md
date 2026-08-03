# Windows Sandbox First-Setup Performance Design

## Problem

Real packaged-app testing on Windows showed that choosing Safe mode and approving UAC can leave the dialog on “configuring” for tens of seconds. The elevated helper currently installs nine firewall rules by starting a fresh PowerShell process for every rule. Each command is correct, but repeated PowerShell cold starts dominate first-setup latency and make the UI appear frozen.

## Chosen design

Keep the rule definitions, delete-before-create semantics, elevation boundary, WFP transaction, account creation, ACL hardening, and live capability verification unchanged. Convert the tuple of firewall commands into one fail-fast PowerShell script and invoke PowerShell once. `$ErrorActionPreference = 'Stop'` makes any failed rule abort the batch, and the existing non-zero-exit handling continues to return a setup failure.

While the setup RPC is pending, the confirmation dialog will show a compact time-based progress line. It starts with Windows authorization, advances to applying file and network protection, and ends with a first-run reassurance. This is presentation-only and does not claim exact backend telemetry; the final transition to Safe mode still depends exclusively on the existing forced live capability check.

## Compatibility and safety

- Do not change firewall rule names, scopes, ports, addresses, or WFP filters.
- Do not weaken UAC, ownership checks, marker validation, ACL validation, or live verification.
- Cancellation continues to keep Full Access selected and produces no partial success claim.
- Old installations and existing setup markers remain valid; this changes only the execution strategy for a required repair or first setup.
- Remote Web clients still cannot request host setup.

## Verification

- Unit-test that all firewall commands are sent in one PowerShell process and failures still surface.
- Unit-test that the setup dialog advances through neutral progress copy while pending.
- Run focused backend and frontend regressions, architecture/type checks, and the packaged build.
- Repeat the official `0.5.2` to modified build upgrade rehearsal.
- Remove the test sandbox account, launch the packaged app without UAC, approve first setup, and compare end-to-end elapsed time with the previous tens-of-seconds run.
