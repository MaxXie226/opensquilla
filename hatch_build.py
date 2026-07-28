"""Hatch build hook for headless and explicitly embedded control UI builds."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BUILD_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BUILD_UI_MODE_ENV = "OPENSQUILLA_BUILD_UI_MODE"
_BUILD_UI_ARTIFACT_ENV = "OPENSQUILLA_BUILD_UI_ARTIFACT"
_HEADLESS_MODE = "headless"
_EMBED_UI_MODE = "embed-ui"
_WHEEL_UI_PREFIX = "opensquilla/gateway/static/dist/"
_SDIST_UI_PREFIX = f"src/{_WHEEL_UI_PREFIX}"


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
    """Keep standard distributions headless unless UI embedding is explicit."""

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
        try:
            mode, artifact_root = self._build_ui_selection(root)
            self._reject_preconfigured_ui_includes(build_data)
            if mode == _EMBED_UI_MODE:
                assert artifact_root is not None
                self._include_verified_ui(root, artifact_root, build_data)
            if self.target_name == "wheel":
                self._inject_build_info(root, build_data, ui_mode=mode)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "OpenSquilla UI packaging configuration is invalid. Standard "
                "wheel/sdist builds default to headless and never discover a "
                "checkout's static/dist directory. To embed a prebuilt UI, set "
                f"{_BUILD_UI_MODE_ENV}={_EMBED_UI_MODE} and "
                f"{_BUILD_UI_ARTIFACT_ENV}=<artifact-directory>. "
                f"Validation failed: {exc}"
            ) from exc

    def _build_ui_selection(self, root: Path) -> tuple[str, Path | None]:
        mode = os.environ.get(_BUILD_UI_MODE_ENV, _HEADLESS_MODE).strip().lower()
        raw_artifact = os.environ.get(_BUILD_UI_ARTIFACT_ENV, "").strip()
        if mode not in {_HEADLESS_MODE, _EMBED_UI_MODE}:
            raise ValueError(
                f"{_BUILD_UI_MODE_ENV} must be {_HEADLESS_MODE!r} or "
                f"{_EMBED_UI_MODE!r}, got {mode!r}"
            )
        if mode == _HEADLESS_MODE:
            if raw_artifact:
                raise ValueError(
                    f"{_BUILD_UI_ARTIFACT_ENV} is set while {_BUILD_UI_MODE_ENV} "
                    f"is {_HEADLESS_MODE!r}; select {_EMBED_UI_MODE!r} explicitly"
                )
            return mode, None
        if not raw_artifact:
            raise ValueError(
                f"{_BUILD_UI_ARTIFACT_ENV} is required when "
                f"{_BUILD_UI_MODE_ENV}={_EMBED_UI_MODE}"
            )
        artifact_root = Path(raw_artifact).expanduser()
        if not artifact_root.is_absolute():
            artifact_root = root / artifact_root
        return mode, artifact_root.resolve()

    def _reject_preconfigured_ui_includes(self, build_data: dict[str, Any]) -> None:
        """Prevent pyproject or another hook from bypassing explicit mode selection."""

        force_include = build_data.setdefault("force_include", {})
        prefixes = (_WHEEL_UI_PREFIX, _SDIST_UI_PREFIX)
        configured = sorted(
            destination
            for destination in force_include.values()
            if str(destination).replace("\\", "/").lstrip("./").startswith(prefixes)
        )
        if configured:
            raise RuntimeError(
                "embedded WebUI paths must be supplied only through "
                f"{_BUILD_UI_ARTIFACT_ENV}: {configured}"
            )

    def _include_verified_ui(
        self,
        root: Path,
        artifact_root: Path,
        build_data: dict[str, Any],
    ) -> None:
        """Validate a caller-selected artifact and force-include its exact files."""

        sys.path.insert(0, str(root))
        try:
            from scripts.verify_webui_artifact import verify_dist

            files = verify_dist(
                artifact_root,
                webui_root=None,
                # Source archives are readily redistributed. Preserve the
                # existing privacy guard for explicit sdist embedding.
                forbid_personal_bgm=self.target_name == "sdist",
            )
        finally:
            sys.path.remove(str(root))

        destination_prefix = (
            _WHEEL_UI_PREFIX if self.target_name == "wheel" else _SDIST_UI_PREFIX
        )
        force_include = build_data.setdefault("force_include", {})
        for relative in files:
            source = artifact_root.joinpath(*relative.split("/"))
            force_include[str(source)] = f"{destination_prefix}{relative}"

    def _inject_build_info(
        self,
        root: Path,
        build_data: dict[str, Any],
        *,
        ui_mode: str,
    ) -> None:
        """Replace the source fallback only inside a standard wheel."""

        source_fallback = root / "src" / "opensquilla" / "_build_info.py"
        if not source_fallback.is_file():
            # The hook is also exercised by a minimal packaging-contract probe
            # that intentionally does not build the OpenSquilla package.
            return
        build_commit = _injected_build_commit()

        generated_dir = Path(self.directory) / "opensquilla-build-metadata"
        generated_dir.mkdir(parents=True, exist_ok=True)
        generated = generated_dir / "_build_info.py"
        generated.write_text(
            (
                '"""Generated build-time artifact identity."""\n\n'
                "from __future__ import annotations\n\n"
                f"BUILD_COMMIT: str | None = {build_commit!r}\n\n"
                f"BUILD_UI_MODE: str | None = {ui_mode!r}\n\n"
                '__all__ = ["BUILD_COMMIT", "BUILD_UI_MODE"]\n'
            ),
            encoding="utf-8",
            newline="\n",
        )
        build_data["force_include"][str(generated)] = "opensquilla/_build_info.py"
