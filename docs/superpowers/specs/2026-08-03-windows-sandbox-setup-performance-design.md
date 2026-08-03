# Windows Sandbox First-Setup Performance Design

## Problem

Real packaged-app testing on Windows showed that choosing Safe mode and approving UAC can leave the dialog on “configuring” for tens of seconds. The elevated helper installs nine firewall rules by starting a fresh PowerShell process for every rule. It also starts `icacls` once for every existing direct child in the persistent sandbox tree. Each operation is correct, but repeated process starts add avoidable first-setup latency and make the UI appear frozen.

## Chosen design

Keep the rule definitions, delete-before-create semantics, elevation boundary, WFP transaction, account creation, ACL hardening, and live capability verification unchanged. Convert the tuple of firewall commands into one fail-fast PowerShell script and invoke PowerShell once. `$ErrorActionPreference = 'Stop'` makes any failed rule abort the batch, and the existing non-zero-exit handling continues to return a setup failure.

Reset existing child ACLs with one `icacls <root>\* /reset /t /L` invocation per non-empty persistent root instead of one invocation per direct child. `icacls` expands the wildcard itself, `/t` retains recursive behavior, and `/L` continues to operate on link objects rather than following them. The root remains outside the wildcard and therefore keeps its newly hardened explicit ACL. The lease revalidation sequence remains identical to the old implementation: it is skipped only across the trusted root grant and child reset operations that already required an uninterrupted ACL transition, then runs again before the offline-SID removal and after the root is complete.

While the setup RPC is pending, the confirmation dialog will show a compact time-based progress line. It starts with Windows authorization, advances to applying file and network protection, and ends with a first-run reassurance. This is presentation-only and does not claim exact backend telemetry; the final transition to Safe mode still depends exclusively on the existing forced live capability check.

## Compatibility and safety

- Do not change firewall rule names, scopes, ports, addresses, or WFP filters.
- Do not weaken UAC, ownership checks, marker validation, ACL validation, or live verification.
- Cancellation continues to keep Full Access selected and produces no partial success claim.
- Old installations and existing setup markers remain valid; this changes only the execution strategy for a required repair or first setup.
- Remote Web clients still cannot request host setup.

## Verification

- Unit-test that all firewall commands are sent in one PowerShell process and failures still surface.
- Unit-test that existing child ACL resets use one wildcard invocation, while empty roots and all security validation steps retain their existing behavior.
- Unit-test that the setup dialog advances through neutral progress copy while pending.
- Run focused backend and frontend regressions, architecture/type checks, and the packaged build.
- Repeat the official `0.5.2` to modified build upgrade rehearsal.
- Remove the test sandbox account, launch the packaged app without UAC, approve first setup, and compare end-to-end elapsed time with the previous tens-of-seconds run.
