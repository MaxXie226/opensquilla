from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from opensquilla.sandbox.upgrade_migration import SandboxUpgradeCoordinator


@pytest.mark.parametrize(
    ("legacy_mode", "canonical"),
    [
        ("standard", "safe"),
        ("trusted", "safe"),
        ("managed", "safe"),
        ("full", "full"),
    ],
)
def test_direct_update_preserves_mode_comments_and_unknown_fields(
    tmp_path: Path,
    legacy_mode: str,
    canonical: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
# retained comment
unknown_top = "keep"

[sandbox]
run_mode = "{legacy_mode}" # retained inline
mystery = 42
""".lstrip(),
        encoding="utf-8",
    )
    preferences = tmp_path / "desktop-preferences.json"
    preferences.write_text(
        json.dumps({"runMode": legacy_mode, "unknown": {"keep": True}}),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.canonical_mode == canonical
    text = config.read_text(encoding="utf-8")
    assert "# retained comment" in text
    assert "# retained inline" in text
    parsed = tomllib.loads(text)
    assert parsed["sandbox"]["run_mode"] == canonical
    assert parsed["sandbox"]["mystery"] == 42
    assert parsed["unknown_top"] == "keep"
    assert json.loads(preferences.read_text()) == {
        "runMode": canonical,
        "unknown": {"keep": True},
    }


def test_direct_update_is_idempotent_and_keeps_one_snapshot(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )
    database = tmp_path / "state" / "sessions.db"
    database.parent.mkdir()
    database.write_bytes(b"legacy-database")

    first = SandboxUpgradeCoordinator(tmp_path).run()
    second = SandboxUpgradeCoordinator(tmp_path).run()

    assert first.ok and second.ok
    assert second.status == "committed"
    assert (tmp_path / ".sandbox-upgrade-snapshot" / "state" / "sessions.db").read_bytes() == (
        b"legacy-database"
    )
    assert len(list(tmp_path.glob(".sandbox-upgrade-snapshot*"))) == 1
