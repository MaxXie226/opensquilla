from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.skills.hub import lockfile as lockfile_module
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillCompatibilityState,
    SkillDiagnostic,
    SkillInstallState,
    SkillInvocationCapabilities,
    SkillLifecycle,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.lockfile import (
    LOCKFILE_SCHEMA_VERSION,
    LockEntry,
    Lockfile,
    LockfileMutationBlockedError,
    lockfile_backup_path,
)


def test_v1_load_save_migrates_to_v2_and_preserves_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "skills-lock.json"
    original = {
        "version": 1,
        "generated_by": {"vendor": "future-writer"},
        "installed": {
            "demo": {
                "source": "clawhub",
                "identifier": "owner/demo",
                "version": "1.2.3",
                "opaque_entry_field": {"keep": True},
            }
        },
    }
    original_bytes = (json.dumps(original, indent=4) + "\n").encode()
    path.write_bytes(original_bytes)

    lockfile = Lockfile.load(path)

    assert lockfile.version == LOCKFILE_SCHEMA_VERSION
    assert lockfile.loaded_version == 1
    assert lockfile.mutation_blocked is False
    assert lockfile.extra == {"generated_by": {"vendor": "future-writer"}}
    assert lockfile.get("demo") is not None
    assert lockfile.get("demo").extra == {"opaque_entry_field": {"keep": True}}

    lockfile.add("second", LockEntry(source="github", identifier="owner/second"))
    lockfile.save(path)

    assert lockfile_backup_path(path).read_bytes() == original_bytes
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert list(saved["installed"]) == ["demo", "second"]
    assert saved["generated_by"] == {"vendor": "future-writer"}
    assert saved["installed"]["demo"]["opaque_entry_field"] == {"keep": True}


@pytest.mark.parametrize(
    ("payload", "expected_names"),
    [
        (
            {
                "skills": [
                    "plain",
                    {"name": "named", "source": "clawhub", "vendor": "keep"},
                    {"skill_name": "alias", "identifier": "owner/alias"},
                ]
            },
            {"plain", "named", "alias"},
        ),
        (
            {"version": "1", "skills": {"mapped": {"identifier": "owner/mapped"}}},
            {"mapped"},
        ),
    ],
)
def test_historical_skills_shapes_remain_readable(
    tmp_path: Path,
    payload: dict[str, object],
    expected_names: set[str],
) -> None:
    path = tmp_path / "skills-lock.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    lockfile = Lockfile.load(path)

    assert set(lockfile.installed) == expected_names
    assert lockfile.loaded_version == 1
    assert lockfile.mutation_blocked is False
    if "named" in lockfile.installed:
        assert lockfile.installed["named"].extra == {
            "name": "named",
            "vendor": "keep",
        }


def test_corrupt_lockfile_reports_diagnostic_and_blocks_all_mutations(tmp_path: Path) -> None:
    path = tmp_path / "skills-lock.json"
    original_bytes = b'{"version": 1, broken'
    path.write_bytes(original_bytes)

    lockfile = Lockfile.load(path)

    assert lockfile.mutation_blocked is True
    assert lockfile.diagnostics[0].code == "LOCKFILE_CORRUPT"
    assert lockfile.diagnostics[0].blocking is True
    with pytest.raises(LockfileMutationBlockedError):
        lockfile.add("demo", LockEntry(identifier="demo"))
    with pytest.raises(LockfileMutationBlockedError):
        lockfile.remove("demo")
    with pytest.raises(LockfileMutationBlockedError):
        lockfile.save(path)

    fresh = Lockfile(installed={"demo": LockEntry(identifier="demo")})
    with pytest.raises(LockfileMutationBlockedError):
        fresh.save(path)

    assert path.read_bytes() == original_bytes
    assert not lockfile_backup_path(path).exists()


