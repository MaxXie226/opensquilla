"""Compatibility facade for the transactional Community Skill manager.

New composition roots should inject :class:`SkillManagementService` directly.
``SkillInstaller`` remains import-compatible for existing extensions and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.hub.management import InstallResult, SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.paths import default_managed_skills_dir


class SkillInstaller(SkillManagementService):
    """Backward-compatible name for the shared management service."""

    def __init__(
        self,
        router: SourceRouter,
        managed_dir: Path | None = None,
        quarantine_dir: Path | None = None,
        lockfile_path: Path | None = None,
        *,
        loader: Any | None = None,
        journal_path: Path | None = None,
        offline: bool | None = None,
    ) -> None:
        selected_managed = managed_dir or default_managed_skills_dir()
        selected_lock = lockfile_path or default_opensquilla_home() / "skills-lock.json"
        # ``quarantine_dir`` remains accepted for constructor compatibility,
        # but transaction state must have one canonical path per managed root.
        # Candidate staging now lives on the managed filesystem and no runtime
        # state is written beneath the legacy quarantine directory.
        _ = quarantine_dir
        super().__init__(
            router=router,
            managed_dir=selected_managed,
            lockfile_path=selected_lock,
            loader=loader,
            journal_path=journal_path,
            offline=(loader is None) if offline is None else offline,
        )


__all__ = ["InstallResult", "SkillInstaller"]
