"""SourceRouter — aggregates search/fetch across multiple SkillSource adapters."""

from __future__ import annotations

import asyncio
from typing import cast

import structlog

from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SourceResolution,
)

log = structlog.get_logger(__name__)


class SourceRouter:
    """Routes skill operations to the appropriate source adapter."""

    def __init__(self, sources: list[SkillSource] | None = None) -> None:
        self._sources: dict[str, SkillSource] = {}
        for s in sources or []:
            self._sources[s.source_id] = s

    def add_source(self, source: SkillSource) -> None:
        self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> SkillSource | None:
        return self._sources.get(source_id)

    @property
    def source_ids(self) -> list[str]:
        return list(self._sources.keys())

    async def search(
        self, query: str, limit: int = 20, source_id: str | None = None
    ) -> list[SkillMeta]:
        """Search across all sources (or a specific one). Returns merged results."""
        if source_id:
            src = self._sources.get(source_id)
            if src is None:
                return []
            results = await src.search(query, limit=limit)
            self._fill_source_ids(results, source_id)
            return self._deduplicate(results, limit)

        # Search all sources in parallel
        sources = list(self._sources.values())
        tasks = [src.search(query, limit=limit) for src in sources]
        if not tasks:
            return []

        all_results: list[SkillMeta] = []
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for source, result_list in zip(sources, gathered, strict=True):
            if isinstance(result_list, list):
                self._fill_source_ids(result_list, source.source_id)
                all_results.extend(result_list)
            else:
                log.warning(
                    "router.search_source_failed",
                    source_id=source.source_id,
                    error=str(result_list),
                )

        return self._deduplicate(all_results, limit)

    @staticmethod
    def _fill_source_ids(results: list[SkillMeta], source_id: str) -> None:
        for result in results:
            if not result.source_id:
                result.source_id = source_id

    @staticmethod
    def _deduplicate(results: list[SkillMeta], limit: int) -> list[SkillMeta]:
        """Deduplicate exact packages without collapsing same-name publishers."""

        seen: set[tuple[str, str]] = set()
        deduped: list[SkillMeta] = []
        for result in results:
            identifier = result.canonical_identifier or result.identifier or result.name
            identity = (result.source_id, identifier)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(result)
        return deduped[:limit]

    async def resolve(self, identifier: str, source_id: str) -> SourceResolution | None:
        """Resolve through a modern source or synthesize a legacy fetch contract."""

        src = self._sources.get(source_id)
        if src is None:
            log.warning("router.resolve_unknown_source", source_id=source_id)
            return None
        resolver = getattr(src, "resolve", None)
        if callable(resolver):
            return cast(SourceResolution | None, await resolver(identifier))
        return SourceResolution(
            source_id=source_id,
            requested_identifier=identifier,
            canonical_identifier=identifier,
        )

    async def fetch(self, identifier: str, source_id: str) -> SkillBundle | None:
        """Resolve then fetch, while retaining support for fetch-only adapters."""

        src = self._sources.get(source_id)
        if src is None:
            log.warning("router.fetch_unknown_source", source_id=source_id)
            return None
        resolution = await self.resolve(identifier, source_id)
        if resolution is None:
            return None
        fetch_resolved = getattr(src, "fetch_resolved", None)
        if callable(fetch_resolved):
            bundle = cast(SkillBundle | None, await fetch_resolved(resolution))
        else:
            bundle = await src.fetch(identifier)
        if bundle is not None and bundle.resolution is None:
            bundle.resolution = resolution
        return bundle

    async def inspect(self, identifier: str, source_id: str) -> SkillMeta | None:
        """Get metadata from a specific source."""
        src = self._sources.get(source_id)
        if src is None:
            return None
        return await src.inspect(identifier)