def test_future_lockfile_is_loss_aware_but_mutation_blocked(tmp_path: Path) -> None:
    path = tmp_path / "skills-lock.json"
    path.write_text(
        json.dumps(
            {
                "version": LOCKFILE_SCHEMA_VERSION + 1,
                "future_top_level": [1, 2, 3],
                "installed": {
                    "demo": {
                        "identifier": "demo",
                        "future_entry_field": {"opaque": True},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    lockfile = Lockfile.load(path)

    assert lockfile.extra == {"future_top_level": [1, 2, 3]}
    assert lockfile.get("demo") is not None
    assert lockfile.get("demo").extra == {"future_entry_field": {"opaque": True}}
    assert lockfile.mutation_blocked is True
    assert {item.code for item in lockfile.diagnostics} == {
        "LOCKFILE_VERSION_UNSUPPORTED"
    }
    with pytest.raises(LockfileMutationBlockedError):
        lockfile.remove("demo")


def test_source_index_is_rebuilt_without_trusting_stale_targets(tmp_path: Path) -> None:
    path = tmp_path / "skills-lock.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "installed": {
                    "demo": {
                        "source": "clawhub",
                        "identifier": "requested-demo",
                        "resolved_identifier": "owner/canonical-demo",
                        "install_id": "install-demo",
                    },
                    "legacy": {
                        "source": "github",
                        "identifier": "owner/legacy",
                    },
                    "local": {"identifier": "no-source"},
                },
                "source_index": {
                    "clawhub": {
                        "owner/canonical-demo": {
                            "name": "wrong-name",
                            "install_id": "wrong-id",
                            "future_index_field": {"keep": True},
                        },
                        "stale": {"name": "deleted", "install_id": "deleted-id"},
                    },
                    "deleted-source": {
                        "deleted": {"name": "deleted", "install_id": "deleted-id"}
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    lockfile = Lockfile.load(path)

    assert lockfile.source_index == {
        "clawhub": {
            "owner/canonical-demo": {
                "future_index_field": {"keep": True},
                "name": "demo",
                "install_id": "install-demo",
            }
        },
        "github": {
            "owner/legacy": {
                "name": "legacy",
                "install_id": "",
            }
        },
    }
    lockfile.save(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["source_index"] == lockfile.source_index


def test_atomic_save_keeps_primary_when_replacement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "skills-lock.json"
    initial = Lockfile(installed={"demo": LockEntry(identifier="demo")})
    initial.save(path)
    original_bytes = path.read_bytes()

    updated = Lockfile.load(path)
    updated.add("second", LockEntry(identifier="second"))
    real_replace = os.replace

    def fail_primary_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == path:
            raise OSError("synthetic replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(lockfile_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="synthetic replacement failure"):
        updated.save(path)

    assert path.read_bytes() == original_bytes
    assert lockfile_backup_path(path).read_bytes() == original_bytes
    assert list(tmp_path.glob(".skills-lock.json.*.tmp")) == []


def test_diagnostic_and_lifecycle_wire_contracts_are_stable() -> None:
    assert {state.value for state in SkillInstallState} == {
        "tracked",
        "untracked",
        "missing",
        "drifted",
    }
    assert SkillLoadState.VALIDATED_OFFLINE.value == "validated_offline"

    diagnostic = SkillDiagnostic(
        code="SKILL_SETUP_REQUIRED",
        severity=DiagnosticSeverity.WARNING,
        phase=DiagnosticPhase.READINESS,
        message="API key is missing",
        blocking=True,
        path="demo/SKILL.md",
        field_name="OPENAI_API_KEY",
        hint="Configure the environment variable.",
        details={"environment": "OPENAI_API_KEY"},
    )
    invocation = SkillInvocationCapabilities(
        model_catalog=True,
        skill_view=True,
        user_completion=True,
        direct_command=True,
        argument_substitution=True,
    )
    lifecycle = SkillLifecycle(
        install_state=SkillInstallState.TRACKED,
        load_state=SkillLoadState.LOADED,
        selection_state=SkillSelectionState.ACTIVE,
        compatibility_state=SkillCompatibilityState.NATIVE,
        readiness_state=SkillReadinessState.READY,
        invocation=invocation,
    )

    assert diagnostic.to_dict() == {
        "code": "SKILL_SETUP_REQUIRED",
        "severity": "warning",
        "phase": "readiness",
        "message": "API key is missing",
        "blocking": True,
        "path": "demo/SKILL.md",
        "field_name": "OPENAI_API_KEY",
        "hint": "Configure the environment variable.",
        "details": {"environment": "OPENAI_API_KEY"},
    }
    assert lifecycle.to_dict() == {
        "install_state": "tracked",
        "load_state": "loaded",
        "selection_state": "active",
        "compatibility_state": "native",
        "readiness_state": "ready",
        "invocation": {
            "model_catalog": True,
            "skill_view": True,
            "user_completion": True,
            "direct_command": True,
            "argument_substitution": True,
            "scoped_tool_permissions": False,
            "sandbox_execution": "unknown",
        },
        "usable": True,
    }
    assert replace(lifecycle, readiness_state=SkillReadinessState.UNKNOWN).usable is None
    assert replace(lifecycle, selection_state=SkillSelectionState.SHADOWED).usable is False
