"""Integration contracts for headless and explicit-UI Hatch builds."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_webui_artifact import MANIFEST_NAME, WHEEL_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_UI_MODE_ENV = "OPENSQUILLA_BUILD_UI_MODE"
BUILD_UI_ARTIFACT_ENV = "OPENSQUILLA_BUILD_UI_ARTIFACT"
UV = shutil.which("uv") or "uv"


def _write_verified_artifact(root: Path, *, personal_bgm: bool = False) -> Path:
    dist = root / "external-ui"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets/app.js").write_text("console.log('probe')\n", encoding="utf-8")
    (dist / "assets/app.css").write_text("body{}\n", encoding="utf-8")
    (dist / "index.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    if personal_bgm:
        (dist / "music").mkdir()
        (dist / "music/local.mp3").write_bytes(b"synthetic private audio\n")

    records = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schemaVersion": 1,
        # Explicit artifact builds may consume a client-produced bundle without
        # its source checkout. File inventory and digests remain fail-closed.
        "sourceFingerprint": "0" * 64,
        "files": records,
    }
    (dist / MANIFEST_NAME).write_text(
        f"{json.dumps(manifest, indent=2)}\n",
        encoding="utf-8",
    )
    return dist


def _build_contract_probe(tmp_path: Path) -> Path:
    """Create a tiny Hatch project that uses the repository's real hook."""

    probe = tmp_path / "probe"
    package = probe / "src" / "probe"
    build_info = probe / "src" / "opensquilla" / "_build_info.py"
    scripts = probe / "scripts"
    package.mkdir(parents=True)
    build_info.parent.mkdir(parents=True)
    scripts.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (build_info.parent / "__init__.py").write_text("", encoding="utf-8")
    build_info.write_text(
        "BUILD_COMMIT = None\nBUILD_UI_MODE = None\n",
        encoding="utf-8",
    )
    shutil.copy2(REPO_ROOT / "hatch_build.py", probe / "hatch_build.py")
    shutil.copy2(
        REPO_ROOT / "scripts" / "verify_webui_artifact.py",
        scripts / "verify_webui_artifact.py",
    )
    (probe / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["hatchling>=1.31,<2"]
build-backend = "hatchling.build"

[project]
name = "opensquilla-headless-build-contract-probe"
version = "0.0.0"
requires-python = ">=3.12"

[tool.hatch.build.targets.wheel]
packages = ["src/probe", "src/opensquilla"]
exclude = [
  "src/opensquilla/gateway/static/dist/**",
  "src/opensquilla/_build_info.py",
]

[tool.hatch.build.targets.sdist]
exclude = [
  "opensquilla-webui/**",
  "**/node_modules/**",
  "desktop/electron/dist/**",
  "desktop/electron/.pyinstaller/**",
  "desktop/electron/runtime/**",
  "packages/*/dist/**",
  "src/opensquilla/cli/tui/opentui/package/bin/**",
  "src/opensquilla/cli/tui/opentui/package/build/**",
  "src/opensquilla/cli/tui/opentui/package/dist/**",
  "src/opensquilla/gateway/static/dist/**",
]

[tool.hatch.build.hooks.custom]
""",
        encoding="utf-8",
    )
    return probe


def _run(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _build_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop(BUILD_UI_MODE_ENV, None)
    env.pop(BUILD_UI_ARTIFACT_ENV, None)
    env.pop("OPENSQUILLA_BUILD_COMMIT", None)
    env.pop("GITHUB_SHA", None)
    env.update(updates)
    return env


def _only(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    assert len(matches) == 1, matches
    return matches[0]


def _wheel_ui_entries(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return sorted(
            name
            for name in archive.namelist()
            if name.startswith(WHEEL_PREFIX) and not name.endswith("/")
        )


def _wheel_build_info(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        return archive.read("opensquilla/_build_info.py").decode("utf-8")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_default_build_is_headless_even_with_residual_dist(tmp_path: Path) -> None:
    probe = _build_contract_probe(tmp_path)
    residual = probe / "src/opensquilla/gateway/static/dist"
    residual.mkdir(parents=True)
    (residual / "index.html").write_text("stale private checkout content", encoding="utf-8")
    generated_outputs = (
        "desktop/electron/node_modules/example/package.json",
        "desktop/electron/dist/main.js",
        "desktop/electron/.pyinstaller/gateway.spec",
        "desktop/electron/runtime/gateway",
        "packages/client-sdk/node_modules/example/package.json",
        "packages/client-sdk/dist/index.js",
        "src/opensquilla/cli/tui/opentui/package/bin/host",
        "src/opensquilla/cli/tui/opentui/package/build/index.js",
        "src/opensquilla/cli/tui/opentui/package/dist/index.js",
    )
    for relative_path in generated_outputs:
        output = probe / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("synthetic local build output\n", encoding="utf-8")

    wheel_dir = tmp_path / "wheel"
    sdist_dir = tmp_path / "sdist"
    wheel = _run(
        UV,
        "build",
        "--wheel",
        "--out-dir",
        str(wheel_dir),
        cwd=probe,
        env=_build_env(),
    )
    sdist = _run(
        UV,
        "build",
        "--sdist",
        "--out-dir",
        str(sdist_dir),
        cwd=probe,
        env=_build_env(),
    )

    assert wheel.returncode == 0, wheel.stderr
    assert sdist.returncode == 0, sdist.stderr
    built_wheel = _only(wheel_dir, "*.whl")
    assert _wheel_ui_entries(built_wheel) == []
    assert "BUILD_COMMIT: str | None = None" in _wheel_build_info(built_wheel)
    assert "BUILD_UI_MODE: str | None = 'headless'" in _wheel_build_info(built_wheel)
    with tarfile.open(_only(sdist_dir, "*.tar.gz"), "r:gz") as archive:
        archive_names = archive.getnames()
        assert not any("gateway/static/dist/" in name for name in archive_names)
        for relative_path in generated_outputs:
            assert not any(name.endswith(relative_path) for name in archive_names)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_headless_sdist_builds_a_headless_wheel_without_node_or_ui_source(
    tmp_path: Path,
) -> None:
    probe = _build_contract_probe(tmp_path)
    sdist_dir = tmp_path / "sdist"
    wheel_dir = tmp_path / "wheel-from-sdist"
    built = _run(
        UV,
        "build",
        "--sdist",
        "--out-dir",
        str(sdist_dir),
        cwd=probe,
        env=_build_env(),
    )
    assert built.returncode == 0, built.stderr

    wheel = _run(
        UV,
        "build",
        "--wheel",
        "--out-dir",
        str(wheel_dir),
        str(_only(sdist_dir, "*.tar.gz")),
        cwd=probe,
        env=_build_env(PATH=str(Path(sys.executable).parent)),
    )

    assert wheel.returncode == 0, wheel.stderr
    built_wheel = _only(wheel_dir, "*.whl")
    assert _wheel_ui_entries(built_wheel) == []
    assert "BUILD_UI_MODE: str | None = 'headless'" in _wheel_build_info(built_wheel)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_explicit_embed_ui_wheel_contains_only_verified_artifact(tmp_path: Path) -> None:
    probe = _build_contract_probe(tmp_path)
    artifact = _write_verified_artifact(tmp_path)
    wheel_dir = tmp_path / "embedded-wheel"
    env = _build_env(
        **{
            BUILD_UI_MODE_ENV: "embed-ui",
            BUILD_UI_ARTIFACT_ENV: str(artifact),
        }
    )

    result = _run(
        UV,
        "build",
        "--wheel",
        "--out-dir",
        str(wheel_dir),
        cwd=probe,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    wheel = _only(wheel_dir, "*.whl")
    expected = sorted(
        f"{WHEEL_PREFIX}{path.relative_to(artifact).as_posix()}"
        for path in artifact.rglob("*")
        if path.is_file()
    )
    assert _wheel_ui_entries(wheel) == expected
    assert "BUILD_UI_MODE: str | None = 'embed-ui'" in _wheel_build_info(wheel)
    with zipfile.ZipFile(wheel) as archive:
        for name in expected:
            assert archive.read(name) == (artifact / name.removeprefix(WHEEL_PREFIX)).read_bytes()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
@pytest.mark.parametrize(
    ("updates", "expected"),
    (
        (
            {BUILD_UI_MODE_ENV: "embed-ui"},
            "OPENSQUILLA_BUILD_UI_ARTIFACT is required",
        ),
        (
            {BUILD_UI_ARTIFACT_ENV: "external-ui"},
            "select 'embed-ui' explicitly",
        ),
        (
            {BUILD_UI_MODE_ENV: "automatic"},
            "must be 'headless' or 'embed-ui'",
        ),
    ),
)
def test_invalid_ui_build_selection_fails_closed(
    tmp_path: Path,
    updates: dict[str, str],
    expected: str,
) -> None:
    probe = _build_contract_probe(tmp_path)
    result = _run(
        UV,
        "build",
        "--wheel",
        "--out-dir",
        str(tmp_path / "invalid"),
        cwd=probe,
        env=_build_env(**updates),
    )

    assert result.returncode != 0
    assert expected in f"{result.stdout}\n{result.stderr}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_explicit_embed_ui_rejects_tampering_and_sdist_personal_bgm(
    tmp_path: Path,
) -> None:
    probe = _build_contract_probe(tmp_path)
    artifact = _write_verified_artifact(tmp_path, personal_bgm=True)
    env = _build_env(
        **{
            BUILD_UI_MODE_ENV: "embed-ui",
            BUILD_UI_ARTIFACT_ENV: str(artifact),
        }
    )
    (artifact / "assets/app.js").write_text("tampered\n", encoding="utf-8")

    tampered = _run(
        UV,
        "build",
        "--wheel",
        "--out-dir",
        str(tmp_path / "tampered"),
        cwd=probe,
        env=env,
    )
    assert tampered.returncode != 0
    assert "do not match the generated manifest" in f"{tampered.stdout}\n{tampered.stderr}"

    artifact = _write_verified_artifact(tmp_path / "personal", personal_bgm=True)
    env[BUILD_UI_ARTIFACT_ENV] = str(artifact)
    private_sdist = _run(
        UV,
        "build",
        "--sdist",
        "--out-dir",
        str(tmp_path / "private-sdist"),
        cwd=probe,
        env=env,
    )
    assert private_sdist.returncode != 0
    assert "personal BGM content is forbidden" in (
        f"{private_sdist.stdout}\n{private_sdist.stderr}"
    )
