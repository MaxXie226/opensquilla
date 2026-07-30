from __future__ import annotations

import pytest

from opensquilla.sandbox.command_policy import (
    CommandAction,
    decide_command,
    decide_shell_command,
    parse_shell_segments,
    validate_command_prefix,
)
from opensquilla.sandbox.policy_models import SandboxPolicy


def test_auto_allow_beats_approval_and_builtin_high_risk() -> None:
    policy = SandboxPolicy()
    policy.commands.auto_allow_prefixes = [["git", "push"]]
    policy.commands.require_approval_prefixes = [["git"]]

    decision = decide_command(["git", "push", "origin", "main"], policy)

    assert decision.action is CommandAction.AUTO
    assert decision.code == "user_auto_allow"


def test_git_push_requires_approval_by_default() -> None:
    decision = decide_command(["git", "push"], SandboxPolicy())

    assert decision.action is CommandAction.APPROVAL
    assert decision.code == "builtin_git_push"


def test_compound_command_uses_strictest_segment() -> None:
    decision = decide_shell_command(
        "python build.py && git push origin main",
        SandboxPolicy(),
        platform="linux",
    )

    assert decision.action is CommandAction.APPROVAL
    assert decision.code == "builtin_git_push"


def test_quoted_control_character_does_not_split_segment() -> None:
    segments = parse_shell_segments(
        'python -c "print(\'a;b\')" && node build.js',
        platform="linux",
    )

    assert len(segments) == 2
    assert segments[0].argv[0] == "python"
    assert segments[1].argv == ("node", "build.js")


def test_shell_wrapper_is_unwrapped_for_matching() -> None:
    decision = decide_shell_command(
        'bash -lc "git push origin main"',
        SandboxPolicy(),
        platform="linux",
    )

    assert decision.action is CommandAction.APPROVAL


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("auto", "auto"), ("prompt", "approval"), ("disabled", "deny")],
)
def test_system_tool_tri_state(mode: str, expected: str) -> None:
    policy = SandboxPolicy()
    policy.commands.system_tools = mode  # type: ignore[assignment]

    assert decide_command(["wsl", "--status"], policy, platform="windows").action == expected


def test_system_tool_disabled_cannot_be_overridden_by_auto_prefix() -> None:
    policy = SandboxPolicy()
    policy.commands.system_tools = "disabled"
    policy.commands.auto_allow_prefixes = [["wsl"]]

    assert (
        decide_command(["wsl", "--status"], policy, platform="windows").action
        is CommandAction.DENY
    )


def test_windows_executable_matching_is_case_insensitive_and_strips_exe() -> None:
    policy = SandboxPolicy()
    policy.commands.require_approval_prefixes = [["Git", "status"]]

    decision = decide_command(
        [r"C:\Program Files\Git\bin\GIT.EXE", "STATUS"],
        policy,
        platform="windows",
    )

    assert decision.action is CommandAction.APPROVAL


def test_rule_rejects_shell_control_tokens() -> None:
    with pytest.raises(ValueError):
        validate_command_prefix(["git", "status;"])
