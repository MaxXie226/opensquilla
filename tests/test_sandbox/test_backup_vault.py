from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.backup_vault import BackupTooLarge, BackupVault


def test_recursive_directory_backup_preserves_tree_and_manifest(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    target = tmp_path / "project"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "data.txt").write_text("important", encoding="utf-8")

    receipt = vault.backup(target, quota_bytes=1024)

    assert receipt.original_path == str(target.resolve())
    assert receipt.size_bytes >= len("important")
    assert (receipt.entry_path / "content" / "nested" / "data.txt").read_text(
        encoding="utf-8"
    ) == "important"
    assert (receipt.entry_path / "manifest.json").is_file()


def test_quota_evicts_oldest_committed_backup(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    first = vault.commit_bytes("first", b"a" * 8, quota_bytes=16, created_at=1)
    second = vault.commit_bytes("second", b"b" * 8, quota_bytes=16, created_at=2)

    vault.enforce_quota(8)

    assert not first.entry_path.exists()
    assert second.entry_path.exists()


def test_oversize_backup_does_not_evict_existing_entries(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    existing = vault.commit_bytes("existing", b"a" * 8, quota_bytes=8)
    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * 9)

    with pytest.raises(BackupTooLarge):
        vault.backup(target, quota_bytes=8)

    assert existing.entry_path.exists()


def test_staging_content_is_not_counted_as_committed_backup(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    target = tmp_path / "data.txt"
    target.write_text("hello", encoding="utf-8")

    staged = vault.stage(target)

    assert vault.list_receipts() == ()
    staged.discard()
    assert not staged.staging_path.exists()

