"""Configuration contract for offline user-profile production."""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import structlog.testing

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.boot import (
    _produce_user_profile_after_dream,
    _user_profile_generation_enabled,
    _user_profile_stream_factory,
    _user_profile_usage_execution_context,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _SAFE_WRITE_PATCH_PATHS,
    _handle_config_patch_safe,
)
from opensquilla.provider.protocol import ProviderMetadata
from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.scheduler.dream_handler import make_memory_dream_handler
from opensquilla.scheduler.types import CronJob
from opensquilla.squilla_router import data_paths
from opensquilla.squilla_router.user_profile import store


def _ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="user-profile-config",
        config=GatewayConfig(config_path=str(tmp_path / "config.toml")),
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def test_user_profile_generation_defaults_to_disabled() -> None:
    config = GatewayConfig()

    assert config.squilla_router.user_profile.enabled is False
    assert _user_profile_generation_enabled(config) is False
    assert config.to_toml_dict()["squilla_router"]["user_profile"]["enabled"] is False


def test_user_profile_generation_can_be_explicitly_enabled() -> None:
    config = GatewayConfig(
        squilla_router={"user_profile": {"enabled": True}},
    )

    assert config.squilla_router.user_profile.enabled is True
    assert _user_profile_generation_enabled(config) is True


async def test_safe_patch_enables_and_persists_user_profile_generation(tmp_path) -> None:
    assert "squilla_router.user_profile.enabled" in _SAFE_WRITE_PATCH_PATHS
    ctx = _ctx(tmp_path)

    await _handle_config_patch_safe(
        {"patches": {"squilla_router.user_profile.enabled": True}},
        ctx,
    )

    assert ctx.config.squilla_router.user_profile.enabled is True
    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert persisted["squilla_router"]["user_profile"]["enabled"] is True


@dataclass
class _RecordingSink:
    started: list[UsageCallStart] = field(default_factory=list)
    finalized: list[tuple[UsageCallStart, UsageCallResult]] = field(default_factory=list)
    unknown: list[tuple[UsageCallStart, str]] = field(default_factory=list)

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


class _ProfileProvider:
    provider_name = "fallback-name"
    model = "fallback-model"

    def __init__(self, events: list[Any] | None = None) -> None:
        self.events = events or [TextDeltaEvent(text="ok"), DoneEvent(input_tokens=1)]
        self.calls = 0

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name="profile-family",
            provider_id="configured-profile",
            provider_kind="openai",
            model="profile-model",
        )

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.calls += 1
        return self._stream()

    async def _stream(self) -> Any:
        for event in self.events:
            yield event


def test_user_profile_usage_context_identity_is_per_run_but_agent_stable() -> None:
    sink = object()

    first = _user_profile_usage_execution_context("agent-a", sink)
    second = _user_profile_usage_execution_context("agent-a", sink)
    other_agent = _user_profile_usage_execution_context("agent-b", sink)

    assert first is not None
    assert second is not None
    assert other_agent is not None
    assert first.run_kind == "user_profile_generation"
    assert first.agent_id == "agent-a"
    assert first.session_epoch == 0
    assert first.execution_id != second.execution_id
    assert first.agent_run_id == first.execution_id
    assert first.turn_id == first.execution_id
    assert second.agent_run_id == second.execution_id
    assert second.turn_id == second.execution_id
    assert first.session_id == second.session_id
    assert first.session_id != other_agent.session_id
    assert _user_profile_usage_execution_context("agent-a", None) is None


async def test_user_profile_stream_factory_without_scope_is_direct() -> None:
    provider = _ProfileProvider()

    stream = _user_profile_stream_factory(
        provider=provider,
        user_prompt="hello",
        system_prompt="system",
        max_output_tokens=32,
        temperature=0.0,
        timeout=1.0,
    )
    events = [event async for event in stream]

    assert provider.calls == 1
    assert [event.kind for event in events] == ["text_delta", "done"]


async def test_user_profile_stream_factory_account_usage_false_ignores_outer_scope() -> None:
    sink = _RecordingSink()
    context = _user_profile_usage_execution_context("agent-a", sink)
    assert context is not None
    provider = _ProfileProvider()

    with bind_usage_accounting_scope(UsageAccountingScope(sink=sink, context=context)):
        stream = _user_profile_stream_factory(
            provider=provider,
            user_prompt="hello",
            system_prompt="system",
            max_output_tokens=32,
            temperature=0.0,
            timeout=1.0,
            account_usage=False,
        )
        events = [event async for event in stream]

    assert provider.calls == 1
    assert [event.kind for event in events] == ["text_delta", "done"]
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []


async def test_user_profile_stream_factory_accounts_non_physical_provider() -> None:
    sink = _RecordingSink()
    context = _user_profile_usage_execution_context("agent-a", sink)
    assert context is not None
    provider = _ProfileProvider()

    with bind_usage_accounting_scope(UsageAccountingScope(sink=sink, context=context)):
        stream = _user_profile_stream_factory(
            provider=provider,
            user_prompt="hello",
            system_prompt="system",
            max_output_tokens=32,
            temperature=0.0,
            timeout=1.0,
        )
        events = [event async for event in stream]

    assert provider.calls == 1
    assert [event.kind for event in events] == ["text_delta", "done"]
    assert len(sink.started) == 1
    assert sink.started[0].run_kind == "user_profile_generation"
    assert sink.started[0].provider == "configured-profile"
    assert sink.started[0].model == "profile-model"
    assert len(sink.finalized) == 1
    assert sink.unknown == []


class _SelectorOwnedAccountingProvider(_ProfileProvider):
    accounts_physical_usage = True

    def __init__(self, physical: _ProfileProvider) -> None:
        super().__init__([])
        self.physical = physical

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        return account_provider_stream(
            lambda: self.physical.chat(*args, **kwargs),
            provider="selector-owned",
            model="selector-model",
        )


async def test_user_profile_stream_factory_does_not_double_account_physical_provider() -> None:
    sink = _RecordingSink()
    context = _user_profile_usage_execution_context("agent-a", sink)
    assert context is not None
    physical = _ProfileProvider()
    provider = _SelectorOwnedAccountingProvider(physical)

    with bind_usage_accounting_scope(UsageAccountingScope(sink=sink, context=context)):
        stream = _user_profile_stream_factory(
            provider=provider,
            user_prompt="hello",
            system_prompt="system",
            max_output_tokens=32,
            temperature=0.0,
            timeout=1.0,
        )
        events = [event async for event in stream]

    assert physical.calls == 1
    assert [event.kind for event in events] == ["text_delta", "done"]
    assert len(sink.started) == 1
    assert sink.started[0].provider == "selector-owned"
    assert len(sink.finalized) == 1


class _ProfileSessionStorage:
    async def list_session_ids_updated_since(
        self,
        since_ms: int,
        *,
        agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, int]]:
        del since_ms, agent_id
        assert limit is None
        updated_ms = int(time.time() * 1000) - 5 * 60 * 60 * 1000
        return [(f"session-{index}", updated_ms) for index in range(20)]

    async def get_canonical_transcript_window(
        self,
        session_id: str,
        *,
        head_rows: int,
        tail_rows: int,
        per_entry_max_chars: int,
    ) -> list[Any]:
        assert (head_rows, tail_rows, per_entry_max_chars) == (64, 64, 6000)
        return [SimpleNamespace(role="user", content=f"write code for {session_id}")]


