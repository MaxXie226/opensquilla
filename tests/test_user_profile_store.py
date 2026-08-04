"""The versioned store: history is never clobbered, active is atomic + isolated.

The plan's §1.6 requirement is that a same-day re-run bumps the sequence rather
than overwriting a prior version, and that this ``active`` pointer is a different
artifact from ``self_learning``'s ``router/active`` bundle pointer. Reads fail
open to ``None`` so a missing or corrupt profile degrades to the mock baseline
rather than failing a turn.
"""

from __future__ import annotations

import builtins
import importlib
import json
import multiprocessing
import os
import sys
import threading
from pathlib import Path

import pytest

from opensquilla.squilla_router.user_profile import store
from opensquilla.squilla_router.user_profile.state import ProfileRunState, save_run_state


def _payload(tag: str) -> dict:
    return {"profile_version": tag, "history": {"feedback_count": 1}, "_meta": {"x": 1}}


def _publish_payload(version: str) -> dict:
    return _payload(version)


def _publish_state(version: str) -> dict:
    return {
        "last_attempt_ts": "2026-07-20T12:00:00Z",
        "last_run_ts": "2026-07-20T12:00:00Z",
        "last_version": version,
        "consecutive_failures": 0,
    }


def _publish_in_process(home: str, state_root: str, queue: multiprocessing.Queue) -> None:
    os.environ["OPENSQUILLA_USER_STATE_DIR"] = state_root
    os.environ["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    try:
        result = store.publish_profile(
            agent_id="main",
            day="2026-07-20",
            build_payload=_publish_payload,
            state_payload=_publish_state,
            home=Path(home),
            lock_timeout=5.0,
        )
        queue.put(("ok", result.version))
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        queue.put(("err", type(exc).__name__, str(exc)))


def _nothing_written_for_agent(agent_id: str, home: Path) -> bool:
    directory = store.profiles_dir(agent_id, home)
    versions = list(directory.glob("user_profile.*.json")) if directory.is_dir() else []
    return store.read_active_name(agent_id, home) is None and versions == []


def _seed_profile_publication_lock(agent_id: str, home: Path) -> None:
    from opensquilla.profile_operation_lock import ProfileOperationLock

    # Seed the stable lock inode before workers race. ProfileOperationLock
    # intentionally fails closed when multiple untrusted paths race first
    # creation; these tests are about publication serialization after the lock
    # identity exists, not about lock bootstrap.
    with ProfileOperationLock(store.profiles_dir(agent_id, home), timeout=1.0):
        pass


def test_next_version_starts_at_one_then_bumps(tmp_path: Path) -> None:
    assert store.next_version("2026-07-20", "main", tmp_path) == "2026-07-20.1"
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    assert store.next_version("2026-07-20", "main", tmp_path) == "2026-07-20.2"


def test_a_same_day_rerun_never_overwrites(tmp_path: Path) -> None:
    p1 = store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    p2 = store.write_profile_version(_payload("v2"), "2026-07-20.2", "main", home=tmp_path)
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_an_existing_version_file_is_immutable(tmp_path: Path) -> None:
    path = store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)

    with pytest.raises(FileExistsError):
        store.write_profile_version(_payload("replacement"), "2026-07-20.1", "main", home=tmp_path)

    assert '"profile_version": "v1"' in path.read_text(encoding="utf-8")


def test_active_pointer_round_trips_the_loaded_profile(tmp_path: Path) -> None:
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    loaded = store.load_active_profile("main", tmp_path)
    assert loaded is not None
    assert loaded["profile_version"] == "v1"
    # _meta survives the load; the read seam strips it, not the store.
    assert loaded["_meta"] == {"x": 1}


def test_publish_profile_serializes_same_process_race_to_distinct_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    _seed_profile_publication_lock("main", tmp_path)
    barrier = threading.Barrier(5)
    versions: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            result = store.publish_profile(
                agent_id="main",
                day="2026-07-20",
                build_payload=_publish_payload,
                state_payload=_publish_state,
                home=tmp_path,
                lock_timeout=5.0,
            )
            with guard:
                versions.append(result.version)
        except BaseException as exc:  # pragma: no cover - asserted below
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(versions) == [f"2026-07-20.{seq}" for seq in range(1, 6)]
    assert store.read_active_name("main", tmp_path) in {
        store.version_filename(version) for version in versions
    }


def test_publish_profile_serializes_cross_process_race_to_distinct_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "locks"
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(state_root))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    _seed_profile_publication_lock("main", tmp_path)
    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_publish_in_process,
            args=(str(tmp_path), str(state_root), queue),
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    results = [queue.get(timeout=2) for _ in processes]
    assert all(result[0] == "ok" for result in results)
    versions = sorted(result[1] for result in results)
    assert versions == [f"2026-07-20.{seq}" for seq in range(1, 5)]
    active_name = store.read_active_name("main", tmp_path)
    assert active_name in {store.version_filename(version) for version in versions}
    assert active_name is not None
    active_path = store.profiles_dir("main", tmp_path) / active_name
    assert active_path.is_file()
    assert json.loads(active_path.read_text(encoding="utf-8"))["profile_version"] in versions


def test_publish_profile_uses_unique_temps_and_leaves_no_fixed_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")

    result = store.publish_profile(
        agent_id="main",
        day="2026-07-20",
        build_payload=_publish_payload,
        state_payload=_publish_state,
        home=tmp_path,
    )

    directory = store.profiles_dir("main", tmp_path)
    assert result.version == "2026-07-20.1"
    assert not (directory / "active.tmp").exists()
    assert not (directory / ".profile_state.tmp").exists()
    assert list(directory.glob("*.tmp")) == []
    assert list(directory.glob(".*.tmp")) == []


