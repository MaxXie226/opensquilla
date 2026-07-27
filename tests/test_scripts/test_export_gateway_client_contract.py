"""CLI behavior for the deterministic Gateway client-contract exporter."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from scripts.export_gateway_client_contract import (
    check_contract,
    main,
    write_contract,
)


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_export_is_byte_identical_across_repeated_runs(tmp_path: Path) -> None:
    output_dir = tmp_path / "contract"

    first = write_contract(output_dir)
    first_inventory = _inventory(output_dir)
    second = write_contract(output_dir)

    assert first.ok
    assert second.ok
    assert _inventory(output_dir) == first_inventory
    assert check_contract(output_dir).ok
    if os.name != "nt":
        for path in output_dir.rglob("*.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_check_detects_changed_missing_and_unexpected_without_writing(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "contract"
    assert write_contract(output_dir).ok

    changed = output_dir / "events.json"
    changed.write_text("{}\n", encoding="utf-8")
    missing = output_dir / "golden" / "error.json"
    missing.unlink()
    unexpected = output_dir / "unexpected.json"
    unexpected.write_text("{}\n", encoding="utf-8")
    before = _inventory(output_dir)

    result = check_contract(output_dir)

    assert [path.as_posix() for path in result.changed] == ["events.json"]
    assert [path.as_posix() for path in result.missing] == ["golden/error.json"]
    assert [path.as_posix() for path in result.unexpected] == ["unexpected.json"]
    assert _inventory(output_dir) == before


def test_main_check_returns_nonzero_for_drift_and_never_repairs_it(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "contract"
    assert main(["--output-dir", str(output_dir)]) == 0
    target = output_dir / "rpc-methods.json"
    target.write_text("{}\n", encoding="utf-8")

    assert main(["--check", "--output-dir", str(output_dir)]) == 1
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_update_preserves_unexpected_files_for_explicit_review(tmp_path: Path) -> None:
    output_dir = tmp_path / "contract"
    assert write_contract(output_dir).ok
    extra = output_dir / "operator-note.txt"
    extra.write_text("keep me\n", encoding="utf-8")

    result = write_contract(output_dir)

    assert not result.ok
    assert [path.as_posix() for path in result.unexpected] == ["operator-note.txt"]
    assert extra.read_text(encoding="utf-8") == "keep me\n"
