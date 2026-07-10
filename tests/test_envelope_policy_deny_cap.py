"""Policy-deny user_message cap override in the tool failure envelope."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.tools.envelope import (
    _policy_deny_max_chars,
    build_tool_failure_envelope,
)
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context
from opensquilla.tools.write_policy import (
    gate_workspace_scratch_artifact,
    gate_workspace_write_deny,
)

_LEVER_ENV = "OPENSQUILLA_TOOL_ENVELOPE_POLICY_DENY_MAX_CHARS"
_GUIDANCE_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE"
_TRUNCATION_MARKER = "...[truncated]"

_DENY_MESSAGE_BASE = (
    "write_file blocked by workspace write deny policy: "
    "config/pinned.txt matches config/*.txt."
)
_GUIDANCE_FILLER = (
    "Pinned configuration files are read-only in this workspace; add an "
    "override file under overrides/ and register it in manifest.toml so "
    "reviewers can trace the change. "
)

# Golden 500-char truncation of the 576-char deny message below, captured
# from the default-cap envelope so the lever-off path is pinned exactly.
_BASELINE_TRUNCATED_USER_MESSAGE = (
    "write_file blocked by workspace write deny policy: config/pinned.txt"
    " matches config/*.txt. Pinned configuration files are read-only in t"
    "his workspace; add an override file under overrides/ and register it"
    " in manifest.toml so reviewers can trace the change. Pinned configur"
    "ation files are read-only in this workspace; add an override file un"
    "der overrides/ and register it in manifest.toml so reviewers can tra"
    "ce the change. Pinned configuration files are read-only in this work"
    "space; add...[truncated]"
)


def _deny_guidance() -> str:
    needed = 576 - len(_DENY_MESSAGE_BASE) - 1
    return (_GUIDANCE_FILLER * (needed // len(_GUIDANCE_FILLER) + 1))[:needed]


def _deny_message() -> str:
    return f"{_DENY_MESSAGE_BASE} {_deny_guidance()}"


def _raise_write_deny_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SafeToolError:
    """Trip the real workspace write deny gate and return the raised error."""
    workspace = tmp_path.resolve()
    monkeypatch.setenv(_GUIDANCE_ENV, _deny_guidance())
    ctx = ToolContext(
        workspace_dir=str(workspace),
        workspace_write_deny_globs=["config/*.txt"],
    )
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(SafeToolError) as exc_info:
            gate_workspace_write_deny(
                "write_file",
                workspace / "config" / "pinned.txt",
                original_path="config/pinned.txt",
                workspace=workspace,
            )
    finally:
        current_tool_context.reset(token)
    return exc_info.value


def _raise_scratch_artifact_error(tmp_path: Path) -> SafeToolError:
    """Trip the real scratch-artifact gate and return the raised error."""
    workspace = tmp_path.resolve()
    scratch = workspace / "scratch"
    scratch.mkdir()
    ctx = ToolContext(workspace_dir=str(workspace), scratch_dir=str(scratch))
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(SafeToolError) as exc_info:
            gate_workspace_scratch_artifact(
                "write_file",
                workspace / "debug_probe.py",
                original_path="debug_probe.py",
                workspace=workspace,
            )
    finally:
        current_tool_context.reset(token)
    return exc_info.value


def test_fixture_message_shape() -> None:
    assert len(_deny_message()) == 576
    assert len(_BASELINE_TRUNCATED_USER_MESSAGE) == 500
    assert _BASELINE_TRUNCATED_USER_MESSAGE.endswith(_TRUNCATION_MARKER)


def test_unset_lever_matches_baseline_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_LEVER_ENV, raising=False)
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    assert str(error) == _deny_message()
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope == {
        "status": "error",
        "tool": "write_file",
        "error_class": "SafeToolError",
        "user_message": _BASELINE_TRUNCATED_USER_MESSAGE,
        "retry_allowed": False,
    }


@pytest.mark.parametrize("raw", ["", "  ", "0", "-1", "not-a-number", "12.5"])
def test_off_values_keep_baseline_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_LEVER_ENV, raw)
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope["user_message"] == _BASELINE_TRUNCATED_USER_MESSAGE


def test_lever_delivers_full_policy_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_LEVER_ENV, "1200")
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope["user_message"] == _deny_message()
    assert len(envelope["user_message"]) == 576
    assert _TRUNCATION_MARKER not in envelope["user_message"]


def test_lever_ignores_non_policy_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_LEVER_ENV, "1200")
    error = SafeToolError(_deny_message())
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope["user_message"] == _BASELINE_TRUNCATED_USER_MESSAGE


def test_lever_truncates_at_configured_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_LEVER_ENV, "550")
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    envelope = build_tool_failure_envelope(error, "write_file")
    expected = (
        _deny_message()[: 550 - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    )
    assert envelope["user_message"] == expected
    assert len(envelope["user_message"]) == 550


@pytest.mark.parametrize("raw", ["1", "5", "13", "14"])
def test_caps_below_marker_width_fall_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_LEVER_ENV, raw)
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope["user_message"] == _BASELINE_TRUNCATED_USER_MESSAGE
    assert len(envelope["user_message"]) <= 500


def test_smallest_active_cap_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_LEVER_ENV, str(len(_TRUNCATION_MARKER) + 1))
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    envelope = build_tool_failure_envelope(error, "write_file")
    assert envelope["user_message"] == _deny_message()[:1] + _TRUNCATION_MARKER
    assert len(envelope["user_message"]) == len(_TRUNCATION_MARKER) + 1


def test_lever_applies_to_scratch_artifact_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_LEVER_ENV, "120")
    error = _raise_scratch_artifact_error(tmp_path)
    assert getattr(error, "policy_gate_denial", False) is True
    envelope = build_tool_failure_envelope(error, "write_file")
    expected = str(error)[: 120 - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    assert envelope["user_message"] == expected
    assert len(envelope["user_message"]) == 120


def test_str_and_constructor_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _raise_write_deny_error(tmp_path, monkeypatch)
    message = _deny_message()
    assert str(error) == message
    assert error.args == (message,)
    assert error.user_message == message
    assert getattr(error, "policy_gate_denial", False) is True

    plain = SafeToolError("plain message")
    assert str(plain) == "plain message"
    assert plain.args == ("plain message",)
    assert getattr(plain, "policy_gate_denial", False) is False

    defaulted = SafeToolError()
    assert str(defaulted) == SafeToolError.user_message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 0),
        ("   ", 0),
        ("0", 0),
        ("-5", 0),
        ("abc", 0),
        ("12.5", 0),
        ("1200", 1200),
        (" 550 ", 550),
        ("1", 1),
    ],
)
def test_policy_deny_max_chars_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv(_LEVER_ENV, raw)
    assert _policy_deny_max_chars() == expected


def test_policy_deny_max_chars_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_LEVER_ENV, raising=False)
    assert _policy_deny_max_chars() == 0
