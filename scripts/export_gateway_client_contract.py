"""Export or verify the committed Gateway client-contract baseline."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from opensquilla.gateway.client_contract import render_contract_artifacts

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "contracts" / "client" / "v3"


@dataclass(frozen=True, slots=True)
class ContractCheckResult:
    """Exact inventory and byte comparison for one output directory."""

    changed: tuple[PurePosixPath, ...] = ()
    missing: tuple[PurePosixPath, ...] = ()
    unexpected: tuple[PurePosixPath, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.changed or self.missing or self.unexpected)


def _existing_files(output_dir: Path) -> dict[PurePosixPath, Path]:
    if not output_dir.exists():
        return {}
    return {
        PurePosixPath(path.relative_to(output_dir).as_posix()): path
        for path in output_dir.rglob("*")
        if path.is_file()
    }


def check_contract(
    output_dir: Path,
    *,
    artifacts: dict[PurePosixPath, bytes] | None = None,
) -> ContractCheckResult:
    """Compare expected artifacts without writing to ``output_dir``."""

    expected = render_contract_artifacts() if artifacts is None else artifacts
    existing = _existing_files(output_dir)
    expected_paths = set(expected)
    existing_paths = set(existing)
    common = expected_paths & existing_paths
    return ContractCheckResult(
        changed=tuple(
            sorted(path for path in common if existing[path].read_bytes() != expected[path])
        ),
        missing=tuple(sorted(expected_paths - existing_paths)),
        unexpected=tuple(sorted(existing_paths - expected_paths)),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
        os.chmod(path, 0o644)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_contract(
    output_dir: Path,
    *,
    artifacts: dict[PurePosixPath, bytes] | None = None,
) -> ContractCheckResult:
    """Write expected files, preserving unexpected files for explicit review."""

    expected = render_contract_artifacts() if artifacts is None else artifacts
    for relative_path, content in expected.items():
        target = output_dir / Path(relative_path.as_posix())
        if target.is_symlink() or not target.exists() or target.read_bytes() != content:
            _atomic_write(target, content)
        else:
            os.chmod(target, 0o644)
    return check_contract(output_dir, artifacts=expected)


def _describe(result: ContractCheckResult) -> list[str]:
    messages: list[str] = []
    messages.extend(f"changed: {path.as_posix()}" for path in result.changed)
    messages.extend(f"missing: {path.as_posix()}" for path in result.missing)
    messages.extend(f"unexpected: {path.as_posix()}" for path in result.unexpected)
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify exact inventory and bytes without writing files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"artifact directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifacts = render_contract_artifacts()
    result = (
        check_contract(args.output_dir, artifacts=artifacts)
        if args.check
        else write_contract(args.output_dir, artifacts=artifacts)
    )
    if result.ok:
        action = "matches" if args.check else "wrote"
        print(f"Gateway client contract {action} {args.output_dir}")
        return 0

    for message in _describe(result):
        print(message, file=sys.stderr)
    if result.unexpected and not args.check:
        print("unexpected files were preserved; remove them explicitly", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
