"""Dependency-free router data path helpers."""

from __future__ import annotations

import re
from pathlib import Path

from opensquilla.paths import default_opensquilla_home

_SAFE_AGENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_agent_id(agent_id: str) -> str:
    cleaned = _SAFE_AGENT_RE.sub("_", (agent_id or "default").strip()) or "default"
    # A pure-dot segment ("." / "..") would escape the data root; everything
    # else is a harmless single path segment (no separators survive the regex).
    if set(cleaned) <= {"."}:
        cleaned = "default"
    return cleaned[:128]


def router_data_root(home: Path | None = None) -> Path:
    """Resolve the single root holding all router artifacts."""

    base = home or default_opensquilla_home()
    return base / "router"


def agent_data_dir(agent_id: str, home: Path | None = None) -> Path:
    """Resolve the per-agent router data directory."""

    return router_data_root(home) / "data" / _safe_agent_id(agent_id)
