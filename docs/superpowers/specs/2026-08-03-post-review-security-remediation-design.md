# Post-review Security Remediation Design

## Purpose

Close the remaining security and reliability gaps found by the full `main..sandbox-settings-reliability` review without changing the approved product model: desktop owner and valid named tokens retain their configured capabilities; missing and invalid named tokens remain identical Web guests in restricted Safe mode; no Token creation UI is exposed remotely.

## Anonymous Web control-plane isolation

An anonymous browser receives a random, non-user-facing guest session key. The Web client stores it locally and sends it on every connection independently of the optional named Token. The Gateway hashes this key into a guest owner id; a missing or malformed key gets a fresh ephemeral replacement. The key grants no host, configuration, log, memory, skill, agent-file, approval, or setup capability.

Guest-created Web chat session keys are server-normalized to include the current guest owner id. A central guest RPC policy has a small allowlist for connection metadata, model discovery, capability availability, chat submission, and session-scoped chat operations. Every session-scoped guest call must target a key carrying the caller's owner id. `sessions.list` returns only matching sessions. Existing owner/token sessions are never adopted: submitting an unowned key creates a new guest-namespaced session instead. Invalid named Tokens enter this exact path.

This identity is intentionally called a guest session key, not a named Token. It is invisible in Settings and cannot enable Full Access. Without TLS it has the same LAN eavesdropping limitation as all other Web traffic, which matches the already-approved HTTP deployment decision.

## Fail-closed live capability verification

Capability verification must exercise the selected native backend, not count a Python policy exception as operating-system isolation. Process and filesystem-worker canaries remain. Protected-write and authority-read canaries run ordinary commands inside the sandbox identity with the logical profile installed, then require a non-zero native result, unchanged protected content, and absence of authority content. Infrastructure exceptions, transport errors, timeouts, or malformed worker results make the report unavailable.

On Windows the report additionally requires the current setup support probe to confirm identity, persistent storage, and proxy/WFP marker compatibility. A future filter-enumeration API may strengthen this further, but marker-only state never substitutes for the native deny canaries.

## Runtime preference and Windows guest reads

Fresh owner installs default to Full Access in the configuration model. `get_run_context` resolves the persisted `sandbox.run_mode` preference from session storage before falling back to explicit config and then the Full default. Guest principals are still coerced to Safe at ingress.

For Windows filesystem-worker read operations, an already-authorized exact read target is projected as a temporary RX ACL grant when the profile's default READ produced no explicit entry. DENY targets and denied globs are rejected before projection; write operations receive no new grant. This makes ordinary host reads work for the isolated account while the built-in sensitive paths remain unreadable.

## Responsiveness and migration privacy

The sandbox Settings page performs one background capability check per mount. A failed check remains visible until the user explicitly retries or selects Safe mode; there is no perpetual ten-second retry loop.

Upgrade snapshots are private before any credential-bearing file is copied. POSIX staging directories/files use `0700`/`0600`. Windows staging receives an inheritance-disabled DACL for the current user, SYSTEM, and Administrators; failure to apply it aborts migration. The final snapshot is hardened again after atomic publication.

## Compatibility and verification

- Existing named Tokens, config values, session keys, and migration journals remain readable.
- Old Web clients without a guest session key can create tasks for the current connection but cannot recover them after reconnect; the current Web client persists the key.
- Add a complete guest RPC deny matrix covering sessions, logs, memory, agent files, configuration, setup, and capability refresh.
- Add guest ownership tests for create, reconnect, list, read, abort, delete denial, invalid Token parity, and attempted adoption of an owner session.
- Add fake-backend transport-failure tests plus packaged Windows native deny canaries.
- Add owner/guest/CLI no-hint run-mode tests, Windows native ordinary-read versus sensitive-read tests, bounded capability-loading tests, and private snapshot permission tests.
