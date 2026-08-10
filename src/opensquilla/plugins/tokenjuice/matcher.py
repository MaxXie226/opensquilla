from __future__ import annotations

import os
import re
import shlex
from typing import Any

from .types import Rule

# Strict matching is the safe default: every declared rule criterion must
# match.  An explicit falsy value retains the old permissive matcher as an
# emergency rollback.  cd unwrapping remains opt-in and is only meaningful
# together with strict matching.
_MATCHER_STRICT_ENV = "OPENSQUILLA_TOOLCOMP_MATCHER_STRICT"
_CD_UNWRAP_ENV = "OPENSQUILLA_TOOLCOMP_CD_UNWRAP"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "off", "no", "disabled"})

# OpenSquilla's command-bearing shell tool.  A field named ``command`` on an
# arbitrary tool is not proof that its result is shell output.
_SHELL_TOOL_NAMES = frozenset({"exec_command"})

# Specialized reducers assume that the result belongs to one command.  Shell
# composition can mix unrelated output into that result, so composite syntax
# must use the generic fallback instead.  Quoted and escaped characters are
# literals and do not make a command composite.
_COMPOSITE_SHELL_CHARS = frozenset({"|", "&", ";", "<", ">", "(", ")", "\n", "\r"})

# Git global options that consume the next argv entry; subcommand extraction
# must skip them (and their inline `--opt=value` forms) to find the verb.
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--super-prefix",
        "--config-env",
    }
)
_GIT_GLOBAL_OPTION_INLINE_PREFIXES = (
    "--git-dir=",
    "--work-tree=",
    "--namespace=",
    "--exec-path=",
    "--super-prefix=",
    "--config-env=",
)

# Only horizontal whitespace may separate the keyword from its argument: a
# real shell terminates a bare `cd` statement at an unquoted newline.
_LEADING_CD_PATTERN = re.compile(r"^\s*(?:cd|pushd)[ \t]+")
_CD_ARG_STOP_CHARS = frozenset({"&", "|", ";", "<", ">", "\n"})
_WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


def _matcher_strict_enabled() -> bool:
    raw_value = os.environ.get(_MATCHER_STRICT_ENV)
    if raw_value is None:
        return True
    raw = raw_value.strip().lower()
    # Unknown values fail safe.  A typo must not silently restore the
    # permissive matcher; only an explicit falsy value is a rollback request.
    return raw not in _FALSE_ENV_VALUES


