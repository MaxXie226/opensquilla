# Desktop and Web Guest Sandbox Boundary

**Date:** 2026-07-31
**Status:** Approved design, awaiting written-spec review

## Purpose

Restore the original deployment boundary between the desktop application and
the standalone Gateway, then give unauthenticated remote Web users a
non-bypassable guest-safe file boundary.

This design preserves the existing Safe mode rule that authenticated users can
read all ordinary host files. Sensitive-path read denial is an additional
restriction only for unauthenticated remote Web users.

## Deployment Boundary

### Desktop application

The desktop-owned Gateway is local-only:

- It always listens on `127.0.0.1`.
- The desktop settings do not expose listener address, LAN CIDR, or Gateway
  port controls.
- A locally proven desktop connection receives owner authority automatically.
- A stale desktop configuration containing `host = "0.0.0.0"` must not change
  the desktop listener. Desktop startup enforces loopback independently of
  persisted standalone-Gateway configuration.

Closing only the desktop window does not restart the Gateway. Any future
boot-time desktop setting change requires fully quitting the tray process and
reopening OpenSquilla.

### Standalone Gateway and Web UI

LAN access belongs to a separately launched Gateway:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Listener address and port are deployment-time settings. Changing them requires
restarting that Gateway. The standalone Gateway retains its existing private
peer protections and optional CIDR narrowing.

## Server-Computed Authority

The server derives authority from the connection and authentication result.
The client cannot declare its own authority, run mode, or workspace path.

| Caller | Effective authority |
| --- | --- |
| Locally proven desktop owner | Full local owner authority |
| Remote Web user with a valid token | Token capabilities; may select Safe mode or Full Access when authorized |
| Remote Web user with no token | Forced Web guest-safe authority |
| Remote Web user with an incorrect token | Same Web guest-safe authority as no token |
| Other authenticated or locally proven callers | Existing authority rules |

No-token and incorrect-token Web users have identical capabilities. When a
user explicitly submits a bad token, the login UI may report that the token is
invalid, but the resulting execution authority remains identical to the
no-token case.

Authority is captured for each submitted execution. Authenticating later does
not upgrade an already running guest process. After successful authentication
and reconnection, subsequent executions use the new token authority.

## Web Guest-Safe File Policy

### Read access

Only unauthenticated remote Web users receive sensitive-path read denial.
Desktop owners, valid-token Web users, CLI callers, locally proven owners, and
other authenticated callers retain the existing rule that host files are
readable in Safe mode.

For a Web guest:

- Non-sensitive host files remain readable.
- OpenSquilla authority, authentication, token, backup, upgrade, and recovery
  data are unreadable.
- The following built-in Windows credential paths are unreadable:

```text
%USERPROFILE%\.ssh\**
%USERPROFILE%\.aws\**
%USERPROFILE%\.kube\config
%USERPROFILE%\.docker\config.json
%USERPROFILE%\.docker\daemon.json
%USERPROFILE%\.netrc
%USERPROFILE%\.npmrc
%USERPROFILE%\.pypirc
%USERPROFILE%\.gem\credentials
%USERPROFILE%\.config\gh\hosts.yml
%USERPROFILE%\.git-credentials
%USERPROFILE%\.config\gcloud\**
%USERPROFILE%\.azure\**
%USERPROFILE%\.terraform.d\credentials.tfrc.json
```

Platform-equivalent built-ins apply on macOS and Linux, including SSH, AWS,
Kubernetes, Docker, package registry, Git/GitHub, cloud provider, Terraform,
GnuPG, keyring, password-store, and system credential locations already
defined by OpenSquilla's platform file policy.

Custom deny-write paths keep their normal Safe mode meaning: they control
mutation approval and do not automatically become read-deny rules.

### Write access

A Web guest may write only beneath the Gateway-configured `workspace_dir`.

- Files and ordinary subdirectories may be created inside that root.
- Existing files inside that root may be edited, moved, renamed, and deleted,
  subject to the normal Safe mode destructive-operation rules.
- Every write outside that root is denied without an elevation prompt.
- OpenSquilla authority roots and built-in sensitive paths remain denied even
  if the configured workspace is incorrectly placed inside one of them.
- A missing default workspace is created at the configured location. If
  creation or validation fails, the guest task fails safely; it does not fall
  back to the process current directory, a temporary directory, or host
  execution.

Lexical and canonical paths are checked. Relative traversal, environment
variable expansion, case variations, symbolic links, Windows directory
junctions, and retargeting races must not escape the boundary. The effective
operating-system sandbox and the tool-level side-effect guard enforce the same
compiled policy.