def test_save_run_state_uses_unique_atomic_temp_and_leaves_no_fixed_tmp(
    tmp_path: Path,
) -> None:
    save_run_state(
        ProfileRunState(
            last_attempt_ts="2026-07-20T12:00:00Z",
            last_run_ts="2026-07-20T12:00:00Z",
            last_version="2026-07-20.1",
            consecutive_failures=0,
        ),
        "main",
        tmp_path,
    )

    directory = store.profiles_dir("main", tmp_path)
    assert (directory / store.PROFILE_STATE_FILENAME).is_file()
    assert not (directory / ".profile_state.tmp").exists()
    assert list(directory.glob("*.tmp")) == []
    assert list(directory.glob(".*.tmp")) == []


def test_publish_profile_pre_commit_fault_preserves_old_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    store.write_profile_version(_payload("old"), "2026-07-20.1", "main", home=tmp_path)
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    original_replace = store.os.replace

    def fail_active_replace(src, dst):  # noqa: ANN001
        if Path(dst).name == store.ACTIVE_POINTER:
            raise OSError("active replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_active_replace)

    with pytest.raises(OSError, match="active replace failed"):
        store.publish_profile(
            agent_id="main",
            day="2026-07-20",
            build_payload=_publish_payload,
            state_payload=_publish_state,
            home=tmp_path,
        )

    assert store.read_active_name("main", tmp_path) == store.version_filename("2026-07-20.1")
    assert store.load_active_profile("main", tmp_path)["profile_version"] == "old"  # type: ignore[index]
    assert (store.profiles_dir("main", tmp_path) / store.version_filename("2026-07-20.2")).is_file()


def test_publish_profile_post_active_state_fault_keeps_new_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    original_replace = store.os.replace

    def fail_state_replace(src, dst):  # noqa: ANN001
        if Path(dst).name == store.PROFILE_STATE_FILENAME:
            raise OSError("state replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_state_replace)

    result = store.publish_profile(
        agent_id="main",
        day="2026-07-20",
        build_payload=_publish_payload,
        state_payload=_publish_state,
        home=tmp_path,
    )

    assert result.version == "2026-07-20.1"
    assert result.state_committed is False
    assert store.read_active_name("main", tmp_path) == store.version_filename(result.version)
    assert store.load_active_profile("main", tmp_path)["profile_version"] == result.version  # type: ignore[index]
    assert not store.profile_state_path("main", tmp_path).exists()


def test_publish_profile_lock_timeout_reports_publication_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.profile_operation_lock import ProfileOperationLock

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with ProfileOperationLock(store.profiles_dir("main", tmp_path), timeout=1.0):
            locked.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert locked.wait(timeout=5)
    try:
        with pytest.raises(store.ProfilePublicationBusyError):
            store.publish_profile(
                agent_id="main",
                day="2026-07-20",
                build_payload=_publish_payload,
                state_payload=_publish_state,
                home=tmp_path,
                lock_timeout=0.01,
            )
    finally:
        release.set()
        holder.join(timeout=5)

    assert _nothing_written_for_agent("main", tmp_path)


def test_active_pointer_targets_valid_file_inside_profiles_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    result = store.publish_profile(
        agent_id="main",
        day="2026-07-20",
        build_payload=_publish_payload,
        state_payload=_publish_state,
        home=tmp_path,
    )

    active_name = store.read_active_name("main", tmp_path)
    assert active_name == store.version_filename(result.version)
    assert active_name == Path(active_name).name
    active_file = store.profiles_dir("main", tmp_path) / active_name
    assert active_file.is_file()
    assert active_file.parent.resolve() == store.profiles_dir("main", tmp_path).resolve()


def test_active_is_independent_of_the_self_learning_bundle_pointer(tmp_path: Path) -> None:
    store.write_profile_version(_payload("v1"), "2026-07-20.1", "main", home=tmp_path)
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    # The profiles pointer lives under profiles/, not the router/active bundle.
    pointer = store.active_pointer_path("main", tmp_path)
    assert pointer.parent.name == "profiles"
    assert pointer.read_text().startswith("user_profile.")


def test_missing_pointer_is_none(tmp_path: Path) -> None:
    assert store.load_active_profile("main", tmp_path) is None


def test_a_pointer_with_a_path_separator_is_rejected(tmp_path: Path) -> None:
    pointer = store.active_pointer_path("main", tmp_path)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("../escape.json", encoding="utf-8")
    assert store.load_active_profile("main", tmp_path) is None


def test_a_dangling_pointer_is_none_not_a_raise(tmp_path: Path) -> None:
    store.write_active_atomic("2026-07-20.9", "main", home=tmp_path)
    assert store.load_active_profile("main", tmp_path) is None


def test_corrupt_json_is_none(tmp_path: Path) -> None:
    directory = store.profiles_dir("main", tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / store.version_filename("2026-07-20.1")).write_text("{bad", encoding="utf-8")
    store.write_active_atomic("2026-07-20.1", "main", home=tmp_path)
    assert store.load_active_profile("main", tmp_path) is None


def test_user_profile_store_does_not_import_self_learning_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile store must stay importable without optional trainer modules."""

    for name in list(sys.modules):
        if name == "opensquilla.squilla_router.user_profile.store" or name.startswith(
            "opensquilla.squilla_router.self_learning"
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name.startswith("opensquilla.squilla_router.self_learning"):
            raise AssertionError(f"unexpected self-learning import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    imported = importlib.import_module("opensquilla.squilla_router.user_profile.store")

    assert imported.profiles_dir("agent/with spaces", tmp_path) == (
        tmp_path / "router" / "data" / "agent_with_spaces" / "profiles"
    )
