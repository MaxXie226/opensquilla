"""pptx skill delivery contract."""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
PPTXGENJS_REFERENCE = BUNDLED / "pptx" / "references" / "pptxgenjs.md"


def test_pptx_skill_instructs_artifact_delivery() -> None:
    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("pptx")

    assert spec is not None
    assert "publish_artifact" in spec.content
    assert "file-authoring tools" in spec.content
    assert "If none of those file-authoring tools are available" in spec.content
    assert "If only `create_pptx` is available" in spec.content
    assert "basic text-only deck" in spec.content
    assert "Do not attempt to generate, save, or modify the `.pptx`" in spec.content
    assert "Ignore the Path B, Path C, and Visual QA sections below" in spec.content
    assert "Do not paste OOXML" in spec.content
    assert "final `.pptx`" in spec.content
    assert "Emoji, colored boxes,\n  and decorative lines do not satisfy" in spec.content
    assert "Visual QA (when render tools are available)" in spec.content
    assert "Do not pass `--range` for final QA" in spec.content
    assert "If inspection\nfinds a defect" in spec.content
    assert "without inventing an\nunnecessary edit" in spec.content
    assert "A B1 text-only\nedit may still be published" in spec.content
    assert "do not use a global npm\n  install" in spec.content
    assert "npm install -g pptxgenjs" not in spec.content
    assert "required before publishing paths B and C" not in spec.content
    assert "at least one fix-and-rerender cycle" not in spec.content
    assert "Do not declare clean unless one fix-and-reverify cycle" not in spec.content
    assert "/tmp/opensquilla-pptxgenjs" not in spec.content
    assert "mkdir -p" not in spec.content

    reference = PPTXGENJS_REFERENCE.read_text(encoding="utf-8")
    assert 'tempfile.mkdtemp(prefix="opensquilla-pptxgenjs-")' in reference
    assert "working directory" in reference
    assert "Do not use `npm install -g`" in reference
    assert "npm install -g pptxgenjs" not in reference
    assert "/tmp/" not in reference
    assert "mkdir -p" not in reference