## Workspace Lifecycle

For a Web guest, the Gateway chooses `workspace_dir` and ignores any
client-supplied alternative workspace path.

Workspace management operations that can add, trust, open, switch, remove, or
otherwise register another workspace require authentication and are rejected
server-side for guests. The Web UI also hides these controls, but UI visibility
is not a security boundary.

Creating files and subdirectories within the default workspace is allowed and
does not count as creating a new workspace.

## Tool and Runtime Enforcement

The Web guest policy applies uniformly to:

- structured file tools;
- Shell commands;
- bundled Python;
- bundled Node.js;
- bundled Git Bash;
- indirect file effects from subprocesses and helper programs.

The policy is compiled once from the server-computed principal and effective
workspace, then passed through task bootstrap into the sandbox backend and
tool context. No individual tool may opt out.

Blocked sensitive reads and out-of-workspace writes are hard policy failures,
not approval requests. Approvals cannot expand the guest's immutable boundary.

Normal approvals remain available for actions already inside the allowed
boundary. For example:

- recursive deletion inside the default workspace still requires the dedicated
  irreversible-action confirmation;
- when backup-before-recursive-delete is enabled, the target is backed up
  first;
- the backup vault defaults to a 3 GiB quota and evicts the oldest content
  first;
- high-risk commands such as `git push` continue to follow command approval
  policy.

## Network and Command Policy

This feature does not introduce a separate Web guest network allowlist.
Existing network defaults and protections remain in force, including domain
rules, block-all configuration, SSRF defenses, and metadata-service
protections.

Existing command auto-run, approval-prefix, auto-allow-prefix, and built-in
high-risk rules remain in force. They cannot override the Web guest file
boundary.

## Errors and User Experience

- The mode indicator says that the user is running in guest Safe mode.
- A blocked sensitive read returns a stable error such as
  `GUEST_SENSITIVE_PATH_DENIED` with a simple explanation that Web guest mode
  cannot access credential data.
- The error must not disclose file contents or use existence-dependent wording.
- An out-of-workspace mutation returns
  `GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE`.
- Guest workspace-management calls return an authentication-required error.
- Sandbox initialization failure, policy compilation failure, or an unusable
  default workspace prevents the guest task from starting. There is no
  fallback to Full Access.

## Compatibility and Migration

Direct updates from older clients preserve tasks, user settings, named tokens,
and standalone Gateway deployment configuration.

The desktop LAN-listener controls introduced by the newer settings UI are
removed. On desktop startup, loopback binding takes precedence over any stale
persisted `0.0.0.0` value so an update cannot unexpectedly expose the desktop
Gateway. Standalone Gateway command-line and configuration behavior remains
available.

Existing authenticated Safe mode behavior remains read-all. The new
sensitive-read denial is activated only when all of the following are true:

1. the caller is using the remote Web boundary;
2. token authentication is missing or invalid;
3. the server resolved the caller to Web guest-safe authority.

## Verification

Automated and packaged-runtime verification must cover:

1. Missing and incorrect tokens resolve to identical Web guest capabilities.
2. A valid token can use its authorized run modes after reconnecting.
3. An already running guest process cannot be upgraded in place.
4. Desktop startup listens only on `127.0.0.1`, including with stale
   `host = "0.0.0.0"` configuration.
5. A standalone Gateway can still listen on `0.0.0.0` at a selected port.
6. Structured file tools, Shell, Python, Node.js, and Git Bash cannot read
   built-in sensitive paths as a Web guest.
7. Those same paths remain readable to authenticated Safe mode callers,
   subject to operating-system permissions.
8. Non-sensitive host files remain readable to Web guests.
9. Writes and ordinary directory creation succeed inside the default
   workspace.
10. All writes outside the default workspace are denied.
11. Workspace add/open/switch/remove RPCs are denied to Web guests.
12. Absolute paths, `..`, environment variables, case variation, symlinks,
    Windows junctions, indirect subprocess writes, and target-retargeting races
    cannot bypass the policy.
13. A default workspace placed beneath a sensitive or OpenSquilla authority
    root is rejected.
14. Recursive deletion confirmation, backup, the 3 GiB default quota, and
    oldest-first eviction work inside the allowed workspace.
15. Sandbox or workspace setup failure denies execution without host fallback.
16. Direct update migration preserves existing user data and tokens while
    removing the desktop LAN exposure path.
