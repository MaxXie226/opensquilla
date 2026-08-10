from __future__ import annotations

from typing import Any

import pytest

from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillBundle, SkillMeta, SourceResolution


class _SearchSource:
    def __init__(self, source_id: str, results: list[SkillMeta]) -> None:
        self.source_id = source_id
        self.trust_level = "community"
        self.results = results

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return self.results

    async def fetch(self, identifier: str) -> SkillBundle | None:
        return None

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return None


@pytest.mark.asyncio
async def test_search_deduplicates_canonical_identity_not_display_name() -> None:
    source = _SearchSource(
        "clawhub",
        [
            SkillMeta(name="Weather", identifier="@alice/weather"),
            SkillMeta(name="Weather", identifier="@bob/weather"),
            SkillMeta(name="Renamed", identifier="legacy", canonical_identifier="@alice/weather"),
        ],
    )

    results = await SourceRouter([source]).search("weather")  # type: ignore[list-item]

    assert [result.identifier for result in results] == ["@alice/weather", "@bob/weather"]


@pytest.mark.asyncio
async def test_same_identifier_from_different_sources_is_not_collapsed() -> None:
    sources = [
        _SearchSource("clawhub", [SkillMeta(name="Demo", identifier="demo")]),
        _SearchSource("github", [SkillMeta(name="Demo", identifier="demo")]),
    ]

    results = await SourceRouter(sources).search("demo")  # type: ignore[arg-type]

    assert [(result.source_id, result.identifier) for result in results] == [
        ("clawhub", "demo"),
        ("github", "demo"),
    ]


class _LegacyFetchOnlySource:
    source_id = "legacy"
    trust_level = "community"

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return []

    async def fetch(self, identifier: str) -> SkillBundle | None:
        return SkillBundle(name=identifier, files={"SKILL.md": "---\n---\n"})

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return None


@pytest.mark.asyncio
async def test_fetch_only_fake_adapter_gets_a_legacy_resolution() -> None:
    source = _LegacyFetchOnlySource()
    router = SourceRouter([source])  # type: ignore[list-item]

    bundle = await router.fetch("demo", "legacy")

    assert bundle is not None
    assert bundle.resolution == SourceResolution(
        source_id="legacy",
        requested_identifier="demo",
        canonical_identifier="demo",
    )


class _ModernSource(_LegacyFetchOnlySource):
    source_id = "modern"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def resolve(self, identifier: str) -> SourceResolution | None:
        self.calls.append(("resolve", identifier))
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=f"{identifier}@fixed",
            immutable=True,
            revision="fixed",
        )

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle | None:
        self.calls.append(("fetch_resolved", resolution))
        return SkillBundle(name="demo", files={"SKILL.md": "---\n---\n"})

    async def fetch(self, identifier: str) -> SkillBundle | None:
        raise AssertionError("router must use fetch_resolved after resolve")


@pytest.mark.asyncio
async def test_modern_adapter_uses_resolve_then_fetch_resolved() -> None:
    source = _ModernSource()
    router = SourceRouter([source])  # type: ignore[list-item]

    bundle = await router.fetch("demo", "modern")

    assert bundle is not None
    assert [call[0] for call in source.calls] == ["resolve", "fetch_resolved"]
    assert bundle.resolution is not None
    assert bundle.resolution.canonical_identifier == "demo@fixed"
