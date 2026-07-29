"""Compatibility wrapper for the public Gateway Runtime entrypoint."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.gateway_runtime.entry import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
