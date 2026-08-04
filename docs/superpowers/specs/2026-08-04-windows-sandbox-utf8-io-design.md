# Windows Sandbox UTF-8 I/O Reliability Design

## Goal

Make safe-mode filesystem operations preserve Unicode text exactly on every Windows locale, including Chinese Windows where the default process code page is GBK. A rejected payload must not truncate or otherwise mutate the target file.

## Root cause

The Windows backend already serializes filesystem-worker JSON as UTF-8 bytes. The restricted filesystem worker currently reads standard input through Python's locale-dependent text wrapper and emits JSON through locale-dependent `print`. On a GBK host, UTF-8 request bytes can be decoded incorrectly and Unicode response text such as emoji cannot be emitted. Corrupted surrogate code points then fail during the final UTF-8 file write, after `Path.write_text` has opened and truncated the destination.

## Design

- Treat the filesystem-worker protocol as explicitly UTF-8 in both directions.
- Read standard input from `sys.stdin.buffer` and decode with strict UTF-8.
- Serialize response/error JSON and write UTF-8 bytes to `sys.stdout.buffer` or `sys.stderr.buffer`.
- Retain a text-stream fallback for in-process tests and embedders that replace standard streams with `StringIO`; this fallback never participates in the packaged Windows worker path.
- Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` in the restricted worker environment as defense in depth. Protocol correctness does not depend on these variables.
- Encode replacement text before opening the destination. `write_text` and `edit_text` write the validated bytes through a same-directory temporary file and replace the destination only after the temporary write succeeds. Temporary files are removed after a failed write.
- Preflight source creation and patch content as UTF-8 before any destination mutation. Existing revision, path-policy, ACL, and exclusive-create behavior remains unchanged.

## Compatibility and security

- User files remain ordinary UTF-8 files; no BOM, base64 wrapper, or new on-disk format is introduced.
- Existing JSON request/response shapes remain unchanged.
- Existing safe-mode path validation and restricted-token execution remain authoritative.
- Same-directory replacement avoids cross-volume moves and does not follow a destination symlink during replacement.
- Full Access behavior is unchanged.

## Testing

- Feed UTF-8 JSON containing Chinese and emoji through a binary stdin while exposing a GBK text wrapper; verify exact payload recovery.
- Emit a read result containing emoji through a GBK text wrapper; verify stdout contains valid UTF-8 JSON.
- Verify `write_text` and `edit_text` preserve the original file when content contains an invalid lone surrogate.
- Verify valid Chinese and emoji content is written exactly.
- Verify the Windows worker request advertises UTF-8 mode.
- Run the complete filesystem-worker and Windows backend test modules, then a real restricted Windows safe-mode write/read probe.

