"""Hatch build hook for the generated Vue control UI artifact."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BUILD_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _injected_build_commit() -> str | None:
    """Read an explicit build identity without consulting the checkout."""

    variable = (
        "OPENSQUILLA_BUILD_COMMIT"
        if "OPENSQUILLA_BUILD_COMMIT" in os.environ
        else "GITHUB_SHA"
    )
    value = os.environ.get(variable, "").strip()
    if not value:
        return None
    if not _BUILD_COMMIT_PATTERN.fullmatch(value):
        raise ValueError(f"{variable} must be a 7-64 character hexadecimal commit")
    return value.lower()


class CustomBuildHook(BuildHookInterface):
    """Fail standard distributions closed when their embedded WebUI is stale."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"wheel", "sdist"}:
            return
        if version != "standard":
            raise RuntimeError(
                "Unsupported Hatchling build mode: "
                f"target={self.target_name!r}, version={version!r}"
            )

        root = Path(self.root).resolve()
        sys.path.insert(0, str(root))
        try:
            from scripts.verify_webui_artifact import (
                verify_dist,
                verify_sdist_source_inventory,
            )

            verify_dist(
                root / "src/opensquilla/gateway/static/dist",
                webui_root=root / "opensquilla-webui",
                # Source archives are easy to redistribute accidentally. Keep
                # standard sdists privacy-safe even when a checkout contains
                # ignored personal music; direct local wheels may still embed
                # an explicitly customized artifact.
                forbid_personal_bgm=self.target_name == "sdist",
            )
            if self.target_name == "sdist":
                verify_sdist_source_inventory(root / "opensquilla-webui")
            if self.target_name == "wheel":
                self._inject_build_info(root, build_data)
        except (ImportError, OSError, RuntimeError) as exc:
            privacy_note = (
                " Standard sdists intentionally reject personal BGM; build a "
                "direct local wheel if you need a private customized artifact."
                if self.target_name == "sdist"
                else ""
            )
            raise RuntimeError(
                "A verified WebUI artifact is required for standard wheel/sdist builds. "
                "From a repository checkout, run "
                "`cd opensquilla-webui && npm ci && npm run build`, then retry. "
                "VCS URL installs cannot build the untracked generated artifact; use "
                "an official release wheel, or clone the repository and run "
                "`bash scripts/install_source.sh` (`powershell -ExecutionPolicy "
                "Bypass -File ./scripts/install_source.ps1` on Windows). "
                f"Validation failed: {exc}{privacy_note}"
            ) from exc
        finally:
            sys.path.remove(str(root))

    def _inject_build_info(self, root: Path, build_data: dict[str, Any]) -> None:
        """Replace the source fallback only inside a standard wheel."""

        source_fallback = root / "src" / "opensquilla" / "_build_info.py"
        if not source_fallback.is_file():
            # The hook is also exercised by a minimal packaging-contract probe
            # that intentionally does not build the OpenSquilla package.
            return
        build_commit = _injected_build_commit()
        if build_commit is None:
            return

        generated_dir = Path(self.directory) / "opensquilla-build-metadata"
        generated_dir.mkdir(parents=True, exist_ok=True)
        generated = generated_dir / "_build_info.py"
        generated.write_text(
            (
                '"""Generated build-time artifact identity."""\n\n'
                "from __future__ import annotations\n\n"
                f"BUILD_COMMIT: str | None = {build_commit!r}\n\n"
                '__all__ = ["BUILD_COMMIT"]\n'
            ),
            encoding="utf-8",
            newline="\n",
        )
        build_data["force_include"][str(generated)] = "opensquilla/_build_info.py"
