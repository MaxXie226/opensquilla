# Windows Sandbox UTF-8 I/O Reliability Implementation Plan

**Goal:** Fix locale-dependent corruption in the Windows safe-mode filesystem worker without changing the UTF-8 user-file format or the sandbox security boundary.

## Task 1: UTF-8 protocol regression tests

**Files:**
- Modify: `tests/test_sandbox/test_filesystem_worker.py`

- Add a binary-backed GBK text-stream fixture.
- Add a failing stdin test with Chinese and emoji.
- Add a failing stdout test with an emoji-bearing read result.
- Run the focused tests and confirm they fail for the locale-dependent stream behavior.

## Task 2: Mutation-preservation regression tests

**Files:**
- Modify: `tests/test_sandbox/test_filesystem_worker.py`

- Add failing tests showing `write_text` and `edit_text` preserve existing content when UTF-8 encoding rejects a lone surrogate.
- Add a passing-behavior assertion for exact Chinese and emoji bytes.
- Run the focused tests and confirm the preservation assertions fail before implementation.

## Task 3: Minimal implementation

**Files:**
- Modify: `src/opensquilla/sandbox/filesystem_worker.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Modify: `src/opensquilla/tools/builtin/patch.py`
- Modify: `tests/test_sandbox/test_windows_default_backend.py`

- Add explicit UTF-8 binary protocol helpers with a test-stream fallback.
- Add validated same-directory replacement for text overwrite/edit operations.
- Pre-encode exclusive source creation and all planned patch writes before mutation.
- Add UTF-8 environment variables to the restricted filesystem-worker request.
- Run each focused test after its minimal implementation, then run both related test modules.

## Task 4: Windows acceptance and audit

- Run formatting/static checks relevant to the modified Python files.
- Exercise the packaged restricted worker with Chinese and emoji content and read it back.
- Verify invalid Unicode leaves an existing probe file unchanged.
- Run `git diff --check` and inspect the final diff without staging unrelated upgrade artifacts.
- Commit only the spec, plan, implementation, and regression tests on `sandbox-settings-reliability`.