class _AnalystProvider(_ProfileProvider):
    def __init__(self) -> None:
        super().__init__([])

    def chat(self, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.calls += 1
        prompt = getattr(messages[0], "content", "")
        session_ids = [item["session_id"] for item in json.loads(prompt)["sessions"]]
        payload = {
            "session_labels": [
                {
                    "session_id": session_id,
                    "capability": "code_generation",
                    "confidence": 0.8,
                }
                for session_id in session_ids
            ],
            "quality_latency_tradeoff": {
                "value": "quality_first",
                "confidence": 0.7,
                "session_ids": session_ids,
            },
            "cost_sensitivity": {"value": "unknown", "confidence": 0.0},
            "model_mentions": [],
        }

        async def stream() -> Any:
            yield TextDeltaEvent(text=json.dumps(payload))
            yield DoneEvent(input_tokens=100, output_tokens=50)

        return stream()


async def test_successful_dream_runs_accounted_profile_flow_and_activates_version(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _AnalystProvider()
    sink = _RecordingSink()
    config = GatewayConfig(squilla_router={"user_profile": {"enabled": True}})
    manager = SimpleNamespace(storage=_ProfileSessionStorage())

    monkeypatch.setattr(data_paths, "default_opensquilla_home", lambda: tmp_path)
    monkeypatch.setattr(
        "opensquilla.memory.dream_factory.build_dream_provider_selector",
        lambda _config: SimpleNamespace(resolve=lambda: provider),
    )

    class _Dream:
        async def run(self) -> Any:
            return SimpleNamespace(files_processed=1, evidence_status="ok", apply_status="ok")

    async def post_hook(agent_id: str, _summary: str) -> None:
        await _produce_user_profile_after_dream(
            agent_id,
            config=config,
            session_manager=manager,
            tool_registry=None,
            usage_event_sink=sink,
        )

    handler = make_memory_dream_handler(
        build_dream=lambda _agent_id: _Dream(),
        post_dream_hook=post_hook,
    )
    with structlog.testing.capture_logs() as captured:
        result = await handler(CronJob(id="dream-profile", payload={"agent_id": "main"}))

    assert result.summary.startswith("dream agent=main")
    active_name = store.read_active_name("main", tmp_path)
    assert active_name is not None
    assert (store.profiles_dir("main", tmp_path) / active_name).is_file()
    assert provider.calls == 2
    assert len(sink.started) == 2
    assert len(sink.finalized) == 2
    assert sink.unknown == []
    assert all(call.run_kind == "user_profile_generation" for call in sink.started)
    event = next(item for item in captured if item["event"] == "user_profile.post_dream")
    assert event["ran"] is True
    assert event["reason"] == "ready"
    assert event["sessions_read"] == 20
    assert isinstance(event["elapsed_ms"], int)


@dataclass
class _FailingStartSink:
    attempts: int = 0

    async def start(self, _call: UsageCallStart) -> None:
        self.attempts += 1
        raise RuntimeError("PRIVATE_LEDGER_FAILURE")

    async def finalize(self, _call: UsageCallStart, _result: UsageCallResult) -> None:
        raise AssertionError("a call that never started cannot finalize")

    async def mark_unknown(self, _call: UsageCallStart, _reason: str) -> None:
        raise AssertionError("a call that never started cannot be marked unknown")


async def test_ledger_start_failure_blocks_provider_but_dream_stays_successful(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _AnalystProvider()
    sink = _FailingStartSink()
    config = GatewayConfig(squilla_router={"user_profile": {"enabled": True}})
    manager = SimpleNamespace(storage=_ProfileSessionStorage())

    monkeypatch.setattr(data_paths, "default_opensquilla_home", lambda: tmp_path)
    monkeypatch.setattr(
        "opensquilla.memory.dream_factory.build_dream_provider_selector",
        lambda _config: SimpleNamespace(resolve=lambda: provider),
    )

    class _Dream:
        async def run(self) -> Any:
            return SimpleNamespace(files_processed=1, evidence_status="ok", apply_status="ok")

    async def post_hook(agent_id: str, _summary: str) -> None:
        await _produce_user_profile_after_dream(
            agent_id,
            config=config,
            session_manager=manager,
            tool_registry=None,
            usage_event_sink=sink,
        )

    handler = make_memory_dream_handler(
        build_dream=lambda _agent_id: _Dream(),
        post_dream_hook=post_hook,
    )
    with structlog.testing.capture_logs() as captured:
        result = await handler(CronJob(id="dream-ledger-barrier", payload={"agent_id": "main"}))

    assert result.summary.startswith("dream agent=main")
    assert provider.calls == 0
    assert sink.attempts == 2
    assert store.read_active_name("main", tmp_path) is None
    assert any(item["event"] == "user_profile.all_batches_failed" for item in captured)
    assert "PRIVATE_LEDGER_FAILURE" not in json.dumps(captured)


async def test_disabled_post_dream_adapter_returns_before_session_access() -> None:
    class _PoisonManager:
        @property
        def storage(self) -> Any:
            raise AssertionError("disabled profile generation must not access sessions")

    await _produce_user_profile_after_dream(
        "main",
        config=GatewayConfig(),
        session_manager=_PoisonManager(),
        tool_registry=None,
        usage_event_sink=None,
    )


async def test_post_dream_adapter_logs_error_category_without_raw_exception() -> None:
    class _PoisonRegistry:
        def list_names(self) -> list[str]:
            raise RuntimeError("PRIVATE_TOOL_REGISTRY_FAILURE")

    with structlog.testing.capture_logs() as captured:
        await _produce_user_profile_after_dream(
            "main",
            config=GatewayConfig(squilla_router={"user_profile": {"enabled": True}}),
            session_manager=SimpleNamespace(storage=_ProfileSessionStorage()),
            tool_registry=_PoisonRegistry(),
            usage_event_sink=None,
        )

    event = next(
        item for item in captured if item["event"] == "user_profile.post_dream_error"
    )
    assert event["error_category"] == "RuntimeError"
    assert "error" not in event
    assert "PRIVATE_TOOL_REGISTRY_FAILURE" not in json.dumps(captured)
