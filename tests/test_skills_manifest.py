from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.manifest import (
    MAX_STANDARD_SKILL_DESCRIPTION_LENGTH,
    compile_skill_manifest,
    validate_hub_candidate,
)
from opensquilla.skills.types import SkillLayer


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "Portable test skill.",
    extra: str = "",
    body: str = "Use the test workflow.",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name if name is not None else directory}\n"
        f"description: {json.dumps(description)}\n"
        f"{extra}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_shared_compiler_and_strict_candidate_accept_portable_manifest(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "portable-skill",
        extra=(
            "user-invocable: true\n"
            "disable-model-invocation: false\n"
            "vendor-extension: retained\n"
            "metadata:\n"
            "  openclaw:\n"
            "    requires:\n"
            "      bins: [python3]\n"
        ),
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="portable-skill",
    )

    assert validation.ok is True
    assert validation.diagnostics == ()
    assert validation.spec is not None
    assert validation.spec.name == "portable-skill"
    assert validation.spec.layer is SkillLayer.MANAGED
    assert validation.spec.instance_id.startswith("managed:")
    assert validation.spec.metadata is not None
    assert validation.spec.metadata.requires is not None
    assert validation.spec.metadata.requires.bins == ["python3"]

    compiled = compile_skill_manifest(skill_dir, SkillLayer.MANAGED)
    assert compiled == validation.spec


@pytest.mark.parametrize(
    "name",
    [
        "Uppercase",
        "leading-",
        "two--hyphens",
        "contains_underscore",
        "a" * 65,
    ],
)
def test_strict_candidate_rejects_noncanonical_agent_skill_names(
    tmp_path: Path,
    name: str,
) -> None:
    skill_dir = _write_skill(tmp_path, name, name=name)

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is False
    assert validation.spec is None
    assert any(item["code"] == "NAME_INVALID" for item in validation.diagnostics)


def test_strict_candidate_rejects_name_mismatches_and_wrong_boolean_types(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "directory-name",
        name="manifest-name",
        extra='user-invocable: "false"\n',
    )

    validation = validate_hub_candidate(
        skill_dir,
        expected_name="source-name",
    )

    codes = {item["code"] for item in validation.diagnostics}
    assert validation.ok is False
    assert all(
        set(item) == {"code", "message", "path", "field"}
        for item in validation.diagnostics
    )
    assert "NAME_DIRECTORY_MISMATCH" in codes
    assert "NAME_SOURCE_MISMATCH" in codes
    assert any(
        item["field"] == "user-invocable" and item["code"] == "FIELD_TYPE_INVALID"
        for item in validation.diagnostics
    )


