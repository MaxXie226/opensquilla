"""List the exact public Gateway Runtime files required for one release."""

from __future__ import annotations

import argparse
import re
import sys

TARGETS = (
    ("darwin", "arm64", ".tar.gz"),
    ("linux", "x64", ".tar.gz"),
    ("win32", "x64", ".zip"),
)
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")


def expected_runtime_asset_names(version: str) -> tuple[str, ...]:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid Runtime version: {version!r}")
    names: list[str] = []
    for platform_name, arch, archive_suffix in TARGETS:
        stem = f"gateway-runtime-{version}-{platform_name}-{arch}"
        names.extend(
            (
                f"{stem}{archive_suffix}",
                f"{stem}.manifest.json",
                f"{stem}.provenance.json",
                f"{stem}.spdx.json",
            )
        )
    return tuple(names)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        names = expected_runtime_asset_names(args.version)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
