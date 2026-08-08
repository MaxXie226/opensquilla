from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import (
    ChatConfig,
    ErrorEvent,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.tools.builtin import file_authoring as _file_authoring  # noqa: F401
from opensquilla.tools.registry import get_default_registry

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _provider(provider_kind: str) -> OpenAIProvider:
    if provider_kind == "gemini":
        return OpenAIProvider(
            api_key="test",
            model="google/gemini-3.1-pro-preview",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            provider_kind="gemini",
        )
    if provider_kind == "openrouter":
        return OpenAIProvider(
            api_key="test",
            model="google/gemini-3.1-pro-preview",
            base_url="https://openrouter.ai/api",
            provider_kind="openrouter",
        )
    return OpenAIProvider(
        api_key="test",
        model="gpt-5",
        provider_kind="openai",
    )


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_kind: str,
    tools: list[ToolDefinition],
) -> tuple[list[Any], list[dict[str, Any]]]:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        body = (
            b'data: {"model":"test/model","choices":[{"delta":{"content":"ok"},'
            b'"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )

    async def run() -> list[Any]:
        return [
            event
            async for event in _provider(provider_kind).chat(
                [Message(role="user", content="hi")],
                tools=tools,
                config=ChatConfig(),
            )
        ]

    return asyncio.run(run()), payloads


def _assert_all_arrays_have_object_items(value: Any) -> None:
    if isinstance(value, Mapping):
        schema_type = value.get("type")
        if schema_type == "array" or (
            isinstance(schema_type, list) and "array" in schema_type
        ):
            assert isinstance(value.get("items"), Mapping)
        for nested in value.values():
            _assert_all_arrays_have_object_items(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_all_arrays_have_object_items(nested)


@pytest.mark.parametrize("provider_kind", ["openrouter", "gemini"])
def test_final_wire_schema_rejects_nested_array_without_items_before_request(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
) -> None:
    invalid_tool = ToolDefinition(
        name="invalid_nested_array",
        description="Invalid only at the direct provider boundary.",
        input_schema=ToolInputSchema(
            properties={
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                }
            },
            required=["rows"],
        ),
    )

    events, payloads = _collect(
        monkeypatch,
        provider_kind=provider_kind,
        tools=[invalid_tool],
    )

    assert payloads == []
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == "invalid_tool_schema"
    assert error.request_started is False
    assert error.physical_request_count == 0
    assert error.usage_missing_count == 0
    assert error.operational_error is None
    assert "$.properties.rows.items" in error.message


@pytest.mark.parametrize("provider_kind", ["openrouter", "gemini"])
def test_create_csv_nested_array_items_survive_final_wire_payload(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
) -> None:
    create_csv_tool = next(
        tool
        for tool in get_default_registry().to_tool_definitions()
        if tool.name == "create_csv"
    )

    events, payloads = _collect(
        monkeypatch,
        provider_kind=provider_kind,
        tools=[create_csv_tool],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(payloads) == 1
    parameters = payloads[0]["tools"][0]["function"]["parameters"]
    rows = parameters["properties"]["rows"]
    assert rows["type"] == "array"
    assert rows["items"] == {"type": "array", "items": {}}
    _assert_all_arrays_have_object_items(parameters)


@pytest.mark.parametrize("provider_kind", ["openrouter", "gemini"])
def test_google_schema_walk_ignores_type_shaped_example_and_default_data(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
) -> None:
    tool = ToolDefinition(
        name="array_with_data_examples",
        description="Example payloads are data, not schemas.",
        input_schema=ToolInputSchema(
            properties={
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "examples": [{"type": "array"}],
                    "default": [{"type": "array"}],
                }
            }
        ),
    )

    events, payloads = _collect(
        monkeypatch,
        provider_kind=provider_kind,
        tools=[tool],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(payloads) == 1


def test_standard_openai_path_is_not_subject_to_google_schema_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = ToolDefinition(
        name="tuple_array_schema",
        description="Outside the scoped Google compatibility boundary.",
        input_schema=ToolInputSchema(
            properties={"values": {"type": "array", "items": True}}
        ),
    )

    events, payloads = _collect(
        monkeypatch,
        provider_kind="openai",
        tools=[tool],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(payloads) == 1