def _cd_unwrap_enabled() -> bool:
    raw = os.environ.get(_CD_UNWRAP_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def command_argv(command: str | None, argv: list[str] | None = None) -> list[str]:
    if argv:
        return argv
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _is_simple_shell_command(command: str) -> bool:
    """Return whether *command* is one parseable, non-composite shell command."""

    quote: str | None = None
    escaping = False
    index = 0
    while index < len(command):
        char = command[index]

        # In POSIX shell syntax, every character inside single quotes is
        # literal, including backslashes.
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue

        if escaping:
            escaping = False
            index += 1
            continue
        if char == "\\":
            escaping = True
            index += 1
            continue

        if quote == '"':
            if char == '"':
                quote = None
            elif char == "`" or command[index : index + 2] == "$(":
                return False
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in _COMPOSITE_SHELL_CHARS:
            return False
        if char == "`" or command[index : index + 2] == "$(":
            return False
        index += 1

    if quote is not None or escaping:
        return False
    try:
        return bool(shlex.split(command))
    except ValueError:
        return False


def _generic_fallback_rule(rules: tuple[Rule, ...]) -> Rule | None:
    return next((rule for rule in rules if rule.id == "generic/fallback"), None)


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return [item for item in value]


def _list_of_string_lists(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    return [
        [item for item in entry if isinstance(item, str)]
        for entry in value
        if isinstance(entry, list)
    ]


def _contains_all(argv: list[str], needles: list[str]) -> bool:
    return all(needle in argv for needle in needles)


def _contains_command_text(command: str, needles: list[str]) -> bool:
    lowered = command.lower()
    return all(needle.lower() in lowered for needle in needles)


def _command_name(argv: list[str]) -> str | None:
    first = argv[0] if argv else None
    if not first:
        return None
    if first[:1] in {"'", '"'}:
        first = first[1:]
    if first[-1:] in {"'", '"'}:
        first = first[:-1]
    return os.path.basename(first)


def _git_subcommand(argv: list[str]) -> str | None:
    if _command_name(argv) != "git":
        return None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if not arg:
            index += 1
            continue
        if arg in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith(_GIT_GLOBAL_OPTION_INLINE_PREFIXES):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def strip_leading_cd_prefix(command: str) -> str:
    current = command.strip()
    for _ in range(8):
        unwrapped = _match_leading_cd_chain(current)
        if unwrapped is None:
            return current
        current = unwrapped
    return current


def _looks_like_windows_cd_arg(raw_arg: str) -> bool:
    value = raw_arg.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return bool(_WINDOWS_DRIVE_PATH_PATTERN.match(value) or value.startswith("\\\\"))


def _match_leading_cd_chain(command: str) -> str | None:
    keyword = _LEADING_CD_PATTERN.match(command)
    if keyword is None:
        return None

    # The cd argument must be a single shell token: quoting and escapes are
    # honoured, but an unquoted operator or redirection makes the prefix
    # unsafe to strip, so the command is left untouched.
    index = keyword.end()
    arg_start = index
    quote: str | None = None
    escaping = False
    saw_arg = False
    while index < len(command):
        char = command[index]
        if escaping:
            escaping = False
        elif char == "\\":
            escaping = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in _CD_ARG_STOP_CHARS:
            return None
        elif char.isspace():
            break
        saw_arg = True
        index += 1

    if not saw_arg:
        return None
    if _looks_like_windows_cd_arg(command[arg_start:index]):
        return None

    while index < len(command) and command[index] in " \t":
        index += 1

    if command[index : index + 2] != "&&":
        return None
    tail = command[index + 2 :].strip()
    return tail or None


def rule_matches(
    rule: Rule,
    *,
    tool_name: str,
    command: str | None,
    argv: list[str] | None,
    content: str,
    exit_code: int,
) -> bool:
    match = rule.match
    if not match:
        return True

    normalized_tool = "exec" if command and tool_name in _SHELL_TOOL_NAMES else tool_name
    tool_names = _list_of_strings(match.get("toolNames"))
    if tool_names:
        if tool_name in _SHELL_TOOL_NAMES:
            tool_matches = normalized_tool in tool_names or tool_name in tool_names
        else:
            # ``exec`` is the rule namespace for the canonical shell tool, not
            # an accepted compatibility alias for a runtime tool named exec.
            tool_matches = tool_name in (set(tool_names) - {"exec"})
        if not tool_matches:
            return False

    # A non-shell tool may legitimately define its own command/argv matcher,
    # but only when the rule explicitly names that tool.  Unscoped git rules
    # must not consume an arbitrary tool's command-shaped argument.
    command_match_allowed = tool_name in _SHELL_TOOL_NAMES or bool(tool_names)
    tokens = command_argv(command, argv) if command_match_allowed else []
    argv0 = _list_of_strings(match.get("argv0"))
    if argv0 and (not tokens or tokens[0] not in argv0):
        return False

    git_subcommands = _list_of_strings(match.get("gitSubcommands"))
    if (
        git_subcommands
        and _matcher_strict_enabled()
        and (_git_subcommand(tokens) or "") not in git_subcommands
    ):
        return False

    argv_includes = _list_of_string_lists(match.get("argvIncludes"))
    if argv_includes and not any(_contains_all(tokens, entry) for entry in argv_includes):
        return False

    argv_includes_any = _list_of_string_lists(match.get("argvIncludesAny"))
    if (
        argv_includes_any
        and _matcher_strict_enabled()
        and not any(_contains_all(tokens, entry) for entry in argv_includes_any)
    ):
        return False

    command_text = (command or " ".join(tokens)) if command_match_allowed else ""
    command_includes = _list_of_strings(match.get("commandIncludes"))
    if command_includes and not _contains_command_text(command_text, command_includes):
        return False

    command_includes_any = _list_of_strings(match.get("commandIncludesAny"))
    if command_includes_any and not any(
        needle.lower() in command_text.lower() for needle in command_includes_any
    ):
        return False

    command_regex = match.get("commandRegex")
    if isinstance(command_regex, str) and not re.search(command_regex, command_text):
        return False

    exit_codes = match.get("exitCodes")
    if isinstance(exit_codes, list) and exit_codes and exit_code not in exit_codes:
        return False

    output_regex = match.get("outputRegex")
    if isinstance(output_regex, str) and not re.search(output_regex, content, re.MULTILINE):
        return False

    return True


def select_rule(
    rules: tuple[Rule, ...],
    *,
    tool_name: str,
    command: str | None,
    argv: list[str] | None,
    content: str,
    exit_code: int,
) -> Rule | None:
    if tool_name in _SHELL_TOOL_NAMES:
        # The canonical shell tool always carries a command.  Missing command
        # data cannot safely select a command-specific reducer.
        if not command:
            return _generic_fallback_rule(rules)

        # cd unwrapping remains opt-in.  Refuse the unsafe historical
        # combination where unwrapping fed a permissive matcher and selected
        # an unrelated rule.
        if _cd_unwrap_enabled() and _matcher_strict_enabled():
            unwrapped = strip_leading_cd_prefix(command)
            if unwrapped != command.strip():
                command = unwrapped
                argv = command_argv(unwrapped, None)

        # A specialized reducer is safe only when all output belongs to one
        # parseable command.  This also guards malformed tails produced by the
        # optional cd unwrapping path.
        if not _is_simple_shell_command(command):
            return _generic_fallback_rule(rules)

    for rule in rules:
        if rule_matches(
            rule,
            tool_name=tool_name,
            command=command,
            argv=argv,
            content=content,
            exit_code=exit_code,
        ):
            return rule
    return None
