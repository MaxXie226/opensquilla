"""End-to-end wiring: the gate decides, and only a full run ships a profile.

The orchestrator is the one place storage, the provider, and the on-disk store
meet, so these tests drive it with fakes and assert the two things the wiring
must guarantee: a gated or provider-less run writes *nothing* (no active pointer,
no version file), and a failure — a storage raise or every batch failing to
parse — leaves no half-written profile while bumping the consecutive-failure
counter so a broken provider backs off instead of retrying every dream.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import structlog.testing

from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.squilla_router.user_profile import orchestrator, store
from opensquilla.squilla_router.user_profile.defaults import default_user_profile
from opensquilla.squilla_router.user_profile.gates import MIN_SESSIONS
from opensquilla.squilla_router.user_profile.orchestrator import (
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    maybe_produce_user_profile,
)
from opensquilla.squilla_router.user_profile.state import load_run_state

_AGENT = "main"
_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def test_llm_budget_leaves_room_for_forced_reasoning_before_json() -> None:
    """Dream-selected reasoning models must still have budget for final JSON."""

    assert MAX_OUTPUT_TOKENS >= 6000
    assert TIMEOUT_SECONDS >= 120.0


@dataclass
class _Row:
    role: str
    content: str | None


class _Storage:
    """Fake session storage: a fixed row set and canned transcripts."""

    def __init__(
        self,
        session_ids: list[str],
        *,
        hours_ago: float = 5.0,
        raise_on_list: bool = False,
        unreadable: set[str] | None = None,
    ) -> None:
        latest_ms = int((_NOW.timestamp() - hours_ago * 3600) * 1000)
        self._rows = [(sid, latest_ms) for sid in session_ids]
        self._raise_on_list = raise_on_list
        self._unreadable = unreadable or set()
        self.last_limit = "not-called"
        self.list_calls = 0
        self.transcript_calls = 0

    async def list_session_ids_updated_since(
        self, since_ms: int, *, agent_id: str | None = None, limit: int | None = None
    ) -> list[tuple[str, int]]:
        self.list_calls += 1
        self.last_limit = limit
        if self._raise_on_list:
            raise RuntimeError("db down")
        return list(self._rows)

    async def get_canonical_transcript_window(
        self,
        session_id: str,
        *,
        head_rows: int,
        tail_rows: int,
        per_entry_max_chars: int,
    ) -> list[_Row]:
        assert head_rows == 64
        assert tail_rows == 64
        assert per_entry_max_chars == 6000
        self.transcript_calls += 1
        if session_id in self._unreadable:
            return []
        return [_Row("user", f"write code for {session_id}"), _Row("assistant", "ok")]


class _Provider:
    """A provider whose ``chat`` replays a scripted event stream."""

    def __init__(self, events=None) -> None:  # noqa: ANN001
        self._events = events

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        events = self._events
        if events is None:
            prompt = str(messages[0]["content"])
            session_ids = [s["session_id"] for s in json.loads(prompt)["sessions"]]
            events = _good_stream(session_ids)

        async def _stream():
            for event in events:
                yield event

        return _stream()


class _SequencedProvider:
    """A provider whose successive ``chat`` calls consume successive streams."""

    def __init__(self, streams) -> None:  # noqa: ANN001
        self._streams = list(streams)
        self.calls = 0

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        events = self._streams[self.calls]
        self.calls += 1

        async def _stream():
            for event in events:
                yield event

        return _stream()


def _good_stream(session_ids: list[str]):
    payload = {
        "session_labels": [
            {"session_id": sid, "capability": "code_generation", "confidence": 0.8}
            for sid in session_ids
        ],
        "quality_latency_tradeoff": {
            "value": "quality_first",
            "confidence": 0.7,
            "session_ids": session_ids,
        },
        "cost_sensitivity": {
            "value": "unknown",
            "confidence": 0.0,
        },
        "model_mentions": [],
    }
    return [TextDeltaEvent(text=json.dumps(payload)), DoneEvent()]


def _stream_factory(
    *,
    provider,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
):
    del system_prompt, max_output_tokens, temperature, timeout
    return provider.chat([{"content": user_prompt}], tools=None, config=None)


async def _produce(
    storage: _Storage,
    provider,
    home: Path,
    *,
    permission_snapshot: dict | None = None,
):
    return await maybe_produce_user_profile(
        _AGENT,
        base_profile=default_user_profile(),
        permission_snapshot=permission_snapshot,
        storage=storage,
        build_provider=lambda: provider,
        stream_factory=_stream_factory,
        home=home,
        now=_NOW,
    )


def _nothing_written(home: Path) -> bool:
    directory = store.profiles_dir(_AGENT, home)
    versions = list(directory.glob("user_profile.*.json")) if directory.is_dir() else []
    return store.read_active_name(_AGENT, home) is None and versions == []


async def test_a_full_run_writes_a_versioned_active_profile(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    storage = _Storage(ids)
    result = await _produce(storage, _Provider(), tmp_path)

    assert result.ran is True
    assert result.version is not None
    assert storage.last_limit is None
    # The active pointer and the version file it names both exist.
    assert store.read_active_name(_AGENT, tmp_path) == store.version_filename(result.version)
    version_file = store.profiles_dir(_AGENT, tmp_path) / store.version_filename(result.version)
    assert version_file.is_file()
    # A successful run stamps the run time and clears the failure counter.
    state = load_run_state(_AGENT, tmp_path)
    assert state.last_attempt_ts is not None
    assert state.last_run_ts is not None
    assert state.last_version == result.version
    assert state.consecutive_failures == 0
    assert result.state_committed is True


async def test_publication_busy_returns_without_shipping_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ids = [f"s{i}" for i in range(25)]

    def busy_publish(**kwargs):  # noqa: ANN003
        raise store.ProfilePublicationBusyError("busy")

    monkeypatch.setattr(store, "publish_profile", busy_publish)

    result = await _produce(_Storage(ids), _Provider(), tmp_path)

    assert result.ran is False
    assert result.reason == "publication_busy"
    assert _nothing_written(tmp_path)
    # The post-read provider attempt remains durable for cooldown/accounting.
    assert load_run_state(_AGENT, tmp_path).last_attempt_ts is not None


async def test_post_active_state_fault_is_returned_as_published_but_repairable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ids = [f"s{i}" for i in range(25)]
    original_publish = store.publish_profile

    def state_fault_publish(**kwargs):  # noqa: ANN003
        return replace(original_publish(**kwargs), state_committed=False)

    monkeypatch.setattr(store, "publish_profile", state_fault_publish)

    result = await _produce(_Storage(ids), _Provider(), tmp_path)

    assert result.ran is True
    assert result.version is not None
    assert result.state_committed is False


async def test_full_run_persists_the_gateway_permission_snapshot(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    permission = {
        "allow_models": ["model-a"],
        "deny_models": ["model-b"],
        "allow_tools": ["memory_search"],
        "risk_allowlist": ["low", "medium"],
    }

    result = await _produce(
        _Storage(ids),
        _Provider(),
        tmp_path,
        permission_snapshot=permission,
    )

    assert result.ran is True
    payload = store.load_active_profile(_AGENT, tmp_path)
    assert payload is not None
    assert payload["permission"] == permission


async def test_partial_permission_snapshot_keeps_the_complete_schema(
    tmp_path: Path,
) -> None:
    ids = [f"s{i}" for i in range(25)]

    result = await _produce(
        _Storage(ids),
        _Provider(),
        tmp_path,
        permission_snapshot={"deny_models": ["model-b"]},
    )

    assert result.ran is True
    payload = store.load_active_profile(_AGENT, tmp_path)
    assert payload is not None
    assert payload["permission"] == {
        "allow_models": [],
        "deny_models": ["model-b"],
        "allow_tools": [],
        "risk_allowlist": ["low", "medium", "high"],
    }


async def test_env_disabled_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """The internal kill switch short-circuits before provider or any write."""
    monkeypatch.setenv("OPENSQUILLA_USER_PROFILE_DISABLED", "1")
    storage = _Storage([f"s{i}" for i in range(25)])
    result = await _produce(storage, None, tmp_path)
    assert result.ran is False
    assert result.reason == "disabled"
    assert storage.list_calls == 0
    assert storage.transcript_calls == 0
    assert _nothing_written(tmp_path)
    assert not (store.profiles_dir(_AGENT, tmp_path) / ".profile_state.json").exists()


async def test_too_few_sessions_never_calls_the_provider_or_writes(tmp_path: Path) -> None:
    # min_sessions is 3; one in-window session cannot produce a stable profile.
    provider = _Provider(_good_stream(["s0"]))
    result = await _produce(_Storage(["s0"]), provider, tmp_path)
    assert result.ran is False
    assert result.reason == "insufficient_sessions"
    assert _nothing_written(tmp_path)


async def test_a_null_provider_after_the_gate_writes_nothing(tmp_path: Path) -> None:
    """Gates pass, but the provider could not be built — no profile ships."""
    result = await _produce(_Storage([f"s{i}" for i in range(25)]), None, tmp_path)
    assert result.ran is False
    assert result.reason == "no_provider"
    assert _nothing_written(tmp_path)
    assert load_run_state(_AGENT, tmp_path).last_attempt_ts is not None


async def test_insufficient_readable_sessions_has_no_attempt_or_provider(
    tmp_path: Path,
) -> None:
    ids = [f"s{i}" for i in range(MIN_SESSIONS)]
    storage = _Storage(ids, unreadable={ids[-1]})
    provider_calls = 0

    def build_provider():
        nonlocal provider_calls
        provider_calls += 1
        return _Provider()

    result = await maybe_produce_user_profile(
        _AGENT,
        base_profile=default_user_profile(),
        storage=storage,
        build_provider=build_provider,
        stream_factory=_stream_factory,
        home=tmp_path,
        now=_NOW,
    )

    assert result.ran is False
    assert result.reason == "insufficient_readable_sessions"
    assert result.sessions_read == MIN_SESSIONS - 1
    assert provider_calls == 0
    assert load_run_state(_AGENT, tmp_path).last_attempt_ts is None
    assert _nothing_written(tmp_path)


async def test_serialized_budget_drop_reapplies_minimum_before_attempt_or_provider(
    tmp_path: Path,
) -> None:
    ids = [f"s{i}" for i in range(MIN_SESSIONS)]

    class _BudgetDropStorage(_Storage):
        async def get_canonical_transcript_window(
            self,
            session_id: str,
            *,
            head_rows: int,
            tail_rows: int,
            per_entry_max_chars: int,
        ) -> list[_Row]:
            if session_id == ids[0]:
                return await super().get_canonical_transcript_window(
                    session_id,
                    head_rows=head_rows,
                    tail_rows=tail_rows,
                    per_entry_max_chars=per_entry_max_chars,
                )
            return [_Row("user", "😀" * per_entry_max_chars)]

    provider_calls = 0

    def build_provider() -> _Provider:
        nonlocal provider_calls
        provider_calls += 1
        return _Provider()

    result = await maybe_produce_user_profile(
        _AGENT,
        base_profile=default_user_profile(),
        storage=_BudgetDropStorage(ids),
        build_provider=build_provider,
        stream_factory=_stream_factory,
        home=tmp_path,
        now=_NOW,
    )

    assert result.ran is False
    assert result.reason == "insufficient_readable_sessions"
    assert result.sessions_read == 1
    assert provider_calls == 0
    assert load_run_state(_AGENT, tmp_path).last_attempt_ts is None
    assert _nothing_written(tmp_path)


async def test_insufficient_readable_sessions_emits_privacy_safe_counts(
    tmp_path: Path,
) -> None:
    ids = [f"private-session-{index}" for index in range(MIN_SESSIONS)]
    storage = _Storage(ids, unreadable={ids[-1]})

    with structlog.testing.capture_logs() as captured:
        result = await _produce(storage, _Provider(), tmp_path)

    assert result.reason == "insufficient_readable_sessions"
    event = next(
        item
        for item in captured
        if item["event"] == "user_profile.insufficient_readable_sessions"
    )
    assert event["sessions_read"] == MIN_SESSIONS - 1
    assert event["min_sessions"] == MIN_SESSIONS
    assert "private-session" not in json.dumps(captured)


async def test_failed_provider_attempt_is_cooldown_gated(tmp_path: Path) -> None:
    """A failed attempt still stamps cooldown before provider construction."""
    ids = [f"s{i}" for i in range(25)]
    first = await _produce(_Storage(ids), None, tmp_path)
    assert first.reason == "no_provider"

    second = await _produce(_Storage(ids), _Provider(), tmp_path)
    assert second.ran is False
    assert second.reason == "cooldown"
    assert _nothing_written(tmp_path)


async def test_a_storage_raise_writes_nothing_and_bumps_failures(tmp_path: Path) -> None:
    """A raise anywhere in the run degrades to a logged no-op, never a raise."""
    storage = _Storage([f"s{i}" for i in range(25)], raise_on_list=True)
    result = await _produce(storage, _Provider(_good_stream([])), tmp_path)

    assert result.ran is False
    assert result.reason == "error"
    assert _nothing_written(tmp_path)
    assert load_run_state(_AGENT, tmp_path).consecutive_failures == 1


async def test_produce_error_logs_only_stable_error_category(tmp_path: Path) -> None:
    storage = _Storage([f"s{i}" for i in range(25)], raise_on_list=True)

    with structlog.testing.capture_logs() as captured:
        result = await _produce(storage, _Provider(), tmp_path)

    assert result.reason == "error"
    event = next(item for item in captured if item["event"] == "user_profile.produce_error")
    assert event["error_category"] == "RuntimeError"
    assert "error" not in event
    assert "db down" not in json.dumps(captured)


async def test_every_batch_failing_bumps_failures_and_writes_nothing(tmp_path: Path) -> None:
    """No parseable batch means no evidence — write nothing, back off."""
    ids = [f"s{i}" for i in range(25)]
    provider = _Provider([TextDeltaEvent(text="I cannot help."), DoneEvent()])
    result = await _produce(_Storage(ids), provider, tmp_path)

    assert result.ran is False
    assert result.reason == "all_batches_failed"
    assert _nothing_written(tmp_path)
    state = load_run_state(_AGENT, tmp_path)
    assert state.last_attempt_ts is not None
    assert state.consecutive_failures == 1


async def test_failed_batches_log_counts_without_transcript_or_raw_response(
    tmp_path: Path,
) -> None:
    ids = [f"s{i}" for i in range(25)]
    raw_response = "PRIVATE_RAW_ANALYST_RESPONSE"
    provider = _Provider([TextDeltaEvent(text=raw_response), DoneEvent()])

    with structlog.testing.capture_logs() as captured:
        result = await _produce(_Storage(ids), provider, tmp_path)

    assert result.reason == "all_batches_failed"
    batch_events = [item for item in captured if item["event"] == "user_profile.batch_failed"]
    assert [item["sessions_in_batch"] for item in batch_events] == [10, 10, 5]
    all_failed = next(
        item for item in captured if item["event"] == "user_profile.all_batches_failed"
    )
    assert all_failed["failed_batches"] == 3
    assert all_failed["sessions_read"] == 25
    rendered_logs = json.dumps(captured)
    assert raw_response not in rendered_logs
    assert "write code for" not in rendered_logs


async def test_feedback_count_counts_only_successful_batches(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    ok_ids = ids[:10]
    provider = _SequencedProvider(
        [
            _good_stream(ok_ids),
            [TextDeltaEvent(text="bad"), DoneEvent()],
            [TextDeltaEvent(text="bad"), DoneEvent()],
        ]
    )
    result = await _produce(_Storage(ids), provider, tmp_path)

    assert result.ran is True
    assert result.sessions_read == 10
    profile = store.load_active_profile(_AGENT, tmp_path)
    assert profile is not None
    assert profile["history"]["feedback_count"] == 10


async def test_normal_run_keeps_historical_versions_unpruned(tmp_path: Path) -> None:
    for seq in range(1, 18):
        store.write_profile_version(
            {"profile_version": f"old-{seq}"},
            f"2026-07-19.{seq}",
            _AGENT,
            home=tmp_path,
        )
    ids = [f"s{i}" for i in range(25)]
    result = await _produce(_Storage(ids), _Provider(), tmp_path)

    assert result.ran is True
    directory = store.profiles_dir(_AGENT, tmp_path)
    versions = sorted(directory.glob("user_profile.*.json"))
    assert len(versions) == 18
    assert (directory / "user_profile.2026-07-19.1.json").is_file()


def test_orchestrator_uses_single_store_publication_entrypoint() -> None:
    source = inspect.getsource(orchestrator)

    assert "store.publish_profile" in source
    assert "store.next_version" not in source
    assert "store.write_profile_version" not in source
    assert "store.write_active_atomic" not in source