def test_strict_candidate_rejects_missing_or_ambiguous_frontmatter(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "SKILL.md").write_text("No frontmatter", encoding="utf-8")
    assert validate_hub_candidate(missing).diagnostics[0]["code"] == "FRONTMATTER_INVALID"

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text(
        "---\n"
        "name: duplicate\n"
        "name: other\n"
        "description: Duplicate key must not be ambiguous.\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    result = validate_hub_candidate(duplicate)
    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "duplicate key" in result.diagnostics[0]["message"]


def test_strict_candidate_quickly_rejects_yaml_alias_expansion(
    tmp_path: Path,
) -> None:
    width = 10
    depth = 8
    aliases = ["alias0: &alias0 [value]"]
    aliases.extend(
        f"alias{level}: &alias{level} ["
        + ", ".join([f"*alias{level - 1}"] * width)
        + "]"
        for level in range(1, depth + 1)
    )
    aliases.append(f"context: *alias{depth}")
    skill_dir = _write_skill(
        tmp_path,
        "alias-expansion",
        extra="\n".join(aliases) + "\n",
    )

    started = time.monotonic()
    result = validate_hub_candidate(skill_dir)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "aliases are not allowed" in result.diagnostics[0]["message"]


def test_strict_candidate_rejects_excessive_yaml_nesting(tmp_path: Path) -> None:
    nested = "[" * 80 + "value" + "]" * 80
    skill_dir = _write_skill(
        tmp_path,
        "deep-frontmatter",
        extra=f"vendor-extension: {nested}\n",
    )

    result = validate_hub_candidate(skill_dir)

    assert result.ok is False
    assert result.diagnostics[0]["code"] == "FRONTMATTER_INVALID"
    assert "maximum YAML nesting depth" in result.diagnostics[0]["message"]


def test_tolerant_compiler_keeps_legacy_yaml_alias_support(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "legacy-alias",
        extra="vendor-defaults: &defaults [one, two]\nvendor-copy: *defaults\n",
    )

    compiled = compile_skill_manifest(skill_dir, SkillLayer.BUNDLED)

    assert compiled.name == "legacy-alias"


def test_strict_candidate_enforces_standard_description_limit(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "long-description",
        description="x" * (MAX_STANDARD_SKILL_DESCRIPTION_LENGTH + 1),
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is False
    assert any(item["code"] == "DESCRIPTION_INVALID" for item in validation.diagnostics)


@pytest.mark.parametrize(
    ("metadata", "field"),
    [
        ("requires: [python3]\n", "metadata.openclaw.requires"),
        ("requires:\n        bins: python3\n", "metadata.openclaw.requires.bins"),
        ("requires:\n        commands: python3\n", "metadata.openclaw.requires.commands"),
        ('always: "yes"\n', "metadata.openclaw.always"),
        ("os: linux\n", "metadata.openclaw.os"),
        ("install: {kind: uv}\n", "metadata.openclaw.install"),
        (
            "install:\n        - kind: uv\n          bins: python3\n",
            "metadata.openclaw.install[0].bins",
        ),
    ],
)
def test_strict_candidate_rejects_known_metadata_type_pollution(
    tmp_path: Path,
    metadata: str,
    field: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "typed-metadata",
        extra="metadata:\n  openclaw:\n    " + metadata,
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is False
    assert any(
        item["code"] == "FIELD_TYPE_INVALID" and item["field"] == field
        for item in validation.diagnostics
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hooks", "{}"),
        ("context", "fork"),
        ("agent", "general-purpose"),
        ("plugin", "vendor/plugin"),
        ("mcpServers", "{}"),
        ("command-dispatch", "tool"),
        ("entrypoint", "{command: 'python run.py'}"),
        ("kind", "meta"),
        ("composition", "{}"),
    ],
)
def test_strict_candidate_rejects_unsupported_execution_dialect_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dialect-skill",
        extra=f"{field}: {value}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is False
    assert any(
        diagnostic["code"] == "DIALECT_FIELD_UNSUPPORTED"
        and diagnostic["field"] == field
        for diagnostic in validation.diagnostics
    )


def test_strict_candidate_accepts_allowed_tools_as_degraded_compatibility(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "tool-preapproval-skill",
        extra="allowed-tools: Bash(npx example@latest *)\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert validation.compatibility_diagnostics == (
        {
            "code": "TOOL_PREAPPROVAL_IGNORED",
            "message": (
                "allowed-tools requests dialect-specific tool preapproval; "
                "OpenSquilla will keep its normal tool approval policy"
            ),
            "path": str(skill_dir / "SKILL.md"),
            "field": "allowed-tools",
        },
    )


@pytest.mark.parametrize(
    ("extra", "expected_field"),
    [
        ("allowed_tools: Read\n", "allowed_tools"),
        (
            "metadata:\n  openclaw:\n    allowed-tools: Bash(npx example@latest *)\n",
            "metadata.openclaw.allowed-tools",
        ),
    ],
)
def test_tool_preapproval_aliases_remain_nonblocking_degradations(
    tmp_path: Path,
    extra: str,
    expected_field: str,
) -> None:
    skill_dir = _write_skill(tmp_path, "tool-preapproval-alias", extra=extra)

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert any(
        item["code"] == "TOOL_PREAPPROVAL_IGNORED"
        and item["field"] == expected_field
        for item in validation.compatibility_diagnostics
    )


def test_tool_preapproval_warning_does_not_dilute_blocking_execution_field(
    tmp_path: Path,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "mixed-dialect-skill",
        extra="allowed-tools: Bash\nhooks: {}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is False
    assert validation.spec is None
    assert any(
        item["code"] == "DIALECT_FIELD_UNSUPPORTED" and item["field"] == "hooks"
        for item in validation.diagnostics
    )
    assert any(
        item["code"] == "TOOL_PREAPPROVAL_IGNORED"
        for item in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize(
    "body",
    [
        "Project context: !`npx example@latest info --json`",
        "```!\nnode --version\nnpm --version\n```",
    ],
)
def test_dynamic_shell_context_is_a_nonblocking_degradation(
    tmp_path: Path,
    body: str,
) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-context-skill",
        body=body,
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert validation.spec is not None
    assert validation.diagnostics == ()
    assert any(
        item["code"] == "DYNAMIC_CONTEXT_UNSUPPORTED"
        and item["field"] == "body.dynamic-context"
        for item in validation.compatibility_diagnostics
    )


def test_fenced_dynamic_shell_context_is_detected_with_crlf(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-context-skill",
        body="```!\nnode --version\nnpm --version\n```",
    )
    manifest = skill_dir / "SKILL.md"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("\n", "\r\n"),
        encoding="utf-8",
        newline="",
    )

    validation = validate_hub_candidate(skill_dir)

    assert validation.ok is True
    assert any(
        item["code"] == "DYNAMIC_CONTEXT_UNSUPPORTED"
        for item in validation.compatibility_diagnostics
    )


@pytest.mark.parametrize(
    ("namespace", "field", "value"),
    [
        ("", "command", "python run.py"),
        ("platform", "plugin", "vendor/plugin"),
        ("openclaw", "mcpServers", "{}"),
        ("clawdbot", "hooks", "{}"),
        ("opensquilla", "context", "fork"),
    ],
)
def test_strict_candidate_rejects_nested_platform_execution_fields(
    tmp_path: Path,
    namespace: str,
    field: str,
    value: str,
) -> None:
    prefix = f"  {namespace}:\n    " if namespace else "  "
    skill_dir = _write_skill(
        tmp_path,
        "dialect-skill",
        extra=f"metadata:\n{prefix}{field}: {value}\n",
    )

    validation = validate_hub_candidate(skill_dir)

    expected_field = f"metadata.{namespace}.{field}" if namespace else f"metadata.{field}"
    assert validation.ok is False
    assert any(
        diagnostic["code"] == "DIALECT_FIELD_UNSUPPORTED"
        and diagnostic["field"] == expected_field
        for diagnostic in validation.diagnostics
    )


@pytest.mark.parametrize("namespace", ["", "openclaw", "opensquilla"])
def test_strict_candidate_rejects_dynamic_command_inside_install_item(
    tmp_path: Path,
    namespace: str,
) -> None:
    metadata = (
        f"metadata:\n  {namespace}:\n    install:\n"
        "      - kind: uv\n"
        "        command: python installer.py\n"
        if namespace
        else (
            "metadata:\n  install:\n"
            "    - kind: uv\n"
            "      command: python installer.py\n"
        )
    )
    skill_dir = _write_skill(
        tmp_path,
        "dynamic-installer",
        extra=metadata,
    )

    validation = validate_hub_candidate(skill_dir)

    field_prefix = f"metadata.{namespace}" if namespace else "metadata"
    assert validation.ok is False
    assert any(
        item["code"] == "DIALECT_FIELD_UNSUPPORTED"
        and item["field"] == f"{field_prefix}.install[0].command"
        for item in validation.diagnostics
    )


def test_existing_loader_layers_remain_tolerant_of_legacy_uppercase_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundled"
    skill_dir = _write_skill(
        root,
        "AwesomeLegacySkill",
        name="AwesomeLegacySkill",
        extra='user-invocable: "false"\n',
    )
    loader = SkillLoader(
        bundled_dir=root,
        snapshot_path=tmp_path / "snapshot.json",
    )

    loaded = loader.get_by_name("AwesomeLegacySkill")

    assert loaded is not None
    # Historical tolerant conversion remains unchanged: a non-empty string is
    # truthy to existing consumers. Only new Hub candidates reject this type.
    assert loaded.user_invocable == "false"
    assert loaded.instance_id.startswith("bundled:")
    assert validate_hub_candidate(skill_dir).ok is False


def test_catalog_exposes_candidates_shadowed_instances_and_diagnostics(
    tmp_path: Path,
) -> None:
    extra = tmp_path / "extra"
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    _write_skill(extra, "alpha", description="extra")
    _write_skill(managed, "alpha", description="managed")
    _write_skill(workspace, "alpha", description="workspace")
    broken = workspace / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    loader = SkillLoader(
        extra_dirs=[extra],
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    result = loader.reload(reason="test")
    snapshot = loader.snapshot()

    assert result.partial is True
    assert [skill.description for skill in snapshot.skills] == ["workspace"]
    assert [skill.description for skill in snapshot.candidates] == [
        "extra",
        "managed",
        "workspace",
    ]
    assert [skill.description for skill in snapshot.shadowed] == ["extra", "managed"]
    assert len({skill.instance_id for skill in snapshot.candidates}) == 3
    assert snapshot.diagnostics == snapshot.errors
    assert snapshot.get_candidate_by_instance_id(snapshot.shadowed[0].instance_id) is not None


def test_v14_snapshot_round_trips_candidate_view_and_invalidates_v13(
    tmp_path: Path,
) -> None:
    low = tmp_path / "low"
    high = tmp_path / "high"
    _write_skill(low, "alpha", description="low")
    _write_skill(high, "alpha", description="high")
    snapshot_path = tmp_path / "snapshot.json"

    loader = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    loader.load_all()
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert data["version"] == 14
    assert len(data["candidates"]) == 2
    assert len(data["shadowed"]) == 1

    restored = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    restored.load_all()
    assert [skill.description for skill in restored.snapshot().candidates] == [
        "low",
        "high",
    ]
    assert [skill.description for skill in restored.snapshot().shadowed] == ["low"]

    # A real v13 cache had only the active set. It must miss the cache so the
    # complete physical candidate/shadow view is rebuilt from source roots.
    data["version"] = 13
    data.pop("candidates", None)
    data.pop("shadowed", None)
    data.pop("diagnostics", None)
    for row in data["skills"]:
        row.pop("instance_id", None)
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    legacy = SkillLoader(
        extra_dirs=[low],
        workspace_dir=high,
        snapshot_path=snapshot_path,
    )
    assert legacy.load_snapshot() is None
    assert [skill.description for skill in legacy.load_all()] == ["high"]
    assert [skill.description for skill in legacy.snapshot().candidates] == [
        "low",
        "high",
    ]
    assert [skill.description for skill in legacy.snapshot().shadowed] == ["low"]
