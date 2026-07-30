from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.guest_profile import (
    GuestProfileFactory,
    cleanup_guest_profile_root,
)
from opensquilla.sandbox.run_mode import RunMode


def test_guest_profile_mounts_only_temp_and_bundled_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = GuestProfileFactory.create(
        "task/unsafe",
        runtime_roots=(runtime,),
        temp_parent=tmp_path / "guests",
    )

    assert profile.host_home_mounted is False
    assert {mount.kind for mount in profile.mounts} == {
        "workspace",
        "bundled-runtime",
    }
    assert profile.run_context().run_mode is RunMode.SAFE
    assert profile.run_context().workspace == str(profile.workspace)

    profile.cleanup()


def test_guest_environment_does_not_inherit_host_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    profile = GuestProfileFactory.create("task", temp_parent=tmp_path)

    assert "AWS_SECRET_ACCESS_KEY" not in profile.environment
    assert "OPENAI_API_KEY" not in profile.environment
    assert profile.environment["HOME"] == str(profile.home)
    assert profile.environment["USERPROFILE"] == str(profile.home)
    assert profile.environment["PATH"] == ""
    profile.cleanup()


def test_guest_cleanup_removes_entire_task_root(tmp_path: Path) -> None:
    profile = GuestProfileFactory.create("task", temp_parent=tmp_path)
    marker = profile.workspace / "result.txt"
    marker.write_text("guest", encoding="utf-8")
    root = profile.root

    profile.cleanup()
    profile.cleanup()

    assert not root.exists()


def test_guest_cleanup_rejects_non_guest_directory(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()

    assert cleanup_guest_profile_root(ordinary) is False
    assert ordinary.exists()
